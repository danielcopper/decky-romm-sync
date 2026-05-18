import { callable } from "@decky/api";
import type { PluginSettings, SyncStats, DownloadItem, InstalledRom, PlatformSyncSetting, CollectionSyncSetting, RegistryPlatform, FirmwareStatus, FirmwareDownloadResult, BiosStatus, BiosFileStatus, RomMetadata, SaveSyncSettings, SaveStatus, SaveSyncDisplay, SyncConflict, AvailableCore, RommErrorCode, SyncPreview, AchievementSummary, AchievementList, AchievementProgress, SaveSlotSummary, SaveSetupInfo, SlotSavesResponse, SwitchSlotResponse, LaunchVerdict } from "../types";

export interface BackendResult {
  success: boolean;
  message: string;
  error_code?: RommErrorCode;
  romm_version?: string;
  /** Set when a callable was rejected because a RetroDECK migration is pending. */
  blocked_by_migration?: boolean;
}

export interface CachedGameDetail {
  found: boolean;
  rom_id?: number;
  rom_name?: string;
  platform_slug?: string;
  platform_name?: string;
  installed?: boolean;
  save_sync_enabled?: boolean;
  save_status?: { files: Array<{ filename: string; status: string; last_sync_at?: string }>; last_sync_check_at?: string; conflicts?: SyncConflict[] } | null;

  metadata?: Record<string, unknown> | null;
  bios_status?: { needs_bios?: boolean; platform_slug: string; server_count: number; local_count: number; all_downloaded: boolean; required_count?: number; required_downloaded?: number; active_core?: string; active_core_label?: string; available_cores?: AvailableCore[]; cached_at?: number; files?: BiosFileStatus[] } | null;
  rom_file?: string;
  ra_id?: number | null;
  achievement_summary?: AchievementSummary | null;
  bios_level?: "ok" | "partial" | "missing" | null;
  bios_label?: string | null;
  save_sync_display?: SaveSyncDisplay | null;
  stale_fields?: string[];
}

// get_cached_game_detail wiring lives in utils/cachedGameDetailStore.ts so the
// module-scope cache + invalidation surface is in one place. Re-exported here
// for back-compat with existing import sites.
export {
  getCachedGameDetail,
  invalidateCachedGameDetail,
} from "../utils/cachedGameDetailStore";
export const getSettings = callable<[], PluginSettings>("get_settings");
export const saveSettings = callable<[string, string, string, boolean], BackendResult>("save_settings");

export interface WhitelistSettings {
  disabled_defaults: string[];
  custom_names: string[];
}
export const getWhitelistSettings = callable<[], WhitelistSettings>("get_whitelist_settings");
export const updateWhitelistSettings = callable<[string[], string[]], { success: boolean; message?: string }>("update_whitelist_settings");

