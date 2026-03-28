import { callable } from "@decky/api";
import type { PluginSettings, SyncStats, DownloadItem, InstalledRom, PlatformSyncSetting, CollectionSyncSetting, RegistryPlatform, FirmwareStatus, FirmwareDownloadResult, BiosStatus, BiosFileStatus, RomMetadata, SaveSyncSettings, SaveStatus, PendingConflict, NewerInSlotConflict, RomLookupResult, AvailableCore, RommErrorCode, SyncPreview, AchievementSummary, AchievementList, AchievementProgress, SaveSlotSummary, SaveSetupInfo } from "../types";

export interface BackendResult {
  success: boolean;
  message: string;
  error_code?: RommErrorCode;
  romm_version?: string;
  version_warning?: string;
}

export interface CachedGameDetail {
  found: boolean;
  rom_id?: number;
  rom_name?: string;
  platform_slug?: string;
  platform_name?: string;
  installed?: boolean;
  save_sync_enabled?: boolean;
  save_status?: { files: Array<{ filename: string; status: string; last_sync_at?: string }>; last_sync_check_at?: string; conflicts?: PendingConflict[] } | null;

  metadata?: Record<string, unknown> | null;
  bios_status?: { needs_bios?: boolean; platform_slug: string; server_count: number; local_count: number; all_downloaded: boolean; required_count?: number; required_downloaded?: number; active_core?: string; active_core_label?: string; available_cores?: AvailableCore[]; cached_at?: number; files?: BiosFileStatus[] } | null;
  rom_file?: string;
  ra_id?: number | null;
  achievement_summary?: AchievementSummary | null;
  bios_level?: "ok" | "partial" | "missing" | null;
  bios_label?: string | null;
  save_sync_display?: { status: "synced" | "conflict" | "none"; label: string } | null;
  stale_fields?: string[];
}

const _cachedGameDetailRaw = callable<[number], CachedGameDetail>("get_cached_game_detail");
export const _cachedGameDetailCache: Record<number, { promise: Promise<CachedGameDetail>; ts: number }> = {};
const CACHE_TTL_MS = 3000; // reuse result for 3 seconds

export function getCachedGameDetail(appId: number): Promise<CachedGameDetail> {
  const now = Date.now();
  const entry = _cachedGameDetailCache[appId];
  if (entry && now - entry.ts < CACHE_TTL_MS) return entry.promise;
  const promise = _cachedGameDetailRaw(appId);
  _cachedGameDetailCache[appId] = { promise, ts: now };
  promise.then(() => {
    setTimeout(() => { delete _cachedGameDetailCache[appId]; }, CACHE_TTL_MS);
  }, () => {
    delete _cachedGameDetailCache[appId];
  });
  return promise;
}
export const getSettings = callable<[], PluginSettings>("get_settings");
export const saveSettings = callable<[string, string, string, boolean], BackendResult>("save_settings");

export interface WhitelistSettings {
  disabled_defaults: string[];
  custom_names: string[];
}
export const getWhitelistSettings = callable<[], WhitelistSettings>("get_whitelist_settings");
export const updateWhitelistSettings = callable<[string[], string[]], { success: boolean; message?: string }>("update_whitelist_settings");

