"""RomAdoptionService — ROM content the plugin finds rather than fetches.

Owns the three answers the adopt dialog needs about the path a download would
write to: whether something is already there, whether what is there is the ROM
the server holds, and — on the user's word — the ``rom_installs`` row that turns
it into an install. The row itself is written by the shared
``RomInstallRecorder``, so an adopted install is derived by exactly the rules a
downloaded one is (ADR-0028).

The service never deletes on its own initiative. The one destructive path is the
replace leg of the download gate, which runs only because the user chose it over
adopting, and only inside the RetroDECK ROMs tree.
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
    compare_manifest,
    digests_to_read,
    is_archive_name,
    occupied_target_refusal,
    server_manifest,
    unconfirmed_reason,
    verification_status,
)
from domain.rom_files import (
    detect_launch_file,
    is_multi_file_download,
    resolve_extract_dir_name,
    resolve_local_file_name,
    synthetic_rom_name,
)
from lib.errors import error_response
from lib.path_safety import PathTraversalError, coerce_safe_component, is_safe_rom_path, safe_join

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import (
        Clock,
        DownloadFileStore,
        EventEmitter,
        RetroDeckPaths,
        RomInstallRecorder,
        RommRomReader,
        SiblingSupersedeProvider,
        SystemM3uSupportFn,
        SystemResolver,
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
    does for an extracted one.
    """

    romm_api: RommRomReader
    download_file_store: DownloadFileStore
    resolve_system: SystemResolver
    retrodeck_paths: RetroDeckPaths
    install_recorder: RomInstallRecorder
    m3u_support: SystemM3uSupportFn
    # Deferred: the supersede lives on DownloadService, which is built after
    # this service (it consumes the occupancy gate below).
    sibling_supersede: SiblingSupersedeProvider
    uow_factory: UnitOfWorkFactory
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    clock: Clock


class RomAdoptionService:
    """Collision detection, content verification, and adoption of on-disk ROMs."""

    def __init__(self, *, config: RomAdoptionServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._download_file_store = config.download_file_store
        self._resolve_system = config.resolve_system
        self._retrodeck_paths = config.retrodeck_paths
        self._install_recorder = config.install_recorder
        self._m3u_support = config.m3u_support
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

        ``None`` means proceed: the path was free, it already belongs to this
        ROM's own install, or it was cleared because the user chose to replace
        what was there. Anything else is a canonical failure the caller returns
        untouched — the ``target_occupied`` refusal carrying both sides of the
        comparison, or a removal that could not be completed.

        Neither leg of the decision is bounded work — describing an occupied
        directory walks it whole (a multi-file install can hold tens of thousands
        of files) and clearing one deletes it whole — so the whole check runs off
        the loop. The offload lives here rather than at the call site: the caller
        asks a question and should not have to know what answering it costs.
        """
        worker = partial(self._check_download_target_io, rom_detail, checked_path, replace=replace)
        return await self._loop.run_in_executor(None, worker)

    def _check_download_target_io(
        self, rom_detail: dict[str, Any], checked_path: str, *, replace: bool
    ) -> dict[str, Any] | None:
        """Synchronous body of the download-target gate. Runs on an executor thread."""
        existing = self._download_file_store.describe_path(checked_path)
        if existing is None:
            return None
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

    async def adopt_existing_rom(self, rom_id) -> dict[str, Any]:
        """Record the content already at this ROM's target path as its install.

        Nothing is downloaded, generated or renamed — adoption records what is
        there. The path is re-validated immediately before the row is written, so
        content that vanished between the dialog and the confirmation is a
        refusal rather than a row pointing at nothing.

        **Order: validate, supersede, record.** Every reason this adoption could
        be refused is decided in the first step, because the second one deletes
        another version's files: superseding first and refusing afterwards would
        leave the user with a working version destroyed and nothing bound in its
        place. Recording first is no better — a supersede that then failed would
        leave the two installed versions the rule exists to prevent, with the
        adoption already committed. Only a refusal that has already been ruled
        out is safe to delete in front of.
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
        refusal = await self._loop.run_in_executor(None, self._validate_adoption_io, rom_id, target)
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

    def _validate_adoption_io(self, rom_id: int, target: _Target) -> dict[str, Any] | None:
        """Every refusal this adoption can produce, decided before anything is deleted.

        ``None`` means the adoption will go through. Runs off the loop: it stats
        the target and reads the ``roms`` row in one short UoW — the row has to
        exist for the install's foreign key, and asking here turns what would
        otherwise be an exception *after* the supersede into a refusal before it.
        """
        existing = self._download_file_store.describe_path(target.path)
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
        with self._uow_factory() as uow:
            known = uow.roms.get(rom_id) is not None
        if not known:
            return {
                "success": False,
                "reason": "invalid_install",
                "message": "This game is not in the local library — nothing was adopted",
            }
        return None

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

    async def verify_existing_content(self, rom_id) -> dict[str, Any]:
        """Compare the content at this ROM's target path against RomM's manifest.

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
        return await self._loop.run_in_executor(None, self._verify_io, rom_id, rom_detail, target)

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