export const testConnection = callable<[], BackendResult>("test_connection");
export const startSync = callable<[], BackendResult>("start_sync");
export const cancelSync = callable<[], BackendResult>("cancel_sync");
export const syncHeartbeat = callable<[], { success: boolean }>("sync_heartbeat");
export const syncPreview = callable<[], SyncPreview>("sync_preview");
export const syncApplyDelta = callable<[string], BackendResult>("sync_apply_delta");
export const syncCancelPreview = callable<[], BackendResult>("sync_cancel_preview");
export const clearSyncCache = callable<[], BackendResult>("clear_sync_cache");
export const getSyncStats = callable<[], SyncStats>("get_sync_stats");
export const startDownload = callable<[number], BackendResult>("start_download");
export const cancelDownload = callable<[number], BackendResult>("cancel_download");
export const getDownloadQueue = callable<[], { downloads: DownloadItem[] }>("get_download_queue");
export const getInstalledRom = callable<[number], InstalledRom | null>("get_installed_rom");
export const evaluateLaunch = callable<[number], LaunchVerdict>("evaluate_launch");
export const removeRom = callable<[number], BackendResult>("remove_rom");
export const getPlatforms = callable<[], { success: boolean; platforms: PlatformSyncSetting[] }>("get_platforms");
export const savePlatformSync = callable<[number, boolean], { success: boolean; message: string }>("save_platform_sync");
export const setAllPlatformsSync = callable<[boolean], { success: boolean; message: string }>("set_all_platforms_sync");
export const getCollections = callable<[], { success: boolean; collections: CollectionSyncSetting[]; message?: string; error_code?: RommErrorCode }>("get_collections");
export const saveCollectionSync = callable<[string, boolean], { success: boolean }>("save_collection_sync");
export const setAllCollectionsSync = callable<[boolean, string | null], { success: boolean }>("set_all_collections_sync");
export const saveCollectionPlatformGroups = callable<[boolean], { success: boolean }>("save_collection_platform_groups");
export const getRegistryPlatforms = callable<[], { platforms: RegistryPlatform[] }>("get_registry_platforms");
export const removePlatformShortcuts = callable<[string], { success: boolean; app_ids: number[]; rom_ids: (string | number)[]; platform_name: string }>("remove_platform_shortcuts");
export const removeAllShortcuts = callable<[], { success: boolean; message: string; removed_count: number; app_ids: number[]; rom_ids: (string | number)[] }>("remove_all_shortcuts");
export const getArtworkBase64 = callable<[number], { base64: string | null }>("get_artwork_base64");
export const getSgdbArtworkBase64 = callable<[number, number], { base64: string | null; no_api_key?: boolean }>("get_sgdb_artwork_base64");
export const reportUnitResults = callable<[Record<string, number>], { success: boolean; count: number }>("report_unit_results");
export const reportRemovalResults = callable<[(string | number)[]], { success: boolean; message: string }>("report_removal_results");
export const uninstallAllRoms = callable<[], { success: boolean; removed_count: number; errors: { rom_id: string; error: string }[] }>("uninstall_all_roms");
export const saveSgdbApiKey = callable<[string], { success: boolean; message: string }>("save_sgdb_api_key");
export const verifySgdbApiKey = callable<[string], { success: boolean; message: string }>("verify_sgdb_api_key");
export const saveSteamInputSetting = callable<[string], { success: boolean }>("save_steam_input_setting");
export const applySteamInputSetting = callable<[], { success: boolean; message: string }>("apply_steam_input_setting");
export const getFirmwareStatus = callable<[], FirmwareStatus>("get_firmware_status");
export const downloadAllFirmware = callable<[string], FirmwareDownloadResult>("download_all_firmware");
export const downloadRequiredFirmware = callable<[string], FirmwareDownloadResult>("download_required_firmware");
export const checkPlatformBios = callable<[string], BiosStatus>("check_platform_bios");
export const getBiosStatus = callable<
  [number],
  {
    bios_status: CachedGameDetail["bios_status"];
    bios_level: "ok" | "partial" | "missing" | null;
    bios_label: string | null;
  }
>("get_bios_status");
export const setSystemCore = callable<[string, string], { success: boolean; message?: string; bios_status?: BiosStatus }>("set_system_core");
export const setGameCore = callable<[string, string, string], { success: boolean; message?: string; bios_status?: BiosStatus }>("set_game_core");
export const saveLogLevel = callable<[string], { success: boolean }>("save_log_level");
export const debugLog = callable<[string], void>("debug_log");
const frontendLog = callable<[string, string], void>("frontend_log");
export const logInfo = (msg: string) => { frontendLog("info", msg); };
export const logWarn = (msg: string) => { frontendLog("warn", msg); };
export const logError = (msg: string) => { frontendLog("error", msg); };
export const fixRetroarchInputDriver = callable<[], { success: boolean; message: string }>("fix_retroarch_input_driver");
export const getRomMetadata = callable<[number], RomMetadata>("get_rom_metadata");
export const getAllMetadataCache = callable<[], Record<string, RomMetadata>>("get_all_metadata_cache");
export const getAppIdRomIdMap = callable<[], Record<string, number>>("get_app_id_rom_id_map");

// Icon support (VDF-based)
export const saveShortcutIcon = callable<[number, string], { success: boolean }>("save_shortcut_icon");

// Save sync callables
export const ensureDeviceRegistered = callable<[], { success: boolean; device_id: string; device_name: string }>("ensure_device_registered");

