/**
 * Connection, settings, and ROM-lookup types — the backend surface that
 * isn't specific to one feature vertical. Things that talk to RomM at the
 * connection/auth/metadata layer live here; per-domain shapes
 * (sync, saves, firmware, downloads, achievements) live in their own files.
 */

/**
 * Canonical failure-`reason` slugs the backend emits on the `{success: false,
 * reason, message}` shape (see py_modules/lib/list_result.py `ErrorCode` + the
 * gate scripts/check_failure_shape.py). The Lean enum plus the bespoke
 * plain-string reasons the frontend actually routes on. Transport failures
 * collapse onto `server_unreachable`; 401/403 onto `auth_failed` (distinguished
 * by `message`, not slug).
 */
export type RommErrorCode =
  | "server_unreachable"
  | "auth_failed"
  | "not_found"
  | "unsupported"
  | "unknown"
  | "version_error"
  | "stale_conflict"
  | "stale_preview"
  | "config_error";

/**
 * Payload of the `server_retry_progress` event (#1345). Emitted once per retry
 * by the backend HTTP adapter's backoff ladder so the frontend can surface a
 * live "connecting… (attempt N/M)" indicator while a server-touching call is
 * in flight.
 */
export interface ServerRetryProgressEvent {
  /** 1-based number of the retry currently being attempted. */
  attempt: number;
  /** Total attempts the ladder makes before giving up. */
  max_attempts: number;
  /** Backoff delay (seconds) before this retry fires. */
  delay_s: number;
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
  has_token: boolean;
  steam_input_mode: "default" | "force_on" | "force_off";
  sgdb_api_key_masked: string;
  log_level: "debug" | "info" | "warn" | "error";
  romm_allow_insecure_ssl: boolean;
  retroarch_input_check?: RetroArchInputCheck;
  collection_create_platform_groups?: boolean;
  // Preferred sibling-group region (ADR-0021 §3). "auto" = the fixed build-time
  // default order (World > USA > Europe > Japan); any other value heads the
  // ranking with that region on the next sync. Optional: the backend always
  // sends it, but older payloads / test fixtures may omit it.
  preferred_region?: string;
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

export interface LaunchVerdict {
  action: "allow" | "warn" | "block";
  reason: "not_installed" | "save_conflict" | "save_status_failed" | null;
  toast_title: string | null;
  toast_body: string | null;
}
