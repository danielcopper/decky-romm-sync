"""Newest-wins matrix executor and per-file sync I/O dispatch.

The decision layer for "which side wins for this file" plus the I/O
helpers that actually move bytes between the local saves directory and
the RomM server. Read-only matrix consumption (status reporting) lives
in StatusService; persistence belongs in StateService; rom-level lock
coordination and public callable orchestration live on
:class:`services.saves.sync_engine.engine.SyncEngine`.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from models.saves import SaveConflict

from domain.emulator_tag import build_emulator_tag
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
from services.saves._helpers import _local_save_target

if TYPE_CHECKING:
    import logging

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

    Yielded by :meth:`MatrixExecutor.iter_matrix_outcomes` for both consumers
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


class MatrixExecutor:
    """Newest-wins matrix executor + per-file sync I/O dispatch.

    Owns every code path that reads the server save list, runs
    ``compute_sync_action`` against per-filename inputs, and dispatches
    the resulting :class:`SyncAction` to disk / server I/O. The public
    rom-level orchestration callables on :class:`SyncEngine` drive this
    executor; peer save sub-services (status, versions, slots) also
    consume specific helpers through ``SyncEngine``'s thin delegate
    surface.
    """

    def __init__(
        self,
        *,
        state: dict,
        state_svc: StateService,
        rom_info: RomInfoService,
        romm_api: RommSyncApi,
        retry: RetryStrategy,
        logger: logging.Logger,
        clock: Clock,
        save_file: SaveFileAdapter,
        log_debug: DebugLogger,
        get_active_core: CoreResolverFn,
    ) -> None:
        self._state = state
        self._state_svc = state_svc
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._logger = logger
        self._clock = clock
        self._save_file = save_file
        self._log_debug = log_debug
        self._get_active_core = get_active_core

    # ------------------------------------------------------------------
    # Server Save Hash Helper
    # ------------------------------------------------------------------

    def get_server_save_hash(self, server_save: dict) -> str | None:
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

    def update_file_sync_state(
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

    def do_download_save(self, server_save: dict, saves_dir: str, filename: str, rom_id_str: str, system: str) -> None:
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
        self.update_file_sync_state(rom_id_str, filename, server_save, local_path, system)
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
        """Mark *slot* as having a server copy after a successful upload of a local-only slot.

        Pure in-memory mutation. The caller (:meth:`do_upload_save`) owns the
        single persistence point so every upload outcome lands on disk exactly
        once, regardless of which branch of this method (promote / no-op) fired.
        """
        rom_state = self._state_svc.state.saves.get(rom_id_str)
        if not rom_state:
            return
        slot_entry = rom_state.slots.get(slot)
        if slot_entry and slot_entry.get("source") == "local":
            slot_entry["source"] = "server"
            slot_entry["count"] = 1

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

    def do_upload_save(
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

        self.update_file_sync_state(
            rom_id_str, filename, result, file_path, system, emulator_tag=emulator, core_so=core_so
        )

        if is_post:
            self._record_own_upload(rom_id_str, result.get("id"))

        if slot:
            self._promote_local_slot_to_server(rom_id_str, slot)

        # Single persistence point for every upload outcome — covers the
        # file-level sync state written by ``update_file_sync_state`` plus the
        # in-memory mutations from ``_record_own_upload`` / ``_promote_local_slot_to_server``.
        self._state_svc.save_state()

        self._confirm_upload_sync(result.get("id"), device_id)

        self._log_debug(f"Uploaded save: {filename} for rom {rom_id_str} (emulator={emulator})")
        return result

    def _record_own_upload(self, rom_id_str: str, new_id: int | None) -> None:
        """Track a save_id we POSTed ourselves for uploader attribution.

        POST = brand-new save; PUT updates an existing tracked save without
        changing ownership. Assumes POST is not upsert-by-filename on the
        server — if RomM ever changes that, revisit this tracker.

        Pure in-memory mutation. The caller (:meth:`do_upload_save`) owns the
        single persistence point so every upload outcome lands on disk exactly
        once, regardless of POST vs PUT.
        """
        if new_id is None:
            return
        rom_state = self._state_svc.ensure_rom_state(rom_id_str)
        if rom_state.own_upload_ids is None:
            rom_state.own_upload_ids = []
        if new_id in rom_state.own_upload_ids:
            return
        rom_state.own_upload_ids.append(new_id)

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
    def filter_server_saves_to_slot(server_saves: list[dict], active_slot: str | None) -> list[dict]:
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

    def build_sync_conflict_entry(
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
            self.adopt_baseline_hash(rom_id_str, filename, local_hash)
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
            self.do_upload_save(rom_id, local_path, filename, rom_id_str, system, None)
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
        self.do_upload_save(rom_id, local_path, filename, rom_id_str, system, server_save)
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

        Centralises the I/O dispatch so ``sync_rom_saves`` stays declarative.
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
                self.do_download_save(action.server_save, saves_dir, filename, rom_id_str, system)
                return True
            if isinstance(action, Conflict):
                conflicts.append(
                    self.build_sync_conflict_entry(rom_id, filename, action.server_save, local_path, local_hash)
                )
                return False
        except RommApiError as e:
            _code, _msg = classify_error(e)
            errors.append(f"{filename}: {_msg}")
        except Exception as e:
            self._handle_unexpected_error(e, filename, saves_dir, errors)
        return False

    def adopt_baseline_hash(self, rom_id_str: str, filename: str, local_hash: str) -> None:
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

    def sync_rom_saves(self, rom_id: int) -> tuple[int, list[str], list[SaveConflict | dict]]:
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
        server_in_slot = self.filter_server_saves_to_slot(server_saves, active_slot)

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
