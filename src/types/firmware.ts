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

/**
 * Response shape of the `get_platform_core_info` callable — the dedicated
 * single-platform core-info path, decoupled from the per-game BIOS payload
 * (#923). The per-game detail page (`RomMPlaySection` / `RomMGameInfoPanel`)
 * reads core data from here. The System page's multi-platform overview instead
 * reads core data off the `get_firmware_status` payload (`FirmwarePlatformExt`),
 * which enumerates every platform in one call — see that interface below.
 */
export interface CoreInfo {
  cores: AvailableCore[];
  active_core: string | null;
  active_core_label: string | null;
  platform_core_label: string | null;
  has_game_override: boolean;
}

/**
 * Per-platform entry in the `get_firmware_status` overview. Carries the
 * platform's active/available cores alongside its BIOS file state so the System
 * page can render the combined core+BIOS overview for every platform from one
 * call. This is the system-wide overview path — distinct from the per-game
 * `check_platform_bios` payload, which no longer carries any core fields (#923).
 */
export interface FirmwarePlatformExt extends FirmwarePlatform {
  has_games?: boolean;
  all_downloaded?: boolean;
  active_core?: string;
  active_core_label?: string;
  available_cores?: AvailableCore[];
  // Per-platform BIOS aggregates computed by the backend from the same
  // core-aware enriched files (`compute_bios_level`), so the System page reads
  // the ok/partial/missing decision and display counts off the payload instead
  // of re-deriving the threshold logic. The optional-missing breakdown stays a
  // local file-level computation (a richer axis the 3-state level doesn't model).
  bios_level?: "ok" | "partial" | "missing" | null;
  required_count?: number;
  required_downloaded?: number;
  server_count?: number;
  local_count?: number;
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
  // ok/partial/missing trichotomy computed by the backend (compute_bios_level)
  // so the frontend reads the classification off the payload instead of
  // re-deriving the threshold logic. Present only when needs_bios is true.
  bios_level?: "ok" | "partial" | "missing" | null;
}

export interface FirmwareDownloadResult {
  success: boolean;
  message?: string;
  file_path?: string;
  md5_match?: boolean | null;
  downloaded?: number;
  blocked_by_migration?: boolean;
}
