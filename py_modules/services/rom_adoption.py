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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.rom_adoption import (
    LocalFile,
    ServerFile,
    compare_manifest,
    occupied_target_refusal,
    server_manifest,
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

    from services.protocols import (
        Clock,
        DownloadFileStore,
        EventEmitter,
        RetroDeckPaths,
        RomInstallRecorder,
        RommRomReader,
        SystemM3uSupportFn,
        SystemResolver,
    )

# Minimum seconds between two ``verify_progress`` frames. Matches the download
# path's throttle so the two feel like one progress channel to the user.
_PROGRESS_INTERVAL_SEC = 0.5

_VERIFY_MESSAGES = {
    "match": "These files match the ones on the server",
    "mismatch": "These files differ from the ones on the server",
    "unverifiable": "This RomM server publishes no checksums, so it cannot confirm these files",
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
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock

    # ── Download pre-flight (DownloadTargetGateFn) ──────────────────

    def check_download_target(
        self, rom_detail: dict[str, Any], checked_path: str, *, replace: bool
    ) -> dict[str, Any] | None:
        """Decide whether a download may write to *checked_path*.

        ``None`` means proceed: the path was free, or it was cleared because the
        user chose to replace what was there. Anything else is a canonical
        failure the caller returns untouched — the ``target_occupied`` refusal
        carrying both sides of the comparison, or a removal that could not be
        completed.
        """
        existing = self._download_file_store.describe_path(checked_path)
        if existing is None:
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
        return await self._loop.run_in_executor(None, self._adopt_io, rom_id, rom_detail, target)

    def _adopt_io(self, rom_id: int, rom_detail: dict[str, Any], target: _Target) -> dict[str, Any]:
        """Re-validate the content and persist the install record. Runs off the loop."""
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
        observed = self._observe(rom_id, manifest, target)
        differences = compare_manifest(manifest, observed)
        status = verification_status(manifest, differences)
        return {
            "status": status,
            "message": _VERIFY_MESSAGES[status],
            "differences": [{"name": d.name, "expected": d.expected, "actual": d.actual} for d in differences],
        }

    def _observe(self, rom_id: int, manifest: tuple[ServerFile, ...], target: _Target) -> dict[str, LocalFile]:
        """Read each manifest entry's counterpart on disk, hashing where it is worth it.

        A directory's files are matched to the manifest **by name**: RomM states
        each file's path relative to its own library root, and the prefix that
        maps onto the ROM directory is server-side knowledge the plugin does not
        model. A digest is computed only when the server has one to compare
        against and the sizes already agree — a size mismatch is proof enough,
        and re-reading a gigabyte to restate it would cost the user 20 seconds
        (measured: 77 MiB/s on a Steam Deck SD card).
        """
        by_name = self._local_files_by_name(target)
        hashable = [
            entry
            for entry in manifest
            if entry.verifiable and entry.name in by_name and by_name[entry.name][1] == entry.size_bytes
        ]
        total_bytes = sum(by_name[entry.name][1] for entry in hashable)
        report = self._make_verify_progress(rom_id, total_bytes)
        observed: dict[str, LocalFile] = {}
        hashable_names = {entry.name for entry in hashable}
        algorithms = {entry.name: entry.algorithm for entry in manifest}
        for name, (path, size) in by_name.items():
            digest = ""
            if name in hashable_names:
                digest = self._download_file_store.checksum(path, algorithms[name], report)
            observed[name] = LocalFile(size_bytes=size, digest=digest)
        return observed

    def _local_files_by_name(self, target: _Target) -> dict[str, tuple[str, int]]:
        """Map each on-disk file's basename to its ``(path, size)``.

        A single-file ROM contributes the one path under the manifest entry's own
        name, because the local filename is derived from ``fs_name`` while the
        manifest states the server's, and the two need not spell the same thing.
        Within a directory the first of two files sharing a basename wins;
        nothing in the manifest can distinguish them.
        """
        if not target.is_multi:
            return {target.manifest_name: (target.path, self._download_file_store.file_size(target.path))}
        by_name: dict[str, tuple[str, int]] = {}
        for path, size in self._download_file_store.scan_files_with_sizes(target.path):
            by_name.setdefault(os.path.basename(path), (path, size))
        return by_name

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
