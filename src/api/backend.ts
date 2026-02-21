import { callable } from "@decky/api";
import type { PluginSettings, SyncStats, DownloadItem, InstalledRom, PlatformSyncSetting, RegistryPlatform, FirmwareStatus, FirmwareDownloadResult, BiosStatus, RomMetadata, SaveSyncSettings, SaveStatus, PendingConflict, OfflineQueueItem } from "../types";

export const getSettings = callable<[], PluginSettings>("get_settings");
export const saveSettings = callable<[string, string, string], { success: boolean; message: string }>("save_settings");
export const testConnection = callable<[], { success: boolean; message: string }>("test_connection");
export const startSync = callable<[], { success: boolean; message: string }>("start_sync");
export const cancelSync = callable<[], { success: boolean; message: string }>("cancel_sync");
export const getSyncStats = callable<[], SyncStats>("get_sync_stats");
export const startDownload = callable<[number], { success: boolean; message: string }>("start_download");
export const cancelDownload = callable<[number], { success: boolean; message: string }>("cancel_download");
export const getDownloadQueue = callable<[], { downloads: DownloadItem[] }>("get_download_queue");
export const getInstalledRom = callable<[number], InstalledRom | null>("get_installed_rom");
export const getRomBySteamAppId = callable<[number], any | null>("get_rom_by_steam_app_id");
export const removeRom = callable<[number], { success: boolean; message: string }>("remove_rom");
export const getPlatforms = callable<[], { success: boolean; platforms: PlatformSyncSetting[] }>("get_platforms");
export const savePlatformSync = callable<[number, boolean], { success: boolean; message: string }>("save_platform_sync");
export const setAllPlatformsSync = callable<[boolean], { success: boolean; message: string }>("set_all_platforms_sync");
export const getRegistryPlatforms = callable<[], { platforms: RegistryPlatform[] }>("get_registry_platforms");
export const removePlatformShortcuts = callable<[string], { success: boolean; app_ids: number[]; rom_ids: (string | number)[]; platform_name: string }>("remove_platform_shortcuts");
export const removeAllShortcuts = callable<[], { success: boolean; message: string; removed_count: number; app_ids: number[]; rom_ids: (string | number)[] }>("remove_all_shortcuts");
export const getArtworkBase64 = callable<[number], { base64: string | null }>("get_artwork_base64");
export const getSgdbArtworkBase64 = callable<[number, number], { base64: string | null; no_api_key?: boolean }>("get_sgdb_artwork_base64");
export const reportSyncResults = callable<[Record<string, number>, number[]], { success: boolean }>("report_sync_results");
export const reportRemovalResults = callable<[(string | number)[]], { success: boolean; message: string }>("report_removal_results");
export const uninstallAllRoms = callable<[], { success: boolean; message: string; removed_count: number }>("uninstall_all_roms");
export const saveSgdbApiKey = callable<[string], { success: boolean; message: string }>("save_sgdb_api_key");
export const verifySgdbApiKey = callable<[string], { success: boolean; message: string }>("verify_sgdb_api_key");
export const saveSteamInputSetting = callable<[string], { success: boolean }>("save_steam_input_setting");
export const applySteamInputSetting = callable<[], { success: boolean; message: string }>("apply_steam_input_setting");
export const getFirmwareStatus = callable<[], FirmwareStatus>("get_firmware_status");
export const downloadFirmware = callable<[number], FirmwareDownloadResult>("download_firmware");
export const downloadAllFirmware = callable<[string], FirmwareDownloadResult>("download_all_firmware");
export const checkPlatformBios = callable<[string], BiosStatus>("check_platform_bios");
export const saveDebugLogging = callable<[boolean], { success: boolean }>("save_debug_logging");
export const debugLog = callable<[string], void>("debug_log");
export const fixRetroarchInputDriver = callable<[], { success: boolean; message: string }>("fix_retroarch_input_driver");
export const getRomMetadata = callable<[number], RomMetadata>("get_rom_metadata");
export const getAllMetadataCache = callable<[], Record<string, RomMetadata>>("get_all_metadata_cache");
export const getAppIdRomIdMap = callable<[], Record<string, number>>("get_app_id_rom_id_map");

// Icon support (VDF-based)
export const saveShortcutIcon = callable<[number, string], { success: boolean }>("save_shortcut_icon");

// Save sync callables
export const ensureDeviceRegistered = callable<[], { success: boolean; device_id: string; device_name: string }>("ensure_device_registered");
export const getSaveStatus = callable<[number], SaveStatus>("get_save_status");
export const preLaunchSync = callable<[number], { success: boolean; message: string; synced?: number; errors?: string[] }>("pre_launch_sync");
export const postExitSync = callable<[number], { success: boolean; message: string; synced?: number; errors?: string[] }>("post_exit_sync");
export const syncRomSaves = callable<[number], { success: boolean; message: string; synced: number; errors?: string[] }>("sync_rom_saves");
export const syncAllSaves = callable<[], { success: boolean; message: string; synced: number; conflicts: number }>("sync_all_saves");
export const resolveConflict = callable<[number, string, string], { success: boolean; message: string }>("resolve_conflict");
export const getPendingConflicts = callable<[], { conflicts: PendingConflict[] }>("get_pending_conflicts");
export const recordSessionStart = callable<[number], { success: boolean }>("record_session_start");
export const recordSessionEnd = callable<[number], { success: boolean; duration_sec?: number; total_seconds?: number; session_count?: number; message?: string }>("record_session_end");
export const getSaveSyncSettings = callable<[], SaveSyncSettings>("get_save_sync_settings");
export const updateSaveSyncSettings = callable<[SaveSyncSettings], { success: boolean }>("update_save_sync_settings");

// Bulk playtime for plugin-load UI update
export const getAllPlaytime = callable<[], { playtime: Record<string, { total_seconds: number; session_count: number }> }>("get_all_playtime");

// Offline queue (failed sync retry)
export const getOfflineQueue = callable<[], { queue: OfflineQueueItem[] }>("get_offline_queue");
export const retryFailedSync = callable<[number, string], { success: boolean; message: string; synced?: number }>("retry_failed_sync");
export const clearOfflineQueue = callable<[], { success: boolean }>("clear_offline_queue");
