/**
 * Firmware and BIOS types — server-side firmware inventory, local BIOS
 * file status, and the available-cores selection presented in the UI.
 */

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
