/**
 * Per-ROM download types — queue entries and progress/completion events
 * for individual ROM file downloads triggered from the UI.
 */

export interface DownloadItem {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_name: string;
  status: "queued" | "downloading" | "completed" | "failed" | "cancelled" | "paused";
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
}

export interface DownloadFailedEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  error_message: string;
}
