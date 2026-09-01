/**
 * Firmware and BIOS types — server-side firmware inventory, local BIOS
 * file status, and the available-cores selection presented in the UI.
 */

/**
 * What the machine answers about one firmware file, in the four values the
 * resolver's per-file answer and the reading's completeness together produce.
 * `not_needed` is a finished answer — every emulator the platform offers was
 * read and none asks for the file — while `unknown` is the absence of one. They
 * are never folded together: the collapse is what made a file the plugin could
 * not ask about look like a file nothing wanted.
 */
export type FirmwareWanted = "needed" | "optional" | "not_needed" | "unknown";

interface FirmwareFile {
  /** `null` for a file an installed emulator asks for that the RomM library does
   *  not hold — there is no server record to name. Nothing reads it: what the
   *  page filters the download buttons and its progress totals on is
   *  `on_server`. */
  id: number | null;
  file_name: string;
  size: number;
  md5: string;
  local_path: string;
  downloaded: boolean;
  description: string;
  wanted: FirmwareWanted;
  /** Whether the platform's launching core is the one that requires it — the
   *  axis the "BIOS needed" badge and the required counts key off, distinct
   *  from `wanted`, which is about every installed emulator. */
  required_by_active: boolean;
  on_server: boolean;
}

interface FirmwarePlatform {
  platform_slug: string;
  files: FirmwareFile[];
}

/**
 * One ES-DE `<command>` classified for launch-bakeability (#1210), the shape the
 * backend `get_emulator_options` / picker payloads carry. `kind` is `"libretro"`
 * (`core_so` set to the bare core name) or `"standalone"` (`core_so` null).
 * `bakeable` is false for the `needs_setup` (`reason: "inject"`) and un-bakeable
 * forms; `reason` names why the frontend can't offer it as a clickable pick.
 */
export interface EmulatorOption {
  label: string;
  kind: "libretro" | "standalone";
  core_so: string | null;
  is_default: boolean;
  bakeable: boolean;
  reason: string | null;
}

/**
 * Response shape of the `get_platform_core_info` callable — the dedicated
 * single-platform emulator-info path, decoupled from the per-game BIOS payload
 * (#923). The per-game detail page (`RomMPlaySection` / `RomMGameInfoPanel`)
 * reads emulator data from here. The System page's multi-platform overview
 * instead reads it off the `get_firmware_status` payload (`FirmwarePlatformExt`),
 * which enumerates every platform in one call — see that interface below.
 * `emulator_data_available` is false when `es_systems.xml` cannot be read
 * (RetroDECK not detected), so the picker can say so instead of an empty list.
 */
export interface CoreInfo {
  emulators: EmulatorOption[];
  emulator_data_available: boolean;
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
 *
 * `files` is the union of what the RomM library offers for the platform and what
 * the platform's emulators ask for, so a row can be present with `on_server`
 * false — wanted, possibly missing, and not downloadable from here.
 */
export interface FirmwarePlatformExt extends FirmwarePlatform {
  has_games?: boolean;
  all_downloaded?: boolean;
  active_core?: string;
  active_core_label?: string;
  emulators?: EmulatorOption[];
  emulator_data_available?: boolean;
  // Per-platform BIOS aggregates computed by the backend from the same
  // core-aware classified files (`compute_bios_level`), so the System page reads
  // the unknown/ok/partial/missing decision and display counts off the payload
  // instead of re-deriving the threshold logic. The optional-missing breakdown
  // stays a local file-level computation (a richer axis the level doesn't model).
  bios_level?: BiosLevel | null;
  required_count?: number;
  required_downloaded?: number;
  server_count?: number;
  local_count?: number;
  known_count?: number;
  unknown_count?: number;
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
  description: string;
  wanted: FirmwareWanted;
  /** Whether the core THIS game launches with requires the file. `wanted` is the
   *  machine's answer about the file; this one is the launch's. */
  required_by_active: boolean;
  cores?: Record<string, { required: boolean }>;
  used_by_active?: boolean;
  /** False for a file an emulator asks for that the RomM library does not hold.
   *  It still counts as missing — it just cannot be fetched from the plugin. */
  on_server?: boolean;
}

/**
 * The backend's readiness decision for a platform's BIOS. `"unknown"` means the
 * server holds firmware and no installed emulator's answer could be established
 * for any of it — a neutral state, never a green all-clear.
 */
export type BiosLevel = "ok" | "partial" | "missing" | "unknown";

export interface BiosStatus {
  needs_bios: boolean;
  server_count?: number;
  local_count?: number;
  all_downloaded?: boolean;
  required_count?: number;
  required_downloaded?: number;
  // Server files an installed emulator asks for, and files nothing could answer
  // about. A `not_needed` file is in neither — it is answered for, and wanted by
  // nothing — which is what keeps "nothing here is needed" apart from "nothing
  // could be established".
  known_count?: number;
  unknown_count?: number;
  files?: BiosFileStatus[];
  // unknown/ok/partial/missing state computed by the backend (compute_bios_level)
  // so the frontend reads the decision off the payload instead of re-deriving the
  // threshold logic. Present only when needs_bios is true.
  bios_level?: BiosLevel | null;
  // Set when the check could not determine the requirement: the firmware fetch
  // failed and nothing was cached, so which files even exist is unknown. The
  // `needs_bios: false` it rides on is ignorance, not an answer, so no consumer
  // may clear a shown requirement on it (#1693).
  bios_status_unknown?: boolean;
}

export interface FirmwareDownloadResult {
  success: boolean;
  message?: string;
  file_path?: string;
  md5_match?: boolean | null;
  downloaded?: number;
  blocked_by_migration?: boolean;
}
