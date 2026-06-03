"""Save-sync aggregate root and facade for the Decky callable surface.

Composes the save-sync sub-services (sync_engine, status, versions,
slots, rom_info) over the SQLite ``rom_save_states`` aggregate (reached
through the injected Unit-of-Work factory) and exposes the public
methods the frontend reaches through callables. The five save-sync
feature toggles and the device label live in ``settings.json`` and are
read/written here directly. Most methods are thin delegations;
orchestration that genuinely spans multiple sub-services lives here,
single-sub-service logic does not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from domain.rom_save_state import RomSaveState
from services.saves._config import SaveServiceConfig
from services.saves._settings import (
    ALLOWED_SETTINGS_KEYS,
    sanitize_setting,
    save_sync_enabled,
    save_sync_settings_view,
)
from services.saves.rom_info import RomInfoService, RomInfoServiceConfig
from services.saves.slots import SlotsService, SlotsServiceConfig
from services.saves.slots.service import NO_MIGRATION
from services.saves.status import StatusService, StatusServiceConfig
from services.saves.sync_engine import SyncEngine, SyncEngineConfig
from services.saves.versions import VersionsService, VersionsServiceConfig

if TYPE_CHECKING:
    from services.protocols import UnitOfWorkFactory


class SaveService:
    """Aggregate root for bidirectional save file sync between RetroDECK and RomM.

    Composes the save-sync sub-services (sync_engine, status, versions, slots,
    rom_info) over the SQLite ``rom_save_states`` aggregate. Exposes the callable
    surface consumed by the Decky entrypoints — every public method delegates to
    a sub-service or reads ``settings.json``. Bulk local-save deletion is the
    only flow whose orchestration lives directly on the aggregate root because it
    spans :class:`RomInfoService` (file discovery), the on-disk save files (via
    the injected ``SaveFileStore``), and the ``rom_save_states`` repository
    (file-tracking state hygiene) without belonging to any single sub-service.

    Parameters
    ----------
    config:
        Construction-time wiring bundle. See :class:`SaveServiceConfig` for
        the per-field rationale.
    """

    def __init__(self, *, config: SaveServiceConfig) -> None:
        self._config = config
        self._settings = config.settings
        self._save_file_store = config.save_file_store
        self._uow_factory: UnitOfWorkFactory = config.uow_factory
        self._settings_persister = config.settings_persister
        # Resolve plugin version once at construction; SyncEngine and any
        # other consumer receive the resolved string, not the Protocol.
        plugin_version = config.plugin_metadata.read_version(config.plugin_dir)

        self._rom_info = RomInfoService(
            config=RomInfoServiceConfig(
                uow_factory=config.uow_factory,
                save_file_store=config.save_file_store,
                retrodeck_paths=config.retrodeck_paths,
                get_active_core=config.get_active_core,
                get_core_name=config.get_core_name,
                logger=config.logger,
            ),
        )

        self._sync_engine = SyncEngine(
            config=SyncEngineConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
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
                machine_id_provider=config.machine_id_provider,
                settings_persister=config.settings_persister,
                plugin_version=plugin_version,
                detect_sort_change=config.detect_sort_change,
                is_retrodeck_migration_pending=config.is_retrodeck_migration_pending,
            ),
        )

        self._status = StatusService(
            config=StatusServiceConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
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
                settings=config.settings,
                uow_factory=config.uow_factory,
                sync_engine=self._sync_engine,
                rom_info=self._rom_info,
                resolve_core=self._sync_engine.resolve_core,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                log_debug=config.log_debug,
            ),
        )

        self._slots = SlotsService(
            config=SlotsServiceConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
                sync_engine=self._sync_engine,
                status_service=self._status,
                rom_info=self._rom_info,
                resolve_core=self._sync_engine.resolve_core,
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
    # Device registration (delegated to SyncEngine)
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict[str, Any]:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        return await self._sync_engine.ensure_device_registered()

    async def list_devices(self) -> dict[str, Any]:
        """List all devices registered with the RomM server for this user."""
        return await self._sync_engine.list_devices()

    # ------------------------------------------------------------------
    # Status (delegated to StatusService)
    # ------------------------------------------------------------------

    async def get_save_status(self, rom_id: int) -> dict[str, Any]:
        """Get save sync status for a ROM (local files, server saves, conflict state)."""
        return await self._status.get_save_status(rom_id)

    async def check_save_status_background(self, rom_id: int) -> None:
        """Run full save status check in background and emit result to frontend."""
        await self._status.check_save_status_background(rom_id)

    def check_core_change(self, rom_id: int) -> dict[str, Any]:
        """Check if emulator core changed since last sync for a ROM."""
        return self._status.check_core_change(rom_id)

    def has_tracked_save(self, rom_id: int) -> bool:
        """Return True when this ROM has at least one tracked save (slot or file).

        Reads the ``rom_save_states`` aggregate through its own narrow read
        UoW — no network. Used by the launch gate to decide whether a
        ``get_save_status`` failure should surface as a soft ``warn`` verdict
        (tracked saves exist — silent allow would risk data loss on an unseen
        conflict) or stay a silent ``allow`` (no tracked saves — nothing to
        corrupt).
        """
        with self._uow_factory() as uow:
            save_entry = uow.rom_save_states.get(int(rom_id))
        if save_entry is None:
            return False
        return bool(save_entry.files) or bool(save_entry.slots)

    # ------------------------------------------------------------------
    # Sync orchestration (delegated to SyncEngine)
    # ------------------------------------------------------------------

    async def pre_launch_sync(self, rom_id: int) -> dict[str, Any]:
        """Download newer saves from server before game launch."""
        return await self._sync_engine.pre_launch_sync(rom_id)

    async def post_exit_sync(self, rom_id: int) -> dict[str, Any]:
        """Upload changed saves after game exit."""
        return await self._sync_engine.post_exit_sync(rom_id)

    async def sync_rom_saves(self, rom_id: int) -> dict[str, Any]:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        return await self._sync_engine.sync_rom_saves(rom_id)

    async def sync_all_saves(self) -> dict[str, Any]:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        return await self._sync_engine.sync_all_saves()

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        server_save_id: int,
        action: str,
    ) -> dict[str, Any]:
        """Resolve a pending sync conflict (true two-sided divergence)."""
        return await self._sync_engine.resolve_sync_conflict(rom_id, filename, server_save_id, action)

    # ------------------------------------------------------------------
    # Slots (delegated to SlotsService)
    # ------------------------------------------------------------------

    async def get_save_slots(self, rom_id: int) -> dict[str, Any]:
        """List available save slots for a ROM."""
        return await self._slots.get_save_slots(rom_id)

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Fetch server save files for a specific slot."""
        return await self._slots.get_slot_saves(rom_id, slot)

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict[str, Any]:
        """Switch the active save slot with immediate state sync."""
        return await self._slots.switch_slot(rom_id, new_slot)

    def is_save_tracking_configured(self, rom_id: int) -> dict[str, Any]:
        """Check if save slot tracking is configured for a game."""
        return self._slots.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id: int) -> dict[str, Any]:
        """Get info needed for the first-sync setup wizard."""
        return await self._slots.get_save_setup_info(rom_id)

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = NO_MIGRATION,
    ) -> dict[str, Any]:
        """Confirm which slot to use for a game's save sync.

        ``migrate_from_slot`` may be the ``NO_MIGRATION`` sentinel, ``None``,
        or ``"__no_migration__"`` (the string the frontend sends when no
        migration is requested). All three are treated as "no migration".
        """
        if migrate_from_slot is None or migrate_from_slot == "__no_migration__":
            migrate_from_slot = NO_MIGRATION
        return await self._slots.confirm_slot_choice(rom_id, chosen_slot, migrate_from_slot)

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Return info about what deleting a slot would do, for the confirmation modal."""
        return await self._slots.get_slot_delete_info(rom_id, slot)

    async def delete_slot(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        return await self._slots.delete_slot(rom_id, slot)

    # ------------------------------------------------------------------
    # Versions (delegated to VersionsService)
    # ------------------------------------------------------------------

    async def list_file_versions(self, rom_id: int, slot: str, filename: str) -> dict[str, Any]:
        """List server-side versions of *filename* in the active slot."""
        return await self._versions.list_file_versions(rom_id, slot, filename)

    async def rollback_to_version(self, rom_id: int, slot: str, save_id: int) -> dict[str, Any]:
        """Switch the local + tracked save to a chosen older server version."""
        return await self._versions.rollback_to_version(rom_id, slot, save_id)

    # ------------------------------------------------------------------
    # Settings (settings.json — read/written directly)
    # ------------------------------------------------------------------

    def is_save_sync_enabled(self) -> bool:
        """Whether the save-sync feature toggle is on."""
        return save_sync_enabled(self._settings)

    def get_save_sync_settings(self) -> dict[str, Any]:
        """Return current save sync settings as the frontend dict shape."""
        return save_sync_settings_view(self._settings)

    def update_save_sync_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Update save sync settings (sync toggles, slot, etc.) in settings.json."""
        for key, value in settings.items():
            if key not in ALLOWED_SETTINGS_KEYS:
                continue
            coerced, skip = sanitize_setting(key, value)
            if skip:
                continue
            self._settings[key] = coerced

        self._settings_persister.save_settings()
        return {"success": True, "settings": save_sync_settings_view(self._settings)}

    def get_device_name(self) -> str | None:
        """Return the user-set device label from settings.json (``None`` if unset)."""
        return self._settings.get("device_name")

    def set_device_name(self, name: str) -> None:
        """Persist the device label to settings.json."""
        self._settings["device_name"] = name
        self._settings_persister.save_settings()

    # ------------------------------------------------------------------
    # Bulk local-save deletion
    # ------------------------------------------------------------------

    def _delete_saves_for_roms(self, rom_ids: list[int]) -> tuple[int, list[str]]:
        """Delete local save files for the given ROM IDs and clear file tracking state.

        For each ROM ID, enumerates files via ``RomInfoService.find_save_files``,
        removes them on disk (counting successes and collecting per-file error
        strings), and clears the ROM's per-file tracking dict via the aggregate's
        ``clear_baselines`` verb. Slot config (``active_slot``, ``slot_confirmed``,
        ``emulator``, ``last_synced_core``, ``own_upload_ids``, ``slots``,
        ``system``) is preserved. Each ROM's state is persisted in its own short
        write UoW.

        Returns a ``(total_deleted, errors)`` tuple.
        """
        total_deleted = 0
        errors: list[str] = []
        for rom_id in rom_ids:
            files = self._rom_info.find_save_files(rom_id)
            for f in files:
                try:
                    self._save_file_store.remove_file(f["path"])
                    total_deleted += 1
                except Exception as e:
                    errors.append(f"{f['filename']}: {e}")
            with self._uow_factory() as uow:
                save_state = uow.rom_save_states.get(rom_id)
                # Nothing to clear when the ROM has neither tracked save state
                # nor any local save files (e.g. a non-installed ROM with no
                # roms row — persisting an empty aggregate would violate the FK).
                if save_state is None and not files:
                    continue
                if save_state is None:
                    save_state = RomSaveState()
                save_state.clear_baselines()
                uow.rom_save_states.save(rom_id, save_state)

        return total_deleted, errors

    def delete_local_saves(self, rom_id: int) -> dict[str, Any]:
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

    def _installed_rom_ids_on_platform(self, platform_slug: str) -> list[int]:
        """Read installed-ROM ids on *platform_slug* from the rom_installs aggregate (WS3)."""
        with self._uow_factory() as uow:
            return [install.rom_id for install in uow.rom_installs.iter_all() if install.platform_slug == platform_slug]

    def delete_platform_saves(self, platform_slug: str) -> dict[str, Any]:
        """Delete local save files for all installed ROMs on a platform."""
        rom_ids = self._installed_rom_ids_on_platform(platform_slug)

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


__all__ = ["SaveService", "SaveServiceConfig"]
