"""Save version history reads and rollback orchestration.

Coordinates the rollback flow (pre-flight sync, version pick, atomic
switch) but does not perform the actual file or server writes — those
go through SyncEngine / LocalSavesAdapter. Anything that lists,
fetches, or rolls back to an older save version lives here. Mutations
of the active save record outside the rollback flow (conflict
resolution, status reporting) belong in SyncEngine or StatusService.
Persistence is the operation's own narrow Unit of Work (ADR-0006).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from domain.rom_save_state import RomSaveState
from services.saves._helpers import local_save_target
from services.saves._settings import resolve_default_slot

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import DebugLogger, RetryStrategy, RommSaveApi, UnitOfWorkFactory
    from services.saves.rom_info import RomInfoService
    from services.saves.sync_engine import SyncEngine


@dataclass(frozen=True)
class VersionsServiceConfig:
    """Frozen wiring bundle handed to ``VersionsService.__init__``.

    Holds the live ``settings.json`` dict (default-slot seeding), the
    Unit-of-Work factory (the transactional seam over the SQLite
    repositories), the peer save sub-services consumed during rollback
    orchestration (sync_engine, rom_info), the core resolver used to
    stamp the upload emulator tag, the Protocol-typed RomM adapter and
    retry strategy, the plugin event loop, the standard-library logger,
    and the ``DebugLogger`` seam.
    """

    settings: dict
    uow_factory: UnitOfWorkFactory
    sync_engine: SyncEngine
    rom_info: RomInfoService
    resolve_core: Callable[[int], str | None]
    romm_api: RommSaveApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    log_debug: DebugLogger


class VersionsService:
    """Aggregate root for the module's contract.

    Per-ROM lock acquisition is delegated to the injected ``SyncEngine``;
    save-state persistence is the operation's own narrow Unit of Work. This
    class owns the rollback orchestration on top of them.
    """

    def __init__(self, *, config: VersionsServiceConfig) -> None:
        self._config = config
        self._settings = config.settings
        self._uow_factory = config.uow_factory
        self._sync_engine = config.sync_engine
        self._rom_info = config.rom_info
        self._resolve_core = config.resolve_core
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._loop = config.loop
        self._logger = config.logger
        self._log_debug = config.log_debug

    # ------------------------------------------------------------------
    # Narrow-UoW read/write helpers (ADR-0006)
    # ------------------------------------------------------------------

    def _read_inputs(self, rom_id: int) -> tuple[RomSaveState, str | None]:
        with self._uow_factory() as uow:
            state = uow.rom_save_states.get(rom_id) or RomSaveState()
            device_id = uow.kv_config.get("device_id")
        return state, device_id

    def _write_save_state(self, rom_id: int, save_state: RomSaveState) -> None:
        with self._uow_factory() as uow:
            uow.rom_save_states.save(rom_id, save_state)

    # ------------------------------------------------------------------
    # Version History API
    # ------------------------------------------------------------------

    async def list_file_versions(self, rom_id: int, slot: str, filename: str) -> dict:
        """List server-side saves in the active slot, excluding the currently-tracked one.

        The slot is the unit, not the filename. Saves uploaded by other
        clients (RomM web UI, third-party clients, etc.) whose naming
        convention differs from ours are first-class versions of the same
        slot, so no filename filter is applied — every save in the slot
        except the one we're currently tracking shows up here.

        ``filename`` is kept in the signature for compatibility with the
        callable wiring but no longer affects which versions are returned.

        Returns a status dict:
        - ``{"status": "ok", "versions": [...]}`` on success. ``versions``
          is sorted by ``updated_at`` descending (newest first); each entry
          contains: id, file_name, emulator, updated_at, file_size_bytes,
          device_syncs, uploaded_by_us. ``versions`` may be empty — the
          server answered, nothing matched.
        - ``{"status": "server_unreachable", "message": ...}`` if the
          ``list_saves`` call failed (network, server, auth, etc.). The
          frontend distinguishes this from an empty list so it can show a
          retry affordance instead of "no versions available".
        """
        rom_id = int(rom_id)
        save_state, device_id = await self._loop.run_in_executor(None, self._read_inputs, rom_id)

        try:
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(
                    lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot if slot else None)
                ),
            )
        except Exception as e:
            self._log_debug(f"list_file_versions: failed to list saves: {e}")
            return {"status": "server_unreachable", "message": str(e)}

        file_state = save_state.files.get(filename)
        tracked_id = file_state.tracked_save_id if file_state else None
        own_upload_ids: list[int] | None = save_state.own_upload_ids

        versions = [
            {
                "id": s["id"],
                "file_name": s.get("file_name", ""),
                "emulator": s.get("emulator"),
                "updated_at": s.get("updated_at", ""),
                "file_size_bytes": s.get("file_size_bytes"),
                "device_syncs": s.get("device_syncs", []),
                "uploaded_by_us": (s["id"] in own_upload_ids) if own_upload_ids is not None else None,
            }
            for s in server_saves
            if s.get("id") != tracked_id
        ]

        versions.sort(key=lambda v: v["updated_at"], reverse=True)
        return {"status": "ok", "versions": versions}

    def _rollback_to_version_io(
        self,
        rom_id: int,
        save_state: RomSaveState,
        device_id: str | None,
        core_so: str | None,
        save_id: int,
        info: dict,
        server_saves: list[dict],
    ) -> dict:
        """Blocking I/O portion of the version-switch flow — runs in executor.

        The caller is responsible for the matrix pre-flight: by the time
        this function runs, the currently-tracked save is already in sync
        with the server (or the switch was aborted before we got here).
        This function is purely the destructive switch:

        1. Download id=save_id content → overwrite local file.
           ``do_download_save`` updates ``tracked_save_id`` /
           ``last_sync_hash`` to point at the target version locally.
        2. PUT id=save_id with the same content. RomM v4.8.1 fires the
           SQLAlchemy ``onupdate=utc_now`` hook, so ``save.updated_at``
           becomes NOW and id=save_id is now newest in the slot — beating
           anything else there.
        3. ``do_upload_save`` calls ``confirm_download(save_id, device_id)``,
           setting our ``last_synced_at = save.updated_at`` so
           ``is_current`` evaluates true for us. Required because v4.8.1
           PUT does NOT auto-upsert sync rows.
        4. ``do_upload_save`` refreshes local sync state via
           ``update_file_sync_state`` to match the post-PUT response.

        After this, the next ``compute_sync_action`` run picks id=save_id
        (now newest), our ``is_current=true``, hash matches →
        ``Skip(synced)``. Other devices on their next sync see id=save_id
        as newest with their ``is_current=false`` → ``Download`` → adopt
        our switch. Cross-device propagation works. Mutates *save_state* in
        memory; the caller owns the write UoW.
        """
        target_save = next(
            (s for s in server_saves if s.get("id") == save_id),
            None,
        )
        if target_save is None:
            return {"status": "version_deleted"}

        saves_dir = info["saves_dir"]
        system = info["system"]
        rom_name = info["rom_name"]
        default_slot = resolve_default_slot(self._settings)
        target_filename = local_save_target(target_save, rom_name)
        local_path = os.path.join(saves_dir, target_filename)

        self._sync_engine.do_download_save(
            target_save, saves_dir, target_filename, save_state, device_id, system, default_slot
        )

        try:
            self._sync_engine.do_upload_save(
                rom_id,
                local_path,
                target_filename,
                save_state,
                device_id,
                system,
                core_so,
                target_save,
                default_slot,
            )
        except Exception as e:
            # Download already mutated local state to reflect ``save_id``, so
            # the switch is locally complete — but cross-device propagation
            # failed because ``updated_at`` was not bumped. Surface this so
            # the caller can prompt the user to retry.
            self._logger.error(
                "_rollback_to_version_io: PUT to bump updated_at failed for rom=%s save=%s: %s",
                rom_id,
                save_id,
                e,
            )
            return {"status": "put_failed", "message": str(e)}

        return {"status": "ok"}

    async def rollback_to_version(self, rom_id: int, slot: str, save_id: int) -> dict:
        """Switch the local + tracked save to a chosen older server version.

        Flow:

        1. Run ``do_sync_rom_saves`` as a matrix pre-flight on the
           currently-tracked save. The matrix decides:

           - ``Skip(synced)`` / ``Skip(adopt_baseline=True)`` — proceed.
           - ``Upload(POST/PUT)`` — silently push local up, then proceed.
           - ``Download(server)`` — silently adopt the server-newest, then
             proceed (the user's chosen target is still in the slot).
           - ``Conflict`` — abort with ``conflict_blocked``; user must
             resolve via the standard ``SyncConflictModal`` first.

        2. After a clean pre-flight, the destructive switch runs in
           ``_rollback_to_version_io``: download chosen → write to
           canonical local target → PUT same content → ``confirm_download``.

        ``filename`` is kept in the signature for callable-wiring stability
        but no longer drives any decision — the canonical local path is
        derived from the target save and the ROM name.

        Returns a status dict:
        - ``{"status": "ok"}`` on success.
        - ``{"status": "rom_not_installed"}`` if the ROM is not installed
          locally. The frontend distinguishes this from
          ``version_deleted`` so it can prompt the user to reinstall the
          ROM rather than telling them the version is gone from the
          server.
        - ``{"status": "version_deleted"}`` if the chosen save id is no
          longer on the server (genuinely deleted — the ``list_saves``
          call succeeded and the id was absent).
        - ``{"status": "server_unreachable", "message": ...}`` if the
          post-preflight ``list_saves`` call failed (network, server,
          auth, etc.). The frontend distinguishes this from
          ``version_deleted`` so it can show a retry affordance instead
          of "version no longer on the server".
        - ``{"status": "conflict_blocked", "conflicts": [...]}`` if the
          pre-flight surfaced a conflict on the currently-tracked save.
          The frontend resolves it via the standard conflict modal.
        - ``{"status": "preflight_failed", "errors": [...]}`` if the
          pre-flight hit non-conflict errors (network, server, etc.).
          No switch was attempted.
        - ``{"status": "put_failed", "message": ...}`` if the local
          download succeeded but the server-side ``updated_at`` bump
          failed. Local file and state already point at the target;
          retrying is safe and idempotent. Without a successful re-PUT
          the switch will not propagate cross-device.
        """
        rom_id = int(rom_id)
        save_id = int(save_id)

        async with self._sync_engine.rom_lock(rom_id):
            info = self._rom_info.get_rom_save_info(rom_id)
            if not info:
                return {"status": "rom_not_installed"}

            save_state, device_id = await self._loop.run_in_executor(None, self._read_inputs, rom_id)
            core_so = await self._loop.run_in_executor(None, self._resolve_core, rom_id)
            default_slot = resolve_default_slot(self._settings)

            # Matrix pre-flight: get the tracked save in sync first, or surface
            # a conflict that the user must resolve before any switch can run.
            _synced, errors, conflicts = await self._loop.run_in_executor(
                None, self._sync_engine.do_sync_rom_saves, rom_id, save_state, device_id, core_so, default_slot
            )
            if conflicts:
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {
                    "status": "conflict_blocked",
                    "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
                }
            if errors:
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"status": "preflight_failed", "errors": errors}

            # Re-fetch server saves after the pre-flight: it may have created
            # or modified saves the switch needs to see.
            try:
                server_saves: list[dict] = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id, slot=slot if slot else None)
                    ),
                )
            except Exception as e:
                self._log_debug(f"rollback_to_version: failed to list saves: {e}")
                # Persist whatever the pre-flight mutated before bailing.
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
                return {"status": "server_unreachable", "message": str(e)}

            result = await self._loop.run_in_executor(
                None,
                self._rollback_to_version_io,
                rom_id,
                save_state,
                device_id,
                core_so,
                save_id,
                info,
                server_saves,
            )

            await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)

            return result
