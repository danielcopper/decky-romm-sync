from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from models.saves import SaveConflict

from domain.emulator_tag import build_emulator_tag
from domain.save_path import sanitize_save_filename
from domain.save_state import FileSyncState, RomSaveState
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
    compute_sync_action,
)
from lib.errors import RommApiError, classify_error
from lib.iso_time import parse_iso_to_epoch
from services.saves._helpers import _local_save_target
from services.saves._messages import DEVICE_NOT_REGISTERED, SAVE_SYNC_DISABLED

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

    from services.protocols import (
        Clock,
        CoreResolverFn,
        DebugLogger,
        RetryStrategy,
        RommSyncApi,
        SaveFileAdapter,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService


@dataclass(frozen=True)
class MatrixOutcome:
    """One newest-wins matrix evaluation, ready for sync dispatch or status rendering.

    Yielded by :meth:`SyncEngine.iter_matrix_outcomes` for both consumers
    (sync I/O dispatch, status DTO building). All fields are read-only —
    the iterator runs pure compute and consumers drive their own side
    effects.
    """

    filename: str
    action: SyncAction
    local_path: str | None
    local_hash: str | None
    local_mtime_iso: str | None
    local_size: int | None
    file_state: FileSyncState
    server_candidates: list[dict]


class SyncEngine:
    """Newest-wins matrix executor, sync orchestration callables, and rom-level lock dispatch."""

    def __init__(
        self,
        *,
        state: dict,
        state_svc: StateService,
        rom_info: RomInfoService,
        romm_api: RommSyncApi,
        retry: RetryStrategy,
        loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        clock: Clock,
        save_file: SaveFileAdapter,
        log_debug: DebugLogger,
        get_active_core: CoreResolverFn,
        plugin_version: str,
        detect_sort_change: Callable[[], None] | None,
        is_retrodeck_migration_pending: Callable[[], bool] | None,
    ) -> None:
        self._state = state
        self._state_svc = state_svc
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._loop = loop
        self._logger = logger
        self._clock = clock
        self._save_file = save_file
        self._log_debug = log_debug
        self._get_active_core = get_active_core
        self._plugin_version = plugin_version
        self._detect_sort_change = detect_sort_change
        self._is_retrodeck_migration_pending = is_retrodeck_migration_pending
        # Per-rom lock dict — serializes concurrent sync operations on the
        # same rom_id (pre_launch_sync, post_exit_sync, manual sync, resolve).
        self._rom_sync_locks: dict[int, asyncio.Lock] = {}

    def _rom_lock(self, rom_id: int) -> asyncio.Lock:
        """Return the lock for this rom_id, creating it lazily."""
        if rom_id not in self._rom_sync_locks:
            self._rom_sync_locks[rom_id] = asyncio.Lock()
        return self._rom_sync_locks[rom_id]

    # ------------------------------------------------------------------
    # Server Save Hash Helper
    # ------------------------------------------------------------------

    def _get_server_save_hash(self, server_save: dict) -> str | None:
        """Download a server save to temp and compute its MD5 hash.

        Used for slow-path conflict detection when no content_hash is available.
        Returns hash string or None on non-retryable error.
        Raises on retryable errors so the caller can retry.
        """
        save_id = server_save.get("id")
        if not save_id:
            return None
        tmp_path: str | None = None
        try:
            tmp_path = self._save_file.make_temp_path(suffix=".tmp")
            self._romm_api.download_save(save_id, tmp_path)
            return self._save_file.checksum_md5(tmp_path)
        except Exception as e:
            self._log_debug(f"Failed to hash server save {save_id}: {e}")
            if self._retry.is_retryable(e):
                raise
            return None
        finally:
            if tmp_path:
                with contextlib.suppress(OSError):
                    self._save_file.remove(tmp_path)

    def _update_file_sync_state(
        self,
        rom_id_str: str,
        filename: str,
        server_response: dict,
        local_path: str,
        system: str,
        *,
        emulator_tag: str | None = None,
        core_so: str | None = None,
    ) -> None:
        """Update per-file sync tracking after a successful sync operation."""
        saves = self._state_svc.state.saves
        if rom_id_str not in saves:
            settings_default_slot = self._state_svc.state.settings.default_slot or "default"
            saves[rom_id_str] = RomSaveState(
                emulator=emulator_tag or "retroarch",
                system=system,
                last_synced_core=core_so,
                active_slot=settings_default_slot,
            )
        save_entry = saves[rom_id_str]
        if emulator_tag is not None:
            save_entry.emulator = emulator_tag
        if core_so is not None:
            save_entry.last_synced_core = core_so

        now = self._clock.now().isoformat()
        local_exists = self._save_file.is_file(local_path)
        local_hash = self._save_file.checksum_md5(local_path) if local_exists else ""

        save_entry.files[filename] = FileSyncState(
            last_sync_hash=local_hash,
            last_sync_at=now,
            last_sync_server_updated_at=server_response.get("updated_at", now) or now,
            last_sync_server_save_id=server_response.get("id"),
            last_sync_server_size=server_response.get("file_size_bytes"),
            last_sync_local_mtime=self._save_file.get_mtime(local_path) if local_exists else None,
            last_sync_local_size=self._save_file.get_size(local_path) if local_exists else None,
            tracked_save_id=server_response.get("id"),
        )

    # ------------------------------------------------------------------
    # Sync Helpers
    # ------------------------------------------------------------------

    def _do_download_save(self, server_save: dict, saves_dir: str, filename: str, rom_id_str: str, system: str) -> None:
        """Download a save file from server. Backs up existing local file first."""
        local_path = os.path.join(saves_dir, filename)
        self._save_file.make_dirs(saves_dir)
        tmp_path = local_path + ".tmp"

        device_id = self._state_svc.get_server_device_id()
        self._retry.with_retry(
            lambda: self._romm_api.download_save_content(
                server_save["id"],
                tmp_path,
                device_id=device_id,
                optimistic=True,
            ),
        )

        # Backup existing local save before overwriting
        if self._save_file.is_file(local_path):
            backup_dir = os.path.join(saves_dir, ".romm-backup")
            self._save_file.make_dirs(backup_dir)
            ts = self._clock.now().strftime("%Y%m%d_%H%M%S")
            name, ext = os.path.splitext(filename)
            self._save_file.rename(local_path, os.path.join(backup_dir, f"{name}_{ts}{ext}"))

        self._save_file.rename(tmp_path, local_path)
        self._update_file_sync_state(rom_id_str, filename, server_save, local_path, system)
        self._log_debug(f"Downloaded save: {filename} for rom {rom_id_str}")

    def _resolve_upload_slot(self, rom_id_str: str, device_id: str | None) -> str | None:
        """The slot field to send with an upload; ``None`` when device sync is off."""
        if not device_id:
            return None
        game_state = self._state_svc.state.saves.get(rom_id_str)
        if game_state and game_state.active_slot is not None:
            return game_state.active_slot
        return "default"

    def _promote_local_slot_to_server(self, rom_id_str: str, slot: str) -> None:
        """Mark *slot* as having a server copy after a successful upload of a local-only slot."""
        rom_state = self._state_svc.state.saves.get(rom_id_str)
        if not rom_state:
            return
        slot_entry = rom_state.slots.get(slot)
        if slot_entry and slot_entry.get("source") == "local":
            slot_entry["source"] = "server"
            slot_entry["count"] = 1
            self._state_svc.save_state()

    def _confirm_upload_sync(self, upload_id: int | None, device_id: str | None) -> None:
        """Ack the uploaded save on the server's DeviceSaveSync row (non-fatal)."""
        # RomM's upload endpoint updates updated_at but NOT last_synced_at,
        # so is_current would be False on the next list_saves without this.
        if not device_id or not upload_id:
            return
        try:
            self._romm_api.confirm_download(upload_id, device_id)
        except Exception:
            self._log_debug(f"confirm_download after upload failed for save {upload_id} (non-fatal)")

    def _do_upload_save(
        self,
        rom_id: int,
        file_path: str,
        filename: str,
        rom_id_str: str,
        system: str,
        server_save: dict | None = None,
    ) -> dict:
        """Upload a local save file to server."""
        save_id = server_save.get("id") if server_save else None

        # Resolve active core for emulator tag
        installed = self._state["installed_roms"].get(rom_id_str, {})
        rom_filename = os.path.basename(installed.get("file_path", "")) or None
        core_so, _label = self._get_active_core(system, rom_filename)
        emulator = build_emulator_tag(core_so)

        # v4.7: pass device_id and slot
        device_id = self._state_svc.get_server_device_id()
        slot = self._resolve_upload_slot(rom_id_str, device_id)

        is_post = save_id is None
        result = self._retry.with_retry(
            lambda: self._romm_api.upload_save(
                int(rom_id), file_path, emulator, save_id, device_id=device_id, slot=slot
            )
        )

        self._update_file_sync_state(
            rom_id_str, filename, result, file_path, system, emulator_tag=emulator, core_so=core_so
        )

        if is_post:
            self._record_own_upload(rom_id_str, result.get("id"))

        if slot:
            self._promote_local_slot_to_server(rom_id_str, slot)

        self._confirm_upload_sync(result.get("id"), device_id)

        self._log_debug(f"Uploaded save: {filename} for rom {rom_id_str} (emulator={emulator})")
        return result

    def _record_own_upload(self, rom_id_str: str, new_id: int | None) -> None:
        """Track a save_id we POSTed ourselves for uploader attribution.

        POST = brand-new save; PUT updates an existing tracked save without
        changing ownership. Assumes POST is not upsert-by-filename on the
        server — if RomM ever changes that, revisit this tracker.
        """
        if new_id is None:
            return
        rom_state = self._state_svc.ensure_rom_state(rom_id_str)
        if rom_state.own_upload_ids is None:
            rom_state.own_upload_ids = []
        if new_id in rom_state.own_upload_ids:
            return
        rom_state.own_upload_ids.append(new_id)
        self._state_svc.save_state()

    def _handle_unexpected_error(
        self,
        e: Exception,
        filename: str,
        saves_dir: str,
        errors: list[str],
    ) -> None:
        """Handle an unexpected exception by recording an error and cleaning up temp files."""
        _code, _msg = classify_error(e)
        errors.append(f"{filename}: {_msg}")
        tmp = os.path.join(saves_dir, filename + ".tmp")
        with contextlib.suppress(OSError):
            self._save_file.remove(tmp)

    @staticmethod
    def _filter_server_saves_to_slot(server_saves: list[dict], active_slot: str | None) -> list[dict]:
        """Filter server saves to the active slot.

        Saves with ``slot=None`` (legacy/no-slot) are accepted under any active
        slot; in legacy mode (no active slot) we only keep saves without a slot.
        """
        if active_slot:
            return [ss for ss in server_saves if ss.get("slot") == active_slot or ss.get("slot") is None]
        return [ss for ss in server_saves if not ss.get("slot")]

    def _build_local_input(self, local_path: str, filename: str) -> dict:
        """Build the dict shape consumed by ``compute_sync_action``."""
        exists = self._save_file.is_file(local_path)
        return {
            "filename": filename,
            "path": local_path,
            "size": self._save_file.get_size(local_path) if exists else None,
            "mtime": self._save_file.get_mtime(local_path) if exists else None,
        }

    def _build_sync_conflict_entry(
        self,
        rom_id: int,
        filename: str,
        server: dict,
        local_path: str | None,
        local_hash: str | None,
    ) -> dict:
        """Build a Phase-2 ``sync_conflict`` descriptor for the frontend."""
        local_mtime = None
        local_size = None
        if local_path and self._save_file.is_file(local_path):
            local_mtime = datetime.fromtimestamp(self._save_file.get_mtime(local_path), tz=UTC).isoformat()
            local_size = self._save_file.get_size(local_path)
        return {
            "type": "sync_conflict",
            "rom_id": rom_id,
            "filename": filename,
            "server_save_id": server.get("id"),
            "server_updated_at": server.get("updated_at", ""),
            "server_size": server.get("file_size_bytes"),
            "local_path": local_path,
            "local_hash": local_hash,
            "local_mtime": local_mtime,
            "local_size": local_size,
            "created_at": self._clock.now().isoformat(),
        }

    def _dispatch_skip(
        self,
        action: Skip,
        *,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local_hash: str | None,
    ) -> None:
        if action.adopt_baseline and local_hash is not None:
            # State-only mutation: write the current local_hash as the baseline
            # so future runs can detect drift. No I/O, no synced count.
            self._log_debug(f"_sync_rom_saves({rom_id}): skip + adopt_baseline {filename} ({action.reason})")
            self._adopt_baseline_hash(rom_id_str, filename, local_hash)
        else:
            self._log_debug(f"_sync_rom_saves({rom_id}): skip {filename} ({action.reason})")

    def _dispatch_upload(
        self,
        action: Upload,
        *,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local_path: str | None,
        system: str,
        server_saves: list[dict],
        errors: list[str],
    ) -> bool:
        """Execute an ``Upload`` action. Returns True iff the upload was issued."""
        if local_path is None:
            errors.append(f"{filename}: upload requested but no local file")
            return False
        if action.target_save_id is None:
            # POST path: brand-new save in slot.
            self._do_upload_save(rom_id, local_path, filename, rom_id_str, system, None)
            return True
        # PUT path: re-upload to update the tracked save (local diverged while
        # is_current=true).
        server_save = next((s for s in server_saves if s.get("id") == action.target_save_id), None)
        if server_save is None:
            # Picked save vanished between read and dispatch — best-effort.
            self._log_debug(
                f"_dispatch_sync_action: target_save_id={action.target_save_id} not in server_saves; skipping",
            )
            return False
        self._do_upload_save(rom_id, local_path, filename, rom_id_str, system, server_save)
        return True

    def _dispatch_sync_action(
        self,
        action: object,
        *,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        local_path: str | None,
        local_hash: str | None,
        saves_dir: str,
        system: str,
        server_saves: list[dict],
        errors: list[str],
        conflicts: list[SaveConflict | dict],
    ) -> bool:
        """Execute one ``SyncAction`` outcome. Returns True if a transfer happened.

        Centralises the I/O dispatch so ``_sync_rom_saves`` stays declarative.
        Errors are caught and pushed onto ``errors`` so a single failure can't
        abort the whole rom-level sync.
        """
        try:
            if isinstance(action, Skip):
                self._dispatch_skip(
                    action,
                    rom_id=rom_id,
                    rom_id_str=rom_id_str,
                    filename=filename,
                    local_hash=local_hash,
                )
                return False
            if isinstance(action, Upload):
                return self._dispatch_upload(
                    action,
                    rom_id=rom_id,
                    rom_id_str=rom_id_str,
                    filename=filename,
                    local_path=local_path,
                    system=system,
                    server_saves=server_saves,
                    errors=errors,
                )
            if isinstance(action, Download):
                self._do_download_save(action.server_save, saves_dir, filename, rom_id_str, system)
                return True
            if isinstance(action, Conflict):
                conflicts.append(
                    self._build_sync_conflict_entry(rom_id, filename, action.server_save, local_path, local_hash)
                )
                return False
        except RommApiError as e:
            _code, _msg = classify_error(e)
            errors.append(f"{filename}: {_msg}")
        except Exception as e:
            self._handle_unexpected_error(e, filename, saves_dir, errors)
        return False

    def _adopt_baseline_hash(self, rom_id_str: str, filename: str, local_hash: str) -> None:
        """Persist ``local_hash`` as the file's ``last_sync_hash`` baseline.

        Used by Skip(adopt_baseline=True) — the algorithm has detected that
        we've observed an is_current=true situation with local content but no
        baseline yet. Recording the baseline lets subsequent runs detect
        offline-edit drift. State mutation only, no I/O.
        """
        rom_state = self._state_svc.ensure_rom_state(rom_id_str)
        file_state = rom_state.files.setdefault(filename, FileSyncState())
        file_state.last_sync_hash = local_hash

    def iter_matrix_outcomes(
        self,
        rom_id: int,
        server_in_slot: list[dict],
        *,
        info: dict,
    ) -> Iterator[MatrixOutcome]:
        """Yield one :class:`MatrixOutcome` per save file in the ROM's active slot.

        Walks the local saves directory + server-only canonical targets,
        runs ``compute_sync_action`` against the per-filename inputs, and
        emits :class:`MatrixOutcome` records ready for sync dispatch or
        status rendering. Pure compute — no I/O writes, no state mutation.
        Consumers drive their own side effects from the yielded outcomes.
        """
        rom_id_str = str(int(rom_id))
        rom_name = info["rom_name"]

        save_state = self._state_svc.state.saves.get(rom_id_str)
        files_state: dict[str, FileSyncState] = save_state.files if save_state else {}
        device_id = self._state_svc.get_server_device_id() or ""

        local_files = self._rom_info.find_save_files(rom_id)

        handled_filenames: set[str] = set()
        for lf in local_files:
            filename = lf["filename"]
            local_path = lf["path"]
            handled_filenames.add(filename)
            local_exists = self._save_file.is_file(local_path)
            local_hash = self._save_file.checksum_md5(local_path) if local_exists else None
            file_state = files_state.get(filename, FileSyncState())
            local_mtime_iso = (
                datetime.fromtimestamp(self._save_file.get_mtime(local_path), tz=UTC).isoformat()
                if local_exists
                else None
            )
            local_size = self._save_file.get_size(local_path) if local_exists else None
            action = compute_sync_action(
                local_file=self._build_local_input(local_path, filename),
                server_saves_in_slot=server_in_slot,
                files_state=file_state.to_dict(),
                device_id=device_id,
                local_hash=local_hash,
            )
            yield MatrixOutcome(
                filename=filename,
                action=action,
                local_path=local_path,
                local_hash=local_hash,
                local_mtime_iso=local_mtime_iso,
                local_size=local_size,
                file_state=file_state,
                server_candidates=server_in_slot,
            )

        # Group server saves by canonical local target filename. Server-only
        # groups (no local file) get matrix-evaluated against their own group;
        # compute_sync_action picks newest-in-group internally.
        server_only_groups: dict[str, list[dict]] = {}
        for ss in server_in_slot:
            target = _local_save_target(ss, rom_name)
            if target in handled_filenames:
                continue
            server_only_groups.setdefault(target, []).append(ss)

        for target_filename, group in server_only_groups.items():
            file_state = files_state.get(target_filename, FileSyncState())
            action = compute_sync_action(
                local_file=None,
                server_saves_in_slot=group,
                files_state=file_state.to_dict(),
                device_id=device_id,
                local_hash=None,
            )
            yield MatrixOutcome(
                filename=target_filename,
                action=action,
                local_path=None,
                local_hash=None,
                local_mtime_iso=None,
                local_size=None,
                file_state=file_state,
                server_candidates=group,
            )

    def _sync_rom_saves(self, rom_id: int) -> tuple[int, list[str], list[SaveConflict | dict]]:
        """Sync saves for a single ROM.

        Drives :meth:`iter_matrix_outcomes` and dispatches each emitted
        outcome through :meth:`_dispatch_sync_action`. Returns
        ``(synced_count, errors_list, conflicts_list)``.
        """
        t_total = self._clock.time()
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            self._log_debug(f"_sync_rom_saves({rom_id}): no save info, skipping")
            return 0, [], []
        system = info["system"]
        saves_dir = info["saves_dir"]

        t0 = self._clock.time()
        try:
            device_id = self._state_svc.get_server_device_id()
            server_saves = self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id))
        except Exception as e:
            self._logger.error(f"_sync_rom_saves({rom_id}): failed to list saves: {e}")
            _code, _msg = classify_error(e)
            return 0, [f"Failed to fetch saves: {_msg}"], []
        self._log_debug(f"[TIMING] _sync_rom_saves({rom_id}): list_saves {self._clock.time() - t0:.3f}s")

        save_state = self._state_svc.state.saves.get(rom_id_str)
        active_slot = save_state.active_slot if save_state else None
        server_in_slot = self._filter_server_saves_to_slot(server_saves, active_slot)

        self._log_debug(
            f"_sync_rom_saves({rom_id}): system={system}, rom_name={info['rom_name']}, "
            f"server_saves={len(server_saves)}, saves_dir={saves_dir}"
        )

        errors: list[str] = []
        conflicts: list[SaveConflict | dict] = []
        synced = 0

        pending_migration = self._rom_info.is_save_sort_changed()
        for outcome in self.iter_matrix_outcomes(rom_id, server_in_slot, info=info):
            origin = "local" if outcome.local_path is not None else "server-only"
            self._log_debug(
                f"_sync_rom_saves({rom_id}): {origin} {outcome.filename} -> {type(outcome.action).__name__}"
            )
            if outcome.local_path is None and pending_migration:
                self._log_debug(
                    f"_sync_rom_saves({rom_id}): skipping server_only {outcome.filename} — migration pending"
                )
                continue
            if self._dispatch_sync_action(
                outcome.action,
                rom_id=rom_id,
                rom_id_str=rom_id_str,
                filename=outcome.filename,
                local_path=outcome.local_path,
                local_hash=outcome.local_hash,
                saves_dir=saves_dir,
                system=system,
                server_saves=outcome.server_candidates,
                errors=errors,
                conflicts=conflicts,
            ):
                synced += 1

        # Record when this sync check ran (regardless of whether files transferred)
        save_entry = self._state_svc.ensure_rom_state(rom_id_str)
        save_entry.last_sync_check_at = self._clock.now().isoformat()

        self._log_debug(
            f"[TIMING] _sync_rom_saves({rom_id}): TOTAL {self._clock.time() - t_total:.3f}s"
            f" synced={synced} errors={len(errors)}"
        )
        return synced, errors, conflicts

    # ------------------------------------------------------------------
    # Device registration — entrypoint for every sync flow that needs
    # ``device_id``. Kept on SyncEngine because pre_launch_sync,
    # post_exit_sync, sync_rom_saves, and sync_all_saves all fall back
    # to this when ``device_id`` is missing; co-locating the fallback
    # with its callers avoids a constructor callback.
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "device_id": "", "device_name": "", "disabled": True}

        # Probe the RomM version when it has not been observed yet. Device
        # registration is the entrypoint reached from background launchers
        # that never call test_connection first, so the version on the API
        # adapter would otherwise stay None and version-gated server-side
        # features couldn't be enabled until the next manual connection
        # test. Probe failures are non-fatal — the registration call below
        # still proceeds and the adapter just retains its current version.
        if not self._romm_api.get_version():
            try:
                heartbeat = await self._loop.run_in_executor(None, self._romm_api.heartbeat)
                with contextlib.suppress(AttributeError, TypeError):
                    version = heartbeat.get("SYSTEM", {}).get("VERSION")
                    if version:
                        self._romm_api.set_version(version)
            except Exception as e:
                self._logger.debug(f"ensure_device_registered: version probe failed (non-fatal): {e}")

        sync_state = self._state_svc.state
        has_device_id = sync_state.device_id
        has_server_id = sync_state.server_device_id
        if has_device_id and has_server_id:
            server_id_str = str(has_server_id)
            with contextlib.suppress(Exception):
                await self._loop.run_in_executor(
                    None,
                    lambda: self._romm_api.update_device(server_id_str, client_version=self._plugin_version),
                )
            return {
                "success": True,
                "device_id": sync_state.device_id,
                "device_name": sync_state.device_name or "",
                "server_device_id": has_server_id,
            }

        hostname = socket.gethostname()

        try:
            result = await self._loop.run_in_executor(
                None,
                lambda: self._romm_api.register_device(
                    name=hostname,
                    platform="linux",
                    client="decky-romm-sync",
                    client_version=self._plugin_version,
                ),
            )
            server_device_id = result.get("id") or result.get("device_id")
            if server_device_id:
                sync_state.device_id = str(server_device_id)
                sync_state.device_name = hostname
                sync_state.server_device_id = str(server_device_id)
                self._state_svc.save_state()
                self._logger.info(f"Device registered with server: {server_device_id} ({hostname})")
                return {
                    "success": True,
                    "device_id": str(server_device_id),
                    "device_name": hostname,
                    "server_device_id": str(server_device_id),
                }
        except Exception as e:
            self._logger.warning(f"Server device registration failed: {e}")

        return {"success": False, "device_id": "", "device_name": "", "error": "registration_failed"}

    async def list_devices(self) -> dict:
        """List all devices registered with the RomM server for this user."""
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "devices": [], "disabled": True}
        try:
            own_id = self._state_svc.get_server_device_id()
            devices = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_devices()),
            )
            own_id_str = str(own_id or "")
            enriched = [
                {**d, "is_current_device": bool(own_id_str) and (str(d.get("id") or "")) == own_id_str} for d in devices
            ]
            return {"success": True, "devices": enriched}
        except Exception as e:
            self._log_debug(f"list_devices failed: {e}")
            return {"success": False, "devices": [], "error": "list_failed"}

    # ------------------------------------------------------------------
    # Public sync orchestration callables
    # ------------------------------------------------------------------

    async def _refresh_save_sort_state(self, where: str) -> None:
        """Refresh save-sort state from the live RetroArch config.

        Save-sync must observe fresh save-sort state before computing
        ``saves_dir``. This call ensures ``detect_save_sort_change`` has
        run at least once before we read state, closing the race where
        another frontend detect trigger arrives after our backend entry
        point. Without this, a direct-Steam-launch with no pre-detect
        would silently download stale server content to the wrong
        layout and destroy real user progress during the subsequent
        migration (#238).

        Graceful degradation: if detect fails (e.g. retroarch.cfg is
        temporarily unreadable) we log and continue with the
        previously-known state — save-sync must not abort because of a
        config read error.
        """
        if self._detect_sort_change is None:
            return
        try:
            await self._loop.run_in_executor(None, self._detect_sort_change)
        except Exception as e:
            self._logger.warning(
                "%s: detect_sort_change failed (%s) — proceeding with stale state",
                where,
                e,
            )

    async def pre_launch_sync(self, rom_id: int) -> dict:
        """Download newer saves from server before game launch."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._state_svc.is_save_sync_enabled():
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: block pre_launch_sync if a future caller bypasses
            # the @migration_blocked decorator at the public callable. saves_dir
            # would otherwise resolve under the new home and silently desync from
            # files still living at the old home. Internal _sync_rom_saves callers
            # (sync_all_saves, rollback_to_version) are protected by the decorator
            # on their own public callables — this guard is for pre_launch_sync.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            # Refresh save-sort state before the migration gate — see #238.
            await self._refresh_save_sort_state("pre_launch_sync")

            if self._rom_info.is_save_sort_changed():
                return {
                    "success": False,
                    "message": "RetroArch save sorting changed — migrate saves in Settings first",
                    "synced": 0,
                    "save_sort_changed": True,
                }

            if not self._state_svc.state.settings.sync_before_launch:
                return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

            if not self._state_svc.state.device_id:
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self._state_svc.save_state()

            msg = f"Downloaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def post_exit_sync(self, rom_id: int) -> dict:
        """Upload changed saves after game exit."""
        self._logger.info("post_exit_sync called for rom_id=%d", rom_id)
        rom_id = int(rom_id)

        async with self._rom_lock(rom_id):
            if not self._state_svc.is_save_sync_enabled():
                self._logger.info("post_exit_sync skipped: save sync disabled")
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: same rationale as pre_launch_sync — internal
            # _sync_rom_saves callers are protected by @migration_blocked on
            # their public callables; this guard covers post_exit_sync only.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                self._logger.info("post_exit_sync skipped: retrodeck migration pending")
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            if not self._state_svc.state.settings.sync_after_exit:
                self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
                return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            await self._refresh_save_sort_state("post_exit_sync")

            try:
                await self._loop.run_in_executor(None, self._romm_api.heartbeat)
            except Exception:
                self._logger.info("post_exit_sync skipped: server offline")
                return {"success": False, "message": "Server offline", "synced": 0, "offline": True}

            if not self._state_svc.state.device_id:
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self._state_svc.save_state()

            self._logger.info(
                "post_exit_sync complete for rom_id=%d: synced=%d, errors=%d, conflicts=%d",
                rom_id,
                synced,
                len(errors),
                len(conflicts),
            )

            msg = f"Uploaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def sync_rom_saves(self, rom_id: int) -> dict:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._state_svc.is_save_sync_enabled():
                return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            # Manual sync paths must observe fresh sort state too: a user could
            # edit retroarch.cfg outside of a session and then trigger a manual
            # sync before any detect has fired.
            await self._refresh_save_sort_state("sync_rom_saves")

            if not self._state_svc.state.device_id:
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id)
            self._state_svc.save_state()

            msg = f"Synced {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def sync_all_saves(self) -> dict:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0, "conflicts": 0}

        # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
        # Manual sync paths must observe fresh sort state too: a user could
        # edit retroarch.cfg outside of a session and then trigger a manual
        # sync before any detect has fired.
        await self._refresh_save_sort_state("sync_all_saves")

        if not self._state_svc.state.device_id:
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": DEVICE_NOT_REGISTERED}

        total_synced = 0
        total_errors: list[str] = []
        all_conflicts: list[SaveConflict | dict] = []
        rom_count = 0

        # Only iterate installed ROMs — non-installed ROMs have no save files
        rom_ids = set(self._state["installed_roms"].keys())
        self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

        for rom_id_str in sorted(rom_ids):
            rom_count += 1
            rom_id_int = int(rom_id_str)
            async with self._rom_lock(rom_id_int):
                synced, errors, conflicts = await self._loop.run_in_executor(None, self._sync_rom_saves, rom_id_int)
            total_synced += synced
            total_errors.extend(errors)
            all_conflicts.extend(conflicts)

        self._state_svc.save_state()

        conflicts_count = len(all_conflicts)
        msg = f"Synced {total_synced} save(s) across {rom_count} ROM(s)"
        if total_errors:
            msg += f", {len(total_errors)} error(s)"
        if conflicts_count:
            msg += f", {conflicts_count} conflict(s)"
        return {
            "success": len(total_errors) == 0,
            "message": msg,
            "synced": total_synced,
            "conflicts": conflicts_count,
            "conflicts_list": [c if isinstance(c, dict) else asdict(c) for c in all_conflicts],
            "roms_checked": rom_count,
            "errors": total_errors,
        }

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        action: str,
    ) -> dict:
        """Resolve a pending sync conflict (true two-sided divergence).

        Reached when ``compute_sync_action`` returned ``Conflict`` — the
        server moved AND local diverged from baseline, so the user picked a
        side via the conflict UI.

        ``action`` is one of:

        - ``"keep_local"`` — push local to the current server save (PUT). When
          the local content already matches the server's content hash we adopt
          it silently without re-uploading.
        - ``"use_server"`` — download the current server save, replacing local.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if action not in ("keep_local", "use_server"):
            return {"success": False, "message": f"Invalid action: {action}"}

        # Frontend-supplied filename flows into ``os.path.join(saves_dir, …)``
        # via ``_resolve_conflict_keep_local``. Reject anything that isn't
        # already a clean basename — legitimate callers always pass one.
        try:
            sanitized = sanitize_save_filename(filename)
        except ValueError as e:
            self._logger.warning(
                "resolve_sync_conflict(rom_id=%d, action=%s) rejected invalid filename: %s",
                rom_id,
                action,
                e,
            )
            return {"success": False, "message": "Invalid filename"}
        if sanitized != filename:
            self._logger.warning(
                "resolve_sync_conflict(rom_id=%d, action=%s) rejected non-basename filename",
                rom_id,
                action,
            )
            return {"success": False, "message": "Invalid filename"}

        async with self._rom_lock(rom_id):
            info = self._rom_info.get_rom_save_info(rom_id)
            if not info:
                return {"success": False, "message": "ROM not installed"}
            system = info["system"]
            saves_dir = info["saves_dir"]

            try:
                device_id = self._state_svc.get_server_device_id()
                server_saves = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                    ),
                )
            except Exception as e:
                _code, _msg = classify_error(e)
                return {"success": False, "message": f"Failed to fetch saves: {_msg}"}

            rom_state = self._state_svc.state.saves.get(rom_id_str)
            active_slot = rom_state.active_slot if rom_state else None
            server_in_slot = self._filter_server_saves_to_slot(server_saves, active_slot)
            if not server_in_slot:
                return {"success": False, "message": "No server save in active slot"}
            server = max(server_in_slot, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)

            try:
                if action == "use_server":
                    await self._loop.run_in_executor(
                        None,
                        self._resolve_conflict_use_server,
                        rom_id_str,
                        server,
                        saves_dir,
                        system,
                        info["rom_name"],
                    )
                    self._logger.info(
                        "resolve_sync_conflict(rom_id=%d, filename=%s, action=%s) -> success",
                        rom_id,
                        filename,
                        action,
                    )
                    return {"success": True, "action": "use_server"}

                # keep_local — resolve on-disk name via the same canonical
                # ``<rom_name>.<server.file_extension>`` rule use_server uses.
                # The frontend-supplied ``filename`` is kept for logging only;
                # using it as the on-disk path would let an extension drift
                # between the two resolution paths produce divergent states.
                await self._loop.run_in_executor(
                    None,
                    self._resolve_conflict_keep_local,
                    rom_id,
                    rom_id_str,
                    server,
                    saves_dir,
                    system,
                    info["rom_name"],
                )
                self._logger.info(
                    "resolve_sync_conflict(rom_id=%d, filename=%s, action=%s) -> success",
                    rom_id,
                    filename,
                    action,
                )
                return {"success": True, "action": "keep_local"}
            except Exception as e:
                self._logger.error(f"resolve_sync_conflict({rom_id}, {filename}, {action}) failed: {e}")
                return {"success": False, "message": str(e)}

    def _resolve_conflict_use_server(
        self,
        rom_id_str: str,
        server: dict,
        saves_dir: str,
        system: str,
        rom_name: str,
    ) -> None:
        """Download *server* into the canonical local save file and update state.

        The write path is always ``<rom_name>.<server.file_extension>`` — the
        path RetroArch reads. Drives state-key consistency too:
        ``_update_file_sync_state`` receives the same target name the file
        lands at.
        """
        target = _local_save_target(server, rom_name)
        self._do_download_save(server, saves_dir, target, rom_id_str, system)
        self._state_svc.save_state()

    def _resolve_conflict_keep_local(
        self,
        rom_id: int,
        rom_id_str: str,
        server: dict,
        saves_dir: str,
        system: str,
        rom_name: str,
    ) -> None:
        """Push the local file to *server* (PUT). Adopt-without-upload when the
        local content already matches the server's content hash.

        The on-disk name is resolved from the server save's ``file_extension``
        via :func:`_local_save_target` — the same canonical
        ``<rom_name>.<server.file_extension>`` rule ``_resolve_conflict_use_server``
        and every other download path uses. This keeps the two resolve paths
        symmetric: the state key and on-disk path are identical regardless of
        which side the user picked. If the local file is not at the canonical
        path (e.g. ``Mario.sav`` locally but the server save has
        ``file_extension=srm``), :class:`FileNotFoundError` is raised — we
        never silently rename across extensions.
        """
        target = _local_save_target(server, rom_name)
        local_path = os.path.join(saves_dir, target)
        if not self._save_file.is_file(local_path):
            raise FileNotFoundError(f"Local save not found: {local_path}")
        local_hash = self._save_file.checksum_md5(local_path)
        try:
            server_hash = self._retry.with_retry(lambda: self._get_server_save_hash(server))
        except Exception:
            server_hash = None

        if server_hash and local_hash == server_hash:
            # Hashes match — adopt server's id without re-uploading.
            self._log_debug(
                f"keep_local: hash matches server, adopting without upload (rom={rom_id} filename={target})"
            )
            rom_state = self._state_svc.ensure_rom_state(rom_id_str)
            file_state = rom_state.files.setdefault(target, FileSyncState())
            file_state.tracked_save_id = server.get("id")
            file_state.last_sync_hash = local_hash
            file_state.last_sync_at = self._clock.now().isoformat()
            file_state.last_sync_server_updated_at = server.get("updated_at", "") or ""
            file_state.last_sync_server_size = server.get("file_size_bytes")
            file_state.last_sync_local_mtime = self._save_file.get_mtime(local_path)
            file_state.last_sync_local_size = self._save_file.get_size(local_path)
            self._state_svc.save_state()
            return

        # Upload local content as a PUT against the existing server save.
        self._do_upload_save(rom_id, local_path, target, rom_id_str, system, server)
        self._state_svc.save_state()
