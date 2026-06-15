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

/** The `reason` slug the sync callables return when save sync is blocked because
 *  RetroArch writes saves to the content directory (#239). A BENIGN SKIP — the
 *  game still launches and no error is surfaced. Mirrors the backend
 *  `SAVE_SYNC_IN_CONTENT_DIR_REASON`. */
export const SAVEFILES_IN_CONTENT_DIR_REASON = "savefiles_in_content_dir";

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
  /** True when the active slot's current save spans more than one distinct
   *  file (e.g. Sega Saturn `.bkr`/`.bcr`/`.smpc`). Those siblings are
   *  components of one game state, not prior versions — so the frontend
   *  suppresses per-file version history + rollback and shows the component
   *  list instead. Interim #908 guard. */
  multi_file?: boolean;
  /** The N filenames that together make up the current save (sorted). Set
   *  alongside `multi_file`; one entry for a single-file slot. */
  component_files?: string[];
  /** False when per-version rollback is unavailable for the slot — currently
   *  only for multi-file saves (mirrors `!multi_file`). */
  rollback_supported?: boolean;
  /** True when RetroArch's `savefiles_in_content_dir=true` — saves are written
   *  next to the ROM, outside the saves tree the plugin syncs, so save sync is
   *  unsupported. Derived from a LOCAL retroarch.cfg read, so it is correct even
   *  when the server is unreachable (independent of `server_query_failed`). In
   *  this case `files` is `[]` and `save_sync_display` reports the "off" state
   *  (#239). */
  savefiles_in_content_dir?: boolean;
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
  reason?: "server_unreachable" | "sync_disabled";
  message?: string;
}

export interface SwitchSlotResponse {
  success: boolean;
  reason?: "pending_uploads" | "server_unreachable" | "sync_disabled" | "not_installed" | "switch_incomplete";
  message?: string;
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

export interface SlotDeleteInfo {
  success: boolean;
  slot?: string;
  source?: "server" | "local";
  server_save_count?: number;
  server_save_ids?: number[];
  local_file_count?: number;
  local_filenames?: string[];
  is_active?: boolean;
  // Coarse failure category for routing (e.g. "server_unreachable",
  // "not_found", "not_installed", "disabled", "active_slot").
  reason?: string;
  message?: string;
}

export interface DeleteSlotResult {
  success: boolean;
  deleted_server_saves?: number;
  cleaned_files?: number;
  reason?: string;
  message?: string;
}

export interface SaveVersionEntry {
  id: number;
  file_name: string;
  emulator: string | null;
  updated_at: string;
  file_size_bytes: number | null;
  device_syncs: Array<{ device_id: string; device_name: string; is_current: boolean; last_synced_at: string | null }>;
  uploaded_by_us?: boolean | null;
}

export type RollbackStatus =
  | { status: "ok" }
  | { status: "rom_not_installed" }
  | { status: "version_deleted" }
  | { status: "unsupported" }
  | { status: "server_unreachable"; message: string }
  | { status: "conflict_blocked"; conflicts: SyncConflict[] }
  | { status: "preflight_failed"; errors: string[] }
  | { status: "put_failed"; message: string };

export type ListFileVersionsResult =
  | { status: "ok"; versions: SaveVersionEntry[] }
  | { status: "multi_file_unsupported"; versions: SaveVersionEntry[] }
  | { status: "server_unreachable"; message: string };
