"""RomAdoptionService — ROM content the plugin finds rather than fetches.

Owns the four answers the adopt dialog needs about the content a download would
otherwise write: whether something is already at the path, whether the same game
is already on disk under a different name, whether what is there is the ROM the
server holds, and — on the user's word — the ``rom_installs`` row that turns it
into an install, carrying the content to the canonical name on the way. The row
itself is written by the shared ``RomInstallRecorder``, so an adopted install is
derived by exactly the rules a downloaded one is (ADR-0028).

The service never deletes on its own initiative. Its two destructive paths are
the replace leg of the download gate and the overwrite leg of the rename, each
run only because the user chose it over the alternative it was shown beside.

What a rename *consists of* — the plan, the collision question, how far a move
got — belongs to ``AdoptionRenamer``, which both exits of the dialog share. What
counts as "already on disk", and what is said when the answer is not a candidate
the dialog can offer, belongs to ``CandidateSearch``, which the game page's probe
and the Download click's gate both go through.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from domain.rom_adoption import (
    DigestRequest,
    FileDifference,
    LocalFile,
    LocalMember,
    ServerFile,
    adoptable_content,
    compare_manifest,
    digests_to_read,
    is_archive_name,
    occupied_target_refusal,
    server_manifest,
    unadoptable_reason,
    unconfirmed_reason,
    verification_status,
)
from domain.rom_candidates import DIR
from domain.rom_files import (
    detect_launch_file,
    is_multi_file_download,
    resolve_extract_dir_name,
    resolve_local_file_name,
    synthetic_rom_name,
)
from lib.errors import error_response
from lib.path_safety import PathTraversalError, coerce_safe_component, is_safe_rom_path, safe_join
from services.rom_adoption._target import Target as _Target
from services.rom_adoption.renamer import AdoptionRenamer, AdoptionRenamerConfig
from services.rom_adoption.search import CandidateSearch, CandidateSearchConfig

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from domain.adoption_rename import RenamePair
    from services.protocols import (
        ActiveCoreReader,
        AdoptionMoveStore,
        Clock,
        CoreNameProviderFn,
        DebugLogger,
        DownloadFileStore,
        EventEmitter,
        RetroArchSaveLayoutProvider,
        RetroArchSavestateLayoutProvider,
        RetroDeckPaths,
        RomInstallRecorder,
        RommRomReader,
        SaveQuarantineFn,
        SaveSortingProvider,
        SiblingSupersedeProvider,
        SystemKnownFn,
        SystemM3uSupportFn,
        SystemResolver,
        SystemSupportedExtensionsFn,
        UnitOfWorkFactory,
    )

# Minimum seconds between two ``verify_progress`` frames. Matches the download
# path's throttle so the two feel like one progress channel to the user.
_PROGRESS_INTERVAL_SEC = 0.5

_VERIFY_MESSAGES = {
    "match": "These files match the ones on the server",
    "mismatch": "These files differ from the ones on the server",
    "unverifiable": "This RomM server publishes no checksums, so it cannot confirm these files",
}

# The other two ways a check can end without a verdict, both reached only once
# the server HAS published digests: bytes the plugin could not read, and a
# number it cannot attribute to anything inside the archive it covers.
_UNCONFIRMED_MESSAGES = {
    "unread": "Some of this game's files could not be read, so they cannot be confirmed",
    "whole_archive": "The server publishes one checksum for this whole archive, which cannot be matched "
    "against the files inside it",
}


def _target_taken_refusal() -> dict[str, Any]:
    """The refusal an adoption returns when the ROM's own canonical path is occupied."""
    return {
        "success": False,
        "reason": "target_taken",
        "message": "Something arrived at this game's own location — nothing was moved",
    }


def _add_carried_note(refusal: dict[str, Any], carried: tuple[RenamePair, ...]) -> dict[str, Any]:
    """Add what the carry already moved to a refusal raised by the step after it.

    Without it the abort reads as clean while the game the user keeps can no
    longer find its saves — they are at the canonical name and it is not.
    """
    if not carried:
        return refusal
    names = ", ".join(os.path.basename(pair.target) for pair in carried)
    return {
        **refusal,
        "message": f"{refusal['message']} This game's saves were already renamed and are now at: {names}.",
    }


def _unsafe_replace_refusal() -> dict[str, Any]:
    """The refusal a replace returns for a path outside the RetroDECK ROMs tree."""
    return {
        "success": False,
        "reason": "unsafe_replace_target",
        "message": "Refusing to remove content outside the ROM directory",
    }


