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

from domain.disc_formats import DISC_IMAGE_EXTENSIONS
from domain.disk_space import disk_space_verdict
from domain.download_frames import cancelled_frame, failed_frame
from domain.rom_files import (
    build_m3u_content,
    detect_launch_file,
    es_de_collapse_rename,
    folder_boot_layout_root,
    is_multi_file_download,
    needs_m3u,
    resolve_extract_dir_name,
    resolve_local_file_name,
    synthetic_rom_name,
)
from lib.errors import error_response
from lib.list_result import ErrorCode
from lib.path_safety import PathTraversalError, coerce_safe_component, safe_join

if TYPE_CHECKING:
    import logging

    from models.state import InstalledRomEntry

    from services.protocols import (
        Clock,
        DownloadFileStore,
        DownloadTargetGateFn,
        EventEmitter,
        RetroDeckPaths,
        RomInstallRecorder,
        RommRomReader,
        RomRemoverProvider,
        Sleeper,
        SystemM3uSupportFn,
        SystemResolver,
        UnitOfWorkFactory,
    )

_DOWNLOAD_QUEUE_MAX_TERMINAL = 50
# One wording for every way a download fails to get off the ground: the user
# reads the same sentence whichever step raised.
_START_FAILED_MESSAGE = "Failed to start download"
# Said twice for a single refusal — once to the frontend as a failure frame,
# once to the caller as the refusal itself — so the two cannot drift apart.
_UNSAFE_PATH_MESSAGE = "Server sent an unsafe platform path — download aborted"

