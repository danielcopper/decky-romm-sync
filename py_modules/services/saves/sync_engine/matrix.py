"""Newest-wins matrix executor and per-file sync I/O dispatch.

The decision layer for "which side wins for this file" plus the I/O
helpers that actually move bytes between the local saves directory and
the RomM server. Read-only matrix consumption (status reporting) lives
in StatusService; the loaded :class:`RomSaveSyncState` aggregate is threaded
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
from enum import Enum
from typing import TYPE_CHECKING, Any

from domain.emulator_tag import build_emulator_tag
from domain.iso_time import parse_iso_to_epoch
from domain.rom_save_sync_state import FileSyncState, RomSaveSyncState
from domain.save_backup import backup_name, select_backups_to_prune
from domain.save_slot import save_in_slot
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
    compute_sync_action,
    resolve_upload_conflict,
)
from lib.errors import DeviceNotRegisteredError, RommApiError, RommConflictError, classify_error
from services.saves._helpers import local_save_target
from services.saves._messages import DEVICE_NOT_REGISTERED

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


_BACKUP_RETENTION = 10  # max .romm-backup copies kept per save file (#974)


class TransferDirection(Enum):
    """Which way a dispatched save transfer actually moved (#250).

    Returned by the sync dispatch so ``sync_rom_saves`` can split the total
    transfer count into per-direction upload / download tallies for the
    completion toast. Direction reflects what moved on the wire, not the
    planned :class:`SyncAction`: an ``Upload`` that RomM rejects with a 409 and
    the backstop downgrades to a fresh-head download is attributed as
    ``DOWNLOAD``. A skip, a surfaced conflict, or a failed transfer yields no
    direction (the dispatch returns ``None``).
    """

    UPLOAD = "upload"
    DOWNLOAD = "download"


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


@dataclass(frozen=True)
class SyncRunOptions:
    """The settings-derived values threaded through one ROM's sync dispatch.

    Resolved once per sync run and constant across every file of the ROM, so
    they ride through ``_dispatch_sync_action`` / ``_dispatch_upload`` as one
    argument rather than widening every signature (mirrors ``DispatchSink``).
    ``default_slot`` seeds the active slot on a brand-new ROM's first sync and
    selects the upload slot; ``autocleanup_limit`` caps server-retained versions
    on the POST (create) upload.
    """

    default_slot: str | None = None
    autocleanup_limit: int | None = None


@dataclass(frozen=True)
class RomDispatchContext:
    """The ROM-level constants threaded through one ROM's sync dispatch.

    Built once per ``do_sync_rom_saves`` run and passed to
    ``_dispatch_sync_action`` and its sub-dispatchers, so the remaining
    per-call args stay to the values that actually vary per file (mirrors
    ``DispatchSink`` / ``SyncRunOptions``).
    """

    rom_id: int
    save_state: RomSaveSyncState
    device_id: str | None
    rom_name: str
    saves_dir: str
    system: str
    core_so: str | None


class MatrixExecutor:
    """Newest-wins matrix executor + per-file sync I/O dispatch.

    Owns every code path that reads the server save list, runs
    ``compute_sync_action`` against per-filename inputs, and dispatches
    the resulting :class:`SyncAction` to disk / server I/O. The loaded
    :class:`RomSaveSyncState` aggregate is threaded in by the public rom-level
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
        """Download a server save to temp and compute its RomM content hash.

        Used for slow-path conflict detection when the listed save carries no
        ``content_hash`` field. Hashes the downloaded copy with the zip-aware
        :meth:`SaveFileStore.content_hash` (never the whole-archive MD5) so the
        digest is comparable to a local ``content_hash`` for zip saves too —
        otherwise the keep_local adopt-without-upload check could never match a
        zip. Returns the hash string or None on non-retryable error. Raises on
        retryable errors so the caller can retry.
        """
        save_id = server_save.get("id")
        if not save_id:
            return None
        tmp_path: str | None = None
        try:
            tmp_path = self._save_file_store.make_temp_path(suffix=".tmp")
            self._romm_api.download_save(save_id, tmp_path)
            return self._save_file_store.content_hash(tmp_path)
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
        save_state: RomSaveSyncState,
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
        :meth:`RomSaveSyncState.adopt_baseline` — the server response always
        carries the tracked save id. Its ``content_hash`` (RomM's own digest of
        the bytes: the save it holds after a download, the bytes it received
        after an upload) is stored as ``last_sync_server_hash`` so the next sync's
        identity check can compare server-produced hashes; a response without one
        records ``None`` and the identity check falls back to parity (#1468).
        """
        if not save_state.system and system:
            save_state.adopt_system(system)
        if save_state.active_slot is None and not save_state.slots:
            save_state.switch_active_slot(default_slot or "default")
        save_state.record_synced_core(core_so, emulator_tag or save_state.emulator or "retroarch")

        now = self._clock.now().isoformat()
        local_exists = self._save_file_store.is_file(local_path)
        # RomM-parity content hash (zip-aware) so a zip save's baseline is on the
        # same scheme the matrix compares against server.content_hash — never the
        # whole-archive MD5, which no server content_hash could match (#1457).
        local_hash = self._save_file_store.content_hash(local_path) if local_exists else ""

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
            last_sync_server_hash=server_response.get("content_hash"),
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
        save_state: RomSaveSyncState,
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
        ``<saves_dir>/.romm-backup/<name>_<ts>[_<n>]<ext>`` (``<ts>`` from the
        injected clock). A same-second collision appends a ``_<n>`` counter so a
        multi-file slot quarantined within one second never overwrites an earlier
        backup, and the newest ``_BACKUP_RETENTION`` backups per save file are
        kept — older ones are pruned to bound the recovery net's disk use (#974).
        The backup written by this call is never pruned in its own call, so under
        sustained same-second churn the folder may briefly hold one extra copy
        (``_BACKUP_RETENTION + 1``) — destroying the just-saved file to honour the
        cap would defeat the backup. Returns ``True`` when a file was moved,
        ``False`` when there was nothing at *filename* to back up.
        """
        local_path = os.path.join(saves_dir, filename)
        if not self._save_file_store.is_file(local_path):
            return False
        backup_dir = os.path.join(saves_dir, ".romm-backup")
        self._save_file_store.make_dirs(backup_dir)
        ts = self._clock.now().strftime("%Y%m%d_%H%M%S")
        existing = set(self._save_file_store.listdir(backup_dir))
        backup = backup_name(filename, ts, existing)
        self._save_file_store.rename(local_path, os.path.join(backup_dir, backup))
        # Bound the recovery net: keep only the newest N backups per save file (#974).
        existing.add(backup)
        for stale in select_backups_to_prune(filename, list(existing), _BACKUP_RETENTION):
            if stale == backup:
                continue  # never prune the backup just created this call (#974 — would destroy the save)
            self._save_file_store.remove_file(os.path.join(backup_dir, stale))
        return True

    @staticmethod
    def _resolve_upload_slot(save_state: RomSaveSyncState, default_slot: str | None = None) -> str | None:
        """The slot field to send with an upload; ``None`` only for an explicit-legacy save.

        Reached only once :meth:`do_upload_save` has confirmed a registered
        ``device_id`` (a missing one refuses the upload outright, #1478), so the
        slot resolves purely from the aggregate's slot state. A named
        ``active_slot`` uploads to that slot; an ``active_slot`` of ``None`` is
        ambiguous and is disambiguated by the ``slots`` map (the same signal
        :meth:`update_file_sync_state` uses to seed the active slot):

        - **Explicit legacy** (``active_slot`` None but ``slots`` populated — the
          state after switching to / confirming the legacy slot) → ``None`` so the
          save is POSTed as ``slot:null``. Returning ``"default"`` here misfiled a
          legacy save into the default slot (#1061).
        - **Brand-new ROM** (``active_slot`` None and ``slots`` empty — never
          configured) → the configured ``default_slot`` so its first sync lands in
          the default slot, matching the active-slot seeding.
        """
        if save_state.active_slot is not None:
            return save_state.active_slot
        if save_state.slots:
            return None
        return default_slot or "default"

    def _confirm_upload_sync(self, response: dict[str, Any], device_id: str | None) -> None:
        """Ack the uploaded save on the server's DeviceSaveSync row, unless redundant.

        ``add_save`` (POST) and ``update_save`` (PUT) both upsert this device's
        DeviceSaveSync row (``synced_at = updated_at``) on every supported RomM
        version and serialize it into the response's ``device_syncs`` — so the
        ack is redundant on the normal upload path and is skipped when *response*
        already shows this device ``is_current`` (one fewer round-trip per
        uploaded file, #1458). The one path that still needs it is ``add_save``'s
        content-dedup early-return: a named-slot ``overwrite=false`` POST whose
        content matches an existing save returns *before* the upsert, reporting
        ``is_current=false``, so the ack is the only writer of our sync row there.
        Fail-open — any non-current / missing / unexpected response confirms.
        Non-fatal: a failed ack is debug-logged and swallowed.
        """
        upload_id = response.get("id")
        if not device_id or not upload_id:
            return
        if _upload_response_proves_current(response, device_id):
            self._log_debug(f"confirm_download after upload skipped for save {upload_id} (already current)")
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
        save_state: RomSaveSyncState,
        device_id: str | None,
        system: str,
        core_so: str | None,
        server_save: dict[str, Any] | None = None,
        default_slot: str | None = None,
        autocleanup_limit: int | None = None,
        overwrite: bool = False,
        planned_group: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Upload a local save file to server.

        Mutates *save_state* in memory (per-file baseline, own-upload
        attribution, local→server slot promotion); the operation entry owns
        the surrounding write Unit of Work. *core_so* is the active core
        resolved once by the caller so this worker stays free of installed-rom
        reads. *autocleanup_limit* caps server-retained versions — the adapter
        honors it on the POST (create) path only, so PUT callers leave it None.
        *overwrite* forces RomM's ``add_save`` to skip its write-time currency
        (409) gate — used only when the caller has already decided our content
        must win (the ``keep_local`` conflict resolution); the automatic sync
        dispatch leaves it ``False`` so the 409 backstop can catch a stale-current
        race (ADR-0017).

        *planned_group* is the automatic POST path's pre-upload ``list_saves``
        snapshot for this file (``None`` for the PUT / explicit-action callers,
        who never dedup): when the POST response deduped to a pre-existing
        non-head save (:func:`_dedup_returned_non_head`, #1482), this raises
        ``RommConflictError`` BEFORE any baseline / own-upload / confirm write so
        the same 409 backstop that catches a write-time stale-current race
        surfaces the true state — no false "synced", no currency stamped on the
        non-head response.

        A missing *device_id* refuses the upload outright (raises
        :class:`DeviceNotRegisteredError` before any server call, #1478): the
        RomM >= 4.9 floor makes device registration the norm, so a falsy id no
        longer means "device sync off" — uploading without it would drop the slot
        field and land a named-slot save in the legacy (``slot:null``) bucket, the
        migration-005 retirement violation that seeds the #1478 corruption. The
        raise reaches every caller's error funnel (the sync dispatch's per-file
        errors, rollback / version-switch surfaced failures) so the file is
        reported, not silently misfiled.
        """
        if not device_id:
            raise DeviceNotRegisteredError(DEVICE_NOT_REGISTERED)

        save_id = server_save.get("id") if server_save else None
        emulator = build_emulator_tag(core_so)

        slot = self._resolve_upload_slot(save_state, default_slot)

        result = self._retry.with_retry(
            lambda: self._romm_api.upload_save(
                int(rom_id),
                file_path,
                emulator,
                save_id,
                device_id=device_id,
                slot=slot,
                overwrite=overwrite,
                autocleanup_limit=autocleanup_limit,
            )
        )

        if planned_group is not None and _dedup_returned_non_head(result, planned_group):
            # Dedup to a non-head save: RomM created no new version and the
            # foreign head still leads the slot, so recording this response as a
            # synced baseline would falsely report the upload landed (#1482).
            # Route it through the write-time 409 backstop — the raise reaches
            # ``_dispatch_upload``'s ``except RommConflictError`` before any
            # baseline / confirm / own-upload write runs, so nothing stamps
            # currency on the non-head response.
            raise RommConflictError(
                "add_save deduped to a non-head save; the slot head is newer",
                method="POST",
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

        self._confirm_upload_sync(result, device_id)

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
        save_state: RomSaveSyncState,
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
        *,
        ctx: RomDispatchContext,
        filename: str,
        local_path: str | None,
        local_hash: str | None,
        last_sync_hash: str | None,
        last_sync_server_hash: str | None,
        server_candidates: list[dict[str, Any]],
        options: SyncRunOptions,
        sink: DispatchSink,
    ) -> TransferDirection | None:
        """Execute an ``Upload`` action by POSTing a new save, with a 409 backstop.

        Every automatic upload POSTs a new save in the slot (``server_save=None``,
        ``overwrite=False``); ``Upload.target_save_id`` no longer selects a PUT
        (ADR-0017). The POST is planned against a ``list_saves`` snapshot
        (*server_candidates*) that can be stale by the time it lands — another
        device may have moved the slot head past our last sync in between. Two
        signals route to the same backstop: RomM rejects the stale POST with a
        409, and RomM's content-dedup can early-return a pre-existing non-head
        save (:func:`_dedup_returned_non_head`, #1482) that ``do_upload_save``
        turns into a ``RommConflictError``. Either way :meth:`_handle_upload_409`
        re-fetches the slot and lets ``resolve_upload_conflict`` decide from
        hashes alone (download the fresh head when local is provably unchanged,
        else surface a conflict). Any other ``RommApiError`` propagates to the
        generic handler in :meth:`_dispatch_sync_action`. Returns the transfer
        direction that was issued (``UPLOAD`` on a clean POST, ``DOWNLOAD`` when
        the backstop downgrades), or ``None`` when nothing transferred.
        """
        if local_path is None:
            self._logger.warning(
                f"_dispatch_upload({ctx.rom_id}): {filename}: upload requested but no local file on disk"
            )
            sink.errors.append(f"{filename}: upload requested but no local file")
            return None
        try:
            # POST (create) a new save in the slot. The retention cap is POST-only —
            # RomM stacks versions on create, so the limit is sent here alone.
            # ``planned_group`` lets the POST catch a dedup-to-non-head response
            # (#1482) and route it through the same 409 backstop below.
            self.do_upload_save(
                ctx.rom_id,
                local_path,
                filename,
                ctx.save_state,
                ctx.device_id,
                ctx.system,
                ctx.core_so,
                None,
                options.default_slot,
                autocleanup_limit=options.autocleanup_limit,
                overwrite=False,
                planned_group=server_candidates,
            )
            return TransferDirection.UPLOAD
        except RommConflictError:
            return self._handle_upload_409(
                ctx=ctx,
                filename=filename,
                local_path=local_path,
                local_hash=local_hash,
                last_sync_hash=last_sync_hash,
                last_sync_server_hash=last_sync_server_hash,
                options=options,
                sink=sink,
            )

    def _handle_upload_409(
        self,
        *,
        ctx: RomDispatchContext,
        filename: str,
        local_path: str,
        local_hash: str | None,
        last_sync_hash: str | None,
        last_sync_server_hash: str | None,
        options: SyncRunOptions,
        sink: DispatchSink,
    ) -> TransferDirection | None:
        """Re-decide an upload the POST could not land as a new slot head.

        Reached from two signals that mean the same thing — the slot head is not
        where our POST assumed: RomM's write-time 409 (the head moved past our
        last sync since the ``list_saves`` snapshot that planned the POST), and a
        dedup-to-non-head early-return the POST turned into a ``RommConflictError``
        (#1482, RomM created no new version and an older save came back while a
        newer head still leads). Re-fetch the slot, regroup to
        this file's canonical target (the same grouping
        :meth:`iter_matrix_outcomes` uses), pick the newest, and let
        ``resolve_upload_conflict`` decide purely from hashes: a provably-unchanged
        local downloads the fresh head; anything else is a genuine two-sided
        divergence the user must resolve. An empty regroup (the head vanished
        again between the 409 and the re-fetch) is recorded as a non-fatal error.
        Returns ``DOWNLOAD`` when the backstop pulled the fresh head, else ``None``
        (empty regroup or surfaced conflict — nothing transferred).
        """
        server_saves = self._retry.with_retry(lambda: self._romm_api.list_saves(ctx.rom_id, device_id=ctx.device_id))
        server_in_slot = self.filter_server_saves_to_slot(server_saves, ctx.save_state.active_slot)
        group = [ss for ss in server_in_slot if local_save_target(ss, ctx.rom_name) == filename]
        if not group:
            self._logger.warning(
                f"_handle_upload_409({ctx.rom_id}): {filename}: 409 on POST but no server save in slot on re-fetch"
            )
            sink.errors.append(f"{filename}: upload rejected by server and no server save found to reconcile")
            return None
        fresh = max(group, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
        if (
            resolve_upload_conflict(local_hash, last_sync_hash, fresh.get("content_hash"), last_sync_server_hash)
            == "download"
        ):
            self.do_download_save(
                fresh, ctx.saves_dir, filename, ctx.save_state, ctx.device_id, ctx.system, options.default_slot
            )
            return TransferDirection.DOWNLOAD
        sink.conflicts.append(self.build_sync_conflict_entry(ctx.rom_id, filename, fresh, local_path, local_hash))
        return None

    def _dispatch_sync_action(
        self,
        action: object,
        *,
        ctx: RomDispatchContext,
        filename: str,
        local_path: str | None,
        local_hash: str | None,
        last_sync_hash: str | None,
        last_sync_server_hash: str | None,
        server_candidates: list[dict[str, Any]],
        options: SyncRunOptions,
        sink: DispatchSink,
    ) -> TransferDirection | None:
        """Execute one ``SyncAction`` outcome and report the transfer direction.

        Centralises the I/O dispatch so ``sync_rom_saves`` stays declarative.
        Errors are caught and pushed onto ``sink.errors`` so a single failure
        can't abort the whole rom-level sync; conflicts land on
        ``sink.conflicts``. ``ctx.rom_name`` + the stored baseline pair
        (``last_sync_hash`` / ``last_sync_server_hash``) + this file's
        ``server_candidates`` (the pre-upload snapshot, for the dedup-to-non-head
        guard) are threaded through for the upload 409 backstop (canonical-target
        regroup + hash re-decision). Returns the :class:`TransferDirection` a
        transfer moved, or ``None`` for a skip, a surfaced conflict, or a failed
        transfer.
        """
        try:
            if isinstance(action, Skip):
                self._dispatch_skip(
                    action,
                    rom_id=ctx.rom_id,
                    save_state=ctx.save_state,
                    filename=filename,
                    local_hash=local_hash,
                )
                return None
            if isinstance(action, Upload):
                return self._dispatch_upload(
                    ctx=ctx,
                    filename=filename,
                    local_path=local_path,
                    local_hash=local_hash,
                    last_sync_hash=last_sync_hash,
                    last_sync_server_hash=last_sync_server_hash,
                    server_candidates=server_candidates,
                    options=options,
                    sink=sink,
                )
            if isinstance(action, Download):
                self.do_download_save(
                    action.server_save,
                    ctx.saves_dir,
                    filename,
                    ctx.save_state,
                    ctx.device_id,
                    ctx.system,
                    options.default_slot,
                )
                return TransferDirection.DOWNLOAD
            if isinstance(action, Conflict):
                sink.conflicts.append(
                    self.build_sync_conflict_entry(ctx.rom_id, filename, action.server_save, local_path, local_hash)
                )
                return None
        except DeviceNotRegisteredError:
            # Precondition refusal, not a transfer error: surface the same
            # device-not-registered message the automatic pre-flight uses so the
            # per-file error reads consistently (#1478). No slug field exists on a
            # per-file sync error (the list is message-only); the reason slug is
            # carried where a failure dict has one — the keep_local resolve path.
            self._logger.warning(f"_dispatch_sync_action({ctx.rom_id}): {filename}: {DEVICE_NOT_REGISTERED}")
            sink.errors.append(f"{filename}: {DEVICE_NOT_REGISTERED}")
        except RommApiError as e:
            _code, _msg = classify_error(e)
            self._logger.warning(f"_dispatch_sync_action({ctx.rom_id}): {filename} failed: {_msg}")
            sink.errors.append(f"{filename}: {_msg}")
        except Exception as e:
            self._logger.warning(f"_dispatch_sync_action({ctx.rom_id}): {filename} unexpected error: {e}")
            self._handle_unexpected_error(e, filename, ctx.saves_dir, sink.errors)
        return None

    def adopt_baseline_hash(self, save_state: RomSaveSyncState, filename: str, local_hash: str) -> None:
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
        save_state: RomSaveSyncState | None,
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
            # RomM-parity content hash (zip-aware) — the kernel compares this to
            # server.content_hash for its byte-identity checks, so a zip save
            # must be hashed per-entry, not as the whole archive (#1457).
            local_hash = self._save_file_store.content_hash(local_path) if local_exists else None
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
        save_state: RomSaveSyncState,
        device_id: str | None,
        core_so: str | None,
        default_slot: str | None = None,
        autocleanup_limit: int | None = None,
    ) -> tuple[int, int, list[str], list[dict[str, Any]]]:
        """Sync saves for a single ROM, mutating *save_state* in memory.

        Drives :meth:`iter_matrix_outcomes` and dispatches each emitted
        outcome through :meth:`_dispatch_sync_action`. Returns
        ``(uploaded_count, downloaded_count, errors_list, conflicts_list)`` — the
        two directional counts split out of the old aggregate transfer count so
        the completion toast can name which way saves moved (#250); a 409-backstop
        download counts as a download, not an upload. *core_so* is the active core
        resolved once by the caller (for the upload emulator tag); *default_slot*
        seeds the active slot when a brand-new ROM's first sync lands;
        *autocleanup_limit* caps server-retained versions on the POST (create)
        upload; the operation entry owns the surrounding read/write Unit of Work.
        """
        t_total = self._clock.time()
        rom_id = int(rom_id)

        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            self._log_debug(f"do_sync_rom_saves({rom_id}): no save info, skipping")
            return 0, 0, [], []
        system = info["system"]
        saves_dir = info["saves_dir"]

        t0 = self._clock.time()
        try:
            server_saves = self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id))
        except Exception as e:
            self._logger.error(f"do_sync_rom_saves({rom_id}): failed to list saves: {e}")
            _code, _msg = classify_error(e)
            return 0, 0, [f"Failed to fetch saves: {_msg}"], []
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
        options = SyncRunOptions(default_slot=default_slot, autocleanup_limit=autocleanup_limit)
        ctx = RomDispatchContext(
            rom_id=rom_id,
            save_state=save_state,
            device_id=device_id,
            rom_name=info["rom_name"],
            saves_dir=saves_dir,
            system=system,
            core_so=core_so,
        )
        uploaded = 0
        downloaded = 0

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
            direction = self._dispatch_sync_action(
                outcome.action,
                ctx=ctx,
                filename=outcome.filename,
                local_path=outcome.local_path,
                local_hash=outcome.local_hash,
                last_sync_hash=outcome.file_state.last_sync_hash,
                last_sync_server_hash=outcome.file_state.last_sync_server_hash,
                server_candidates=outcome.server_candidates,
                options=options,
                sink=sink,
            )
            if direction is TransferDirection.UPLOAD:
                uploaded += 1
            elif direction is TransferDirection.DOWNLOAD:
                downloaded += 1

        # Record when this sync check ran (regardless of whether files transferred)
        save_state.mark_sync_evaluated(self._clock.now().isoformat())

        self._log_debug(
            f"[TIMING] do_sync_rom_saves({rom_id}): TOTAL {self._clock.time() - t_total:.3f}s"
            f" uploaded={uploaded} downloaded={downloaded} errors={len(errors)}"
        )
        return uploaded, downloaded, errors, conflicts


def _dedup_returned_non_head(response: dict[str, Any], planned_group: list[dict[str, Any]]) -> bool:
    """Whether an ``add_save`` POST deduped to a pre-existing NON-head save (#1482).

    RomM's ``add_save`` content-dedup can early-return an EXISTING save whose
    content matches the upload instead of creating a new version. When that
    returned save is not the slot head the run planned against — an older save
    while a newer, different head still leads the slot — the upload intent (make
    our content the slot head) went silently unfulfilled: no new version, the
    foreign head stays authoritative, yet the response looks like a successful
    upload. Recording it as a synced baseline would falsely report "synced".

    Detected purely from ids against *planned_group* (the pre-upload
    ``list_saves`` snapshot for this file, the same group ``compute_sync_action``
    evaluated): the response is a member of that snapshot (an existing save, not
    a freshly minted version whose id is absent from it) that is not the newest
    save in it. Benign — today's baseline write stands — for an empty planned
    slot (no head to bypass, e.g. an empty-slot race whose only signal is the
    dedup response itself), a freshly created version, or the head coming back.
    """
    response_id = response.get("id")
    if response_id is None or not planned_group:
        return False
    head = max(planned_group, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)
    group_ids = {s.get("id") for s in planned_group}
    return response_id in group_ids and response_id != head.get("id")


def _upload_response_proves_current(response: dict[str, Any], device_id: str) -> bool:
    """Whether *response* proves *device_id* already holds the current save.

    RomM's ``add_save`` / ``update_save`` upsert the uploading device's
    DeviceSaveSync row (``synced_at = updated_at``) and serialize it into the
    response's ``device_syncs`` with ``is_current=true`` — except ``add_save``'s
    content-dedup early-return, which returns the pre-existing save (its stale
    row, or a synthesized ``is_current=false`` placeholder) *before* the upsert.
    Reads the same ``device_syncs`` shape ``domain.sync_action`` reads. Fail-open:
    only an entry for *device_id* whose ``is_current`` is exactly ``True`` counts;
    any missing / non-list / otherwise-unexpected shape returns ``False`` so the
    caller still confirms.
    """
    device_syncs = response.get("device_syncs")
    if not isinstance(device_syncs, list):
        return False
    return any(
        isinstance(ds, dict) and ds.get("device_id") == device_id and ds.get("is_current") is True
        for ds in device_syncs
    )


def _file_state_to_dict(file_state: FileSyncState) -> dict[str, Any]:
    """Project a :class:`FileSyncState` value object onto the dict shape
    ``compute_sync_action`` consumes (the legacy ``to_dict`` surface)."""
    return {
        "tracked_save_id": file_state.tracked_save_id,
        "last_sync_hash": file_state.last_sync_hash,
        "last_sync_server_hash": file_state.last_sync_server_hash,
        "last_sync_at": file_state.last_sync_at,
        "last_sync_server_updated_at": file_state.last_sync_server_updated_at,
        "last_sync_server_save_id": file_state.last_sync_server_save_id,
        "last_sync_server_size": file_state.last_sync_server_size,
        "last_sync_local_mtime": file_state.last_sync_local_mtime,
        "last_sync_local_size": file_state.last_sync_local_size,
    }