@dataclass(frozen=True)
class RomAdoptionServiceConfig:
    """Frozen wiring bundle handed to ``RomAdoptionService.__init__``.

    ``install_recorder`` is the shared install writer — the reason an adopted row
    cannot drift from a downloaded one. ``m3u_support`` gates whether a bundled
    ``.m3u`` may be chosen as an adopted directory's launch file, exactly as it
    does for an extracted one. ``system_extensions`` and ``system_known`` are the
    two questions the candidate search puts to ``es_systems.xml``: what a system
    accepts, and whether the directory it is about to search is a system at all.
    The layout and core seams below are the renamer's, and the search seams the
    search's — they are taken here and handed straight on, so the service has one
    constructor rather than three the composition root has to keep in step.
    """

    romm_api: RommRomReader
    download_file_store: DownloadFileStore
    adoption_move: AdoptionMoveStore
    quarantine_save: SaveQuarantineFn
    resolve_system: SystemResolver
    retrodeck_paths: RetroDeckPaths
    install_recorder: RomInstallRecorder
    m3u_support: SystemM3uSupportFn
    system_extensions: SystemSupportedExtensionsFn
    system_known: SystemKnownFn
    save_layout: RetroArchSaveLayoutProvider
    save_sorting: SaveSortingProvider
    savestate_layout: RetroArchSavestateLayoutProvider
    active_core: ActiveCoreReader
    get_core_name: CoreNameProviderFn
    # Deferred: the supersede lives on DownloadService, which is built after
    # this service (it consumes the occupancy gate below).
    sibling_supersede: SiblingSupersedeProvider
    uow_factory: UnitOfWorkFactory
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    log_debug: DebugLogger
    emit: EventEmitter
    clock: Clock


