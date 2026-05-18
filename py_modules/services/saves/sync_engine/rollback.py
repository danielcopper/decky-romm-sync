"""Conflict-resolution rollback paths.

Anything that commits one side of a true two-sided sync conflict (the
``Conflict`` outcome from the newest-wins matrix) lives here. The
``keep_local`` / ``use_server`` decision is a one-shot rollback to the
chosen side: it canonicalises the on-disk filename, writes the picked
content, and synchronises file-tracking state with the choice. Public-
callable async orchestration (lock acquisition, server-head fetch,
freshness check) is also driven from here; the rom-level lock itself
lives on :class:`services.saves.sync_engine.engine.SyncEngine` so that
every save-sync entry point shares one queue. The multi-version
timeline rollback flow (older save versions) lives in
:class:`services.saves.versions.VersionsService`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from domain.save_path import sanitize_save_filename
from domain.save_state import FileSyncState
from lib.errors import classify_error
from lib.iso_time import parse_iso_to_epoch
from services.saves._helpers import _local_save_target

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        Clock,
        DebugLogger,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService
    from services.saves.sync_engine.matrix import MatrixExecutor


class RollbackOrchestrator:
    """Commit one side of a true sync conflict by rolling to the chosen content.

    Owns the user-driven "pick a side" flow that follows a
    :class:`Conflict` matrix outcome. Lock acquisition is the caller's
    responsibility (:class:`SyncEngine` holds the per-rom lock dict);
    everything else — filename validation, server-head freshness check,
    use_server/keep_local dispatch, post-write state sync — lives here.
    Calls into :class:`MatrixExecutor` for the actual download/upload
    I/O and server-hash probe.
    """

    def __init__(
        self,
        *,
        state_svc: StateService,
        rom_info: RomInfoService,
        romm_api: RommSyncApi,
        matrix: MatrixExecutor,
        retry: RetryStrategy,
        clock: Clock,
        save_file_store: SaveFileStore,
        logger: logging.Logger,
        log_debug: DebugLogger,
    ) -> None:
        self._state_svc = state_svc
        self._rom_info = rom_info
        self._romm_api = romm_api
        self._matrix = matrix
        self._retry = retry
        self._clock = clock
        self._save_file_store = save_file_store
        self._logger = logger
        self._log_debug = log_debug

    async def resolve(
        self,
        rom_id: int,
        filename: str,
        server_save_id: int,
        action: str,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> dict:
        """Drive the post-lock conflict-resolution flow.

        ``loop`` is passed per call so :class:`SyncEngine` can hand its
        live (test-rebindable) ``_loop`` attribute through without this
        orchestrator caching a stale reference. The rom-level lock must
        be held by the caller — every save-sync entry point serialises
        through ``SyncEngine._rom_lock(rom_id)``.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if action not in ("keep_local", "use_server"):
            return {"success": False, "message": f"Invalid action: {action}"}

        validation_error = self._validate_filename(rom_id, action, filename)
        if validation_error:
            return validation_error

        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            return {"success": False, "message": "ROM not installed"}
        system = info["system"]
        saves_dir = info["saves_dir"]

        try:
            device_id = self._state_svc.get_server_device_id()
            server_saves = await loop.run_in_executor(
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
        server_in_slot = self._matrix.filter_server_saves_to_slot(server_saves, active_slot)
        if not server_in_slot:
            return {"success": False, "message": "No server save in active slot"}
        server = max(server_in_slot, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)

        actual_server_id = server.get("id")
        if actual_server_id != server_save_id:
            self._logger.warning(
                "resolve_sync_conflict(rom_id=%d, action=%s) stale: client server_save_id=%s, head=%s",
                rom_id,
                action,
                server_save_id,
                actual_server_id,
            )
            return {
                "success": False,
                "error_code": "stale_conflict",
                "message": "Server save changed since conflict was shown; please retry sync.",
            }

        try:
            if action == "use_server":
                await loop.run_in_executor(
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
            await loop.run_in_executor(
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

    def _validate_filename(self, rom_id: int, action: str, filename: str) -> dict | None:
        """Reject non-basename filenames; ``None`` if the filename is safe.

        The frontend-supplied filename flows into
        ``os.path.join(saves_dir, …)`` via the keep_local path. Reject
        anything that isn't already a clean basename — legitimate callers
        always pass one.
        """
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
        return None

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
        ``update_file_sync_state`` receives the same target name the file
        lands at.
        """
        target = _local_save_target(server, rom_name)
        self._matrix.do_download_save(server, saves_dir, target, rom_id_str, system)
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
        ``<rom_name>.<server.file_extension>`` rule
        :meth:`_resolve_conflict_use_server` and every other download path uses.
        This keeps the two resolve paths symmetric: the state key and on-disk
        path are identical regardless of which side the user picked. If the
        local file is not at the canonical path (e.g. ``Mario.sav`` locally
        but the server save has ``file_extension=srm``),
        :class:`FileNotFoundError` is raised — we never silently rename across
        extensions.
        """
        target = _local_save_target(server, rom_name)
        local_path = os.path.join(saves_dir, target)
        if not self._save_file_store.is_file(local_path):
            raise FileNotFoundError(f"Local save not found: {local_path}")
        local_hash = self._save_file_store.checksum_md5(local_path)
        try:
            server_hash = self._retry.with_retry(lambda: self._matrix.get_server_save_hash(server))
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
            file_state.last_sync_local_mtime = self._save_file_store.get_mtime(local_path)
            file_state.last_sync_local_size = self._save_file_store.get_size(local_path)
            self._state_svc.save_state()
            return

        # Upload local content as a PUT against the existing server save.
        self._matrix.do_upload_save(rom_id, local_path, target, rom_id_str, system, server)
