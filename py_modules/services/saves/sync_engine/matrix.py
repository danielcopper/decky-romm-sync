"""Newest-wins matrix executor and per-file sync I/O dispatch.

The decision layer for "which side wins for this file" plus the I/O
helpers that actually move bytes between the local saves directory and
the RomM server. Read-only matrix consumption (status reporting) lives
in StatusService; the loaded :class:`RomSaveState` aggregate is threaded
in by the operation entry, which owns the Unit-of-Work read/write
bracketing this executor's in-memory mutations (ADR-0006). Rom-level
lock coordination and public callable orchestration live on
:class:`services.saves.sync_engine.engine.SyncEngine`.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from domain.emulator_tag import build_emulator_tag
from domain.rom_save_state import FileSyncState, RomSaveState
from domain.save_slot import save_in_slot
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
    compute_sync_action,
)
from lib.errors import RommApiError, classify_error
from services.saves._helpers import local_save_target

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator

    from services.protocols import (
        Clock,
        DebugLogger,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
    )
    from services.saves.rom_info import RomInfoService


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
    server_candidates: list[dict[str, Any]]


@dataclass(frozen=True)
class DispatchSink:
    """The two output accumulators a single ROM's sync dispatch appends to.

    Holds the mutable ``errors`` and ``conflicts`` lists that
    :meth:`MatrixExecutor._dispatch_sync_action` (and its sub-dispatchers)
    append onto. The dataclass itself is frozen — only the referenced lists
    are mutated — so it threads both sinks through as one argument without
    becoming a stateful object.
    """

    errors: list[str]
    conflicts: list[dict[str, Any]]


class MatrixExecutor:
    """Newest-wins matrix executor + per-file sync I/O dispatch.

    Owns every code path that reads the server save list, runs
    ``compute_sync_action`` against per-filename inputs, and dispatches
    the resulting :class:`SyncAction` to disk / server I/O. The loaded
    :class:`RomSaveState` aggregate is threaded in by the public rom-level
    orchestration callables on :class:`SyncEngine`; this executor mutates
    it in memory via the aggregate's verb methods and never persists —
    the operation entry owns the single write Unit of Work.
    """

    def __init__(
        self,
        *,
        rom_info: RomInfoService,
        romm_api: RommSyncApi,
        retry: RetryStrategy,
        logger: logging.Logger,
        clock: Clock,
        save_file_store: SaveFileStore,
        log_debug: DebugLogger,
    ) -> None:
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._retry = retry
        self._logger = logger
        self._clock = clock
        self._save_file_store = save_file_store
        self._log_debug = log_debug

    # ------------------------------------------------------------------
    # Server Save Hash Helper
    # ------------------------------------------------------------------

    def get_server_save_hash(self, server_save: dict[str, Any]) -> str | None:
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
            tmp_path = self._save_file_store.make_temp_path(suffix=".tmp")
            self._romm_api.download_save(save_id, tmp_path)
            return self._save_file_store.checksum_md5(tmp_path)
        except Exception as e:
            self._log_debug(f"Failed to hash server save {save_id}: {e}")
            if self._retry.is_retryable(e):
                raise
            return None
        finally:
            if tmp_path:
                with contextlib.suppress(OSError):
                    self._save_file_store.remove_file(tmp_path)

    def update_file_sync_state(
        self,
        save_state: RomSaveState,
        filename: str,
        server_response: dict[str, Any],
        local_path: str,
        system: str,
        *,
        default_slot: str | None = None,
        emulator_tag: str | None = None,
        core_so: str | None = None,
    ) -> None:
        """Update per-file sync tracking on *save_state* after a successful sync op.

        Mutates the passed aggregate in memory via its verb methods; the
        operation entry owns the surrounding write Unit of Work. When the
        aggregate is brand new (no active slot, default emulator) it seeds the
        active slot from *default_slot* so the first sync lands in the
        configured slot. The per-file baseline is recorded via
        :meth:`RomSaveState.adopt_baseline` — the server response always
        carries the tracked save id.
        """
        if not save_state.system and system:
            save_state.adopt_system(system)
        if save_state.active_slot is None and not save_state.slots:
            save_state.switch_active_slot(default_slot or "default")
        save_state.record_synced_core(core_so, emulator_tag or save_state.emulator or "retroarch")

        now = self._clock.now().isoformat()
        local_exists = self._save_file_store.is_file(local_path)
        local_hash = self._save_file_store.checksum_md5(local_path) if local_exists else ""

        server_save_id = server_response.get("id")
        if server_save_id is None or not local_hash:
            # A baseline needs a server save id and a content hash (invariant 1).
            # A sync response missing the id, or a save file that vanished before
            # we could hash it, is genuinely untrackable — log and skip the
            # baseline rather than abort the whole rom-level sync.
            self._logger.warning(
                "update_file_sync_state: skipping untrackable baseline for %s (id=%r, has_hash=%s)",
                filename,
                server_save_id,
                bool(local_hash),
            )
            return

        save_state.adopt_baseline(
            filename,
            tracked_save_id=int(server_save_id),
            last_sync_hash=local_hash,
            last_sync_at=now,
            last_sync_server_updated_at=server_response.get("updated_at", now) or now,
            last_sync_server_save_id=server_save_id,
            last_sync_server_size=server_response.get("file_size_bytes"),
            last_sync_local_mtime=self._save_file_store.get_mtime(local_path) if local_exists else None,
            last_sync_local_size=self._save_file_store.get_size(local_path) if local_exists else None,
        )

    # ------------------------------------------------------------------
    # Sync Helpers
    # ------------------------------------------------------------------

    def do_download_save(
        self,
        server_save: dict[str, Any],
        saves_dir: str,
        filename: str,
        save_state: RomSaveState,
        device_id: str | None,
        system: str,
        default_slot: str | None = None,
    ) -> None:
        """Download a save file from server. Backs up existing local file first.

        Mutates *save_state* in memory (per-file baseline); the operation entry
        owns the surrounding write Unit of Work.
        """
        local_path = os.path.join(saves_dir, filename)
        self._save_file_store.make_dirs(saves_dir)
        tmp_path = local_path + ".tmp"

        self._retry.with_retry(
            lambda: self._romm_api.download_save_content(
                server_save["id"],
                tmp_path,
                device_id=device_id,
                optimistic=True,
            ),
        )

        # Back up the existing local save before the download overwrites it.
        self.quarantine_local_file(saves_dir, filename)

        self._save_file_store.rename(tmp_path, local_path)
        self.update_file_sync_state(save_state, filename, server_save, local_path, system, default_slot=default_slot)
        self._log_debug(f"Downloaded save: {filename}")

    def quarantine_local_file(self, saves_dir: str, filename: str) -> bool:
        """Move a local save file aside into ``.romm-backup`` before it is destroyed.

        The single source of truth for the save-file backup discipline: both the
        download-overwrite path (:meth:`do_download_save`) and the slot-switch
        removal path route through here, so no local save is ever destroyed
        without a recoverable copy (#965). The backup lands at
        ``<saves_dir>/.romm-backup/<name>_<ts><ext>`` (``<ts>`` from the injected
        clock). Returns ``True`` when a file was moved, ``False`` when there was
        nothing at *filename* to back up.
        """
        local_path = os.path.join(saves_dir, filename)
        if not self._save_file_store.is_file(local_path):
            return False
        backup_dir = os.path.join(saves_dir, ".romm-backup")
        self._save_file_store.make_dirs(backup_dir)
        ts = self._clock.now().strftime("%Y%m%d_%H%M%S")
        name, ext = os.path.splitext(filename)
        self._save_file_store.rename(local_path, os.path.join(backup_dir, f"{name}_{ts}{ext}"))
        return True

    @staticmethod
    def _resolve_upload_slot(
        save_state: RomSaveState, device_id: str | None, default_slot: str | None = None
    ) -> str | None:
        """The slot field to send with an upload; ``None`` when device sync is off.

        With device sync on, a named ``active_slot`` uploads to that slot. An
        ``active_slot`` of ``None`` is ambiguous and must be disambiguated by the
        ``slots`` map (the same signal :meth:`update_file_sync_state` uses to seed
        the active slot):

        - **Explicit legacy** (``active_slot`` None but ``slots`` populated — the
          state after switching to / confirming the legacy slot) → ``None`` so the
          save is POSTed as ``slot:null``. Returning ``"default"`` here misfiled a
          legacy save into the default slot (#1061).
        - **Brand-new ROM** (``active_slot`` None and ``slots`` empty — never
          configured) → the configured ``default_slot`` so its first sync lands in
          the default slot, matching the active-slot seeding.
        """
        if not device_id:
            return None
        if save_state.active_slot is not None:
            return save_state.active_slot
        if save_state.slots:
            return None
        return default_slot or "default"

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
        save_state: RomSaveState,
        device_id: str | None,
        system: str,
        core_so: str | None,
        server_save: dict[str, Any] | None = None,
        default_slot: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local save file to server.

        Mutates *save_state* in memory (per-file baseline, own-upload
        attribution, local→server slot promotion); the operation entry owns
        the surrounding write Unit of Work. *core_so* is the active core
        resolved once by the caller so this worker stays free of installed-rom
        reads.
        """
        save_id = server_save.get("id") if server_save else None
        emulator = build_emulator_tag(core_so)

        # v4.7: pass device_id and slot
        slot = self._resolve_upload_slot(save_state, device_id, default_slot)

        result = self._retry.with_retry(
            lambda: self._romm_api.upload_save(
                int(rom_id), file_path, emulator, save_id, device_id=device_id, slot=slot
            )
        )

        self.update_file_sync_state(
            save_state,
            filename,
            result,
            file_path,
            system,
            default_slot=default_slot,
            emulator_tag=emulator,
            core_so=core_so,
        )

        new_id = result.get("id")
        if new_id is not None:
            save_state.track_own_upload(new_id)

        if slot:
            save_state.promote_slot_to_server(slot)

        self._confirm_upload_sync(result.get("id"), device_id)

        self._log_debug(f"Uploaded save: {filename} (emulator={emulator})")
        return result

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
            self._save_file_store.remove_file(tmp)

    @staticmethod
    def filter_server_saves_to_slot(
        server_saves: list[dict[str, Any]], active_slot: str | None
    ) -> list[dict[str, Any]]:
        """Filter server saves to the active slot by exact slot membership.

        A legacy (``slot:null`` / ``""``) save belongs ONLY to the legacy slot —
        it is never surfaced under a named slot. Sharing
        :func:`domain.save_slot.save_in_slot` keeps the sync matrix, the status
        display, and rollback consistent with the per-slot read/delete paths
        (#1061): the legacy save is visible and syncable only in legacy mode, so
        it can't bleed into a named slot's status or get downloaded into it.
        """
        return [ss for ss in server_saves if save_in_slot(ss, active_slot)]

    def _build_local_input(self, local_path: str, filename: str) -> dict[str, Any]:
        """Build the dict shape consumed by ``compute_sync_action``."""
        exists = self._save_file_store.is_file(local_path)
        return {
            "filename": filename,
            "path": local_path,
            "size": self._save_file_store.get_size(local_path) if exists else None,
            "mtime": self._save_file_store.get_mtime(local_path) if exists else None,
        }

    def build_sync_conflict_entry(
        self,
        rom_id: int,
        filename: str,
        server: dict[str, Any],
        local_path: str | None,
        local_hash: str | None,
    ) -> dict[str, Any]:
        """Build a Phase-2 ``sync_conflict`` descriptor for the frontend."""
        local_mtime = None
        local_size = None
        if local_path and self._save_file_store.is_file(local_path):
            local_mtime = datetime.fromtimestamp(self._save_file_store.get_mtime(local_path), tz=UTC).isoformat()
            local_size = self._save_file_store.get_size(local_path)
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
        save_state: RomSaveState,
        filename: str,
        local_hash: str | None,
    ) -> None:
        if action.adopt_baseline and local_hash is not None:
            # State-only mutation: write the current local_hash as the baseline
            # so future runs can detect drift. No I/O, no synced count.
            self._log_debug(f"do_sync_rom_saves({rom_id}): skip + adopt_baseline {filename} ({action.reason})")
            self.adopt_baseline_hash(save_state, filename, local_hash)
        else:
            self._log_debug(f"do_sync_rom_saves({rom_id}): skip {filename} ({action.reason})")

    def _dispatch_upload(
        self,
        action: Upload,
        *,
        rom_id: int,
        save_state: RomSaveState,
        device_id: str | None,
        filename: str,
        local_path: str | None,
        system: str,
        core_so: str | None,
        default_slot: str | None,
        server_saves: list[dict[str, Any]],
        errors: list[str],
    ) -> bool:
        """Execute an ``Upload`` action. Returns True iff the upload was issued."""
        if local_path is None:
            errors.append(f"{filename}: upload requested but no local file")
            return False
        if action.target_save_id is None:
            # POST path: brand-new save in slot.
            self.do_upload_save(
                rom_id, local_path, filename, save_state, device_id, system, core_so, None, default_slot
            )
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
        self.do_upload_save(
            rom_id, local_path, filename, save_state, device_id, system, core_so, server_save, default_slot
        )
        return True

    def _dispatch_sync_action(
        self,
        action: object,
        *,
        rom_id: int,
        save_state: RomSaveState,
        device_id: str | None,
        filename: str,
        local_path: str | None,
        local_hash: str | None,
        saves_dir: str,
        system: str,
        core_so: str | None,
        default_slot: str | None,
        server_saves: list[dict[str, Any]],
        sink: DispatchSink,
    ) -> bool:
        """Execute one ``SyncAction`` outcome. Returns True if a transfer happened.

        Centralises the I/O dispatch so ``sync_rom_saves`` stays declarative.
        Errors are caught and pushed onto ``sink.errors`` so a single failure
        can't abort the whole rom-level sync; conflicts land on
        ``sink.conflicts``.
        """
        try:
            if isinstance(action, Skip):
                self._dispatch_skip(
                    action,
                    rom_id=rom_id,
                    save_state=save_state,
                    filename=filename,
                    local_hash=local_hash,
                )
                return False
            if isinstance(action, Upload):
                return self._dispatch_upload(
                    action,
                    rom_id=rom_id,
                    save_state=save_state,
                    device_id=device_id,
                    filename=filename,
                    local_path=local_path,
                    system=system,
                    core_so=core_so,
                    default_slot=default_slot,
                    server_saves=server_saves,
                    errors=sink.errors,
                )
            if isinstance(action, Download):
                self.do_download_save(
                    action.server_save, saves_dir, filename, save_state, device_id, system, default_slot
                )
                return True
            if isinstance(action, Conflict):
                sink.conflicts.append(
                    self.build_sync_conflict_entry(rom_id, filename, action.server_save, local_path, local_hash)
                )
                return False
        except RommApiError as e:
            _code, _msg = classify_error(e)
            sink.errors.append(f"{filename}: {_msg}")
        except Exception as e:
            self._handle_unexpected_error(e, filename, saves_dir, sink.errors)
        return False

    def adopt_baseline_hash(self, save_state: RomSaveState, filename: str, local_hash: str) -> None:
        """Record ``local_hash`` as the file's ``last_sync_hash`` baseline.

        Used by Skip(adopt_baseline=True) — the algorithm has detected that
        we've observed an is_current=true situation with local content but no
        baseline yet. Recording the baseline lets subsequent runs detect
        offline-edit drift. In-memory mutation only, no I/O; the operation
        entry owns the surrounding write Unit of Work.
        """
        save_state.update_baseline_hash(filename, local_hash)

    def iter_matrix_outcomes(
        self,
        rom_id: int,
        server_in_slot: list[dict[str, Any]],
        *,
        save_state: RomSaveState | None,
        device_id: str | None,
        info: dict[str, Any],
    ) -> Iterator[MatrixOutcome]:
        """Yield one :class:`MatrixOutcome` per save file in the ROM's active slot.

        Walks the local saves directory + server-only canonical targets,
        runs ``compute_sync_action`` against the per-filename inputs, and
        emits :class:`MatrixOutcome` records ready for sync dispatch or
        status rendering. Pure compute — no I/O writes, no state mutation.
        Consumers drive their own side effects from the yielded outcomes.
        """
        rom_name = info["rom_name"]

        files_state: dict[str, FileSyncState] = save_state.files if save_state else {}
        device_id_str = device_id or ""

        local_files = self._rom_info.find_save_files(rom_id)

        handled_filenames: set[str] = set()
        for lf in local_files:
            filename = lf["filename"]
            local_path = lf["path"]
            handled_filenames.add(filename)
            local_exists = self._save_file_store.is_file(local_path)
            local_hash = self._save_file_store.checksum_md5(local_path) if local_exists else None
            file_state = files_state.get(filename, FileSyncState())
            local_mtime_iso = (
                datetime.fromtimestamp(self._save_file_store.get_mtime(local_path), tz=UTC).isoformat()
                if local_exists
                else None
            )
            local_size = self._save_file_store.get_size(local_path) if local_exists else None
            # Group server saves to this file's own canonical target — symmetric
            # with the server-only loop below — so a multi-file save set never
            # cross-contaminates extensions (#1006). Without this, a sibling
            # extension's newer server record would win max(updated_at) and the
            # file would be evaluated/dispatched against the wrong save.
            group = [ss for ss in server_in_slot if local_save_target(ss, rom_name) == filename]
            action = compute_sync_action(
                local_file=self._build_local_input(local_path, filename),
                server_saves_in_slot=group,
                files_state=_file_state_to_dict(file_state),
                device_id=device_id_str,
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
                server_candidates=group,
            )

        # Group server saves by canonical local target filename. Server-only
        # groups (no local file) get matrix-evaluated against their own group;
        # compute_sync_action picks newest-in-group internally.
        server_only_groups: dict[str, list[dict[str, Any]]] = {}
        for ss in server_in_slot:
            target = local_save_target(ss, rom_name)
            if target in handled_filenames:
                continue
            server_only_groups.setdefault(target, []).append(ss)

        for target_filename, group in server_only_groups.items():
            file_state = files_state.get(target_filename, FileSyncState())
            action = compute_sync_action(
                local_file=None,
                server_saves_in_slot=group,
                files_state=_file_state_to_dict(file_state),
                device_id=device_id_str,
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

    def sync_rom_saves(
        self,
        rom_id: int,
        save_state: RomSaveState,
        device_id: str | None,
        core_so: str | None,
        default_slot: str | None = None,
    ) -> tuple[int, list[str], list[dict[str, Any]]]:
        """Sync saves for a single ROM, mutating *save_state* in memory.

        Drives :meth:`iter_matrix_outcomes` and dispatches each emitted
        outcome through :meth:`_dispatch_sync_action`. Returns
        ``(synced_count, errors_list, conflicts_list)``. *core_so* is the
        active core resolved once by the caller (for the upload emulator tag);
        *default_slot* seeds the active slot when a brand-new ROM's first sync
        lands; the operation entry owns the surrounding read/write Unit of Work.
        """
        t_total = self._clock.time()
        rom_id = int(rom_id)

        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            self._log_debug(f"do_sync_rom_saves({rom_id}): no save info, skipping")
            return 0, [], []
        system = info["system"]
        saves_dir = info["saves_dir"]

        t0 = self._clock.time()
        try:
            server_saves = self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id))
        except Exception as e:
            self._logger.error(f"do_sync_rom_saves({rom_id}): failed to list saves: {e}")
            _code, _msg = classify_error(e)
            return 0, [f"Failed to fetch saves: {_msg}"], []
        self._log_debug(f"[TIMING] do_sync_rom_saves({rom_id}): list_saves {self._clock.time() - t0:.3f}s")

        active_slot = save_state.active_slot
        server_in_slot = self.filter_server_saves_to_slot(server_saves, active_slot)

        self._log_debug(
            f"do_sync_rom_saves({rom_id}): system={system}, rom_name={info['rom_name']}, "
            f"server_saves={len(server_saves)}, saves_dir={saves_dir}"
        )

        errors: list[str] = []
        conflicts: list[dict[str, Any]] = []
        sink = DispatchSink(errors=errors, conflicts=conflicts)
        synced = 0

        pending_migration = self._rom_info.is_save_sort_changed()
        for outcome in self.iter_matrix_outcomes(
            rom_id, server_in_slot, save_state=save_state, device_id=device_id, info=info
        ):
            origin = "local" if outcome.local_path is not None else "server-only"
            self._log_debug(
                f"do_sync_rom_saves({rom_id}): {origin} {outcome.filename} -> {type(outcome.action).__name__}"
            )
            if outcome.local_path is None and pending_migration:
                self._log_debug(
                    f"do_sync_rom_saves({rom_id}): skipping server_only {outcome.filename} — migration pending"
                )
                continue
            if self._dispatch_sync_action(
                outcome.action,
                rom_id=rom_id,
                save_state=save_state,
                device_id=device_id,
                filename=outcome.filename,
                local_path=outcome.local_path,
                local_hash=outcome.local_hash,
                saves_dir=saves_dir,
                system=system,
                core_so=core_so,
                default_slot=default_slot,
                server_saves=outcome.server_candidates,
                sink=sink,
            ):
                synced += 1

        # Record when this sync check ran (regardless of whether files transferred)
        save_state.mark_sync_evaluated(self._clock.now().isoformat())

        self._log_debug(
            f"[TIMING] do_sync_rom_saves({rom_id}): TOTAL {self._clock.time() - t_total:.3f}s"
            f" synced={synced} errors={len(errors)}"
        )
        return synced, errors, conflicts


def _file_state_to_dict(file_state: FileSyncState) -> dict[str, Any]:
    """Project a :class:`FileSyncState` value object onto the dict shape
    ``compute_sync_action`` consumes (the legacy ``to_dict`` surface)."""
    return {
        "tracked_save_id": file_state.tracked_save_id,
        "last_sync_hash": file_state.last_sync_hash,
        "last_sync_at": file_state.last_sync_at,
        "last_sync_server_updated_at": file_state.last_sync_server_updated_at,
        "last_sync_server_save_id": file_state.last_sync_server_save_id,
        "last_sync_server_size": file_state.last_sync_server_size,
        "last_sync_local_mtime": file_state.last_sync_local_mtime,
        "last_sync_local_size": file_state.last_sync_local_size,
    }
