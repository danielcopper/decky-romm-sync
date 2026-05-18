"""Save-sync aggregate root and facade for the Decky callable surface.

Composes the save-sync sub-services (state, sync_engine, status, versions,
slots, rom_info) over the shared :class:`SaveSyncState` aggregate and
exposes the public methods the frontend reaches through callables. Most
methods are thin delegations; orchestration that genuinely spans multiple
sub-services lives here, single-sub-service logic does not.
"""

from __future__ import annotations

from domain.save_state import SaveSyncState
from services.saves._config import SaveServiceConfig
from services.saves.rom_info import RomInfoService, RomInfoServiceConfig
from services.saves.slots import SlotsService, SlotsServiceConfig
from services.saves.slots.service import _NO_MIGRATION
from services.saves.state import StateService, StateServiceConfig
from services.saves.status import StatusService, StatusServiceConfig
from services.saves.sync_engine import SyncEngine, SyncEngineConfig
from services.saves.versions import VersionsService, VersionsServiceConfig


class SaveService:
    """Aggregate root for bidirectional save file sync between RetroDECK and RomM.

    Owns the live :class:`SaveSyncState` aggregate (via :class:`StateService`)
    and composes the save-sync sub-services (sync_engine, status, versions,
    slots, rom_info). Exposes the callable surface consumed by the Decky
    entrypoints — every public method delegates to a sub-service. Bulk
    local-save deletion is the only flow whose orchestration lives directly
    on the aggregate root because it spans :class:`RomInfoService` (file
    discovery), the on-disk save files (via the injected ``SaveFileStore``),
    and :class:`StateService` (file-tracking state hygiene) without belonging
    to any single sub-service.

    Parameters
    ----------
    config:
        Construction-time wiring bundle. See :class:`SaveServiceConfig` for
        the per-field rationale.
    """

    def __init__(self, *, config: SaveServiceConfig) -> None:
        self._config = config
        self._state = config.state
        self._save_file_store = config.save_file_store
        # Resolve plugin version once at construction; SyncEngine and any
        # other consumer receive the resolved string, not the Protocol.
        plugin_version = config.plugin_metadata.read_version(config.plugin_dir)

        self._state_svc = StateService(
            config=StateServiceConfig(
                save_sync_state=config.save_sync_state,
                state=config.state,
                persister=config.save_sync_state_persister,
                logger=config.logger,
            ),
        )
        # Convenience alias — both names reference the same aggregate.
        self._save_sync_state = self._state_svc.state

        self._rom_info = RomInfoService(
            config=RomInfoServiceConfig(
                state=config.state,
                save_file_store=config.save_file_store,
                retrodeck_paths=config.retrodeck_paths,
                get_active_core=config.get_active_core,
                get_core_name=config.get_core_name,
                logger=config.logger,
            ),
        )

        self._sync_engine = SyncEngine(
            config=SyncEngineConfig(
                state=config.state,
                state_svc=self._state_svc,
                rom_info=self._rom_info,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                clock=config.clock,
                save_file_store=config.save_file_store,
                log_debug=config.log_debug,
                get_active_core=config.get_active_core,
                hostname_provider=config.hostname_provider,
                plugin_version=plugin_version,
                detect_sort_change=config.detect_sort_change,
                is_retrodeck_migration_pending=config.is_retrodeck_migration_pending,
            ),
        )

        self._status = StatusService(
            config=StatusServiceConfig(
                state=config.state,
                state_svc=self._state_svc,
                sync_engine=self._sync_engine,
                rom_info=self._rom_info,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                log_debug=config.log_debug,
                get_active_core=config.get_active_core,
                emit=config.emit,
            ),
        )

        self._versions = VersionsService(
            config=VersionsServiceConfig(
                state_svc=self._state_svc,
                sync_engine=self._sync_engine,
                rom_info=self._rom_info,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                log_debug=config.log_debug,
            ),
        )

        self._slots = SlotsService(
            config=SlotsServiceConfig(
                state=config.state,
                state_svc=self._state_svc,
                sync_engine=self._sync_engine,
                status_service=self._status,
                rom_info=self._rom_info,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                clock=config.clock,
                save_file_store=config.save_file_store,
                log_debug=config.log_debug,
                get_active_core=config.get_active_core,
            ),
        )

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------

    @staticmethod
    def make_default_state() -> SaveSyncState:
        """Return a fresh default save-sync state aggregate."""
        return StateService.make_default_state()

    def init_state(self) -> None:
        """No-op for the typed aggregate (defaults ship at construction)."""
        self._state_svc.init_state()

    def load_state(self) -> None:
        """Load save sync state from disk, merging with defaults."""
        self._state_svc.load_state()

    def save_state(self) -> None:
        """Persist save sync state to disk (atomic write)."""
        self._state_svc.save_state()

    def prune_orphaned_state(self) -> None:
        """Remove save sync state entries for rom_ids no longer in shortcut registry."""
        self._state_svc.prune_orphaned_state()

    # ------------------------------------------------------------------
    # Device registration (delegated to SyncEngine)
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        return await self._sync_engine.ensure_device_registered()

    async def list_devices(self) -> dict:
        """List all devices registered with the RomM server for this user."""
        return await self._sync_engine.list_devices()

    # ------------------------------------------------------------------
    # Status (delegated to StatusService)
    # ------------------------------------------------------------------

    async def get_save_status(self, rom_id: int) -> dict:
        """Get save sync status for a ROM (local files, server saves, conflict state)."""
        return await self._status.get_save_status(rom_id)

    async def check_save_status_background(self, rom_id: int) -> None:
        """Run full save status check in background and emit result to frontend."""
        await self._status.check_save_status_background(rom_id)

    def check_core_change(self, rom_id: int) -> dict:
        """Check if emulator core changed since last sync for a ROM."""
        return self._status.check_core_change(rom_id)

    def has_tracked_save(self, rom_id: int) -> bool:
        """Return True when this ROM has at least one tracked save (slot or file).

        Reads in-memory aggregate state only — no I/O, no network. Used by
        the launch gate to decide whether a ``get_save_status`` failure
        should surface as a soft ``warn`` verdict (tracked saves exist —
        silent allow would risk data loss on an unseen conflict) or stay
        a silent ``allow`` (no tracked saves — nothing to corrupt).
        """
        save_entry = self._save_sync_state.saves.get(str(rom_id))
        if save_entry is None:
            return False
        return bool(save_entry.files) or bool(save_entry.slots)

    # ------------------------------------------------------------------
    # Sync orchestration (delegated to SyncEngine)
    # ------------------------------------------------------------------

    async def pre_launch_sync(self, rom_id: int) -> dict:
        """Download newer saves from server before game launch."""
        return await self._sync_engine.pre_launch_sync(rom_id)

    async def post_exit_sync(self, rom_id: int) -> dict:
        """Upload changed saves after game exit."""
        return await self._sync_engine.post_exit_sync(rom_id)

    async def sync_rom_saves(self, rom_id: int) -> dict:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        return await self._sync_engine.sync_rom_saves(rom_id)

    async def sync_all_saves(self) -> dict:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        return await self._sync_engine.sync_all_saves()

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        server_save_id: int,
        action: str,
    ) -> dict:
        """Resolve a pending sync conflict (true two-sided divergence)."""
        return await self._sync_engine.resolve_sync_conflict(rom_id, filename, server_save_id, action)

    # ------------------------------------------------------------------
    # Slots (delegated to SlotsService)
    # ------------------------------------------------------------------

    async def get_save_slots(self, rom_id: int) -> dict:
        """List available save slots for a ROM."""
        return await self._slots.get_save_slots(rom_id)

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Fetch server save files for a specific slot."""
        return await self._slots.get_slot_saves(rom_id, slot)

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict:
        """Switch the active save slot with immediate state sync."""
        return await self._slots.switch_slot(rom_id, new_slot)

    def is_save_tracking_configured(self, rom_id: int) -> dict:
        """Check if save slot tracking is configured for a game."""
        return self._slots.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id: int) -> dict:
        """Get info needed for the first-sync setup wizard."""
        return await self._slots.get_save_setup_info(rom_id)

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = _NO_MIGRATION,
    ) -> dict:
        """Confirm which slot to use for a game's save sync.

        ``migrate_from_slot`` may be the ``_NO_MIGRATION`` sentinel, ``None``,
        or ``"__no_migration__"`` (the string the frontend sends when no
        migration is requested). All three are treated as "no migration".
        """
        if migrate_from_slot is None or migrate_from_slot == "__no_migration__":
            migrate_from_slot = _NO_MIGRATION
        return await self._slots.confirm_slot_choice(rom_id, chosen_slot, migrate_from_slot)

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict:
        """Return info about what deleting a slot would do, for the confirmation modal."""
        return await self._slots.get_slot_delete_info(rom_id, slot)

    async def delete_slot(self, rom_id: int, slot: str) -> dict:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        return await self._slots.delete_slot(rom_id, slot)

    # ------------------------------------------------------------------
    # Versions (delegated to VersionsService)
    # ------------------------------------------------------------------

    async def list_file_versions(self, rom_id: int, slot: str, filename: str) -> dict:
        """List server-side versions of *filename* in the active slot."""
        return await self._versions.list_file_versions(rom_id, slot, filename)

    async def rollback_to_version(self, rom_id: int, slot: str, save_id: int) -> dict:
        """Switch the local + tracked save to a chosen older server version."""
        return await self._versions.rollback_to_version(rom_id, slot, save_id)

    # ------------------------------------------------------------------
    # Settings (delegated to StateService)
    # ------------------------------------------------------------------

    def get_save_sync_settings(self) -> dict:
        """Return current save sync settings as the on-disk dict shape."""
        return self._state_svc.get_save_sync_settings()

    def update_save_sync_settings(self, settings: dict) -> dict:
        """Update save sync settings (sync toggles, slot, etc.)."""
        return self._state_svc.update_save_sync_settings(settings)

    # ------------------------------------------------------------------
    # Bulk local-save deletion
    # ------------------------------------------------------------------

    def _delete_saves_for_roms(self, rom_ids: list[int]) -> tuple[int, list[str]]:
        """Delete local save files for the given ROM IDs and clear file tracking state.

        For each ROM ID, enumerates files via ``RomInfoService.find_save_files``,
        removes them on disk (counting successes and collecting per-file error
        strings), and clears the ROM's per-file tracking dict via
        ``StateService.clear_files_state``. Slot config (``active_slot``,
        ``slot_confirmed``, ``emulator``, ``last_synced_core``,
        ``own_upload_ids``, ``slots``, ``system``) is preserved. Persists state
        once at the end via ``save_state()``.

        Returns a ``(total_deleted, errors)`` tuple.
        """
        total_deleted = 0
        errors: list[str] = []
        for rom_id in rom_ids:
            rom_id_str = str(rom_id)
            files = self._rom_info.find_save_files(rom_id)
            for f in files:
                try:
                    self._save_file_store.remove_file(f["path"])
                    total_deleted += 1
                except Exception as e:
                    errors.append(f"{f['filename']}: {e}")
            self._state_svc.clear_files_state(rom_id_str)

        self.save_state()
        return total_deleted, errors

    def delete_local_saves(self, rom_id: int) -> dict:
        """Delete local save files (.srm, .rtc) for a ROM."""
        rom_id = int(rom_id)

        deleted, errors = self._delete_saves_for_roms([rom_id])

        if deleted == 0 and not errors:
            return {"success": True, "deleted_count": 0, "message": "No local save files found"}

        if errors:
            return {
                "success": False,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        return {
            "success": True,
            "deleted_count": deleted,
            "message": f"Deleted {deleted} save file(s)",
        }

    def delete_platform_saves(self, platform_slug: str) -> dict:
        """Delete local save files for all installed ROMs on a platform."""
        rom_ids: list[int] = []
        for rom_id_str, entry in self._state["installed_roms"].items():
            if entry.get("platform_slug") != platform_slug:
                continue
            rom_ids.append(int(rom_id_str))

        rom_count = len(rom_ids)
        total_deleted, total_errors = self._delete_saves_for_roms(rom_ids)

        if total_errors:
            return {
                "success": False,
                "deleted_count": total_deleted,
                "message": (f"Deleted {total_deleted} file(s) from {rom_count} ROM(s), {len(total_errors)} error(s)"),
            }
        return {
            "success": True,
            "deleted_count": total_deleted,
            "message": f"Deleted {total_deleted} save file(s) from {rom_count} ROM(s)",
        }