export interface RegisteredDevice {
  id: string;
  name: string | null;
  platform: string | null;
  client: string | null;
  client_version: string | null;
  last_seen: string | null;
  created_at: string;
  is_current_device: boolean;
  user_id?: number;
  ip_address?: string | null;
  mac_address?: string | null;
  hostname?: string | null;
  sync_mode?: string | null;
  sync_enabled?: boolean;
  updated_at?: string | null;
}

export interface ListDevicesResponse {
  success: boolean;
  devices: RegisteredDevice[];
  disabled?: boolean;
  error?: string;
}

export const listDevices = callable<[], ListDevicesResponse>("list_devices");
export const getSaveStatus = callable<[number], SaveStatus>("get_save_status");
export const preLaunchSync = callable<[number], { success: boolean; message: string; synced?: number; errors?: string[]; conflicts?: SyncConflict[] }>("pre_launch_sync");
export const syncRomSaves = callable<[number], { success: boolean; message: string; synced: number; errors?: string[]; conflicts?: SyncConflict[] }>("sync_rom_saves");
export const syncAllSaves = callable<[], { success: boolean; message: string; synced: number; conflicts: number }>("sync_all_saves");
export const resolveSyncConflict = callable<
  [number, string, number, "keep_local" | "use_server"],
  { success: boolean; message?: string; error_code?: "stale_conflict"; action?: "keep_local" | "use_server" }
>("resolve_sync_conflict");
export const recordSessionStart = callable<[number], { success: boolean }>("record_session_start");
export const getSaveSyncSettings = callable<[], SaveSyncSettings>("get_save_sync_settings");
export const updateSaveSyncSettings = callable<[SaveSyncSettings], { success: boolean }>("update_save_sync_settings");
export const getSaveSlots = callable<[number], { success: boolean; slots: SaveSlotSummary[]; active_slot: string; error?: string }>("get_save_slots");
export const getSlotSaves = callable<[number, string], SlotSavesResponse>("get_slot_saves");
export const switchSlot = callable<[number, string], SwitchSlotResponse>("switch_slot");

export interface SlotDeleteInfo {
  success: boolean;
  slot?: string;
  source?: "server" | "local";
  server_save_count?: number;
  server_save_ids?: number[];
  local_file_count?: number;
  local_filenames?: string[];
  is_active?: boolean;
  reason?: string;
  // Coarse failure category for routing (e.g. "server_unreachable").
  // Mirrors `reason` for the cases the modal needs to handle differently.
  error?: string;
  message?: string;
}

export interface DeleteSlotResult {
  success: boolean;
  deleted_server_saves?: number;
  cleaned_files?: number;
  reason?: string;
  message?: string;
}

export const getSlotDeleteInfo = callable<[number, string], SlotDeleteInfo>("get_slot_delete_info");
export const deleteSlot = callable<[number, string], DeleteSlotResult>("delete_slot");

export const isSaveTrackingConfigured = callable<[number], { configured: boolean; active_slot: string | null }>("is_save_tracking_configured");
export const getSaveSetupInfo = callable<[number], SaveSetupInfo>("get_save_setup_info");
export const confirmSlotChoice = callable<[number, string, string | null], { success: boolean; needs_conflict_resolution?: boolean; message: string }>("confirm_slot_choice");
export const checkCoreChange = callable<[number], { changed: boolean; old_core?: string; new_core?: string; old_label?: string; new_label?: string }>("check_core_change");

// Bulk playtime for plugin-load UI update
export const getAllPlaytime = callable<[], { playtime: Record<string, { total_seconds: number; session_count: number }> }>("get_all_playtime");

// RetroDECK path migration
export interface MigrationStatus {
  pending: boolean;
  old_path?: string;
  new_path?: string;
  roms_count?: number;
  bios_count?: number;
  saves_count?: number;
}

interface ConflictDetail {
  filename: string;
  old_path: string;
  old_size: number;
  old_mtime: string;
  new_path: string;
  new_size: number;
  new_mtime: string;
}

