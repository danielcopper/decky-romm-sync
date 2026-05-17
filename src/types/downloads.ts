/**
 * Per-ROM download types — queue entries and progress/completion events
 * for individual ROM file downloads triggered from the UI.
 */

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

export interface DownloadCompleteEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  file_path: string;
}

export interface DownloadFailedEvent {
  rom_id: number;
  rom_name: string;
  platform_name: string;
  error_message: string;
}
