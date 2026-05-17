export type RommErrorCode =
  | "auth_error"
  | "forbidden_error"
  | "connection_error"
  | "timeout_error"
  | "ssl_error"
  | "server_error"
  | "not_found_error"
  | "unsupported_error"
  | "version_error"
  | "config_error"
  | "disk_error"
  | "api_error"
  | "stale_conflict"
  | "stale_preview"
  | "unknown_error";

export interface InstalledRom {
  rom_id: number;
  file_name: string;
  file_path: string;
  system: string;
  platform_slug: string;
  installed_at: string;
}

export interface RetroArchInputCheck {
  warning: boolean;
  current?: string;
  config_path?: string;
}

export interface PluginSettings {
  romm_url: string;
  romm_user: string;
  romm_pass_masked: string;
  has_credentials: boolean;
  steam_input_mode: "default" | "force_on" | "force_off";
  sgdb_api_key_masked: string;
  log_level: "debug" | "info" | "warn" | "error";
  romm_allow_insecure_ssl: boolean;
  retroarch_input_check?: RetroArchInputCheck;
  collection_create_platform_groups?: boolean;
}

export interface DownloadItem {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_name: string;
  status: "queued" | "downloading" | "completed" | "failed" | "cancelled";
  progress: number;
  bytes_downloaded: number;
  total_bytes: number;
  error?: string;
}

export interface PlatformSyncSetting {
  id: number;
  name: string;
  slug: string;
  rom_count: number;
  sync_enabled: boolean;
}

export interface CollectionSyncSetting {
  id: string;
  name: string;
  rom_count: number;
  sync_enabled: boolean;
  category: "favorites" | "user" | "franchise";
}

export interface SyncProgress {
  running: boolean;
  phase?: string;
  current?: number;
  total?: number;
  message?: string;
  step?: number;
  totalSteps?: number;
}

export interface SyncStats {
  last_sync: string | null;
  platforms: number;
  collections?: number;
  roms: number;
  total_shortcuts: number;
}

export interface RegistryPlatform {
  name: string;
  slug: string;
  count: number;
}

export interface SyncAddItem {
  rom_id: number;
  name: string;
  exe: string;
  start_dir: string;
  launch_options: string;
  platform_name: string;
  cover_path: string;
}

export interface SyncPreviewSummary {
  new_count: number;
  changed_count: number;
  unchanged_count: number;
  remove_count: number;
  disabled_platform_remove_count: number;
  collection_diff?: {
    has_changes: boolean;
    added: string[];
    removed: string[];
  };
  platform_collection_diff?: {
    has_changes: boolean;
    added_count: number;
    removed_count: number;
  };
}

export interface SyncPreview {
  success: boolean;
  summary: SyncPreviewSummary;
  new_names: string[];
  changed_names: string[];
  preview_id: string;
  message?: string;
  blocked_by_migration?: boolean;
}

interface SyncPlanUnit {
  type: "platform" | "collection";
  id: number | string;
  name: string;
  slug: string;
  rom_count: number;
}

export interface SyncPlanData {
  units: SyncPlanUnit[];
  total_units: number;
  total_roms: number;
}

export interface SyncApplyUnitData {
  unit_type: "platform" | "collection";
  unit_id: number | string;
  unit_name: string;
  unit_index: number;
  total_units: number;
  shortcuts: SyncAddItem[];
}

export interface SyncStaleData {
  remove_rom_ids: number[];
}

export interface SyncCollectionsData {
  platform_app_ids: Record<string, number[]>;
  romm_collection_app_ids: Record<string, number[]>;
}

interface FirmwareFile {
  id: number;
  file_name: string;
  size: number;
  md5: string;
  downloaded: boolean;
  required: boolean;
  description: string;
  hash_valid: boolean | null;
  classification: "required" | "optional" | "unknown";
}

interface FirmwarePlatform {
  platform_slug: string;
  files: FirmwareFile[];
}

export interface AvailableCore {
  core_so: string;
  label: string;
  is_default: boolean;
}

export interface FirmwarePlatformExt extends FirmwarePlatform {
  has_games?: boolean;
  all_downloaded?: boolean;
  active_core?: string;
  active_core_label?: string;
  available_cores?: AvailableCore[];
}

export interface FirmwareStatus {
  success: boolean;
  message?: string;
  server_offline?: boolean;
  platforms: FirmwarePlatformExt[];
}

export interface BiosFileStatus {
  file_name: string;
  downloaded: boolean;
  local_path: string;
  required: boolean;
  description: string;
  classification: "required" | "optional" | "unknown";
  cores?: Record<string, { required: boolean }>;
  used_by_active?: boolean;
}

export interface BiosStatus {
  needs_bios: boolean;
  server_count?: number;
  local_count?: number;
  all_downloaded?: boolean;
  required_count?: number;
  required_downloaded?: number;
  unknown_count?: number;
  files?: BiosFileStatus[];
  active_core?: string;
  active_core_label?: string;
  available_cores?: AvailableCore[];
}

export interface FirmwareDownloadResult {
  success: boolean;
  message?: string;
  file_path?: string;
  md5_match?: boolean | null;
  downloaded?: number;
  blocked_by_migration?: boolean;
}