export const testConnection = callable<[], BackendResult>("test_connection");
export const getRommVersion = callable<[], { version: string | null }>("get_romm_version");
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
export const getRomBySteamAppId = callable<[number], RomLookupResult | null>("get_rom_by_steam_app_id");
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
export const reportSyncResults = callable<[Record<string, number>, number[], boolean], { success: boolean }>("report_sync_results");
export const reportRemovalResults = callable<[(string | number)[]], { success: boolean; message: string }>("report_removal_results");
export const uninstallAllRoms = callable<[], { success: boolean; message: string; removed_count: number }>("uninstall_all_roms");
export const saveSgdbApiKey = callable<[string], { success: boolean; message: string }>("save_sgdb_api_key");
export const verifySgdbApiKey = callable<[string], { success: boolean; message: string }>("verify_sgdb_api_key");
export const saveSteamInputSetting = callable<[string], { success: boolean }>("save_steam_input_setting");
export const applySteamInputSetting = callable<[], { success: boolean; message: string }>("apply_steam_input_setting");
export const getFirmwareStatus = callable<[], FirmwareStatus>("get_firmware_status");
export const downloadFirmware = callable<[number], FirmwareDownloadResult>("download_firmware");
export const downloadAllFirmware = callable<[string], FirmwareDownloadResult>("download_all_firmware");
export const downloadRequiredFirmware = callable<[string], FirmwareDownloadResult>("download_required_firmware");
export const checkPlatformBios = callable<[string], BiosStatus>("check_platform_bios");
export const getBiosStatus = callable<[number], { bios_status: CachedGameDetail["bios_status"] }>("get_bios_status");
export const getAvailableCores = callable<[string], { cores: AvailableCore[]; active_core: string | null; active_core_label: string | null }>("get_available_cores");
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
export const getSaveStatus = callable<[number], SaveStatus>("get_save_status");
export const preLaunchSync = callable<[number], { success: boolean; message: string; synced?: number; errors?: string[]; conflicts?: (PendingConflict | NewerInSlotConflict)[] }>("pre_launch_sync");
export const postExitSync = callable<[number], { success: boolean; message: string; synced?: number; errors?: string[]; conflicts?: (PendingConflict | NewerInSlotConflict)[]; offline?: boolean }>("post_exit_sync");
export const syncRomSaves = callable<[number], { success: boolean; message: string; synced: number; errors?: string[] }>("sync_rom_saves");
export const syncAllSaves = callable<[], { success: boolean; message: string; synced: number; conflicts: number }>("sync_all_saves");
export const resolveConflict = callable<[number, string, string, number, string], { success: boolean; message: string }>("resolve_conflict");
export const recordSessionStart = callable<[number], { success: boolean }>("record_session_start");
export const recordSessionEnd = callable<[number], { success: boolean; duration_sec?: number; total_seconds?: number; session_count?: number; message?: string }>("record_session_end");
export const getSaveSyncSettings = callable<[], SaveSyncSettings>("get_save_sync_settings");
export const updateSaveSyncSettings = callable<[SaveSyncSettings], { success: boolean }>("update_save_sync_settings");
export const getSaveSlots = callable<[number], { success: boolean; slots: SaveSlotSummary[]; active_slot: string }>("get_save_slots");
export const setGameSlot = callable<[number, string], { success: boolean; active_slot?: string; message?: string }>("set_game_slot");
export const isSaveTrackingConfigured = callable<[number], { configured: boolean; active_slot: string | null }>("is_save_tracking_configured");
export const getSaveSetupInfo = callable<[number], SaveSetupInfo>("get_save_setup_info");
export const confirmSlotChoice = callable<[number, string, string | null], { success: boolean; needs_conflict_resolution?: boolean; message: string }>("confirm_slot_choice");
export const resolveNewerInSlot = callable<[number, string, "use_newer" | "keep_current" | "dismiss", number], { success: boolean; message?: string }>("resolve_newer_in_slot");

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

export interface ConflictDetail {
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
  conflict_details?: ConflictDetail[];
  roms_moved?: number;
  bios_moved?: number;
  saves_moved?: number;
  errors?: string[];
}

export const getMigrationStatus = callable<[], MigrationStatus>("get_migration_status");
export const migrateRetroDeckFiles = callable<[string | null], MigrationResult>("migrate_retrodeck_files");

export interface SaveSortMigrationStatus {
  pending: boolean;
  old_settings?: { sort_by_content: boolean; sort_by_core: boolean };
  new_settings?: { sort_by_content: boolean; sort_by_core: boolean };
  saves_count?: number;
}

export const getSaveSortMigrationStatus = callable<[], SaveSortMigrationStatus>("get_save_sort_migration_status");
export const migrateSaveSortFiles = callable<[string | null], MigrationResult>("migrate_save_sort_files");
export const dismissSaveSortMigration = callable<[], { success: boolean }>("dismiss_save_sort_migration");

// Delete operations
export const deleteLocalSaves = callable<[number], { success: boolean; deleted_count: number; message: string }>("delete_local_saves");
export const deletePlatformSaves = callable<[string], { success: boolean; deleted_count: number; message: string }>("delete_platform_saves");
export const deletePlatformBios = callable<[string], { success: boolean; deleted_count: number; message: string }>("delete_platform_bios");

// Achievements callables
export const getAchievements = callable<[number], AchievementList>("get_achievements");
export const getAchievementProgress = callable<[number], AchievementProgress>("get_achievement_progress");
export const syncAchievementsAfterSession = callable<[number], AchievementProgress>("sync_achievements_after_session");
