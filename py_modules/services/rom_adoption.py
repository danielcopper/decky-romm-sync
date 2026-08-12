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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from domain.adoption_rename import (
    OVERWRITE,
    SAVE,
    SAVESTATE,
    CompanionDir,
    RenamePair,
    collision_refusal,
    pairs_for_choice,
    rename_pairs,
    split_collisions,
)
from domain.rom_adoption import (
    DigestRequest,
    FileDifference,
    LocalFile,
    LocalMember,
    ServerFile,
    compare_manifest,
    digests_to_read,
    is_archive_name,
    occupied_target_refusal,
    server_manifest,
    unconfirmed_reason,
    verification_status,
)
from domain.rom_candidates import (
    LocalEntry,
    candidates_refusal,
    matching_entries,
    normalize_rom_name,
    rank_candidates,
)
from domain.rom_files import (
    detect_launch_file,
    is_multi_file_download,
    resolve_extract_dir_name,
    resolve_local_file_name,
    synthetic_rom_name,
)
from domain.save_layout import InSaveDir
from domain.save_path import resolve_save_dir
from lib.errors import error_response
from lib.path_safety import PathTraversalError, coerce_safe_component, is_safe_rom_path, safe_join

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from domain.save_layout import SaveLayout
    from services.protocols import (
        ActiveCoreReader,
        AdoptionMoveStore,
        Clock,
        CoreNameProviderFn,
        DownloadFileStore,
        EventEmitter,
        RetroArchSaveLayoutProvider,
        RetroArchSavestateLayoutProvider,
        RetroDeckPaths,
        RomInstallRecorder,
        RommRomReader,
        SiblingSupersedeProvider,
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


def _whole_file_crc32(rom_detail: dict[str, Any]) -> str:
    """RomM's CRC32 for this ROM's one file, or ``""`` when it does not have exactly one.

    A ROM the server holds as several files publishes no single number that could
    describe one entry on disk, so the candidate search ranks those on size or
    name instead of on a checksum it would have to invent.
    """
    manifest = server_manifest(rom_detail)
    return manifest[0].crc32 if len(manifest) == 1 else ""


def _travels_with(rom_source: str, directory: str) -> bool:
    """Whether *directory* moves with *rom_source* rather than standing beside it.

    True for a directory inside the content being renamed — a multi-file ROM
    whose emulator writes saves next to the game. Its files arrive at the new
    name as part of the ROM's own move, so pairing them up would move them twice.
    """
    return directory == rom_source or directory.startswith(rom_source + os.sep)


@dataclass(frozen=True)
class _Target:
    """The path a ROM's content occupies, and what the plugin expects to find there.

    ``manifest_name`` is the name the server's manifest uses for the single-file
    case, which need not equal the on-disk name the download derives from
    ``fs_name``; for a directory it is the directory's own name and unused.
    """

    path: str
    system: str
    is_multi: bool
    manifest_name: str


@dataclass(frozen=True)
class RomAdoptionServiceConfig:
    """Frozen wiring bundle handed to ``RomAdoptionService.__init__``.

    ``install_recorder`` is the shared install writer — the reason an adopted row
    cannot drift from a downloaded one. ``m3u_support`` gates whether a bundled
    ``.m3u`` may be chosen as an adopted directory's launch file, exactly as it
    does for an extracted one. ``system_extensions`` is the live ES-DE accept-list
    the candidate search filters a platform directory through. ``save_layout`` and
    ``savestate_layout`` are read separately because RetroArch sorts the two
    independently, and ``active_core`` / ``get_core_name`` answer the per-core
    subdirectory only when a layout says there is one.
    """

    romm_api: RommRomReader
    download_file_store: DownloadFileStore
    adoption_move: AdoptionMoveStore
    resolve_system: SystemResolver
    retrodeck_paths: RetroDeckPaths
    install_recorder: RomInstallRecorder
    m3u_support: SystemM3uSupportFn
    system_extensions: SystemSupportedExtensionsFn
    save_layout: RetroArchSaveLayoutProvider
    savestate_layout: RetroArchSavestateLayoutProvider
    active_core: ActiveCoreReader
    get_core_name: CoreNameProviderFn
    # Deferred: the supersede lives on DownloadService, which is built after
    # this service (it consumes the occupancy gate below).
    sibling_supersede: SiblingSupersedeProvider
    uow_factory: UnitOfWorkFactory
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    clock: Clock


class RomAdoptionService:
    """Collision detection, candidate search, content verification, and adoption of on-disk ROMs."""

    def __init__(self, *, config: RomAdoptionServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._download_file_store = config.download_file_store
        self._adoption_move = config.adoption_move
        self._resolve_system = config.resolve_system
        self._retrodeck_paths = config.retrodeck_paths
        self._install_recorder = config.install_recorder
        self._m3u_support = config.m3u_support
        self._system_extensions = config.system_extensions
        self._save_layout = config.save_layout
        self._savestate_layout = config.savestate_layout
        self._active_core = config.active_core
        self._get_core_name = config.get_core_name
        self._sibling_supersede = config.sibling_supersede
        self._uow_factory = config.uow_factory
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock

    # ── Download pre-flight (DownloadTargetGateFn) ──────────────────

    async def check_download_target(
        self, rom_detail: dict[str, Any], checked_path: str, *, replace: bool
    ) -> dict[str, Any] | None:
        """Decide whether a download may write to *checked_path*.

        ``None`` means proceed: the path was free and nothing else on disk looks
        like this game, it already belongs to this ROM's own install, or the user
        chose to download over whatever the gate found. Anything else is a
        canonical failure the caller returns untouched — the ``target_occupied``
        refusal carrying both sides of the comparison, the ``adoption_candidates``
        refusal carrying the short list, or a removal that could not be completed.

        None of the legs is bounded work — describing an occupied directory walks
        it whole (a multi-file install can hold tens of thousands of files),
        clearing one deletes it whole, and the candidate search lists a directory
        and reads archive indexes — so the whole check runs off the loop. The
        offload lives here rather than at the call site: the caller asks a
        question and should not have to know what answering it costs.
        """
        worker = partial(self._check_download_target_io, rom_detail, checked_path, replace=replace)
        return await self._loop.run_in_executor(None, worker)

    def _check_download_target_io(
        self, rom_detail: dict[str, Any], checked_path: str, *, replace: bool
    ) -> dict[str, Any] | None:
        """Synchronous body of the download-target gate. Runs on an executor thread."""
        existing = self._download_file_store.describe_path(checked_path)
        if existing is None:
            # A free path is the only state worth searching from: an occupied one
            # is already the dialog's subject, and the user who answered
            # ``replace`` has been shown what is there and chosen the download.
            return None if replace else self._candidate_refusal(rom_detail, checked_path)
        if self._is_own_install(rom_detail, checked_path):
            return None
        if not replace:
            return occupied_target_refusal(
                path=existing["path"],
                is_dir=existing["is_dir"],
                size_bytes=existing["size_bytes"],
                modified_at=existing["modified_at"],
                incoming_name=os.path.basename(checked_path),
                incoming_size=rom_detail.get("fs_size_bytes", 0),
                adoptable=existing["is_dir"] == is_multi_file_download(rom_detail),
            )
        return self._clear_for_replace(rom_detail, checked_path, is_dir=existing["is_dir"])

    # ── Candidate search ────────────────────────────────────────────

    def _candidate_refusal(self, rom_detail: dict[str, Any], checked_path: str) -> dict[str, Any] | None:
        """Refuse the download when the same game is already on disk under another name.

        ``None`` when nothing on disk could be this game, which is every ordinary
        download. The search runs here — at Download-click time — and never on the
        game-detail read, which stays at its single ``stat`` (ADR-0028).
        """
        candidates, truncated = self._search_candidates(rom_detail, checked_path)
        if not candidates:
            return None
        self._logger.info(f"Found {len(candidates)} adoption candidate(s) for rom {rom_detail.get('id')}")
        return candidates_refusal(
            candidates,
            truncated=truncated,
            incoming_name=os.path.basename(checked_path),
            incoming_size=rom_detail.get("fs_size_bytes", 0),
        )

    def _search_candidates(self, rom_detail: dict[str, Any], checked_path: str):
        """List the platform directory's top level and rank what could be this ROM.

        The name both sides are matched on is ``checked_path``'s basename — the
        name the download itself derived — so the search can never disagree with
        the path it is searching around.
        """
        platform_dir = os.path.dirname(checked_path)
        entries = tuple(
            LocalEntry(
                name=entry["name"],
                path=entry["path"],
                is_dir=entry["is_dir"],
                size_bytes=entry["size_bytes"],
                modified_at=entry["modified_at"],
            )
            for entry in self._download_file_store.list_top_level_entries(platform_dir)
        )
        if not entries:
            return ((), False)
        system = self._resolve_system(rom_detail.get("platform_slug", ""), rom_detail.get("platform_fs_slug"))
        matches = matching_entries(
            entries,
            wanted_name=normalize_rom_name(os.path.basename(checked_path)),
            want_dir=is_multi_file_download(rom_detail),
            accepted_extensions=self._system_extensions(system),
            covered_paths=self._installed_paths() | {checked_path},
        )
        return rank_candidates(
            matches,
            server_size=rom_detail.get("fs_size_bytes", 0),
            server_crc32=_whole_file_crc32(rom_detail),
            member_crc32s={match.path: self._member_crc32s(match) for match in matches},
        )

    def _member_crc32s(self, entry: LocalEntry) -> tuple[str, ...]:
        """The CRC32 of every member inside *entry*, from the central directory alone.

        Empty for anything that is not an archive by name and for a container this
        store cannot open — an absence of evidence, which leaves the entry ranked
        on what else it has.
        """
        if entry.is_dir or not is_archive_name(entry.name):
            return ()
        members = self._download_file_store.list_archive_members(entry.path)
        return () if members is None else tuple(member["crc32"] for member in members)

    def _installed_paths(self) -> frozenset[str]:
        """Every path a ``rom_installs`` row already accounts for.

        A ROM the plugin installed is another game's content and the row is the
        plugin's claim on it, so the search subtracts it rather than offering one
        game's files as a candidate for another's.
        """
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
        return frozenset(
            path for install in installs for path in (install.file_path, install.rom_dir) if path is not None
        )

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

        Refuses rather than deleting when the path is not safely inside the
        RetroDECK ROMs tree, and reports a failed removal instead of letting the
        download proceed onto ground it could not clear.
        """
        roms_base = self._retrodeck_paths.roms_path()
        if not roms_base or not is_safe_rom_path(checked_path, roms_base):
            self._logger.error(f"Refusing to replace content outside the ROMs directory: {checked_path}")
            return {
                "success": False,
                "reason": "unsafe_replace_target",
                "message": "Refusing to remove content outside the ROM directory",
            }
        if not is_dir and not is_multi_file_download(rom_detail):
            return None
        try:
            if is_dir:
                self._download_file_store.remove_tree(checked_path)
            else:
                self._download_file_store.remove_file(checked_path)
        except OSError as e:
            self._logger.error(f"Failed to remove existing content at {checked_path}: {e}")
            return {
                "success": False,
                "reason": "replace_failed",
                "message": "Could not remove the existing files — download aborted",
            }
        self._logger.info(f"Replacing existing content at {checked_path}")
        return None

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
            worker = partial(self._carry_to_canonical_io, rom_id, target, source_path, collision_choice)
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
        existing = self._download_file_store.describe_path(source_path)
        if existing is None:
            return {
                "success": False,
                "reason": "nothing_to_adopt",
                "message": "The files are no longer there — nothing was adopted",
            }
        if existing["is_dir"] != target.is_multi:
            return {
                "success": False,
                "reason": "unexpected_content_kind",
                "message": (
                    "A folder is in the way where a file belongs"
                    if existing["is_dir"]
                    else "A file is in the way where a folder belongs"
                ),
            }
        if source_path != target.path and self._adoption_move.exists(target.path):
            return {
                "success": False,
                "reason": "target_taken",
                "message": "Something arrived at this game's own location — nothing was moved",
            }
        with self._uow_factory() as uow:
            known = uow.roms.get(rom_id) is not None
        if not known:
            return {
                "success": False,
                "reason": "invalid_install",
                "message": "This game is not in the local library — nothing was adopted",
            }
        return None

    # ── Carry a candidate to the canonical name ─────────────────────

    def _carry_to_canonical_io(
        self, rom_id: int, target: _Target, source_path: str, collision_choice
    ) -> dict[str, Any] | None:
        """Move the candidate, and everything RetroArch named after it, into place.

        ``None`` means every file arrived. The whole plan is computed and every
        target checked **before** the first file moves: renaming as you go and
        asking at the first collision would leave half the set moved when the
        question appears.

        The ROM's own target is not part of that question. A name that appeared
        there since the validation is the occupied-target case the other dialog
        owns, so it is refused outright rather than offered as something to
        overwrite or skip.
        """
        pairs = self._rename_plan(rom_id, target, source_path)
        occupied = frozenset(pair.target for pair in pairs if self._adoption_move.exists(pair.target))
        if target.path in occupied:
            return {
                "success": False,
                "reason": "target_taken",
                "message": "Something arrived at this game's own location — nothing was moved",
            }
        clear, colliding = split_collisions(pairs, occupied)
        to_move = clear
        if colliding:
            choice = str(collision_choice or "")
            chosen = pairs_for_choice(clear, colliding, choice)
            if chosen is None:
                return collision_refusal(colliding)
            if choice == OVERWRITE:
                refusal = self._replace_occupied(colliding)
                if refusal is not None:
                    return refusal
            to_move = chosen
        return self._report_move(self._adoption_move.move_pairs(tuple((pair.source, pair.target) for pair in to_move)))

    def _replace_occupied(self, colliding: tuple[RenamePair, ...]) -> dict[str, Any] | None:
        """Delete the files an Overwrite answers for, before anything moves.

        Clearing first rather than replacing as each file lands keeps the two
        halves apart: one destructive phase the user answered for, then a move
        phase with no collisions left in it. A failure here has moved nothing, so
        the ROM is exactly where it was — but files the user chose to lose are
        already gone, and the refusal names them.
        """
        removed, error = self._adoption_move.remove_targets(tuple(pair.target for pair in colliding))
        if not error:
            return None
        already = (
            " These were already replaced: " + ", ".join(os.path.basename(path) for path in removed) + "."
            if removed
            else ""
        )
        self._logger.error(f"Adoption overwrite failed after removing {len(removed)} file(s): {error}")
        return {
            "success": False,
            "reason": "replace_failed",
            "message": f"Could not replace the existing files ({error}). Nothing was moved.{already}",
        }

    def _report_move(self, outcome) -> dict[str, Any] | None:
        """Turn a move outcome into a refusal, or ``None`` when everything arrived.

        A source left beside a completed target is not a failure: one inode under
        two names loses nothing and a re-run finishes it. It is logged rather than
        surfaced, because the user's game is playable and the alternative is a
        scary dialog about a state that harmed nothing.
        """
        if outcome["stranded"]:
            self._logger.warning(f"Adoption left old copies behind: {outcome['error']}")
        if not outcome["unmoved"]:
            return None
        moved = ", ".join(os.path.basename(path) for path in outcome["moved"])
        unmoved = ", ".join(os.path.basename(path) for path in outcome["unmoved"])
        self._logger.error(f"Adoption rename failed: {outcome['error']}")
        arrived = f" These are at their new names: {moved}." if moved else ""
        return {
            "success": False,
            "reason": "rename_failed",
            "message": (
                f"Could not rename this game's files ({outcome['error']}). "
                f"Still under the old name: {unmoved}.{arrived}"
            ),
        }

    def _rename_plan(self, rom_id: int, target: _Target, source_path: str) -> tuple[RenamePair, ...]:
        """Every source → target pair adopting this candidate consists of.

        The stems come from the **launch file**, not from what is being renamed:
        for a multi-file ROM the launch file sits inside the directory that moves,
        so its name does not change while the directory's does — which is exactly
        the case where the save *directory* moves and the save *filenames* stay.
        """
        launch_source, launch_target = self._launch_paths(target, source_path)
        return rename_pairs(
            rom_source=source_path,
            rom_target=target.path,
            stem_source=os.path.splitext(os.path.basename(launch_source))[0] if launch_source else "",
            stem_target=os.path.splitext(os.path.basename(launch_target))[0] if launch_target else "",
            companions=self._companions(rom_id, target, source_path, launch_source, launch_target),
        )

    def _launch_paths(self, target: _Target, source_path: str) -> tuple[str, str]:
        """The file RetroArch names the saves after, where it is now and where it will be.

        For a single-file ROM that is the ROM itself. For a directory it is the
        launch file inside, picked by the download's own rule, and its place under
        the renamed directory is the same relative path. Two empty strings when a
        directory holds no file to launch — there is then no stem, and no
        companion can be attributed to this ROM.
        """
        if not target.is_multi:
            return (source_path, target.path)
        files = self._download_file_store.scan_files_with_sizes(source_path)
        detected = detect_launch_file(files, self._m3u_support(target.system))
        if detected is None:
            return ("", "")
        return (detected, os.path.join(target.path, os.path.relpath(detected, source_path)))

    def _companions(
        self, rom_id: int, target: _Target, source_path: str, launch_source: str, launch_target: str
    ) -> tuple[CompanionDir, ...]:
        """The save and savestate directories this rename has to carry files out of.

        The two layouts are read independently because RetroArch sorts them
        independently — a stock RetroDECK install content-sorts its savefiles and
        leaves its savestates unsorted, so assuming one from the other addresses
        the wrong directory. A directory that sits **inside** the content being
        moved is skipped: it travels with the rename already, and pairing its
        files up would move them a second time.
        """
        if not launch_source:
            return ()
        layouts = (
            (SAVE, self._save_layout(), self._retrodeck_paths.saves_path()),
            (SAVESTATE, self._savestate_layout(), self._retrodeck_paths.states_path()),
        )
        core_name = (
            self._core_name_for(rom_id)
            if any(isinstance(layout, InSaveDir) and layout.sort_by_core for _kind, layout, _root in layouts)
            else None
        )
        found: list[CompanionDir] = []
        for kind, layout, root in layouts:
            source_dir, target_dir = self._layout_dirs(
                layout, root, target.system, launch_source, launch_target, core_name
            )
            if _travels_with(source_path, source_dir) or _travels_with(source_path, target_dir):
                continue
            names = self._adoption_move.list_names(source_dir)
            if names:
                found.append(CompanionDir(kind=kind, source_dir=source_dir, target_dir=target_dir, names=names))
        return tuple(found)

    def _layout_dirs(
        self,
        layout: SaveLayout,
        root: str,
        system: str,
        launch_source: str,
        launch_target: str,
        core_name: str | None,
    ) -> tuple[str, str]:
        """Resolve one layout's directory for the old name and for the new one.

        The same resolver the save sync itself uses, pointed at the root the
        layout belongs to. A ``ContentDir`` layout has RetroArch writing beside
        the ROM, which is a directory the resolver does not describe — so it is
        answered directly, and for a multi-file ROM the caller then drops it
        because that directory moves with the ROM.
        """
        if not isinstance(layout, InSaveDir):
            return (os.path.dirname(launch_source), os.path.dirname(launch_target))
        roms_base = self._retrodeck_paths.roms_path()

        def resolved(rom_path: str) -> str:
            return resolve_save_dir(
                rom_path,
                root,
                system,
                roms_base=roms_base,
                sort_by_content=layout.sort_by_content,
                sort_by_core=layout.sort_by_core,
                core_name=core_name,
            )

        return (resolved(launch_source), resolved(launch_target))

    def _core_name_for(self, rom_id: int) -> str | None:
        """The RetroArch ``corename`` whose subdirectory this ROM's saves sit in, or ``None``.

        Warn-and-fall-back rather than fail-loud, matching what the save sync does
        with the same question: an unresolvable corename sends both this rename and
        every later sync to the parent directory, so they stay pointed at the same
        place instead of disagreeing about where the saves are.
        """
        core_so, _label = self._active_core.active_core_for_rom(rom_id)
        core_name = self._get_core_name(core_so) if core_so else None
        if core_name is None:
            self._logger.warning(
                f"RetroArch sorts this ROM's saves per core, but its corename could not be resolved "
                f"(core={core_so or 'unresolved'}) — looking in the parent directory instead"
            )
        return core_name

    def _adopt_io(self, rom_id: int, rom_detail: dict[str, Any], target: _Target) -> dict[str, Any]:
        """Persist the install record for content ``_validate_adoption_io`` accepted.

        The target is stat'd once more: the supersede ran in between, and content
        that vanished across it must be refused rather than recorded. That window
        is the one refusal that can follow a completed supersede, and it cannot
        be closed — a row pointing at files that are gone would be worse.
        """
        existing = self._download_file_store.describe_path(target.path)
        if existing is None or existing["is_dir"] != target.is_multi:
            return {
                "success": False,
                "reason": "nothing_to_adopt",
                "message": "The files are no longer there — nothing was adopted",
            }
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