class RomAdoptionService:
    """Collision detection, candidate search, content verification, and adoption of on-disk ROMs."""

    def __init__(self, *, config: RomAdoptionServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._download_file_store = config.download_file_store
        self._resolve_system = config.resolve_system
        self._retrodeck_paths = config.retrodeck_paths
        self._install_recorder = config.install_recorder
        self._m3u_support = config.m3u_support
        self._system_extensions = config.system_extensions
        self._renamer = AdoptionRenamer(
            config=AdoptionRenamerConfig(
                adoption_move=config.adoption_move,
                quarantine_save=config.quarantine_save,
                download_file_store=config.download_file_store,
                retrodeck_paths=config.retrodeck_paths,
                m3u_support=config.m3u_support,
                save_layout=config.save_layout,
                save_sorting=config.save_sorting,
                savestate_layout=config.savestate_layout,
                active_core=config.active_core,
                get_core_name=config.get_core_name,
                logger=config.logger,
            )
        )
        self._search = CandidateSearch(
            config=CandidateSearchConfig(
                download_file_store=config.download_file_store,
                resolve_system=config.resolve_system,
                system_extensions=config.system_extensions,
                system_known=config.system_known,
                retrodeck_paths=config.retrodeck_paths,
                uow_factory=config.uow_factory,
                logger=config.logger,
                log_debug=config.log_debug,
            )
        )
        self._sibling_supersede = config.sibling_supersede
        self._uow_factory = config.uow_factory
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock

    # ── Download pre-flight (DownloadTargetGateFn) ──────────────────

    async def check_download_target(
        self,
        rom_detail: dict[str, Any],
        checked_path: str,
        *,
        replace: bool,
        resume: bool = False,
        candidate_path=None,
        collision_choice=None,
        page_saw_candidate: bool = False,
    ) -> dict[str, Any] | None:
        """Decide whether a download may write the content it computed *checked_path* for.

        ``None`` means proceed: nothing is in the way, what is there already
        belongs to this ROM's own install, or the user chose to download over
        whatever the gate showed them and it has been cleared. Anything else is a
        canonical failure the caller returns untouched — the ``target_occupied``
        refusal carrying both sides of the comparison, one of the three the
        candidate search can return (``adoption_candidates``,
        ``unusable_namesake``, ``candidate_vanished``), the ``rename_collisions``
        refusal raised by carrying a discarded candidate's saves, or a removal
        that could not be completed.

        *page_saw_candidate* is what the game page told the user before they
        pressed. It is carried this far because the search's last answer is a
        backstop over it: a page that found a copy must never end in a silent
        download, whatever the two searches disagree about (ADR-0028).

        None of the legs is bounded work — describing an occupied directory walks
        it whole (a multi-file install can hold tens of thousands of files),
        clearing one deletes it whole, and the candidate search lists a directory
        and reads archive indexes — so the whole check runs off the loop. The
        offload lives here rather than at the call site: the caller asks a
        question and should not have to know what answering it costs.
        """
        worker = partial(
            self._check_download_target_io,
            rom_detail,
            checked_path,
            replace=replace,
            resume=resume,
            candidate_path=candidate_path,
            collision_choice=collision_choice,
            page_saw_candidate=page_saw_candidate,
        )
        return await self._loop.run_in_executor(None, worker)

    def _check_download_target_io(
        self,
        rom_detail: dict[str, Any],
        checked_path: str,
        *,
        replace: bool,
        resume: bool = False,
        candidate_path=None,
        collision_choice=None,
        page_saw_candidate: bool = False,
    ) -> dict[str, Any] | None:
        """Synchronous body of the download-target gate. Runs on an executor thread."""
        existing = self._download_file_store.describe_path(checked_path)
        if existing is None:
            if replace:
                return self._discard_candidate(rom_detail, candidate_path, collision_choice)
            # A resume continues a decision already taken: "is this game already
            # here" was answered when the download was admitted, and pausing does
            # not change the answer — the candidate is still on disk and is
            # precisely what the user declined, so searching again would refuse
            # the transfer they started with no exit but Cancel.
            #
            # This is NOT the reason ``_replace_existing`` is dropped for a
            # multi-file replace. There the answer is *spent*: the directory has
            # already been removed, so anything at that path now is content the
            # user has never seen and the gate must ask about it. Here nothing was
            # consumed. Two different reasons, and neither generalises to the
            # other.
            if resume:
                return None
            return self._search.refusal(rom_detail, checked_path, page_saw_candidate=page_saw_candidate)
        if self._is_own_install(rom_detail, checked_path):
            return None
        if not replace:
            return occupied_target_refusal(
                path=existing["path"],
                kind=existing["kind"],
                size_bytes=existing["size_bytes"],
                modified_at=existing["modified_at"],
                incoming_name=os.path.basename(checked_path),
                incoming_size=rom_detail.get("fs_size_bytes", 0),
                served_dir=is_multi_file_download(rom_detail),
            )
        return self._clear_for_replace(rom_detail, checked_path, is_dir=existing["kind"] == DIR)

    # ── Candidate search ────────────────────────────────────────────

    def has_adoption_candidate(self, platform_slug: str, fs_name: str) -> bool:
        """Whether this platform folder holds an entry that could be this ROM.

        The game-detail read's half of the search: enough to label the button, not
        enough to fill the dialog. It stops at the name match, so it is a
        ``readdir`` with no size-or-mtime ``stat``, one install-row query and
        pure string work — the archive central-directory reads that rank
        candidates are skipped entirely, because a page that only has to say
        "something is here" never needs to know which of several is strongest.

        It reads a ``roms`` row and the click-time search reads the server
        payload, so the two answer from different knowledge and are not held to
        agreeing. What holds instead is that a ``True`` here always ends in an
        answer when the button is pressed: every way the click-time search can
        find less than this did is a refusal that says so, the last of them being
        the backstop for the ways nobody has thought of yet (ADR-0028).

        Every failure is quiet and answers ``False``: an unresolvable roms path,
        an unreadable folder, an accept-list the source could not answer. A search
        that could not run must never make a game look uninstallable.
        """
        try:
            return bool(self._search.name_matches(platform_slug, fs_name))
        except Exception as e:
            self._logger.warning(f"Adoption candidate probe failed for {platform_slug}/{fs_name}: {e}")
            return False

    def _is_own_install(self, rom_detail: dict[str, Any], checked_path: str) -> bool:
        """Whether *checked_path* is already this ROM's recorded install.

        A re-download of a ROM the plugin installed itself finds its own files in
        the way. Those are not content to ask about — the install record is the
        plugin's claim on them, the same authority ``installed`` and Uninstall
        act on — so the download proceeds and replaces them as it always has.
        The dialog exists for content **no** row accounts for (ADR-0028).
        """
        rom_id = int(rom_detail.get("id") or 0)
        if rom_id <= 0:
            return False
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(rom_id)
        if install is None:
            return False
        return checked_path in (install.rom_dir, install.file_path)

    def _clear_for_replace(
        self, rom_detail: dict[str, Any], checked_path: str, *, is_dir: bool
    ) -> dict[str, Any] | None:
        """Remove what occupies *checked_path* so the download starts on clean ground.

        A directory always goes: ``make_dirs`` is ``exist_ok=True`` and the
        extract would otherwise merge into whatever is already there, leaving a
        hybrid that a later uninstall deletes whole — the user chose replace, not
        merge. A file goes only when a *directory* is about to take its place; a
        single-file download leaves it, because ``os.replace`` swaps the new
        bytes in atomically and a delete-then-fetch would leave the user with
        neither copy if the transfer failed.

        That last carve-out is specific to content sitting **at the target path**,
        which is why :meth:`_discard_candidate` removes its subject through
        :meth:`_remove_under_roms` directly: a candidate under a different name is
        never the thing ``os.replace`` swaps, so leaving it would leave it.
        """
        if not is_dir and not is_multi_file_download(rom_detail):
            roms_base = self._retrodeck_paths.roms_path()
            return None if roms_base and is_safe_rom_path(checked_path, roms_base) else _unsafe_replace_refusal()
        return self._remove_under_roms(checked_path, is_dir=is_dir)

    def _remove_under_roms(self, path: str, *, is_dir: bool) -> dict[str, Any] | None:
        """Delete *path*, refusing anything that is not safely inside the ROMs tree.

        The one place this service deletes ROM content, shared by both legs of a
        replace so neither can acquire its own containment rule. Reports a failed
        removal instead of letting the download proceed onto ground it could not
        clear.
        """
        roms_base = self._retrodeck_paths.roms_path()
        if not roms_base or not is_safe_rom_path(path, roms_base):
            self._logger.error(f"Refusing to replace content outside the ROMs directory: {path}")
            return _unsafe_replace_refusal()
        try:
            if is_dir:
                self._download_file_store.remove_tree(path)
            else:
                self._download_file_store.remove_file(path)
        except OSError as e:
            self._logger.error(f"Failed to remove existing content at {path}: {e}")
            return {
                "success": False,
                "reason": "replace_failed",
                "message": "Could not remove the existing files — download aborted",
            }
        self._logger.info(f"Replacing existing content at {path}")
        return None

    # ── Downloading over a candidate ────────────────────────────────

    def _discard_candidate(self, rom_detail: dict[str, Any], candidate_path, collision_choice) -> dict[str, Any] | None:
        """Remove the candidate the user chose to download over, and carry its saves.

        The dialog's second confirmation names this deletion, so it happens: the
        file the user was shown goes, and the server's copy takes its place. One
        rule for both exits of that dialog — content at the target path and
        content beside it under another name are removed alike.

        ``None`` — proceed with the download — when no particular file was the
        subject: a target-path replace (:meth:`_clear_for_replace` owns that one)
        or "None of These", where the user declined every candidate rather than
        choosing one, and nothing may be deleted on their behalf.

        **Carry, then remove.** A carry that fails aborts with nothing deleted
        and, on the link-then-unlink path, nothing moved either. Removing first
        would mean a failed carry leaves the saves orphaned under a name whose ROM
        is already gone — the exact outcome the rename exists to prevent.

        A removal that fails **after** the carry is the one abort that is not
        clean: the file the user keeps can no longer find its saves, which now sit
        under the canonical name. It is named rather than moved back — a retry of
        the same download finds the saves already in place and re-plans to
        nothing, where an undo would have to be undone again.

        A path the store cannot describe at all is left alone and the download
        simply proceeds: nothing was named that could be removed.
        """
        if not candidate_path:
            return None
        target = self._resolve_target(rom_detail)
        if target is None:
            return {
                "success": False,
                "reason": "path_traversal",
                "message": "Server sent an unsafe platform path — download aborted",
            }
        source_path = self._resolve_source(target, candidate_path)
        if source_path is None:
            return {
                "success": False,
                "reason": "invalid_candidate",
                "message": "That file is not in this game's platform folder — nothing was removed",
            }
        existing = self._download_file_store.describe_path(source_path)
        if existing is None:
            return None
        rom_id = int(rom_detail.get("id") or 0)
        refusal, carried = self._renamer.move_planned(
            self._renamer.discarded_save_pairs(rom_id, target, source_path), collision_choice
        )
        if refusal is not None:
            return refusal
        removal = self._remove_under_roms(source_path, is_dir=existing["kind"] == DIR)
        return removal if removal is None else _add_carried_note(removal, carried)

    # ── Adopt ───────────────────────────────────────────────────────

    async def adopt_existing_rom(self, rom_id, candidate_path=None, collision_choice=None) -> dict[str, Any]:
        """Record content already on disk as this ROM's install.

        *candidate_path* is empty for content sitting at the ROM's own target
        path — the case the occupied-target dialog opens on — and names an entry
        elsewhere in the platform directory when the user picked one the search
        offered. A candidate is **renamed into place**, saves and savestates with
        it, so an adopted install is what ADR-0028 says it is: indistinguishable
        from a downloaded one. *collision_choice* answers the second dialog, and
        is empty until that dialog has been shown.

        Nothing is downloaded or generated. Every path is re-validated
        immediately before it is used, so content that vanished between the
        dialog and the confirmation is a refusal rather than a row pointing at
        nothing.

        **Order: validate, carry, supersede, record.** Every reason this adoption
        could be refused is decided in the first step, because the third one
        deletes another version's files: superseding first and refusing
        afterwards would leave the user with a working version destroyed and
        nothing bound in its place. The carry sits before the supersede for the
        same reason — a rename that fails must not find a sibling already gone.
        Recording first is no better: a supersede that then failed would leave the
        two installed versions the rule exists to prevent, with the adoption
        already committed.
        """
        rom_id = int(rom_id)
        try:
            rom_detail = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._logger.error(f"Failed to fetch ROM {rom_id} for adoption: {e}")
            return error_response(e)
        target = self._resolve_target(rom_detail)
        if target is None:
            return {
                "success": False,
                "reason": "path_traversal",
                "message": "Server sent an unsafe platform path — adoption aborted",
            }
        source_path = self._resolve_source(target, candidate_path)
        if source_path is None:
            return {
                "success": False,
                "reason": "invalid_candidate",
                "message": "That file is not in this game's platform folder — nothing was adopted",
            }
        refusal = await self._loop.run_in_executor(None, self._validate_adoption_io, rom_id, target, source_path)
        if refusal is not None:
            return refusal
        if source_path != target.path:
            worker = partial(self._carry_io, rom_id, target, source_path, collision_choice)
            refusal = await self._loop.run_in_executor(None, worker)
            if refusal is not None:
                return refusal
        # At most one installed version per shortcut binding (#1298), whichever
        # route produced it. The dialog's promise not to delete covers the
        # content the USER placed, at this ROM's own path; a superseded sibling
        # is a different thing at a different path — content the plugin
        # downloaded and can fetch again (ADR-0028). A removal failure aborts,
        # exactly as it does for a download, so the rule is never half-applied.
        cleanup_failure = await self._sibling_supersede()(rom_id)
        if cleanup_failure is not None:
            return cleanup_failure
        return await self._loop.run_in_executor(None, self._adopt_io, rom_id, rom_detail, target)

    def _resolve_source(self, target: _Target, candidate_path) -> str | None:
        """Where the content to adopt sits now: the target path, or a vetted candidate.

        ``None`` refuses a *candidate_path* that is not a direct entry of this
        ROM's own platform directory. The frontend hands the path back from a list
        this service produced, but it crosses the wire in between, and the rename
        that follows both moves and — on an overwrite — deletes.
        """
        if not candidate_path:
            return target.path
        path = os.path.normpath(str(candidate_path))
        roms_base = self._retrodeck_paths.roms_path()
        if os.path.dirname(path) != os.path.dirname(target.path) or not is_safe_rom_path(path, roms_base):
            self._logger.error(f"Rejected adoption candidate outside this game's platform directory: {path}")
            return None
        return path

    def _validate_adoption_io(self, rom_id: int, target: _Target, source_path: str) -> dict[str, Any] | None:
        """Every refusal this adoption can produce, decided before anything is moved.

        ``None`` means the adoption will go through. Runs off the loop: it stats
        the content and reads the ``roms`` row in one short UoW — the row has to
        exist for the install's foreign key, and asking here turns what would
        otherwise be an exception *after* the supersede into a refusal before it.
        """
        # Asked here rather than trusted from the dialog: the entry offered as a
        # regular file may have become a link between the gate's answer and the
        # user's confirmation, and this service re-validates every path
        # immediately before it uses it.
        refusal = self._unadoptable_refusal(source_path, target)
        if refusal is not None:
            return refusal
        if source_path != target.path and self._renamer.target_taken(target):
            return _target_taken_refusal()
        with self._uow_factory() as uow:
            known = uow.roms.get(rom_id) is not None
        if not known:
            return {
                "success": False,
                "reason": "invalid_install",
                "message": "This game is not in the local library — nothing was adopted",
            }
        return None

    def _unadoptable_refusal(self, path: str, target: _Target) -> dict[str, Any] | None:
        """Why content at *path* cannot become *target*'s install, or ``None``.

        Both the validation before the move and the last check after it ask this,
        and they must give the same answers: a user who is told "the files are no
        longer there" by one and "a shortcut is in the way" by the other for the
        same disk state has been told two different things about one folder. One
        function is what makes that true rather than asserted — the argument
        ``unadoptable_reason`` makes for not taking the served shape, and the one
        this whole search has been rebuilt around.

        The two refusals are two situations. Content that vanished is
        ``nothing_to_adopt``; content still sitting there but of a kind no
        install row may point at — a link, a directory where the server serves
        one file, something with no kind at all — is ``unexpected_content_kind``.
        Saying "no longer there" of a file that merely became a link sends the
        user looking for something that has not happened.
        """
        existing = self._download_file_store.describe_path(path)
        if existing is None:
            return {
                "success": False,
                "reason": "nothing_to_adopt",
                "message": "The files are no longer there — nothing was adopted",
            }
        if not adoptable_content(existing["kind"], served_dir=target.is_multi):
            return {
                "success": False,
                "reason": "unexpected_content_kind",
                "message": unadoptable_reason(existing["kind"]),
            }
        return None

    def _carry_io(self, rom_id: int, target: _Target, source_path: str, collision_choice) -> dict[str, Any] | None:
        """Rename the candidate into place. Runs off the loop.

        The ROM's own target is re-checked here rather than left to the plan: a
        name that appeared there since the validation is the occupied-target case
        the *other* dialog owns, so it is refused outright rather than offered as
        one more thing to overwrite or skip.
        """
        if self._renamer.target_taken(target):
            return _target_taken_refusal()
        return self._renamer.carry_to_canonical(rom_id, target, source_path, collision_choice)

    def _adopt_io(self, rom_id: int, rom_detail: dict[str, Any], target: _Target) -> dict[str, Any]:
        """Persist the install record for content ``_validate_adoption_io`` accepted.

        The target is asked about once more through the same
        :meth:`_unadoptable_refusal` the validation used: the supersede ran in
        between, and content that vanished across it — or turned into something
        no install row may point at — must be refused rather than recorded. That
        window is the one refusal that can follow a completed supersede, and it
        cannot be closed: a row pointing at files that are gone would be worse,
        and one pointing at a link could never be removed.
        """
        refusal = self._unadoptable_refusal(target.path, target)
        if refusal is not None:
            return refusal
        file_path = self._adopted_launch_file(target) if target.is_multi else target.path
        rom_dir = target.path if target.is_multi else None
        # A no-op cleanup, deliberately: the recorder removes the artifact when
        # the RomM metadata fails the aggregate's invariant, which is right for a
        # download that just placed it and catastrophic for content the user put
        # there. Adoption refuses; it never deletes what it failed to record.
        recorded, error = self._install_recorder.do_record_install(
            rom_id=rom_id,
            rom_detail=rom_detail,
            file_path=file_path,
            rom_dir=rom_dir,
            system=target.system,
            cleanup=lambda: None,
        )
        if error is not None or recorded is None:
            return {"success": False, "reason": "invalid_install", "message": error or "Could not record the install"}
        app_id, launch_options = self._install_recorder.do_resolve_launch_bake(rom_id, rom_detail, recorded)
        # Record the freshly baked command as this ROM's applied state (the value
        # the frontend writes onto the shortcut on this result), so the next sync
        # skips the now-correct shortcut instead of re-touching it (delta apply,
        # #1383). Only when the ROM has a bound shortcut — an unbound ROM has none
        # to record, and the next sync creates + records it. Third of the six
        # writer sites, immediately after download-complete: the same moment in
        # the flow, because an adopted install is an install (ADR-0028).
        if app_id is not None:
            self._install_recorder.do_record_applied_launch_options(rom_id, launch_options)
        self._logger.info(f"Adopted existing content for rom {rom_id}: {recorded}")
        return {
            "success": True,
            "message": "Using the files already on this device",
            "file_path": recorded,
            "rom_dir": rom_dir,
            "app_id": app_id,
            "launch_options": launch_options,
        }

    def _adopted_launch_file(self, target: _Target) -> str:
        """Pick the launch file inside an adopted directory by the download's own rule.

        No ``.m3u`` is generated and nothing is renamed: the directory is
        recorded as it stands, so the ES-DE collapse a downloaded ROM gets does
        not apply here. Falls back to the directory itself when it holds no
        files, which the recorder's launchable check then reads as a folder-boot
        or unlaunchable target.
        """
        files = self._download_file_store.scan_files_with_sizes(target.path)
        detected = detect_launch_file(files, self._m3u_support(target.system))
        return detected if detected is not None else target.path

    # ── Verify ──────────────────────────────────────────────────────

    async def verify_existing_content(self, rom_id, candidate_path=None) -> dict[str, Any]:
        """Compare content already on disk against RomM's manifest for this ROM.

        *candidate_path* is empty for the content at the ROM's own target path and
        names an entry elsewhere in the platform directory when the user is
        deciding about one the search offered. Either way the manifest is the
        same: the question is whether these bytes are that ROM, not where they
        currently sit.

        A discriminated-status union rather than a success flag: ``match``,
        ``mismatch`` (naming what differed), ``unverifiable`` (the server holds
        no checksums, which is neither), ``missing`` and ``error`` are five
        outcomes, and collapsing "the server cannot confirm this" onto either
        verdict would be a claim the plugin cannot make. Only ever runs on the
        user's request — the dialog opens on cheap evidence and never waits for
        this.
        """
        rom_id = int(rom_id)
        try:
            rom_detail = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._logger.error(f"Failed to fetch ROM {rom_id} for verification: {e}")
            failure = error_response(e)
            return {"status": "error", "message": failure["message"], "differences": []}
        target = self._resolve_target(rom_detail)
        if target is None:
            return {
                "status": "error",
                "message": "Server sent an unsafe platform path — nothing was checked",
                "differences": [],
            }
        source_path = self._resolve_source(target, candidate_path)
        if source_path is None:
            return {
                "status": "error",
                "message": "That file is not in this game's platform folder — nothing was checked",
                "differences": [],
            }
        checked = replace(target, path=source_path)
        return await self._loop.run_in_executor(None, self._verify_io, rom_id, rom_detail, checked)

    def _verify_io(self, rom_id: int, rom_detail: dict[str, Any], target: _Target) -> dict[str, Any]:
        """Hash what is on disk and compare it against the manifest. Runs off the loop."""
        if self._download_file_store.describe_path(target.path) is None:
            return {"status": "missing", "message": "There is nothing at this game's location", "differences": []}
        manifest = server_manifest(rom_detail)
        if not any(entry.verifiable for entry in manifest):
            return {
                "status": "unverifiable",
                "message": "This RomM server publishes no checksums, so it cannot confirm these files",
                "differences": [],
            }
        checkable, escaping = self._partition_escaping(manifest, target)
        observed = self._observe(rom_id, checkable, target)
        differences = escaping + compare_manifest(checkable, observed)
        # The FULL manifest decides verifiability: an entry refused for escaping
        # the ROM directory was still a digest the server published, so dropping
        # it from that question could turn a mismatch into "cannot confirm".
        status = verification_status(manifest, observed, differences)
        # Past the no-checksums guard above, so "unverifiable" here never means
        # the server published none — telling the user that would send them
        # looking for a problem their server does not have.
        message = (
            _UNCONFIRMED_MESSAGES[unconfirmed_reason(manifest, observed)]
            if status == "unverifiable"
            else _VERIFY_MESSAGES[status]
        )
        return {
            "status": status,
            "message": message,
            "differences": [{"name": d.name, "detail": d.detail} for d in differences],
        }

    def _partition_escaping(
        self, manifest: tuple[ServerFile, ...], target: _Target
    ) -> tuple[tuple[ServerFile, ...], tuple[FileDifference, ...]]:
        """Split the manifest into entries that may be looked up and entries that may not.

        A relative path is server-supplied, so it passes ``safe_join`` before it
        is used to address anything. One that escapes the ROM directory is
        refused outright rather than looked up somewhere else — and reported, so
        the check names the problem instead of quietly reading as "missing".
        """
        checkable: list[ServerFile] = []
        escaping: list[FileDifference] = []
        for entry in manifest:
            if not entry.rel_path:
                checkable.append(entry)
                continue
            try:
                safe_join(target.path, entry.rel_path)
            except PathTraversalError:
                self._logger.error(f"Manifest entry escapes the ROM directory: {entry.rel_path!r}")
                escaping.append(FileDifference(name=entry.lookup_key, detail="sits outside this game's folder"))
                continue
            checkable.append(entry)
        return (tuple(checkable), tuple(escaping))

    def _observe(self, rom_id: int, manifest: tuple[ServerFile, ...], target: _Target) -> dict[str, LocalFile]:
        """Read each manifest entry's counterpart on disk, hashing where it is worth it.

        The result is keyed by each entry's ``lookup_key``, so an entry the server
        located is read from exactly that place and one it did not locate falls
        back to a search by filename. Anything whose name says archive is looked
        inside first: a ZIP's central directory names every member and states its
        uncompressed size and CRC32 without decompressing a byte, and the content
        in there is what RomM's digest for such a file describes.

        Which bytes are worth reading is :func:`digests_to_read`'s decision; the
        whole plan is built before the first read so the progress the user sees
        counts down against the real total.
        """
        located = self._locate(manifest, target)
        entries = [entry for entry in manifest if entry.lookup_key in located]
        seen = {entry.lookup_key: self._inspect(located[entry.lookup_key]) for entry in entries}
        plan = {entry.lookup_key: digests_to_read(entry, seen[entry.lookup_key]) for entry in entries}
        report = self._make_verify_progress(
            rom_id, sum(request.size_bytes for requests in plan.values() for request in requests)
        )
        observed: dict[str, LocalFile] = {}
        for entry in entries:
            key = entry.lookup_key
            found = seen[key]
            digests = {request.member: self._read_digest(located[key][0], request, report) for request in plan[key]}
            observed[key] = replace(
                found,
                digest=digests.get("", ""),
                members=None
                if found.members is None
                else tuple(replace(member, digest=digests.get(member.name, "")) for member in found.members),
            )
        return observed

    def _inspect(self, location: tuple[str, int]) -> LocalFile:
        """Describe what sits at *location* before anything is hashed.

        An archive is opened here and nowhere else, and the name is what decides:
        it is the server's own, and RomM hashed a file with such a name by its
        contents. Listing costs one read of the central directory; when that
        fails the file stays an unopened container, which the comparison reports
        as something it cannot confirm rather than something that differs.
        """
        path, size = location
        if not is_archive_name(os.path.basename(path)):
            return LocalFile(size_bytes=size, digest="")
        found = self._download_file_store.list_archive_members(path)
        return LocalFile(
            size_bytes=size,
            digest="",
            members=None
            if found is None
            else tuple(
                LocalMember(name=member["name"], size_bytes=member["size_bytes"], crc32=member["crc32"])
                for member in found
            ),
            is_archive=True,
        )

    def _read_digest(self, path: str, request: DigestRequest, report: Callable[[int], None]) -> str:
        """Compute one planned digest, or ``""`` when the member cannot be read.

        A member the store cannot decompress — an unsupported compression
        method, or a container damaged since it was listed — leaves the entry
        unconfirmed, which the verdict reports as "cannot confirm". Accusing the
        content of differing on bytes that were never read would be the stronger
        claim and the plugin has not earned it.
        """
        if not request.member:
            return self._download_file_store.checksum(path, request.algorithm, report)
        try:
            return self._download_file_store.checksum_archive_member(path, request.member, request.algorithm, report)
        except Exception as e:
            self._logger.error(f"Could not read {request.member!r} inside {path} for verification: {e}")
            return ""

    def _locate(self, manifest: tuple[ServerFile, ...], target: _Target) -> dict[str, tuple[str, int]]:
        """Resolve each manifest entry to the ``(path, size)`` on disk it names.

        A single-file ROM has exactly one entry and one file, so the entry's key
        addresses the target path directly — the local filename comes from
        ``fs_name`` while the manifest states the server's, and the two need not
        spell the same thing.

        Within a directory an entry the server located is looked up by its
        ROM-relative path, which is what distinguishes two files sharing a
        basename in different subdirectories. An entry the server did **not**
        locate falls back to a search by basename (first match wins) — weaker,
        but honest about what the payload said.
        """
        if not target.is_multi:
            key = manifest[0].lookup_key if manifest else target.manifest_name
            return {key: (target.path, self._download_file_store.file_size(target.path))}

        by_rel: dict[str, tuple[str, int]] = {}
        by_name: dict[str, tuple[str, int]] = {}
        for path, size in self._download_file_store.scan_files_with_sizes(target.path):
            by_rel[os.path.relpath(path, target.path).replace(os.sep, "/")] = (path, size)
            by_name.setdefault(os.path.basename(path), (path, size))

        located: dict[str, tuple[str, int]] = {}
        for entry in manifest:
            found = by_rel.get(entry.rel_path) if entry.rel_path else by_name.get(entry.name)
            if found is not None:
                located[entry.lookup_key] = found
        return located

    def _make_verify_progress(self, rom_id: int, total_bytes: int):
        """Build the throttled byte-delta callback the hashing loop reports through.

        Runs on the executor worker thread, so the emit is marshaled back to the
        loop via ``call_soon_threadsafe`` — the same shape the download progress
        callback uses.
        """
        done = [0]
        last_emit = [0.0]

        def report(chunk_bytes: int) -> None:
            done[0] += chunk_bytes
            now = self._clock.monotonic()
            if now - last_emit[0] < _PROGRESS_INTERVAL_SEC and done[0] < total_bytes:
                return
            last_emit[0] = now
            self._loop.call_soon_threadsafe(self._schedule_verify_frame, rom_id, done[0], total_bytes)

        return report

    def _schedule_verify_frame(self, rom_id: int, bytes_done: int, bytes_total: int) -> None:
        """Fire one ``verify_progress`` frame from the loop thread."""
        self._loop.create_task(
            self._emit(
                "verify_progress",
                {"rom_id": rom_id, "bytes_done": bytes_done, "bytes_total": bytes_total},
            )
        )

    # ── Target resolution ───────────────────────────────────────────

    def _resolve_target(self, rom_detail: dict[str, Any]) -> _Target | None:
        """Resolve the path this ROM's content occupies, or ``None`` on an unsafe slug.

        Mirrors the download's own derivation: the platform directory is joined
        under containment, the server-supplied name is coerced to a single safe
        component, and a multi-file ROM is named by its ROM identity rather than
        by ``files[0]``. A multi-file ROM's path is its directory; a single-file
        ROM's is the file itself.
        """
        platform_slug = rom_detail.get("platform_slug", "")
        system = self._resolve_system(platform_slug, rom_detail.get("platform_fs_slug"))
        try:
            roms_dir = safe_join(self._retrodeck_paths.roms_path(), system)
        except (PathTraversalError, TypeError) as e:
            self._logger.error(f"Rejected adoption target: unsafe platform slug {system!r}: {e}")
            return None
        fallback = synthetic_rom_name(rom_detail)
        if is_multi_file_download(rom_detail):
            name, _changed = coerce_safe_component(resolve_extract_dir_name(rom_detail), fallback)
            return _Target(path=os.path.join(roms_dir, name), system=system, is_multi=True, manifest_name=name)
        local_name, _missing = resolve_local_file_name(rom_detail)
        name, _changed = coerce_safe_component(local_name, fallback)
        manifest = server_manifest(rom_detail)
        return _Target(
            path=os.path.join(roms_dir, name),
            system=system,
            is_multi=False,
            manifest_name=manifest[0].name if manifest else name,
        )
