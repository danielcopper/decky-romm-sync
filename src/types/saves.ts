/**
 * Save-sync types — per-file save status, sync conflicts, device attribution,
 * slot-based browser shapes, and the initial save-setup wizard payload.
 * Anything related to RomM save synchronization lives here.
 */

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
  /** True when the backend's ``list_saves`` call raised before the matrix
   *  ran. Every file row carries ``status: "unknown"`` in that case — the
   *  empty server list would otherwise be classified as "ready to upload"
   *  and surface a misleading uploads-pending indicator on what is in
   *  fact a connectivity blip. */
  server_query_failed?: boolean;
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
  // "server_unreachable" means the server-saves fetch failed — the wizard MUST
  // hold and offer a retry instead of treating the empty server_slots as
  // authoritative (auto-confirming default would clobber real server saves on
  // first sync). See backend `get_save_setup_info`.
  recommended_action: "auto_confirm_default" | "show_wizard" | "server_unreachable";
  // Mirrors recommended_action === "server_unreachable" — explicit flag for
  // call sites that route on the boolean rather than the enum.
  server_query_failed?: boolean;
}
