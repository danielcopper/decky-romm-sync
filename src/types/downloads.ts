/**
 * Per-ROM download types — queue entries and progress/completion events
 * for individual ROM file downloads triggered from the UI.
 */

export interface DownloadItem {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_name: string;
  status: "queued" | "downloading" | "extracting" | "completed" | "failed" | "cancelled" | "paused";
  progress: number;
  bytes_downloaded: number;
  total_bytes: number;
  /**
   * Whether the in-flight transfer can be paused and resumed. True only for
   * single-file ROMs on a direct connection where the server honoured the
   * Range probe; false for multi-file (zip) ROMs and servers behind Cloudflare.
   * The frontend offers Pause/Resume only when this is true.
   */
  resumable: boolean;
  error?: string;
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
  /** Server's Range-support verdict; carried live once response headers arrive. */
  resumable?: boolean;
}

export interface DownloadCompleteEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_path: string;
  /**
   * Bound Steam `app_id` for this ROM, or `null` when the ROM isn't synced
   * yet (no shortcut). Resolved on the backend so the handler confirm-sets
   * launch options on the exact shortcut without a full-library scan.
   */
  app_id: number | null;
  /** Full launch command for the just-downloaded ROM (now installed/launchable). */
  launch_options: string;
  /** Whether the just-finished transfer was resumable (carried for store parity). */
  resumable?: boolean;
  prune_lease_token?: string;
}

export interface DownloadFailedEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  error_message: string;
}

/**
 * The two sides of a download that stopped because its target path is taken
 * (#260). `sizes_match` is the comparison already made — `null` when the server
 * stated no size, so the dialog says "can't compare" rather than implying a
 * difference. `adoptable` is false when what is in the way is the wrong shape
 * for this ROM (a folder where the server serves one file, or the reverse),
 * which leaves replacing or cancelling as the only honest exits.
 */
export interface TargetOccupiedResult {
  success: false;
  reason: "target_occupied";
  message: string;
  existing: {
    name: string;
    path: string;
    is_dir: boolean;
    size_bytes: number;
    /** POSIX epoch seconds. */
    modified_at: number;
  };
  incoming: { name: string; size_bytes: number };
  sizes_match: boolean | null;
  adoptable: boolean;
}

/** Outcome of `adopt_existing_rom` — shaped like a completed download's bake. */
export interface AdoptResult {
  success: boolean;
  message: string;
  reason?: string;
  file_path?: string;
  rom_dir?: string | null;
  /** Bound Steam `app_id`, or `null` when the ROM has no shortcut yet. */
  app_id?: number | null;
  launch_options?: string;
  prune_lease_token?: string;
}

/**
 * Outcome of `verify_existing_content`. A status union rather than a boolean:
 * "the server publishes no checksums" is its own answer, never a match and
 * never a mismatch.
 *
 * Each difference names one file and carries the whole sentence about it, so it
 * renders as one line. The backend owns that wording: two 32-character digests
 * said no more than "these differ" and wrapped the line into an unreadable
 * block, while a size difference states both numbers because those are numbers
 * a person can act on.
 */
export interface VerifyContentResult {
  status: "match" | "mismatch" | "unverifiable" | "missing" | "error";
  message: string;
  differences: Array<{ name: string; detail: string }>;
}

/** Byte progress of a user-requested content verification. */
export interface VerifyProgressEvent {
  rom_id: number;
  bytes_done: number;
  bytes_total: number;
}

/**
 * Per-file progress of an in-flight uninstall. Emitted only while removing a
 * multi-file ROM — a single-file removal has nothing to report between "started"
 * and "done".
 */
export interface UninstallProgressEvent {
  rom_id: number;
  files_removed: number;
  files_total: number;
}
