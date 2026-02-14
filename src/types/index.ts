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

export interface PluginSettings {
  romm_url: string;
  romm_user: string;
  romm_pass_masked: string;
  has_credentials: boolean;
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