_ZIP_TMP_EXT = ".zip.tmp"
_TMP_EXT = ".tmp"
# A download in one of these statuses has run to a terminal end — it is no
# longer active/queued/paused/extracting. The queue prune trims the oldest of
# these over the cap, and "Clear Completed" evicts all of them (#149). In normal
# flow only completed/failed actually LINGER: a cancel is an explicit discard and
# evicts its entry on the spot (#149 downloads-round), so "cancelled" stays in
# this classification defensively (a stray cancelled row is still prune/clearable)
# and as the transient status the terminal cancel FRAME still carries.
_TERMINAL_DOWNLOAD_STATUSES = ("completed", "failed", "cancelled")


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
    DownloadService needs at construction time. ``install_recorder`` is the
    shared writer of the ``rom_installs`` row and the shortcut bake behind it;
    ``target_gate`` is the pre-flight that refuses to write over content the
    plugin did not put there (ADR-0028).
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
    install_recorder: RomInstallRecorder
    target_gate: DownloadTargetGateFn
    m3u_support: SystemM3uSupportFn
    uow_factory: UnitOfWorkFactory
    # Deferred access to RomRemovalService.remove_rom — the two services form a
    # construction cycle, so the composition root binds it after both exist
    # (#1298 sibling supersede).
    rom_remover: RomRemoverProvider


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
        self._install_recorder = config.install_recorder
        self._target_gate = config.target_gate
        self._m3u_support = config.m3u_support
        self._uow_factory = config.uow_factory
        self._rom_remover = config.rom_remover

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
        """Remove oldest terminal items when over the limit.

        Keeps all non-terminal (queued/downloading/paused/extracting) items.
        Retains up to _DOWNLOAD_QUEUE_MAX_TERMINAL terminal items, removing the
        oldest (by insertion order) when the count exceeds the limit. In practice
        the retained terminals are completed/failed — a cancel evicts its own
        entry immediately (#149 downloads-round), so it rarely reaches the cap.
        """
        terminal_ids = [
            rid for rid, item in self._download_queue.items() if item.get("status") in _TERMINAL_DOWNLOAD_STATUSES
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

    async def start_download(
        self, rom_id, replace_existing=False, candidate_path=None, collision_choice=None, page_saw_candidate=False
    ):
        """Start a download, refusing when this game is already on disk.

        The user's answers to that refusal and the game page's own report ride on
        this callable rather than a second one, so every download — first attempt
        or replace — passes the same prologue: the ``already_downloading`` guard,
        the path-safety coercion, the occupancy gate, the supersede, the disk
        pre-flight. ``DownloadTargetGateFn`` owns what each of them means.
        """
        rom_id = int(rom_id)
        if rom_id in self._download_in_progress:
            return {"success": False, "reason": "already_downloading", "message": "Already downloading"}
        # The page's report rides with the user's answers because the gate reads
        # them together, but it is added apart from them to keep the difference
        # visible: the two above are choices the user made, this one is not.
        answer = {"candidate_path": candidate_path, "collision_choice": collision_choice}
        answer["page_saw_candidate"] = page_saw_candidate
        return await self._begin_download(rom_id, resume=False, replace_existing=bool(replace_existing), **answer)

    async def supersede_sibling_installs(self, rom_id: int) -> dict[str, Any] | None:
        """Strip any other installed version of ``rom_id``'s sibling group (#1298 T7).

        The single home of the supersede — ``_begin_download`` once its occupancy
        gate has passed (a refusal must not already have deleted another version),
        and adoption through ``SiblingSupersedeFn`` for the same reason: an adopted
        install is an install (ADR-0028). Neither caller may copy the selection
        rule below. The group's members are snapshotted in one short read UoW,
        closed before the removal seam runs — the removal opens its own UoW, which
        must not nest (ADR-0006). Each superseded install is removed through the
        canonical ``RomRemovalService.remove_rom`` (files + ``rom_installs`` row;
        saves untouched per ADR-0007) rather than duplicating its deletion logic.
        Every attempt is logged with both rom ids so a failure is attributable (S7).
        A removal that reports ``not_installed`` raced clean and is skipped; any
        other failure is returned so the caller aborts with that shape. A superseded
        sibling's *paused* queue entry is evicted so the queue stays coherent with
        disk (S1). Returns ``None`` when the group is clean / all removals succeeded.
        """
        sibling_ids = self._conflicting_sibling_install_ids(rom_id)
        if not sibling_ids:
            return None
        remove_rom = self._rom_remover()
        for sibling_id in sibling_ids:
            result = await remove_rom(sibling_id)
            if result.get("success"):
                self._logger.info(f"Superseding install of rom {sibling_id} (group of rom {rom_id})")
                self._evict_if_paused(sibling_id)
                continue
            if result.get("reason") == "not_installed":
                continue  # raced clean — nothing to supersede
            self._logger.error(
                f"Superseding install of rom {sibling_id} (group of rom {rom_id}) failed: {result.get('message')}"
            )
            return result
        return None

    def _evict_if_paused(self, rom_id: int) -> None:
        """Drop a superseded sibling's queue entry when it is paused (#1298 S1).

        A paused download whose install this supersede just removed is stale — its
        "paused" row (and partial ``.tmp``) would otherwise linger and could be
        resumed into a second installed version. Only a paused row is evicted; a
        live or terminal row for the sibling is left to its own lifecycle.
        """
        entry = self._download_queue.get(rom_id)
        if entry is not None and entry.get("status") == "paused":
            self.evict(rom_id)

    def _conflicting_sibling_install_ids(self, rom_id: int) -> list[int]:
        """Sibling members with a ``rom_installs`` row that this download supersedes.

        Read in one short UoW (closed before the removal seam runs). A member is
        superseded when it is installed and either unbound or bound to the *same*
        shortcut as ``rom_id`` — a member bound to a **different** shortcut is a
        grandfathered duplicate (ADR-0021 §5) with its own Steam entry and is
        never removed. A ROM with no sibling group key (solo / unbackfilled) has
        no siblings, so the list is empty.
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None or rom.sibling_group_key is None:
                return []
            x_app_id = rom.shortcut_app_id
            superseded: list[int] = []
            for member in uow.roms.iter_by_group_key(rom.sibling_group_key):
                if member.rom_id == rom_id:
                    continue
                if uow.rom_installs.get(member.rom_id) is None:
                    continue
                if member.shortcut_app_id is not None and member.shortcut_app_id != x_app_id:
                    continue  # grandfathered separate shortcut — never removed
                superseded.append(member.rom_id)
            return superseded

    async def _begin_download(self, rom_id, *, resume: bool, replace_existing=False, **answer):
        """Shared core of ``start_download`` and ``resume_download``.

        *answer* passes the adopt dialog's answers, and what the game page reported, through to the gate unread.
        Fetches ROM detail, resolves the platform path, then runs the three pre-flights **in this order**: the occupancy
        gate, the #1298 sibling supersede, the disk-space check. Only then are the queue entry, task, byte reservation
        and control token registered. On ``resume=True`` the disk pre-flight discounts the bytes already on the existing
        ``.tmp`` and ``_do_download`` appends rather than restarts. The ``already_downloading`` guard stays with
        ``start_download``; ``resume_download`` validates the paused entry before calling here.
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

        # Path building, the occupancy gate, directory creation and the disk
        # pre-flight can all raise (SD card unmounted → OSError; ``roms_path()``
        # returning None → TypeError in the join). Any raise across the three
        # blocks below must release the in-progress flag so the ROM isn't stuck
        # "Already downloading" until a plugin reload (#1048). The explicit
        # early-return guards inside still ``return`` (not raise) and discard the
        # flag themselves; a ``return`` does not trip the except.
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
                name = rom_detail.get("name", "")
                platform = rom_detail.get("platform_name", platform_slug)
                await self._emit("download_failed", failed_frame(rom_id, name, platform, _UNSAFE_PATH_MESSAGE))
                return {"success": False, "reason": "path_traversal", "message": _UNSAFE_PATH_MESSAGE}
            file_name = self._safe_local_file_name(rom_detail)
            file_size = rom_detail.get("fs_size_bytes", 0)
            target_path = os.path.join(roms_dir, file_name)

            # Refuse before a single byte moves when this game is already on disk
            # — at the path this download would claim, or beside it under another
            # name — and let the user decide what happens to it: adopt, replace,
            # or (where nothing can be adopted) accept a second copy (ADR-0028).
            # A multi-file ROM claims its extract directory, not the archive name.
            checked_path = target_path
            if is_multi_file_download(rom_detail):
                checked_path = os.path.join(roms_dir, self._resolve_safe_extract_dir_name(rom_detail))
            occupied = await self._target_gate(
                rom_detail, checked_path, replace=replace_existing, resume=resume, **answer
            )
            if occupied is not None:
                self._download_in_progress.discard(rom_id)
                return occupied
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to prepare download for ROM {rom_id}: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": _START_FAILED_MESSAGE}

        # At most one downloaded version per shortcut binding (#1298): strip a
        # sibling install bound to this shortcut (or unbound) before this one
        # lands — a grandfathered sibling with its own shortcut is exempt. A
        # removal failure aborts the download so the invariant stays honest.
        #
        # Ordering is load-bearing at both ends. AFTER the gate, because the
        # supersede deletes another version's files: refusing afterwards would
        # leave a user who then presses Cancel with one version uninstalled and
        # nothing in its place. BEFORE the disk pre-flight, because the removal
        # frees the space the replacement needs — checking first would reject a
        # same-size swap on a nearly full card. The in-progress claim is already
        # held (B1), so a second start_download during this await is rejected by
        # ``start_download``'s guard rather than racing past it.
        try:
            cleanup_failure = await self.supersede_sibling_installs(rom_id)
        except Exception:
            self._download_in_progress.discard(rom_id)
            raise
        if cleanup_failure is not None:
            self._download_in_progress.discard(rom_id)
            return cleanup_failure

        try:
            self._download_file_store.make_dirs(roms_dir)
            verdict = disk_space_verdict(
                file_size=file_size,
                free_space=self._download_file_store.disk_free(roms_dir),
                reserved_bytes=sum(self._reserved_bytes.values()),
                multi_file=is_multi_file_download(rom_detail),
                already_on_disk=self._partial_tmp_size(target_path, rom_detail) if resume else 0,
            )
            if not verdict.fits:
                self._download_in_progress.discard(rom_id)
                return {
                    "success": False,
                    "reason": "insufficient_space",
                    "message": f"Not enough disk space ({verdict.free_mb}MB free, need {verdict.needed_mb}MB)",
                }
        except Exception as e:
            self._download_in_progress.discard(rom_id)
            self._logger.error(f"Failed to prepare download for ROM {rom_id}: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": _START_FAILED_MESSAGE}

        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", platform_slug)
        # Carry the prior resumability verdict across a resume so the UI keeps
        # showing Pause before the resumed transfer's headers re-confirm it.
        resumable = bool(self._download_queue.get(rom_id, {}).get("resumable", False))

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
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": _START_FAILED_MESSAGE}

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
            # The user's replace answer, carried across a pause so a resumed
            # replace-download is not refused by the very file it is replacing.
            # Kept only where the answer is still LIVE: a single-file replace
            # deliberately deletes nothing (``os.replace`` is atomic), so the
            # original is still there and still the content the user was shown. A
            # multi-file replace already removed its directory, so the answer is
            # spent — anything at that path now is content the user has never
            # seen, and the gate must ask about it rather than delete it.
            "_replace_existing": replace_existing and not is_multi_file_download(rom_detail),
        }
        self._download_tasks[rom_id] = task
        # Reserve this download's required bytes so a concurrent sibling's
        # pre-flight sees the outstanding claim (released in ``_do_download``'s
        # ``finally``).
        self._reserved_bytes[rom_id] = verdict.needed_bytes
        self._control_tokens[rom_id] = control
        return {"success": True, "message": "Download started"}

    def task_for_rom(self, rom_id: int) -> asyncio.Task[None] | None:
        """Return the detached task whose lifetime owns this ROM's install write."""
        return self._download_tasks.get(int(rom_id))

    def _safe_local_file_name(self, rom_detail: dict[str, Any]) -> str:
        """The on-disk name for this ROM: the server's, coerced to one safe component.

        Strips any directory portion and rejects a degenerate result — ``..``
        would escape the platform directory and ``.`` or ``""`` would resolve to
        it — falling back to the synthetic ``rom_<id>`` identity. Both repairs
        are logged, because the name the user sees on disk then differs from the
        one the server states.
        """
        file_name, files_missing = resolve_local_file_name(rom_detail)
        if files_missing:
            self._logger.warning(
                f"has_nested_single_file=true but files list is empty; falling back to fs_name='{file_name}'"
            )
        safe_name, name_changed = coerce_safe_component(file_name, synthetic_rom_name(rom_detail))
        if name_changed:
            self._logger.warning(f"Sanitized fs_name from '{file_name}' to '{safe_name}'")
        return safe_name

    def _partial_tmp_size(self, target_path, rom_detail) -> int:
        """Bytes already on disk in the partial ``.tmp`` for *target_path*.

        Single-file ROMs stream to ``target_path + .tmp``; multi-file ROMs to
        ``target_path + .zip.tmp``. Returns 0 when no partial exists (the file
        store reports a missing path as size 0).
        """
        tmp_ext = _ZIP_TMP_EXT if is_multi_file_download(rom_detail) else _TMP_EXT
        return self._download_file_store.file_size(target_path + tmp_ext)

    def _resolve_safe_extract_dir_name(self, rom_detail: dict[str, Any]) -> str:
        """Resolve the sanitized base name for a multi-file ROM's extract dir.

        Names the directory after the ROM's identity via
        ``resolve_extract_dir_name`` (never ``files[0]``), then coerces the
        server-supplied value to a single safe path component
        (``coerce_safe_component``): a directory portion (``../evil``, an
        absolute path) is stripped to its basename, AND a degenerate result
        (``..``/``.``/empty/whitespace — which would resolve to the roms root
        or the platform dir and turn a later ``remove_tree`` into a
        library-wide delete) falls back to the synthetic ``rom_<id>`` identity.
        Mirrors the ``file_name`` guard in ``_begin_download``.
        """
        raw = resolve_extract_dir_name(rom_detail)
        safe, changed = coerce_safe_component(raw, synthetic_rom_name(rom_detail))
        if changed:
            self._logger.warning(f"Sanitized extract dir name from '{raw}' to '{safe}'")
        return safe

    def _post_download_multi_io(self, rom_id, rom_detail, target_path, file_name, system, extract_dir_name):
        """Sync helper for _do_download multi-file — extraction + renames in executor.

        *extract_dir_name* is the ROM-identity-derived, sanitized base name for
        the extract directory (``_resolve_safe_extract_dir_name``) — never a
        name derived from ``files[0]``. Returns ``(launch_file, error)``.
        ``error`` is a string when the RomM data fails the ``RomInstall``
        invariant — the extracted directory is removed and nothing is
        persisted — otherwise ``None``.
        """
        extract_dir = os.path.join(os.path.dirname(target_path), extract_dir_name)
        self._download_file_store.make_dirs(extract_dir)
        roms_base = self._retrodeck_paths.roms_path()
        tmp_zip = target_path + _ZIP_TMP_EXT
        # ZIP-slip protection: adapter validates members resolve within extract_dir
        # AND that extract_dir itself resolves within roms_base.
        rom_name = rom_detail.get("name", file_name)
        platform_name = rom_detail.get("platform_name", rom_detail.get("platform_slug", ""))
        extract_cb = self._make_extract_callback(rom_id, rom_name, platform_name, file_name)
        self._download_file_store.extract_zip(tmp_zip, extract_dir, roms_base, progress_callback=extract_cb)
        self._download_file_store.remove_file(tmp_zip)
        self._download_file_store.decode_url_encoded_names(extract_dir)
        # Heal a folder-boot disc dump whose PS3_DISC.SFB ships .txt-suffixed,
        # before launch detection reads the layout (ADR-0019 / #1212).
        self._maybe_heal_ps3_sfb_io(extract_dir)
        # Whether ES-DE lists .m3u for this system (gates both M3U generation and
        # launch-file selection so a RomM-bundled .m3u is ignored on Switch/Xbox).
        m3u_supported = self._m3u_support(system)
        # Auto-generate M3U if missing and multiple disc files exist
        self._maybe_generate_m3u_io(extract_dir, rom_detail, m3u_supported)
        # Detect launch file: prefer M3U > CUE > largest file
        launch_file = self._collect_and_detect_launch_file(extract_dir, m3u_supported)
        # ES-DE collapses a multi-file dir into one game entry only when the
        # dir is named after the launch file *including* the extension. The
        # launch file is only known after extraction (the M3U may be
        # auto-generated above), so the rename happens here, last of all the
        # filesystem work, so a later failure cleans up the renamed dir.
        extract_dir, launch_file = self._maybe_es_de_collapse_io(extract_dir, launch_file)

        return self._install_recorder.do_record_install(
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

        return self._install_recorder.do_record_install(
            rom_id=rom_id,
            rom_detail=rom_detail,
            file_path=target_path,
            rom_dir=None,
            system=system,
            cleanup=lambda: self._download_file_store.remove_file(target_path),
        )

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

    def _make_extract_callback(self, rom_id, rom_name, platform_name, file_name):
        """Build a throttled extraction-progress callback for a multi-file ROM.

        Mirrors ``_make_progress_callback`` but for the post-transfer ZIP
        extraction. ``last_emit`` starts at 0.0 so the FIRST tick emits
        immediately — the UI switches to the "extracting" phase promptly once
        the byte transfer finishes. Emits are then throttled to 0.5s and a
        human-readable log line to ~30s. Unlike the download callback this does
        NOT poll the cancel/pause token: extraction is not cancellable this
        iteration. Runs on the executor worker thread, so the queue mutation
        and emit-scheduling are marshaled to the loop thread via
        ``call_soon_threadsafe`` (#973).
        """
        last_emit = [0.0]  # mutable container for closure
        last_log = [0.0]

        def extract_callback(extracted, total):
            now = self._clock.monotonic()
            if now - last_log[0] >= 30.0:
                last_log[0] = now
                self._log_extract_progress(rom_name, extracted, total)
            if now - last_emit[0] < 0.5 and extracted < total:
                return
            last_emit[0] = now
            progress = extracted / total if total else 0
            self._loop.call_soon_threadsafe(
                self._apply_extract_progress,
                rom_id,
                rom_name,
                platform_name,
                file_name,
                progress,
                extracted,
                total,
            )

        return extract_callback

    def _log_extract_progress(self, rom_name, extracted, total):
        """Log a throttled one-line human-readable extraction summary (MB + %)."""
        mb_done = extracted / (1024 * 1024)
        mb_total = total / (1024 * 1024) if total else 0
        pct = (extracted / total * 100) if total else 0
        self._logger.info(f"Extract progress: {rom_name} — {mb_done:.1f}/{mb_total:.1f} MB ({pct:.0f}%)")

    def _apply_extract_progress(self, rom_id, rom_name, platform_name, file_name, progress, extracted, total):
        """Update the live queue entry and schedule an ``extracting`` ``download_progress`` emit.

        Runs on the loop thread (marshaled from the executor worker via
        ``call_soon_threadsafe``). Guarded by ``.get`` — if the entry was evicted
        between ticks we must not resurrect it or raise KeyError off-thread (#973).
        Mirrors ``_apply_download_progress`` but flips the entry to the
        "extracting" phase and reports ``resumable: False`` (extraction is never
        resumable).
        """
        entry = self._download_queue.get(rom_id)
        if entry is None:
            return  # evicted mid-extraction — do not resurrect or emit
        entry.update(
            {
                "status": "extracting",
                "progress": progress,
                "bytes_downloaded": extracted,
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
                    "status": "extracting",
                    "progress": progress,
                    "bytes_downloaded": extracted,
                    "total_bytes": total,
                    "resumable": False,
                },
            )
        )

    async def _finalize_download_complete(self, rom_id, rom_detail, final_path, rom_name, platform_name):
        """Mark the queue entry completed and emit ``download_complete``.

        Resolves the bound Steam ``shortcut_app_id`` for this rom_id (or ``None``
        when the ROM hasn't been synced yet) plus the ROM's full active core
        (resolved ``.so`` or ``None``) and the multi-disc launch path so the
        frontend confirm-sets launch options on the exact shortcut without a
        full-library scan to re-resolve rom_id→app_id, and the re-bake keeps the
        per-game/per-platform core AND the selected disc across uninstall →
        reinstall. Called from the normal success path and from the cancel handler
        when the install committed before the cancel landed (#1049).
        """
        entry = self._download_queue[rom_id]
        entry["status"] = "completed"
        entry["progress"] = 1.0
        app_id, launch_options = await self._loop.run_in_executor(
            None, self._install_recorder.do_resolve_launch_bake, rom_id, rom_detail, final_path
        )
        await self._emit(
            "download_complete",
            {
                "rom_id": rom_id,
                "rom_name": rom_name,
                "platform_name": platform_name,
                "file_path": final_path,
                "app_id": app_id,
                "launch_options": launch_options,
                "resumable": entry.get("resumable", False),
            },
        )
        # Record the freshly baked launch command as this ROM's applied state (the
        # value the frontend confirm-sets onto the shortcut), so the next sync
        # skips the now-correct shortcut instead of re-touching it (delta apply,
        # #1383). Only when the ROM has a bound shortcut — an unbound ROM has none
        # to record, and the next sync creates + records it. Second of the six
        # writer sites.
        if app_id is not None:
            await self._loop.run_in_executor(
                None, self._install_recorder.do_record_applied_launch_options, rom_id, launch_options
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
        # Name the extract dir from the ROM's identity, not files[0] (which may be
        # an arbitrary inner asset for a nested-single folder game). Computed once
        # here and threaded to both the extraction and the cleanup so the dir a
        # failure tears down matches the dir extraction created. Only multi-file
        # ROMs own a dedicated dir, so single-file skips the derivation.
        extract_dir_name = self._resolve_safe_extract_dir_name(rom_detail) if has_multiple else ""
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
                        None,
                        self._post_download_multi_io,
                        rom_id,
                        rom_detail,
                        target_path,
                        file_name,
                        system,
                        extract_dir_name,
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
                # completed/failed/cancelled, so "paused" is retained). Record the
                # target path (internal ``_``-prefixed key, stripped from the wire
                # by ``get_download_queue``) so a later cancel of this now-task-less
                # download can delete the partial without re-fetching rom detail.
                entry = self._download_queue[rom_id]
                entry["status"] = "paused"
                entry["_target_path"] = target_path
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
                self._cleanup_partial_download(target_path, has_multiple, extract_dir_name, final_path)
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
                # A cancel is an explicit discard — drop the entry so no
                # "cancelled" row lingers in the queue view or QAM summary (#149
                # downloads-round). The terminal frame above already told the
                # frontend; the store listener drops it there. Emitted BEFORE the
                # evict so the frame carries the entry's final progress values.
                self.evict(rom_id)
                self._logger.info(f"Download cancelled: {rom_name}")
            raise

        except Exception as e:
            self._download_queue[rom_id]["status"] = "failed"
            self._download_queue[rom_id]["error"] = str(e)
            self._cleanup_partial_download(target_path, has_multiple, extract_dir_name, final_path)
            self._logger.error(f"Download failed for {rom_name}: {e}")
            await self._emit("download_failed", failed_frame(rom_id, rom_name, platform_name, str(e)))

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

    def _maybe_heal_ps3_sfb_io(self, extract_dir: str) -> None:
        """Copy a folder-boot dump's ``PS3_DISC.SFB.txt`` back to ``PS3_DISC.SFB``.

        RPCS3 identifies a disc-dump folder by a ``PS3_DISC.SFB`` at the game
        root; some RomM dumps ship it renamed ``PS3_DISC.SFB.txt`` and never
        boot until it is restored (verified on-device, #1212). Only for a
        folder-boot layout (:func:`folder_boot_layout_root` locates the game
        root) and only the exact ``.txt`` → real-name case: when the ``.txt``
        exists and the real name does not, a copy is written (the original is
        kept). Any other shape — real SFB already present, no ``.txt``, no
        folder-boot layout — is left untouched. Scoped to this one known dump
        quirk, not a general renamer.
        """
        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        root = folder_boot_layout_root([path for path, _size in all_files])
        if root is None:
            return
        sfb = os.path.join(root, "PS3_DISC.SFB")
        sfb_txt = os.path.join(root, "PS3_DISC.SFB.txt")
        if self._download_file_store.exists(sfb_txt) and not self._download_file_store.exists(sfb):
            self._download_file_store.copy_file(sfb_txt, sfb)
            self._logger.info("healed PS3_DISC.SFB from .txt-suffixed dump: %s", sfb)

    def _maybe_generate_m3u_io(self, extract_dir: str, rom_detail: dict[str, Any], m3u_supported: bool) -> None:
        """Auto-generate a game-named M3U playlist when one is warranted (see ``needs_m3u``).

        Writes ``<fs_name_no_ext>.m3u`` when the platform supports ``.m3u``, no
        M3U already exists, and the disc files warrant one: multi-disc ROMs (any
        of cue/chd/iso) for disc switching, or a single-disc bin/cue ROM so the
        extract dir collapses to a game-named entry in ES-DE. When the platform
        does not support ``.m3u`` (per ES-DE's ``es_systems.xml``), no playlist
        is generated.
        """
        if not m3u_supported:
            return

        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        # A folder-boot dump (PS3 — …/PS3_GAME/USRDIR/EBOOT.BIN) launches the game
        # directory directly (ADR-0019), never a playlist — and its many payload
        # files must never be misread as "discs". Suppress the M3U outright even
        # though ES-DE lists .m3u for the platform. (#1212)
        if folder_boot_layout_root([path for path, _size in all_files]) is not None:
            return
        # Check if an M3U already exists (search recursively)
        if any(path.lower().endswith(".m3u") for path, _size in all_files):
            return

        # Collect disc files (the DISC_IMAGE_EXTENSIONS set; search recursively).
        disc_suffixes = tuple(DISC_IMAGE_EXTENSIONS)
        disc_files = [
            os.path.relpath(path, extract_dir) for path, _size in all_files if path.lower().endswith(disc_suffixes)
        ]

        if not needs_m3u(disc_files, m3u_supported):
            return

        rom_name = rom_detail.get("fs_name_no_ext", rom_detail.get("name", "playlist"))
        m3u_path = os.path.join(extract_dir, f"{rom_name}.m3u")
        self._download_file_store.write_text_atomic(m3u_path, build_m3u_content(disc_files))
        self._logger.info(f"Auto-generated M3U playlist: {m3u_path}")

    def _collect_and_detect_launch_file(self, extract_dir: str, m3u_supported: bool) -> str:
        """Find the best launch file in an extracted multi-file ROM directory.

        *m3u_supported* gates whether a ``.m3u`` (including a RomM-bundled one)
        may be chosen as the launch file — see ``detect_launch_file``.
        """
        all_files = self._download_file_store.scan_files_with_sizes(extract_dir)
        result = detect_launch_file(all_files, m3u_supported)
        return result if result is not None else extract_dir

    def _cleanup_partial_download(self, target_path, has_multiple, extract_dir_name, final_path=None):
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

        *extract_dir_name* is the same ROM-identity-derived base name extraction
        created the staging dir under (``_resolve_safe_extract_dir_name``), so
        cleanup targets exactly that dir rather than re-deriving it from a stale
        source (unused for single-file ROMs, which own no dir).
        """
        self._remove_partial_tmp_files(target_path)
        if has_multiple:
            staging_dir = os.path.join(os.path.dirname(target_path), extract_dir_name)
            dirs_to_remove = {staging_dir}
            if final_path:
                dirs_to_remove.add(os.path.dirname(final_path))
            for extract_dir in dirs_to_remove:
                try:
                    self._download_file_store.remove_tree(extract_dir)
                except Exception as e:
                    self._logger.warning(f"Cleanup failed for directory {extract_dir}: {e}")

    def _remove_partial_tmp_files(self, target_path: str) -> None:
        """Remove the ``.zip.tmp`` and ``.tmp`` partials for *target_path*.

        Each removal is independent so one failure doesn't block the other, and a
        missing partial is not an error. The bare ``target_path`` is NEVER touched
        — only the transient transfer artifacts. Shared by
        ``_cleanup_partial_download`` (running-cancel / failure) and the
        paused-cancel path, which has no live task in scope to name the extension.
        """
        for path in (target_path + _ZIP_TMP_EXT, target_path + _TMP_EXT):
            try:
                self._download_file_store.remove_file(path)
            except Exception as e:
                self._logger.warning(f"Cleanup failed for {path}: {e}")

    def cancel_download(self, rom_id):
        """Cancel a download, whether it is running or paused.

        A running download has a live task: flip its cooperative-cancel token
        (stops the executor transfer thread, not just the asyncio wrapper — #144)
        and cancel the task; ``_do_download``'s terminal handler deletes the
        partial, emits the terminal ``cancelled`` frame, and evicts the entry.

        A **paused** download has NO task — the pause already cancelled it,
        leaving the entry "paused" and the partial ``.tmp`` kept for resume. A
        plain task lookup then found nothing and silently no-op'd (the
        downloads-round finding). Handle it directly here:
        ``_cancel_paused_download`` deletes the partial, emits the same terminal
        ``cancelled`` frame, and evicts the entry.

        A cancel that can act on neither (no task AND no paused entry) keeps the
        canonical failure shape so the frontend can surface it.
        """
        rom_id = int(rom_id)
        task = self._download_tasks.get(rom_id)
        if task:
            token = self._control_tokens.get(rom_id)
            if token is not None:
                token.cancelled = True  # stop the executor transfer thread, not just the asyncio wrapper (#144)
            task.cancel()
            return {"success": True, "message": "Download cancelled"}
        entry = self._download_queue.get(rom_id)
        if entry is not None and entry.get("status") == "paused":
            self._cancel_paused_download(rom_id, entry)
            return {"success": True, "message": "Download cancelled"}
        return {"success": False, "reason": "no_active_download", "message": "No active download for this ROM"}

    def _cancel_paused_download(self, rom_id: int, entry: dict[str, Any]) -> None:
        """Cancel a paused (task-less) download: delete its partial, notify, evict.

        Does the cleanup the running-cancel terminal branch would have, minus the
        task: removes the partial ``.tmp``/``.zip.tmp`` (via the ``_target_path``
        recorded when the entry paused — skipped if absent, the startup sweep
        still reaps it; a paused transfer is mid-download so it owns no extract
        dir), emits the same terminal ``download_progress`` ``cancelled`` frame
        the running-cancel emits (so the button resets and the store drops the
        row), then evicts the entry — a cancel is an explicit discard that leaves
        no residue (#149 downloads-round).
        """
        target_path = entry.get("_target_path")
        if target_path:
            self._remove_partial_tmp_files(target_path)
        # cancel_download runs on the loop thread; schedule the terminal frame the
        # same way the progress callbacks do (fire-and-forget on the loop).
        self._loop.create_task(self._emit("download_progress", cancelled_frame(rom_id, entry)))
        self.evict(rom_id)

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
        ``not_paused`` failure shape. A switch may have moved the group's shortcut
        binding to a sibling while this download was paused (#1298 S1): if another
        member now owns the shortcut, this target is stale — the resume is refused
        (``superseded``) and its queue entry dropped rather than re-downloading a
        version the picker has already moved away from. Otherwise the transfer
        appends onto the existing bytes instead of restarting (when the server
        honoured the original ``Range`` probe).

        The occupancy gate runs again, against the **final** path rather than the
        partial ``.tmp``. A single-file replace's final path still holds the file
        the user chose to replace, so the entry's stored answer goes back to the
        gate — without it the resume is refused by the very file it is replacing,
        every time. Anything else faces a free path, or a refusal if content
        appeared there while it sat paused — but never the candidate search,
        which is skipped on a resume so a file the user already declined cannot
        refuse the transfer they started.
        """
        rom_id = int(rom_id)
        entry = self._download_queue.get(rom_id)
        if entry is None or entry.get("status") != "paused":
            return {"success": False, "reason": "not_paused", "message": "No paused download for this ROM"}
        if self._resume_target_superseded(rom_id):
            self.evict(rom_id)
            return {"success": False, "reason": "superseded", "message": "Another version is now active"}
        return await self._begin_download(rom_id, resume=True, replace_existing=bool(entry.get("_replace_existing")))

    def _resume_target_superseded(self, rom_id: int) -> bool:
        """True when a sibling now owns the group's shortcut, stranding this resume.

        Reads the target's sibling group in one short UoW. A solo ROM (no group
        key), a target that is itself a bound member, or a group with no bound
        member at all is never superseded — those resume normally. Only a group
        whose shortcut binding has moved to a DIFFERENT member (a version switch
        landed while this download was paused) strands this target: resuming it
        would re-create a second installed version the supersede predicate can't
        reach (it protects installs bound to a different app_id — #1298 S1).
        """
        with self._uow_factory() as uow:
            rom = uow.roms.get(rom_id)
            if rom is None or rom.sibling_group_key is None:
                return False
            if rom.shortcut_app_id is not None:
                return False  # target is (a) bound member — resume normally
            return any(
                member.rom_id != rom_id and member.shortcut_app_id is not None
                for member in uow.roms.iter_by_group_key(rom.sibling_group_key)
            )

    def get_download_queue(self):
        # Drop internal ``_``-prefixed keys (e.g. ``_target_path``, kept on a
        # paused entry only for the paused-cancel cleanup) so they never cross
        # the wire to the frontend ``DownloadItem`` shape.
        return {
            "downloads": [
                {k: v for k, v in entry.items() if not k.startswith("_")} for entry in self._download_queue.values()
            ]
        }

    def clear_completed_downloads(self) -> dict[str, Any]:
        """Evict every terminal (completed/failed/cancelled) entry from the queue.

        The frontend "Clear Completed" action calls this so a dismissed download
        stays cleared across a QAM reopen or menu switch (#149): the backend queue
        is the source ``get_download_queue`` re-seeds the frontend from on every
        mount, so a purely-local hide reappeared on the next reopen. Active,
        queued, paused, and extracting entries are left untouched — only terminal
        ones are removed. In normal flow those are completed/failed only, since a
        cancel already evicted its own entry (#149 downloads-round); a stray
        cancelled row would still clear here. Idempotent: an empty queue or one
        with no terminal entries clears nothing and reports ``cleared: 0``. A ROM
        re-downloaded after being cleared re-enters the queue via ``start_download``.
        """
        terminal_ids = [
            rid for rid, item in self._download_queue.items() if item.get("status") in _TERMINAL_DOWNLOAD_STATUSES
        ]
        for rid in terminal_ids:
            del self._download_queue[rid]
        return {"success": True, "cleared": len(terminal_ids)}

    def active_download_rom_ids(self) -> set[int]:
        """Snapshot the rom ids with an in-flight or queued download (#1298 F1).

        VersionSwitchService consults this to refuse a switch while any member of
        the target's sibling group is actively downloading (the user cancels
        first). A *paused* download is not in this set — its task's ``finally``
        already released the in-progress claim — so a switch is allowed while a
        sibling is paused, and a later resume of a superseded target is refused by
        ``resume_download`` instead. Returns a copy so the caller cannot mutate the
        live control set.
        """
        return set(self._download_in_progress)

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
            "launchable": install.launchable,
        }
        return entry

    # ── DownloadQueueCleanup Protocol ──────────────────────────────

    def evict(self, rom_id: int) -> None:
        """Remove the queue entry for *rom_id* if present. Idempotent."""
        self._download_queue.pop(int(rom_id), None)

    def clear(self) -> None:
        """Remove all entries from the download queue."""
        self._download_queue.clear()
