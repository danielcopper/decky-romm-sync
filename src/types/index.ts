export interface RomMPlatform {
  id: number;
  slug: string;
  fs_slug: string;
  name: string;
  rom_count: number;
}

export interface RomMRom {
  id: number;
  igdb_id: number | null;
  platform_id: number;
  platform_slug: string;
  platform_name: string;
  file_name: string;
  name: string;
  slug: string;
  summary: string;
  path_cover_s: string;
  path_cover_l: string;
  has_cover: boolean;
  revision: string;
}

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
  debug_logging: boolean;
  retroarch_input_check?: RetroArchInputCheck;
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

export interface SyncProgress {
  running: boolean;
  phase?: string;
  current?: number;
  total?: number;
  message?: string;
}

export interface SyncStats {
  last_sync: string | null;
  platforms: number;
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

export interface SyncApplyData {
  shortcuts: SyncAddItem[];
  remove_rom_ids: number[];
}

export interface FirmwareFile {
  id: number;
  file_name: string;
  size: number;
  md5: string;
  downloaded: boolean;
}

export interface FirmwarePlatform {
  platform_slug: string;
  files: FirmwareFile[];
}

export interface FirmwarePlatformExt extends FirmwarePlatform {
  has_games?: boolean;
  all_downloaded?: boolean;
}

export interface FirmwareStatus {
  success: boolean;
  message?: string;
  platforms: FirmwarePlatformExt[];
}

export interface BiosFileStatus {
  file_name: string;
  downloaded: boolean;
}

export interface BiosStatus {
  needs_bios: boolean;
  server_count?: number;
  local_count?: number;
  all_downloaded?: boolean;
  files?: BiosFileStatus[];
}

export interface FirmwareDownloadResult {
  success: boolean;
  message?: string;
  file_path?: string;
  md5_match?: boolean | null;
  downloaded?: number;
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
}

export type ConflictMode = "newest_wins" | "always_upload" | "always_download" | "ask_me";

export interface SaveSyncSettings {
  save_sync_enabled: boolean;
  conflict_mode: ConflictMode;
  sync_before_launch: boolean;
  sync_after_exit: boolean;
  clock_skew_tolerance_sec: number;
}

export interface PendingConflict {
  rom_id: number;
  filename: string;
  local_path: string | null;
  local_hash: string | null;
  local_mtime: string | null;
  local_size: number | null;
  server_save_id: number;
  server_updated_at: string;
  server_size: number | null;
  created_at: string;
}

export interface OfflineQueueItem {
  rom_id: number;
  filename: string;
  direction: string;
  error: string;
  failed_at: string;
  retry_count: number;
}

export interface SaveFileStatus {
  filename: string;
  local_path: string | null;
  local_hash: string | null;
  local_mtime: string | null;
  local_size: number | null;
  server_save_id: number | null;
  server_updated_at: string | null;
  server_size: number | null;
  last_sync_at: string | null;
  status: "skip" | "download" | "upload" | "conflict";
}

export interface PlaytimeEntry {
  total_seconds: number;
  session_count: number;
  last_session_start: string | null;
  last_session_duration_sec: number | null;
}

export interface SaveStatus {
  rom_id: number;
  files: SaveFileStatus[];
  playtime: PlaytimeEntry;
  device_id: string;
}

export interface DownloadProgressEvent {
  rom_id: number;
  rom_name: string;
  status: string;
  progress: number;
  bytes_downloaded: number;
  total_bytes: number;
}

export interface DownloadCompleteEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_path: string;
}