export interface MigrationResult {
  success: boolean;
  message: string;
  needs_confirmation?: boolean;
  conflict_count?: number;
  conflicts?: string[] | ConflictDetail[];
  roms_moved?: number;
  bios_moved?: number;
  saves_moved?: number;
  errors?: string[];
}

export const getMigrationStatus = callable<[], MigrationStatus>("get_migration_status");
export const migrateRetroDeckFiles = callable<[string | null], MigrationResult>("migrate_retrodeck_files");
export const dismissRetrodeckMigration = callable<[], { success: boolean }>("dismiss_retrodeck_migration");

export interface SaveSortMigrationStatus {
  pending: boolean;
  old_settings?: { sort_by_content: boolean; sort_by_core: boolean };
  new_settings?: { sort_by_content: boolean; sort_by_core: boolean };
  saves_count?: number;
}

export const getSaveSortMigrationStatus = callable<[], SaveSortMigrationStatus>("get_save_sort_migration_status");
export const migrateSaveSortFiles = callable<[string | null], MigrationResult>("migrate_save_sort_files");
export const dismissSaveSortMigration = callable<[], { success: boolean }>("dismiss_save_sort_migration");
export const refreshMigrationState = callable<[], { retrodeck: MigrationStatus; save_sort: SaveSortMigrationStatus }>("refresh_migration_state");

// End-of-session orchestration — collapses recordSessionEnd + syncAchievementsAfterSession
// + postExitSync + refreshMigrationState into a single backend round-trip.
// See SessionLifecycleService in py_modules/services/session_lifecycle.py.
interface SessionFinalizeSyncResult {
  offline: boolean;
  success: boolean;
  synced: number | null;
  conflicts: SyncConflict[];
  toast_title: string | null;
  toast_body: string | null;
  conflicts_toast: string | null;
}

interface SessionFinalizeMigration {
  retrodeck: MigrationStatus;
  save_sort: SaveSortMigrationStatus;
}

export interface SessionFinalizeResult {
  total_seconds: number | null;
  sync: SessionFinalizeSyncResult;
  // ``null`` when the backend's migration-state refresh raised — the
  // frontend then leaves the migration stores untouched (any stale
  // ``pending`` badge keeps showing), matching the pre-PR behavior
  // where ``refreshMigrationState().catch`` logged without clearing.
  migration: SessionFinalizeMigration | null;
}

export const finalizeGameSession = callable<[number], SessionFinalizeResult>("finalize_game_session");

// Delete operations
export const deleteLocalSaves = callable<[number], { success: boolean; deleted_count: number; message: string }>("delete_local_saves");
export const deletePlatformSaves = callable<[string], { success: boolean; deleted_count: number; message: string }>("delete_platform_saves");
export const deletePlatformBios = callable<[string], { success: boolean; deleted_count: number; message: string }>("delete_platform_bios");

// Save version history callables
export interface SaveVersionEntry {
  id: number;
  file_name: string;
  emulator: string | null;
  updated_at: string;
  file_size_bytes: number | null;
  device_syncs: Array<{ device_id: string; device_name: string; is_current: boolean; last_synced_at: string | null }>;
  uploaded_by_us?: boolean | null;
}

export type RollbackStatus =
  | { status: "ok" }
  | { status: "rom_not_installed" }
  | { status: "version_deleted" }
  | { status: "unsupported" }
  | { status: "server_unreachable"; error: string }
  | { status: "conflict_blocked"; conflicts: SyncConflict[] }
  | { status: "preflight_failed"; errors: string[] }
  | { status: "put_failed"; error: string };

export type ListFileVersionsResult =
  | { status: "ok"; versions: SaveVersionEntry[] }
  | { status: "server_unreachable"; error: string };

export const savesListFileVersions = callable<[number, string, string], ListFileVersionsResult>("saves_list_file_versions");
export const savesRollbackToVersion = callable<[number, string, number], RollbackStatus>("saves_rollback_to_version");

// Achievements callables
export const getAchievements = callable<[number], AchievementList>("get_achievements");
export const getAchievementProgress = callable<[number], AchievementProgress>("get_achievement_progress");
