"""DownloadService — ROM download orchestration.

Owns every step between a frontend download request and a ROM
landing on disk: disk-space pre-flight, single-file and multi-file
downloads, ZIP extraction, and partial-download cleanup.
Raw filesystem I/O flows through the ``DownloadFileStore`` Protocol;
HTTP traffic flows through ``RommRomReader``.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from domain.rom_files import (
    build_m3u_content,
    detect_launch_file,
    es_de_collapse_rename,
    is_multi_file_download,
    needs_m3u,
    resolve_local_file_name,
)
from domain.rom_install import RomInstall
from domain.shortcut_data import build_launch_options, resolve_emulator_invocation
from lib.errors import error_response
from lib.list_result import ErrorCode
from lib.path_safety import PathTraversalError, safe_join

if TYPE_CHECKING:
    import logging

    from models.state import InstalledRomEntry

    from services.protocols import (
        ActiveCoreReader,
        Clock,
        DownloadFileStore,
        EventEmitter,
        RetroDeckPaths,
        RommRomReader,
        Sleeper,
        SystemResolver,
        UnitOfWorkFactory,
    )

_DOWNLOAD_QUEUE_MAX_TERMINAL = 50
_ZIP_TMP_EXT = ".zip.tmp"
_TMP_EXT = ".tmp"


class _DownloadControl:
    """Per-download cooperative-control flags. Set on the event-loop thread by
    ``cancel_download`` / ``pause_download``; polled on the executor worker
    thread by the progress callback, which raises ``CancelledError`` to abort
    the in-flight HTTP transfer when EITHER flag is set (#144).

    ``cancelled`` and ``paused`` differ only in the terminal handling: a cancel
    deletes the partial ``.tmp``; a pause keeps it so the transfer can resume
    from where it stopped. The abort mechanism (raise to unwind the executor
    transfer) is identical.

    Plain bools — not ``threading.Event`` — because the import-linter
    ``no-stdlib-io-in-services`` contract forbids ``threading`` in services, and
    under the GIL a one-way set-once bool flip needs no synchronisation.
    """

    __slots__ = ("cancelled", "paused")

    def __init__(self) -> None:
        self.cancelled = False
        self.paused = False


@dataclass(frozen=True)
class DownloadServiceConfig:
    """Frozen wiring bundle handed to ``DownloadService.__init__``.

    Holds the Protocol-typed adapters, runtime infrastructure, time/sleep
    seams, the SQLite Unit-of-Work factory, and path providers
    DownloadService needs at construction time. The shared ``active_core``
    resolver resolves a reinstalled ROM's full active core so
    ``download_complete`` re-bakes the ``-e`` override into ``launch_options``
    (the per-game/per-platform selection survives uninstall → reinstall).
    """

    romm_api: RommRomReader
    download_file_store: DownloadFileStore
    resolve_system: SystemResolver
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    emit: EventEmitter
    clock: Clock
    sleeper: Sleeper
    retrodeck_paths: RetroDeckPaths
    active_core: ActiveCoreReader
    uow_factory: UnitOfWorkFactory


class DownloadService:
    """ROM download engine: downloads and queue management."""

    def __init__(self, *, config: DownloadServiceConfig) -> None:
        self._romm_api = config.romm_api
        self._download_file_store = config.download_file_store
        self._resolve_system = config.resolve_system
        self._loop = config.loop
        self._logger = config.logger
        self._emit = config.emit
        self._clock = config.clock
        self._sleeper = config.sleeper
        self._retrodeck_paths = config.retrodeck_paths
        self._active_core = config.active_core
        self._uow_factory = config.uow_factory

        # Owned state
        self._download_in_progress: set[int] = set()
        self._download_queue: dict[int, dict[str, Any]] = {}
        self._download_tasks: dict[int, asyncio.Task[None]] = {}
        # Bounded concurrency: at most two ROMs transfer at once. Excess
        # downloads enter the queue with status "queued" and acquire the
        # semaphore in FIFO order inside ``_do_download``.
        self._download_semaphore = asyncio.Semaphore(2)
        # Reserved bytes per in-flight ROM, so the disk pre-flight accounts for
        # siblings already committed to download but not yet written to disk.
        self._reserved_bytes: dict[int, int] = {}
        # Per-download cooperative-control tokens. The progress callback polls its
        # captured token on the executor thread; ``cancel_download`` /
        # ``pause_download`` flip it on the loop thread to abort the in-flight
        # transfer (#144).
        self._control_tokens: dict[int, _DownloadControl] = {}

    async def shutdown(self) -> None:
        """Cancel in-flight per-ROM download tasks on plugin unload.

        Per-ROM tasks are cancelled fire-and-forget; their ``finally``
        clauses run on the event loop after this method returns, which
        is acceptable on plugin unload.
        """
        for task in self._download_tasks.values():
            task.cancel()
        self._download_tasks.clear()

    def _prune_download_queue(self):
        """Remove oldest completed/failed/cancelled items when over the limit.

        Keeps all active (downloading) items. Retains up to
        _DOWNLOAD_QUEUE_MAX_TERMINAL terminal items, removing the oldest
        (by insertion order) when the count exceeds the limit.
        """
        terminal_ids = [
            rid
            for rid, item in self._download_queue.items()
            if item.get("status") in ("completed", "failed", "cancelled")
        ]
        excess = len(terminal_ids) - _DOWNLOAD_QUEUE_MAX_TERMINAL
        if excess <= 0:
            return
        # Dict preserves insertion order (Python 3.7+), so the first
        # entries in terminal_ids are the oldest.
        for rid in terminal_ids[:excess]:
            del self._download_queue[rid]

    def _remove_tmp_files(self, paths: list[str]) -> int:
        """Remove each path in *paths*, logging a warning on per-file failure.

        Returns the count of successful removals. Mirrors the
        SteamGridService cache-prune pattern: service owns the loop +
        ``try``/``except`` + ``logger.warning`` so the operational
        signal on each failure is preserved instead of being swallowed
        inside the adapter.
        """
        removed = 0
        for path in paths:
            try:
                self._download_file_store.remove_file(path)
                removed += 1
            except OSError as e:
                self._logger.warning(f"Failed to remove tmp file {path}: {e}")
        return removed

    def _clean_rom_tmp_files(self):
        """Remove leftover .tmp and .zip.tmp files from ROM directories."""
        roms_base = self._retrodeck_paths.roms_path()
        if not roms_base:
            return 0
        paths = self._download_file_store.walk_files_matching_suffixes(roms_base, (_TMP_EXT, _ZIP_TMP_EXT))
        return self._remove_tmp_files(paths)

    def _clean_bios_tmp_files(self):
        """Remove leftover .tmp files from BIOS directory."""
        bios_base = self._retrodeck_paths.bios_path()
        if not bios_base:
            return 0
        paths = self._download_file_store.walk_files_matching_suffixes(bios_base, (_TMP_EXT,))
        return self._remove_tmp_files(paths)

    def cleanup_leftover_tmp_files(self):
        """Remove leftover .tmp and .zip.tmp files from ROM and BIOS directories on startup.

        v1 note: this also deletes the ``.tmp`` of a download paused before a
        plugin reload. That is acceptable — the in-memory download queue does not
        survive a reload either, so a paused download could not have been resumed
        across one regardless; the next download restarts from scratch.
        """
        cleaned = self._clean_rom_tmp_files() + self._clean_bios_tmp_files()
        if cleaned:
            self._logger.info(f"Cleaned {cleaned} leftover tmp file(s)")

    async def start_download(self, rom_id):
        rom_id = int(rom_id)
        if rom_id in self._download_in_progress:
            return {"success": False, "reason": "already_downloading", "message": "Already downloading"}
        return await self._begin_download(rom_id, resume=False)

    async def _begin_download(self, rom_id, *, resume: bool):
        """Shared core of ``start_download`` and ``resume_download``.

        Fetches ROM detail, resolves the platform path, runs the disk pre-flight,
        then registers the queue entry, task, byte reservation, and control token.
        On ``resume=True`` the disk pre-flight discounts the bytes already on the
        existing ``.tmp`` (only the remainder is still needed) and ``_do_download``
        is started with ``resume=True`` so the transfer appends rather than restarts.

        The ``already_downloading`` guard stays with ``start_download``;
        ``resume_download`` validates the paused entry before calling here.
        """
        self._download_in_progress.add(rom_id)
        try:
            rom_detail = await self._loop.run_in_executor(None, self._romm_api.get_rom, rom_id)
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to fetch ROM {rom_id}: {e}")
            return error_response(e)

        platform_slug = rom_detail.get("platform_slug", "")
        platform_fs_slug = rom_detail.get("platform_fs_slug")
        system = self._resolve_system(platform_slug, platform_fs_slug)

        # Path building, directory creation, and the disk pre-flight can raise
        # (SD card unmounted → OSError; ``roms_path()`` returning None → TypeError
        # in the join). Any raise here must release the in-progress flag so the
        # ROM isn't stuck "Already downloading" until a plugin reload (#1048).
        # The explicit early-return guards inside still ``return`` (not raise)
        # and discard the flag themselves; a ``return`` does not trip the except.
        try:
            roms_path = self._retrodeck_paths.roms_path()
            try:
                # ``system`` may be an unmapped server slug passed through verbatim
                # (ADR-0010). Validate it stays under roms_path BEFORE any make_dirs
                # so a slug like "../../etc" cannot create or write outside roms.
                roms_dir = safe_join(roms_path, system)
            except PathTraversalError as e:
                self._download_in_progress.discard(rom_id)
                self._logger.error(f"Rejected download for ROM {rom_id}: unsafe platform slug {system!r}: {e}")
                await self._emit(
                    "download_failed",
                    {
                        "rom_id": rom_id,
                        "rom_name": rom_detail.get("name", ""),
                        "platform_name": rom_detail.get("platform_name", platform_slug),
                        "error_message": "Server sent an unsafe platform path — download aborted",
                    },
                )
                return {
                    "success": False,
                    "reason": "path_traversal",
                    "message": "Server sent an unsafe platform path — download aborted",
                }
            file_name, files_missing = resolve_local_file_name(rom_detail)
            if files_missing:
                self._logger.warning(
                    f"has_nested_single_file=true but files list is empty; falling back to fs_name='{file_name}'"
                )
            # Fix 1: Sanitize fs_name to prevent path traversal
            safe_name = os.path.basename(file_name)
            if safe_name != file_name:
                self._logger.warning(f"Sanitized fs_name from '{file_name}' to '{safe_name}'")
                file_name = safe_name
            file_size = rom_detail.get("fs_size_bytes", 0)
            target_path = os.path.join(roms_dir, file_name)

            # Check disk space: multi-file ROMs need space for ZIP + extracted contents
            self._download_file_store.make_dirs(roms_dir)
            free_space = self._download_file_store.disk_free(roms_dir)
            buffer = 100 * 1024 * 1024
            required = file_size * 2 + buffer if is_multi_file_download(rom_detail) else file_size + buffer
            # On resume, the partial ``.tmp`` already holds some of the bytes, so
            # only the remainder still needs free space — discount what's on disk
            # so a near-complete resume isn't rejected for the full size.
            if resume:
                required = max(required - self._partial_tmp_size(target_path, rom_detail), 0)
            # Account for siblings already reserved but not yet written to disk,
            # so two concurrent downloads can't each pass a pre-flight that only
            # one of them actually fits (#1053).
            reserved_total = sum(self._reserved_bytes.values())
            if file_size and free_space - reserved_total < required:
                self._download_in_progress.discard(rom_id)
                free_mb = max(free_space - reserved_total, 0) // (1024 * 1024)
                need_mb = required // (1024 * 1024)
                return {
                    "success": False,
                    "reason": "insufficient_space",
                    "message": f"Not enough disk space ({free_mb}MB free, need {need_mb}MB)",
                }
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to prepare download for ROM {rom_id}: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": "Failed to start download"}

        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", platform_slug)
        # Carry the prior resumability verdict across a resume so the UI keeps
        # showing Pause before the resumed transfer's headers re-confirm it.
        prior = self._download_queue.get(rom_id, {})
        resumable = bool(prior.get("resumable", False))

        # Create the control token BEFORE the task so the closure captured inside
        # ``_do_download``'s progress callback polls this exact object. A later
        # re-download installs a fresh token, leaving the zombie's callback bound
        # to the cancelled one (#144).
        control = _DownloadControl()
        try:
            task = self._loop.create_task(
                self._do_download(rom_id, rom_detail, target_path, system, file_name, control, resume=resume)
            )
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to start download task for ROM {rom_id}: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": "Failed to start download"}

        self._download_queue[rom_id] = {
            "rom_id": rom_id,
            "rom_name": rom_name,
            "platform_name": platform_name,
            "file_name": file_name,
            # Honest initial status: the task hasn't acquired the concurrency
            # semaphore yet. ``_do_download`` flips this to "downloading" once it
            # enters the critical section (#1053).
            "status": "queued",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": file_size,
            # Whether the server proved byte-range resume support for this ROM.
            # Re-confirmed live by the ``on_meta`` callback once headers arrive.
            "resumable": resumable,
        }
        self._download_tasks[rom_id] = task
        # Reserve this download's required bytes so a concurrent sibling's
        # pre-flight sees the outstanding claim (released in ``_do_download``'s
        # ``finally``).
        self._reserved_bytes[rom_id] = required
        self._control_tokens[rom_id] = control
        return {"success": True, "message": "Download started"}

    def _partial_tmp_size(self, target_path, rom_detail) -> int:
        """Bytes already on disk in the partial ``.tmp`` for *target_path*.

        Single-file ROMs stream to ``target_path + .tmp``; multi-file ROMs to
        ``target_path + .zip.tmp``. Returns 0 when no partial exists (the file
        store reports a missing path as size 0).
        """
        tmp_ext = _ZIP_TMP_EXT if is_multi_file_download(rom_detail) else _TMP_EXT
        return self._download_file_store.file_size(target_path + tmp_ext)

    def _record_install_io(self, *, rom_id, rom_detail, file_path, rom_dir, system, cleanup):
        """Build the ``RomInstall`` aggregate and persist it in a short write UoW.

        The filesystem work (rename, extraction, M3U detection) has already run
        outside any transaction; only the ``RomInstall`` upsert is wrapped here
        (ADR-0006). ``rom_dir`` is the dedicated extract directory for a
        multi-file ROM, or ``None`` for a single-file ROM (which owns no folder).
        If the RomM data fails the aggregate's invariant (non-positive
        ``rom_id``), nothing is persisted, *cleanup* removes the just-installed
        artifact, and a failure message is returned.

        Returns ``(file_path, None)`` on success or ``(None, error)`` when the
        invariant rejects the data.
        """
        try:
            install = RomInstall.mark_installed(
                rom_id=int(rom_id),
                file_path=file_path,
                rom_dir=rom_dir,
                platform_slug=rom_detail.get("platform_slug", ""),
                system=system,
                installed_at=self._clock.now().isoformat(),
            )
        except ValueError as e:
            cleanup()
            return None, f"Invalid install metadata: {e}"

        with self._uow_factory() as uow:
            uow.rom_installs.save(install)
        return file_path, None

    def _post_download_multi_io(self, rom_id, rom_detail, target_path, file_name, system):
        """Sync helper for _do_download multi-file — extraction + renames in executor.

        Returns ``(launch_file, error)``. ``error`` is a string when the RomM
        data fails the ``RomInstall`` invariant — the extracted directory is
        removed and nothing is persisted — otherwise ``None``.
        """
        rom_dir_name = os.path.splitext(file_name)[0]
        extract_dir = os.path.join(os.path.dirname(target_path), rom_dir_name)
        self._download_file_store.make_dirs(extract_dir)
        roms_base = self._retrodeck_paths.roms_path()
        tmp_zip = target_path + _ZIP_TMP_EXT
        # ZIP-slip protection: adapter validates members resolve within extract_dir
        # AND that extract_dir itself resolves within roms_base.
        self._download_file_store.extract_zip(tmp_zip, extract_dir, roms_base)
        self._download_file_store.remove_file(tmp_zip)
        self._download_file_store.decode_url_encoded_names(extract_dir)
        # Auto-generate M3U if missing and multiple disc files exist
        self._maybe_generate_m3u_io(extract_dir, rom_detail)
        # Detect launch file: prefer M3U > CUE > largest file
        launch_file = self._collect_and_detect_launch_file(extract_dir)
        # ES-DE collapses a multi-file dir into one game entry only when the
        # dir is named after the launch file *including* the extension. The
        # launch file is only known after extraction (the M3U may be
        # auto-generated above), so the rename happens here, last of all the
        # filesystem work, so a later failure cleans up the renamed dir.
        extract_dir, launch_file = self._maybe_es_de_collapse_io(extract_dir, launch_file)

        return self._record_install_io(
            rom_id=rom_id,
            rom_detail=rom_detail,
            file_path=launch_file,
            rom_dir=extract_dir,
            system=system,
            cleanup=lambda: self._download_file_store.remove_tree(extract_dir),
        )

    def _maybe_es_de_collapse_io(self, extract_dir: str, launch_file: str) -> tuple[str, str]:
        """Rename *extract_dir* after the launch file so ES-DE collapses it to one entry.

        Returns ``(rom_dir, launch_file)`` — the renamed pair when the move
        applied, or the originals unchanged. Moves the *whole* directory
        (never just the launch file — ADR-0008). Skips the move when
        ``es_de_collapse_rename`` reports no rename is needed, and on
        collision: if the target already exists the staging dir is kept and a
        warning is logged rather than clobbering or merging an existing dir.
        """
        rename = es_de_collapse_rename(extract_dir, launch_file)
        if rename is None:
            return (extract_dir, launch_file)
        new_rom_dir, new_launch_file = rename
        if self._download_file_store.exists(new_rom_dir):
            self._logger.warning(
                "ES-DE collapse rename skipped: target '%s' already exists; keeping staging dir '%s'",
                new_rom_dir,
                extract_dir,
            )
            return (extract_dir, launch_file)
        self._download_file_store.move_dir(extract_dir, new_rom_dir)
        return (new_rom_dir, new_launch_file)

    def _post_download_single_io(self, rom_id, rom_detail, target_path, system):
        """Sync helper for _do_download single-file — rename + DB persist in executor.

        Returns ``(target_path, error)``. ``error`` is a string when the RomM
        data fails the ``RomInstall`` invariant — the renamed file is removed
        and nothing is persisted — otherwise ``None``.
        """
        tmp_path = target_path + _TMP_EXT
        self._download_file_store.rename(tmp_path, target_path)

        return self._record_install_io(
            rom_id=rom_id,
            rom_detail=rom_detail,
            file_path=target_path,
            rom_dir=None,
            system=system,
            cleanup=lambda: self._download_file_store.remove_file(target_path),
        )

    def _resolve_bound_app_id(self, rom_id: int) -> tuple[int | None, str | None]:
        """Return the ROM's ``(shortcut_app_id, active_core_so)`` for the re-bake.

        Reads the ROM's Steam ``app_id`` in a short read UoW, then resolves the
        ROM's FULL active core through the shared ``active_core`` resolver so
        ``download_complete`` re-bakes the right launch command. ``app_id`` is
        ``None`` when the ROM has no Steam shortcut yet (not synced) — the
        frontend no-ops and the next sync writes the launch command.
        ``active_core_so`` is the resolved ``.so`` when the ROM's
        per-game/per-platform/system resolution yields a core (bake the ``-e``
        form), ``None`` when it resolves to ``(None, None)`` — a genuinely
        unresolvable platform (bake the plain launch). The resolver already warns
        + degrades on a stale label, so no bogus ``None.so`` ever reaches the
        bake. This is the load-bearing site: the per-game override lives on
        ``roms`` so it survives uninstall → reinstall, and reinstall goes through
        here.
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(int(rom_id))
        if rom is None:
            return (None, None)
        core_so, _label = self._active_core.active_core_for_rom(int(rom_id))
        return (rom.shortcut_app_id, core_so)

    def _make_progress_callback(self, rom_id, rom_name, platform_name, file_name, control=None):
        """Build a throttled progress callback for a download."""
        if control is None:
            control = _DownloadControl()
        last_emit = [0.0]  # mutable container for closure
        last_log = [0.0]

        def progress_callback(downloaded, total):
            if control.cancelled or control.paused:
                # Abort the in-flight transfer thread (#144). CancelledError is a
                # BaseException, so it propagates untouched through the adapter's
                # Exception-only retry/translate — no retry, no error translation.
                # Both cancel and pause unwind through here; the terminal handling
                # in ``_do_download`` branches on which flag was set.
                raise asyncio.CancelledError()
            now = self._clock.monotonic()
            if now - last_log[0] >= 30.0:
                last_log[0] = now
                self._log_download_progress(rom_name, downloaded, total)
            if now - last_emit[0] < 0.5 and downloaded < total:
                return
            last_emit[0] = now
            progress = downloaded / total if total else 0

            # This callback runs on a ``run_in_executor`` worker thread. Both the
            # queue-dict mutation and the emit-scheduling must happen on the loop
            # thread, so marshal them across via ``call_soon_threadsafe`` (#973).
            self._loop.call_soon_threadsafe(
                self._apply_download_progress,
                rom_id,
                rom_name,
                platform_name,
                file_name,
                progress,
                downloaded,
                total,
            )

        return progress_callback

    def _log_download_progress(self, rom_name, downloaded, total):
        """Log a throttled one-line human-readable progress summary (MB + %)."""
        mb_dl = downloaded / (1024 * 1024)
        mb_total = total / (1024 * 1024) if total else 0
        pct = (downloaded / total * 100) if total else 0
        self._logger.info(f"Download progress: {rom_name} — {mb_dl:.1f}/{mb_total:.1f} MB ({pct:.0f}%)")

    def _apply_download_progress(self, rom_id, rom_name, platform_name, file_name, progress, downloaded, total):
        """Update the live queue entry and schedule a ``download_progress`` emit.

        Runs on the loop thread (marshaled from the executor worker via
        ``call_soon_threadsafe``). Guarded by ``.get`` — if the entry was evicted
        between ticks we must not resurrect it or raise KeyError off-thread (#973).
        """
        entry = self._download_queue.get(rom_id)
        if entry is None:
            return  # evicted mid-download — do not resurrect or emit
        entry.update(
            {
                "progress": progress,
                "bytes_downloaded": downloaded,
                "total_bytes": total,
            }
        )
        self._loop.create_task(
            self._emit(
                "download_progress",
                {
                    "rom_id": rom_id,
                    "rom_name": rom_name,
                    "platform_name": platform_name,
                    "file_name": file_name,
                    "status": "downloading",
                    "progress": progress,
                    "bytes_downloaded": downloaded,
                    "total_bytes": total,
                    "resumable": entry.get("resumable", False),
                },
            )
        )

    async def _finalize_download_complete(self, rom_id, rom_detail, final_path, rom_name, platform_name):
        """Mark the queue entry completed and emit ``download_complete``.

        Resolves the bound Steam ``shortcut_app_id`` for this rom_id (or ``None``
        when the ROM hasn't been synced yet) plus the ROM's full active core
        (resolved ``.so`` or ``None``) so the frontend confirm-sets launch options
        on the exact shortcut without a full-library scan to re-resolve
        rom_id→app_id, and the re-bake keeps the per-game/per-platform core across
        uninstall → reinstall. Called from the normal success path and from the
        cancel handler when the install committed before the cancel landed (#1049).
        """
        entry = self._download_queue[rom_id]
        entry["status"] = "completed"
        entry["progress"] = 1.0
        app_id, active_core_so = await self._loop.run_in_executor(None, self._resolve_bound_app_id, rom_id)
        await self._emit(
            "download_complete",
            {
                "rom_id": rom_id,
                "rom_name": rom_name,
                "platform_name": platform_name,
                "file_path": final_path,
                "app_id": app_id,
                "launch_options": build_launch_options(
                    resolve_emulator_invocation(rom_detail, active_core_so), final_path
                ),
                "resumable": entry.get("resumable", False),
            },
        )
        self._logger.info(f"Download complete: {rom_name} -> {final_path}")

    async def _reconcile_post_io(self, post_io_future):
        """After a cancel, settle an in-flight post-IO commit and report whether
        the install committed. Executor threads run to completion regardless of
        cancellation, so letting the future settle here lets a race-committed
        install be honored instead of torn down. Returns (final_path, committed).
        """
        if post_io_future is None:
            return (None, False)  # cancel landed before the post-IO phase started
        # Let the (shielded) executor future settle WITHOUT awaiting it directly:
        # ``asyncio.wait`` reports completion through the future's own state, so a
        # cancelled or failed commit is inspected here, never swallowed — the
        # caller's ``except asyncio.CancelledError`` keeps ownership of the re-raise.
        await asyncio.wait({post_io_future})
        if post_io_future.cancelled() or post_io_future.exception() is not None:
            # Executor work was cancelled before it ran, or the commit raised.
            return (None, False)
        final_path, post_io_error = post_io_future.result()
        if post_io_error is None and final_path is not None:
            return (final_path, True)
        return (None, False)

    def _make_on_meta(self, rom_id, rom_name, platform_name, file_name):
        """Build the one-shot resumability callback the adapter fires when the
        download's response headers arrive (before the body streams).

        It records the server's ``range_supported`` verdict on the queue entry
        and emits a ``download_progress`` frame carrying it, so the frontend can
        flip Pause/Cancel live DURING the transfer instead of only learning the
        verdict at the end. Runs on the executor transfer thread, so it hops back
        to the loop via ``call_soon_threadsafe`` like the progress callback (#973).
        """

        def on_meta(range_supported: bool) -> None:
            def _apply() -> None:
                entry = self._download_queue.get(rom_id)
                if entry is None:
                    return  # evicted mid-download — do not resurrect or emit
                entry["resumable"] = range_supported
                self._loop.create_task(
                    self._emit(
                        "download_progress",
                        {
                            "rom_id": rom_id,
                            "rom_name": rom_name,
                            "platform_name": platform_name,
                            "file_name": file_name,
                            "status": entry.get("status", "downloading"),
                            "progress": entry.get("progress", 0),
                            "bytes_downloaded": entry.get("bytes_downloaded", 0),
                            "total_bytes": entry.get("total_bytes", 0),
                            "resumable": range_supported,
                        },
                    )
                )

            self._loop.call_soon_threadsafe(_apply)

        return on_meta

    async def _do_download(self, rom_id, rom_detail, target_path, system, file_name, control=None, *, resume=False):
        if control is None:
            # Direct invocation (no ``_begin_download``): own + register a control
            # so the ``finally``'s identity-gated cleanup releases this task's
            # registrations like the real path does.
            control = _DownloadControl()
            self._control_tokens[rom_id] = control
        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", rom_detail.get("platform_slug", ""))
        has_multiple = is_multi_file_download(rom_detail)
        progress_callback = self._make_progress_callback(rom_id, rom_name, platform_name, file_name, control)
        on_meta = self._make_on_meta(rom_id, rom_name, platform_name, file_name)
        # Tracks the resolved launch path once extraction returns it, so a
        # failure AFTER the ES-DE collapse rename cleans up the *renamed* dir
        # (``os.path.dirname(final_path)``) — not just the staging name.
        final_path: str | None = None
        # The post-IO commit future, declared before the try so the cancel
        # handler can reconcile a race-committed install (#1049). Stays ``None``
        # while waiting on the concurrency semaphore or during the transfer.
        post_io_future: asyncio.Future[tuple[str | None, str | None]] | None = None

        try:
            self._logger.info(f"Download starting: {rom_name} (rom_id={rom_id}, multi={has_multiple}) -> {target_path}")

            # Bounded concurrency (#1053): only two ROMs transfer at once. If the
            # semaphore is already held, surface an honest "queued" frame so the
            # UI shows the wait instead of a stalled "downloading" bar.
            if self._download_semaphore.locked():
                entry = self._download_queue[rom_id]
                entry["status"] = "queued"
                await self._emit(
                    "download_progress",
                    {
                        "rom_id": rom_id,
                        "rom_name": rom_name,
                        "platform_name": platform_name,
                        "file_name": file_name,
                        "status": "queued",
                        "progress": 0,
                        "bytes_downloaded": 0,
                        "total_bytes": rom_detail.get("fs_size_bytes", 0),
                        "resumable": entry.get("resumable", False),
                    },
                )

            async with self._download_semaphore:
                self._download_queue[rom_id]["status"] = "downloading"

                if has_multiple:
                    # Multi-file ROM: API returns ZIP, download to temp then extract
                    tmp_zip = target_path + _ZIP_TMP_EXT
                    await self._loop.run_in_executor(
                        None,
                        partial(
                            self._romm_api.download_rom_content,
                            rom_id,
                            file_name,
                            tmp_zip,
                            progress_callback,
                            resume=resume,
                            on_meta=on_meta,
                        ),
                    )
                    post_io_future = self._loop.run_in_executor(
                        None, self._post_download_multi_io, rom_id, rom_detail, target_path, file_name, system
                    )
                    # Shield the commit await: a cancel here must propagate to this
                    # coroutine WITHOUT cancelling the underlying future, so
                    # ``_reconcile_post_io`` can re-await it for the real result. A
                    # bare ``await`` cancels the asyncio future (the executor thread
                    # still commits), so the re-await would raise CancelledError and
                    # the committed install would be mis-reported as not committed
                    # → torn down (#1049).
                    final_path, post_io_error = await asyncio.shield(post_io_future)
                else:
                    tmp_path = target_path + _TMP_EXT
                    await self._loop.run_in_executor(
                        None,
                        partial(
                            self._romm_api.download_rom_content,
                            rom_id,
                            file_name,
                            tmp_path,
                            progress_callback,
                            resume=resume,
                            on_meta=on_meta,
                        ),
                    )
                    post_io_future = self._loop.run_in_executor(
                        None, self._post_download_single_io, rom_id, rom_detail, target_path, system
                    )
                    # Shielded so a racing cancel leaves the future intact for
                    # ``_reconcile_post_io`` to re-await (see the multi-file branch).
                    final_path, post_io_error = await asyncio.shield(post_io_future)

                if post_io_error is not None or final_path is None:
                    # The download succeeded but the install record failed its
                    # invariant; the artifact was already cleaned up by the worker.
                    # ``final_path is None`` always coincides with a non-None error
                    # — the guard narrows the type for the launch-command build below.
                    raise ValueError(post_io_error or "install record produced no launch path")

                await self._finalize_download_complete(rom_id, rom_detail, final_path, rom_name, platform_name)

        except asyncio.CancelledError:
            # The cancel/pause may have raced a committing install. Executor
            # threads run to completion, so wait for the post-IO future before
            # deciding (#1049).
            committed_path, committed = await self._reconcile_post_io(post_io_future)
            if committed:
                # The ROM IS installed — surface completed, don't tear it down.
                # This also bakes launch_options for the just-committed install.
                await self._finalize_download_complete(rom_id, rom_detail, committed_path, rom_name, platform_name)
                self._logger.info(f"Download cancelled after install committed; surfaced as complete: {rom_name}")
            elif control.paused:
                # PAUSE: keep the partial ``.tmp`` so the transfer can resume from
                # where it stopped. NOTHING is cleaned up. The entry stays "paused"
                # in the queue (``_prune_download_queue`` only prunes terminal
                # completed/failed/cancelled, so "paused" is retained).
                entry = self._download_queue[rom_id]
                entry["status"] = "paused"
                await self._emit(
                    "download_progress",
                    {
                        "rom_id": rom_id,
                        "rom_name": rom_name,
                        "platform_name": platform_name,
                        "file_name": file_name,
                        "status": "paused",
                        "progress": entry.get("progress", 0),
                        "bytes_downloaded": entry.get("bytes_downloaded", 0),
                        "total_bytes": entry.get("total_bytes", 0),
                        "resumable": entry.get("resumable", False),
                    },
                )
                self._logger.info(f"Download paused: {rom_name}")
            else:
                entry = self._download_queue[rom_id]
                entry["status"] = "cancelled"
                self._cleanup_partial_download(target_path, has_multiple, file_name, final_path)
                # #1017: emit a terminal frame so the frontend resets the button
                # out of its downloading state (the global cancel path was silent).
                await self._emit(
                    "download_progress",
                    {
                        "rom_id": rom_id,
                        "rom_name": rom_name,
                        "platform_name": platform_name,
                        "file_name": file_name,
                        "status": "cancelled",
                        "progress": entry.get("progress", 0),
                        "bytes_downloaded": entry.get("bytes_downloaded", 0),
                        "total_bytes": entry.get("total_bytes", 0),
                        "resumable": entry.get("resumable", False),
                    },
                )
                self._logger.info(f"Download cancelled: {rom_name}")
            raise

        except Exception as e:
            self._download_queue[rom_id]["status"] = "failed"
            self._download_queue[rom_id]["error"] = str(e)
            self._cleanup_partial_download(target_path, has_multiple, file_name, final_path)
            self._logger.error(f"Download failed for {rom_name}: {e}")
            await self._emit(
                "download_failed",
                {
                    "rom_id": rom_id,
                    "rom_name": rom_name,
                    "platform_name": platform_name,
                    "error_message": str(e),
                },
            )

        finally:
            # A re-download (or resume) can overwrite these per-download
            # registrations with a fresh attempt's BEFORE this (older/superseded)
            # task's finally runs. Gate ALL of them on the control-token identity
            # so a zombie/superseded task never evicts the newer attempt's task,
            # in-progress flag, reservation, or token (#144). The control is
            # registered by ``_begin_download``; a direct-call test that never
            # registered it simply skips these no-op pops.
            if self._control_tokens.get(rom_id) is control:
                self._download_tasks.pop(rom_id, None)
                self._download_in_progress.discard(rom_id)
                self._reserved_bytes.pop(rom_id, None)
                del self._control_tokens[rom_id]
            self._prune_download_queue()

    def _maybe_generate_m3u_io(self, extract_dir: str, rom_detail: dict[str, Any]) -> None:
        """Auto-generate a game-named M3U playlist when one is warranted (see ``needs_m3u``).

        Writes ``<fs_name_no_ext>.m3u`` when no M3U already exists and the disc
        files warrant one: multi-disc ROMs (any of cue/chd/iso) for disc
        switching, or a single-disc bin/cue ROM so the extract dir collapses to
        a game-named entry in ES-DE.
        """
        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        # Check if an M3U already exists (search recursively)
        if any(path.lower().endswith(".m3u") for path, _size in all_files):
            return

        # Collect disc files: .cue, .chd, .iso (search recursively)
        disc_files = [
            os.path.relpath(path, extract_dir)
            for path, _size in all_files
            if path.lower().endswith((".cue", ".chd", ".iso"))
        ]

        if not needs_m3u(disc_files):
            return

        rom_name = rom_detail.get("fs_name_no_ext", rom_detail.get("name", "playlist"))
        m3u_path = os.path.join(extract_dir, f"{rom_name}.m3u")
        self._download_file_store.write_text_atomic(m3u_path, build_m3u_content(disc_files))
        self._logger.info(f"Auto-generated M3U playlist: {m3u_path}")

    def _collect_and_detect_launch_file(self, extract_dir: str) -> str:
        """Find the best launch file in an extracted multi-file ROM directory."""
        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        result = detect_launch_file(all_files)
        return result if result is not None else extract_dir

    def _cleanup_partial_download(self, target_path, has_multiple, file_name, final_path=None):
        """Clean up partial download files. Each step is independent so one failure doesn't block others.

        Only ever called for a download that did NOT commit an install (the
        failure path and the cancel-without-commit path); a cancel that loses the
        race to a committed install routes to ``_finalize_download_complete``
        instead and never reaches here.

        Removes ONLY the transient transfer artifacts (``.zip.tmp`` / ``.tmp``)
        and, for a multi-file ROM, the extract dir(s) this download created. The
        bare ``target_path`` is NEVER removed: a single-file transfer writes to
        ``target_path + .tmp`` and only renames to ``target_path`` on success, so
        deleting the bare path would destroy a PRE-EXISTING install (a re-download
        that fails mid-stream) or a just-committed one (a cancel race) — the #1049
        data-loss bug.

        For a multi-file ROM the extract dir may have been renamed for ES-DE
        collapse after extraction. *final_path* (the resolved launch file,
        ``None`` until extraction returns it) lets cleanup tear down whichever
        of the two dir names exists — the staging name *and* the renamed dir
        (``os.path.dirname(final_path)``) — so no failure path orphans a dir.
        """
        paths_to_remove = [
            target_path + _ZIP_TMP_EXT,
            target_path + _TMP_EXT,
        ]
        for path in paths_to_remove:
            try:
                self._download_file_store.remove_file(path)
            except Exception as e:
                self._logger.warning(f"Cleanup failed for {path}: {e}")
        if has_multiple:
            staging_dir = os.path.join(os.path.dirname(target_path), os.path.splitext(file_name)[0])
            dirs_to_remove = {staging_dir}
            if final_path:
                dirs_to_remove.add(os.path.dirname(final_path))
            for extract_dir in dirs_to_remove:
                try:
                    self._download_file_store.remove_tree(extract_dir)
                except Exception as e:
                    self._logger.warning(f"Cleanup failed for directory {extract_dir}: {e}")

    def cancel_download(self, rom_id):
        rom_id = int(rom_id)
        task = self._download_tasks.get(rom_id)
        if not task:
            return {"success": False, "reason": "no_active_download", "message": "No active download for this ROM"}
        token = self._control_tokens.get(rom_id)
        if token is not None:
            token.cancelled = True  # stop the executor transfer thread, not just the asyncio wrapper (#144)
        task.cancel()
        return {"success": True, "message": "Download cancelled"}

    def pause_download(self, rom_id):
        """Pause an in-flight download, keeping the partial ``.tmp`` for resume.

        Mirrors ``cancel_download`` but flips ``control.paused`` instead of
        ``cancelled`` before cancelling the task, so ``_do_download``'s terminal
        handler routes to the pause branch (status "paused", no cleanup) rather
        than the cancel branch (status "cancelled", ``.tmp`` deleted). Kept
        defensive — the frontend only offers Pause when a download is resumable.
        """
        rom_id = int(rom_id)
        task = self._download_tasks.get(rom_id)
        if not task:
            return {"success": False, "reason": "no_active_download", "message": "No active download for this ROM"}
        token = self._control_tokens.get(rom_id)
        if token is not None:
            token.paused = True  # stop the executor transfer thread, keeping the .tmp (#144)
        task.cancel()
        return {"success": True, "message": "Download paused"}

    async def resume_download(self, rom_id):
        """Resume a previously paused download from its partial ``.tmp``.

        Requires a queue entry in status "paused"; otherwise returns the
        ``not_paused`` failure shape. Re-begins the download with ``resume=True``
        so the transfer appends onto the existing bytes (when the server honoured
        the original ``Range`` probe) instead of restarting.
        """
        rom_id = int(rom_id)
        entry = self._download_queue.get(rom_id)
        if entry is None or entry.get("status") != "paused":
            return {"success": False, "reason": "not_paused", "message": "No paused download for this ROM"}
        return await self._begin_download(rom_id, resume=True)

    def get_download_queue(self):
        return {"downloads": list(self._download_queue.values())}

    def get_installed_rom(self, rom_id: int) -> InstalledRomEntry | None:
        """Return the install record for *rom_id* as a frontend-shaped dict, or ``None``.

        Reads the ``RomInstall`` aggregate via the Unit of Work and projects it
        onto the ``InstalledRom`` shape the QAM panel + launch gate consume.
        ``file_name`` is derived from the launch ``file_path`` (the aggregate
        stores the launch file, not the original archive name).
        """
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(int(rom_id))
        if install is None:
            return None
        entry: InstalledRomEntry = {
            "rom_id": install.rom_id,
            "file_name": os.path.basename(install.file_path),
            "file_path": install.file_path,
            "system": install.system,
            "platform_slug": install.platform_slug,
            "installed_at": install.installed_at,
        }
        return entry

    # ── DownloadQueueCleanup Protocol ──────────────────────────────

    def evict(self, rom_id: int) -> None:
        """Remove the queue entry for *rom_id* if present. Idempotent."""
        self._download_queue.pop(int(rom_id), None)

    def clear(self) -> None:
        """Remove all entries from the download queue."""
        self._download_queue.clear()
