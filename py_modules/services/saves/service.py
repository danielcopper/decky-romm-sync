"""Save-sync aggregate root and facade for the Decky callable surface.

Composes the save-sync sub-services (sync_engine, status, versions,
slots, rom_info) over the SQLite ``rom_save_sync_states`` aggregate (reached
through the injected Unit-of-Work factory) and exposes the public
methods the frontend reaches through callables. The five save-sync
feature toggles and the device label live in ``settings.json`` and are
read/written here directly. Most methods are thin delegations;
orchestration that genuinely spans multiple sub-services lives here,
single-sub-service logic does not.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

from domain.iso_time import epoch_to_iso
from domain.rom_save_sync_state import RomSaveSyncState
from domain.save_backup import is_backup_for
from lib.list_result import ErrorCode
from services.saves._config import SaveServiceConfig
from services.saves._settings import (
    ALLOWED_SETTINGS_KEYS,
    sanitize_setting,
    save_sync_enabled,
    save_sync_settings_view,
)
from services.saves.copies import SaveCopyService, SaveCopyServiceConfig
from services.saves.rom_info import RomInfoService, RomInfoServiceConfig
from services.saves.slots import SlotsService, SlotsServiceConfig
from services.saves.status import StatusService, StatusServiceConfig
from services.saves.sync_engine import SyncEngine, SyncEngineConfig
from services.saves.sync_engine.devices import DeviceRegistry
from services.saves.versions import VersionsService, VersionsServiceConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from models.prune import SourceIdentity
    from models.sync import ClientSaveState

    from services.protocols import UnitOfWorkFactory


class SaveService:
    """Aggregate root for bidirectional save file sync between RetroDECK and RomM.

    Composes the save-sync sub-services (sync_engine, status, versions, slots,
    rom_info) over the SQLite ``rom_save_sync_states`` aggregate. Exposes the callable
    surface consumed by the Decky entrypoints — every public method delegates to
    a sub-service or reads ``settings.json``. Bulk local-save deletion is the
    only flow whose orchestration lives directly on the aggregate root because it
    spans :class:`RomInfoService` (file discovery), the on-disk save files (via
    the injected ``SaveFileStore``), and the ``rom_save_sync_states`` repository
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
        # Resolve plugin version once at construction; the DeviceRegistry and
        # any other consumer receive the resolved string, not the Protocol.
        plugin_version = config.plugin_metadata.read_version(config.plugin_dir)

        # The single owner of the server device id (kv_config["device_id"]).
        # Built once here and shared into every sub-service that needs the id
        # (sync_engine, status, versions, slots) — the same-bounded-context
        # peer-ref carve-out — so the id is read once and cached, never
        # re-queried per flow through a fresh UoW. The device label stays in
        # settings.json (ADR-0003).
        self._device_registry = DeviceRegistry(
            uow_factory=config.uow_factory,
            settings=config.settings,
            romm_api=config.romm_api,
            retry=config.retry,
            logger=config.logger,
            log_debug=config.log_debug,
            settings_persister=config.settings_persister,
            plugin_version=plugin_version,
        )

        self._rom_info = RomInfoService(
            config=RomInfoServiceConfig(
                uow_factory=config.uow_factory,
                save_file_store=config.save_file_store,
                retrodeck_paths=config.retrodeck_paths,
                active_core=config.active_core,
                get_core_name=config.get_core_name,
                logger=config.logger,
            ),
        )

        self._sync_engine = SyncEngine(
            config=SyncEngineConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
                rom_info=self._rom_info,
                device_registry=self._device_registry,
                romm_api=config.romm_api,
                retry=config.retry,
                resolve_upload_conflict=config.resolve_upload_conflict,
                loop=config.loop,
                logger=config.logger,
                clock=config.clock,
                save_file_store=config.save_file_store,
                log_debug=config.log_debug,
                active_core=config.active_core,
                hostname_provider=config.hostname_provider,
                machine_id_provider=config.machine_id_provider,
                detect_sort_change=config.detect_sort_change,
                is_retrodeck_migration_pending=config.is_retrodeck_migration_pending,
                build_inventory=self.build_save_inventory,
            ),
        )

        self._status = StatusService(
            config=StatusServiceConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
                sync_engine=self._sync_engine,
                device_registry=self._device_registry,
                rom_info=self._rom_info,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                log_debug=config.log_debug,
                active_core=config.active_core,
                emit=config.emit,
                get_save_layout=config.get_save_layout,
            ),
        )

        self._versions = VersionsService(
            config=VersionsServiceConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
                sync_engine=self._sync_engine,
                device_registry=self._device_registry,
                rom_info=self._rom_info,
                resolve_core=self._sync_engine.resolve_core,
                romm_api=config.romm_api,
                retry=config.retry,
                loop=config.loop,
                logger=config.logger,
                log_debug=config.log_debug,
            ),
        )

        self._copies = SaveCopyService(
            config=SaveCopyServiceConfig(
                settings=config.settings,
                uow_factory=config.uow_factory,
                sync_engine=self._sync_engine,
                device_registry=self._device_registry,
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
                device_registry=self._device_registry,
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

        Reads the ``rom_save_sync_states`` aggregate through its own narrow read
        UoW — no network. Used by the launch gate to decide whether a
        ``get_save_status`` failure should surface as a soft ``warn`` verdict
        (tracked saves exist — silent allow would risk data loss on an unseen
        conflict) or stay a silent ``allow`` (no tracked saves — nothing to
        corrupt).
        """
        with self._uow_factory() as uow:
            save_entry = uow.rom_save_sync_states.get(int(rom_id))
        if save_entry is None:
            return False
        return bool(save_entry.files) or bool(save_entry.slots)

    def find_local_save_files(self, rom_id: int) -> list[dict[str, str]]:
        """Enumerate the ROM's local save files (``[{"path", "filename"}]``).

        Delegates to the shared ``RomInfoService.find_save_files`` discovery —
        the same enumeration the sync/status path uses — so the launch gate's
        drift check sees exactly the files a real sync would. Returns ``[]``
        when the ROM is not installed or no save files are present. Satisfies
        the ``LaunchGateDriftReader`` seam.
        """
        return self._rom_info.find_save_files(int(rom_id))

    def last_sync_hashes(self, rom_id: int) -> dict[str, str | None]:
        """Return the per-file ``last_sync_hash`` baselines for a ROM.

        Reads the ``rom_save_sync_states`` aggregate through a narrow read UoW —
        no network — and projects each tracked file's baseline hash onto a
        ``{filename: last_sync_hash}`` map (``None`` for a file with no
        baseline yet). An untracked ROM yields ``{}``. Satisfies the
        ``LaunchGateDriftReader`` seam.
        """
        with self._uow_factory() as uow:
            save_entry = uow.rom_save_sync_states.get(int(rom_id))
        if save_entry is None:
            return {}
        return {filename: state.last_sync_hash for filename, state in save_entry.files.items()}

    @contextlib.asynccontextmanager
    async def lock_prune_roms(self, rom_ids: list[int]) -> AsyncIterator[None]:
        """Hold affected save locks in ascending id order for recovery/removal."""
        async with contextlib.AsyncExitStack() as stack:
            for rom_id in sorted({int(value) for value in rom_ids}):
                await stack.enter_async_context(self._sync_engine.rom_lock(rom_id))
            yield

    def inventory_prune_saves(self, purge_rom_ids: list[int]) -> dict[str, Any]:
        """Build exact-path save ownership and recovery artifacts for a purge set."""
        purge_ids = {int(value) for value in purge_rom_ids}
        with self._uow_factory() as uow:
            installed_ids = [install.rom_id for install in uow.rom_installs.iter_all()]
            persisted_names = {
                rom_id: list(state.files)
                for rom_id, state in uow.rom_save_sync_states.iter_all()
                if rom_id in installed_ids
            }

        ownership: dict[str, set[int]] = {}
        expected_by_id: dict[int, list[dict[str, str]]] = {}
        for rom_id in installed_ids:
            expected = self._rom_info.expected_save_files(rom_id)
            if expected:
                saves_dir = expected[0]["saves_dir"]
                known = {item["filename"] for item in expected}
                for filename in persisted_names.get(rom_id, []):
                    if (
                        filename not in known
                        and filename not in {"", ".", ".."}
                        and os.path.basename(filename) == filename
                        and "\x00" not in filename
                    ):
                        expected.append(
                            {"path": os.path.join(saves_dir, filename), "filename": filename, "saves_dir": saves_dir}
                        )
            expected_by_id[rom_id] = expected
            for item in expected:
                ownership.setdefault(self._save_file_store.canonical_path(item["path"]), set()).add(rom_id)

        saves_root = self._config.retrodeck_paths.saves_path()
        artifacts: list[dict[str, object]] = []
        exclusive: list[dict[str, str]] = []
        shared: list[str] = []
        warnings: list[str] = []
        lock_ids = set(purge_ids)
        for rom_id in sorted(purge_ids):
            expected = expected_by_id.get(rom_id, [])
            if not expected:
                warnings.append(f"ROM {rom_id}: save path could not be resolved; physical saves were left untouched")
                continue
            for item in expected:
                path = item["path"]
                if not self._save_file_store.is_within(path, saves_root):
                    warnings.append(f"ROM {rom_id}: save path is outside the supported saves root; left untouched")
                    continue
                owners = ownership.get(self._save_file_store.canonical_path(path), {rom_id})
                lock_ids.update(owners)
                if self._save_file_store.is_file(path):
                    artifacts.append(
                        {"source_path": path, "safe_root": saves_root, "kind": "current_save", "rom_id": rom_id}
                    )
                    if owners <= purge_ids:
                        exclusive.append(item)
                    else:
                        shared.append(path)
                backup_dir = os.path.join(item["saves_dir"], ".romm-backup")
                if self._save_file_store.is_symlink(backup_dir) or not self._save_file_store.is_within(
                    backup_dir, saves_root
                ):
                    raise ValueError(f"ROM {rom_id}: save backup directory is unsafe: {backup_dir}")
                for entry in self._save_file_store.listdir(backup_dir):
                    backup_path = os.path.join(backup_dir, entry)
                    if is_backup_for(item["filename"], entry) and self._save_file_store.is_file(backup_path):
                        artifacts.append(
                            {
                                "source_path": backup_path,
                                "safe_root": saves_root,
                                "kind": "save_backup",
                                "rom_id": rom_id,
                            }
                        )
        return {
            "artifacts": artifacts,
            "exclusive": exclusive,
            "shared": sorted(set(shared)),
            "warnings": warnings,
            "lock_rom_ids": sorted(lock_ids),
        }

    def quarantine_prune_saves(
        self, files: list[dict[str, str]], identities: dict[str, SourceIdentity] | None = None
    ) -> dict[str, Any]:
        """Move exclusive current saves through the sanctioned backup funnel."""
        moved: list[str] = []
        saves_root = self._config.retrodeck_paths.saves_path()
        try:
            for item in files:
                backup_dir = os.path.join(item["saves_dir"], ".romm-backup")
                if (
                    not self._save_file_store.is_within(item["path"], saves_root)
                    or not self._save_file_store.is_within(backup_dir, saves_root)
                    or self._save_file_store.is_symlink(backup_dir)
                ):
                    raise ValueError(f"Unsafe save quarantine destination: {backup_dir}")
                identity = identities.get(item["path"]) if identities is not None else None
                if self._sync_engine.quarantine_local_file(
                    item["saves_dir"],
                    item["filename"],
                    preserve_history=True,
                    expected_identity=identity,
                    safe_root=saves_root if identity is not None else None,
                ):
                    moved += [item["path"]]
        except Exception as exc:
            return {"success": False, "reason": "save_quarantine_failed", "message": str(exc), "moved": moved}
        return {"success": True, "moved": moved}

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
        chosen_slot: str | None,
        migrate: bool = False,
        migrate_from_slot: str | None = None,
        use_server_on_conflict: bool = False,
    ) -> dict[str, Any]:
        """Confirm which slot to use for a game's save sync.

        ``chosen_slot`` is the slot name. Migration runs only when ``migrate`` is
        true, copying the newest legacy save per canonical target from
        ``migrate_from_slot`` (``None`` = the legacy no-slot source) into
        ``chosen_slot`` (#1498). A differing local save is held for the user's
        decision unless ``use_server_on_conflict`` resolves it in the server's
        favour.
        """
        return await self._slots.confirm_slot_choice(
            rom_id, chosen_slot, migrate, migrate_from_slot, use_server_on_conflict
        )

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
    # Copy-to-slot (delegated to SaveCopyService)
    # ------------------------------------------------------------------

    async def copy_save_to_slot(self, rom_id: int, save_id: int, target_slot: str) -> dict[str, Any]:
        """Copy a specific server save into another slot (which becomes active)."""
        return await self._copies.copy_save_to_slot(rom_id, save_id, target_slot)

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
        """Persist the device label to settings.json, atomic on failure.

        Mutates the in-memory settings dict and triggers the persist; if the
        persist raises, the in-memory dict is rolled back to its prior value so
        an unsaved label never lingers in memory (a later unrelated
        ``save_settings`` would otherwise commit it), then the failure
        re-raises.
        """
        had_name = "device_name" in self._settings
        prior = self._settings.get("device_name")
        self._settings["device_name"] = name
        try:
            self._settings_persister.save_settings()
        except Exception:
            if had_name:
                self._settings["device_name"] = prior
            else:
                self._settings.pop("device_name", None)
            raise

    def forget_device(self) -> None:
        """Drop the registered server device id on a server-origin change.

        Delegates to the DeviceRegistry — the single owner of
        ``kv_config["device_id"]``. The composition root wires this as the
        ``DeviceForgetFn`` handed to ConnectionService, which invokes it after
        a successful sign-in to a different origin so the stale id cannot 404
        against the new server's negotiate.
        """
        self._device_registry.forget_device()

    def get_device_id(self) -> str | None:
        """Return the registered server device id (``None`` when unregistered).

        Delegates to the DeviceRegistry — the single owner of
        ``kv_config["device_id"]``. The composition root wires this as the
        ``DeviceIdProvider`` handed to PlaytimeService, which reads the id to
        attribute native play-session ingests and gate the offline outbox.
        """
        return self._device_registry.get_device_id()

    # ------------------------------------------------------------------
    # Negotiate inventory (Phase 1c)
    # ------------------------------------------------------------------

    def build_save_inventory(self, rom_id: int | None = None) -> list[ClientSaveState]:
        """Build the negotiate inventory of this device's local save files.

        Gathers one :class:`ClientSaveState` per local save file belonging to a
        ROM whose slot is **confirmed** and whose ``active_slot`` is a real
        (non-legacy) slot — ``slot_confirmed`` is true and ``active_slot`` is
        truthy (excludes both ``None`` and the legacy ``""``). A confirmed ROM
        with no local files contributes nothing; per-file granularity means
        each local file yields its own entry.

        ``rom_id`` scopes the inventory: ``None`` (the default) builds the
        whole-device inventory for the bulk ``sync_all_saves`` pre-negotiate; a
        concrete id restricts it to that one ROM for the single-ROM negotiate
        trigger. The in-scope predicate is unchanged either way.

        ``content_hash`` is always set via :meth:`SaveFileStore.content_hash`
        (the zip-aware RomM-parity hash — never ``checksum_md5``), and
        ``updated_at`` is the local file's mtime rendered as a UTC ISO-8601
        string.

        Single-file-first: the multi-file-per-slot collision case (several local
        files mapping to one slot) is a Phase 4 concern tracked in #1235.
        """
        with self._uow_factory() as uow:
            confirmed = [
                (rid, state)
                for rid, state in uow.rom_save_sync_states.iter_all()
                if state.slot_confirmed and state.active_slot and (rom_id is None or rid == rom_id)
            ]

        inventory: list[ClientSaveState] = []
        for rid, state in confirmed:
            for f in self._rom_info.find_save_files(rid):
                path = f["path"]
                entry: ClientSaveState = {
                    "rom_id": rid,
                    "file_name": f["filename"],
                    "slot": state.active_slot,
                    "emulator": state.emulator,
                    "content_hash": self._save_file_store.content_hash(path),
                    "updated_at": epoch_to_iso(self._save_file_store.get_mtime(path)),
                    "file_size_bytes": self._save_file_store.get_size(path),
                }
                inventory.append(entry)
        return inventory

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
                save_state = uow.rom_save_sync_states.get(rom_id)
                # Nothing to clear when the ROM has neither tracked save state
                # nor any local save files (e.g. a non-installed ROM with no
                # roms row — persisting an empty aggregate would violate the FK).
                if save_state is None and not files:
                    continue
                if save_state is None:
                    save_state = RomSaveSyncState()
                save_state.clear_baselines()
                uow.rom_save_sync_states.save(rom_id, save_state)

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
                "reason": ErrorCode.UNKNOWN.value,
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
                "reason": ErrorCode.UNKNOWN.value,
                "deleted_count": total_deleted,
                "message": (f"Deleted {total_deleted} file(s) from {rom_count} ROM(s), {len(total_errors)} error(s)"),
            }
        return {
            "success": True,
            "deleted_count": total_deleted,
            "message": f"Deleted {total_deleted} save file(s) from {rom_count} ROM(s)",
        }


__all__ = ["SaveService", "SaveServiceConfig"]