export interface RomMetadata {
  summary: string;
  genres: string[];
  companies: string[];
  first_release_date: number | null;
  average_rating: number | null;
  game_modes: string[];
  player_count: string;
  cached_at: number;
  steam_categories?: number[];
}

export interface SaveSyncSettings {
  save_sync_enabled: boolean;
  sync_before_launch: boolean;
  sync_after_exit: boolean;
  default_slot: string | null;
  autocleanup_limit: number;
}

export interface SyncConflict {
  type: "sync_conflict";
  rom_id: number;
  filename: string;
  server_save_id: number;
  server_updated_at: string;
  server_size: number | null;
  local_path: string | null;
  local_hash: string | null;
  local_mtime: string | null;
  local_size: number | null;
  created_at: string;
}

export interface DeviceSyncInfo {
  device_id: string;
  device_name: string;
  is_current: boolean;
  last_synced_at: string | null;
}

export interface SaveFileStatus {
  filename: string;
  local_path: string | null;
  local_hash: string | null;
  local_mtime: string | null;
  local_size: number | null;
  server_save_id: number | null;
  server_file_name: string | null;
  server_emulator: string | null;
  server_updated_at: string | null;
  server_size: number | null;
  last_sync_at: string | null;
  status: "skip" | "download" | "upload" | "conflict" | "synced" | "unknown";
  device_syncs?: DeviceSyncInfo[];
  is_current?: boolean;
  uploaded_by_us?: boolean | null;
}

interface PlaytimeEntry {
  total_seconds: number;
  session_count: number;
  last_session_start: string | null;
  last_session_duration_sec: number | null;
}

export interface SaveSyncDisplay {
  status: "synced" | "conflict" | "none";
  /** Static label, e.g. "No saves" / "Conflict" / "Not synced". `null` for the
   *  synced+recent-check case, where the frontend formats a time-ago label
   *  from `last_sync_check_at`. */
  label: string | null;
  /** Raw ISO-8601 timestamp passed through from the backend for time-ago
   *  formatting. `null` whenever `label` carries a fully-formed string. */
  last_sync_check_at: string | null;
}

export interface SaveStatus {
  rom_id: number;
  files: SaveFileStatus[];
  playtime: PlaytimeEntry;
  device_id: string;
  last_sync_check_at: string | null;
  conflicts?: SyncConflict[];
  active_slot?: string | null;
  save_sync_display?: SaveSyncDisplay;
}

export interface SaveSlotSummary {
  slot: string;
  source: "server" | "local";
  count: number;
  latest_updated_at: string | null;
}

export interface SlotSaveFile {
  filename: string;
  id: number;
  size: number | null;
  updated_at: string;
  emulator: string;
}

export interface SlotSavesResponse {
  success: boolean;
  slot: string;
  saves: SlotSaveFile[];
  error?: string;
}

export interface SwitchSlotResponse {
  success: boolean;
  reason?: "pending_uploads" | "server_unreachable" | "sync_disabled" | "not_installed";
  files?: string[];
  save_status?: SaveStatus;
}

interface SaveSetupSlotInfo {
  slot: string | null;
  saves: Array<{
    id: number;
    file_name: string;
    emulator: string;
    updated_at: string;
    file_size_bytes: number;
  }>;
  count: number;
  latest_updated_at: string | null;
}

export interface SaveSetupInfo {
  has_local_saves: boolean;
  local_files: Array<{ filename: string; size: number }>;
  server_slots: SaveSetupSlotInfo[];
  default_slot: string;
  slot_confirmed: boolean;
  active_slot: string | null;
  recommended_action: "auto_confirm_default" | "show_wizard";
}

export interface RomLookupResult {
  rom_id: number;
  name: string;
  platform_name: string;
  platform_slug: string;
  installed: InstalledRom | null;
}

export interface LaunchVerdict {
  action: "allow" | "block";
  reason: "not_installed" | "save_conflict" | null;
  toast_title: string | null;
  toast_body: string | null;
}

export interface DownloadProgressEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_name: string;
  status: string;
  progress: number;
  bytes_downloaded: number;
  total_bytes: number;
}

export interface Achievement {
  ra_id: number;
  badge_id: string;
  title: string;
  description: string;
  points: number;
  badge_url: string;
  badge_url_lock: string;
  display_order: number;
  type: string;
  num_awarded: number;
  num_awarded_hardcore: number;
}

export interface EarnedAchievement {
  id: string;
  date: string;
  date_hardcore: string | null;
}

export interface AchievementSummary {
  earned: number;
  total: number;
  earned_hardcore: number;
  cached_at?: number;
}

export interface AchievementList {
  success: boolean;
  achievements: Achievement[];
  total: number;
  no_ra_id?: boolean;
  stale?: boolean;
  message?: string;
}

export interface AchievementProgress {
  success: boolean;
  earned: number;
  earned_hardcore?: number;
  total: number;
  earned_achievements: EarnedAchievement[];
  no_ra_id?: boolean;
  stale?: boolean;
  message?: string;
}

export interface DownloadCompleteEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_path: string;
}
