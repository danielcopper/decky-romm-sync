/**
 * Pure formatters and selectors for the SavesTab UI. Anything that takes
 * inputs and returns outputs without touching component state or React
 * belongs here; rendering helpers live alongside their components.
 */

import type { DeviceSyncInfo, SaveStatus, SyncConflict, SlotDeleteInfo } from "../../types";

export const MUTED_COLOR = "#8f98a0";

/** Display a slot name, using "(no slot)" for null/empty values */
export function displaySlot(slot: string | null | undefined): string {
  if (slot === null || slot === undefined || slot === "") return "(no slot)";
  return slot;
}

/** Format a relative time string (e.g. "5m ago", "2h ago") from an ISO string */
export function formatRelativeTime(isoStr: string | null): string {
  if (!isoStr) return "";
  const date = new Date(isoStr);
  if (Number.isNaN(date.getTime())) return "";
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffMin < 1440) return `${Math.floor(diffMin / 60)}h ago`;
  const d = date.getDate();
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[date.getMonth()]}`;
}

/** Pick the most recently synced device from a device_syncs array, or null */
export function pickLastSyncer(syncs: DeviceSyncInfo[] | undefined): DeviceSyncInfo | null {
  if (!syncs || syncs.length === 0) return null;
  return syncs.reduce<DeviceSyncInfo | null>((latest, ds) => {
    if (!latest) return ds;
    if (!ds.last_synced_at) return latest;
    if (!latest.last_synced_at) return ds;
    return ds.last_synced_at > latest.last_synced_at ? ds : latest;
  }, null);
}

/**
 * Return an attribution label based on the uploaded_by_us flag, or null if unknown.
 * NOTE: "this device" is really "this plugin installation" — if state.json is
 * copied to another machine, the label would incorrectly claim local ownership.
 */
export function attributionLabel(uploadedByUs: boolean | null | undefined): string | null {
  if (uploadedByUs === true) return "(this device)";
  if (uploadedByUs === false) return "(not this device)";
  return null;
}

/**
 * Format the per-save attribution+checkmark segment for the sync-time line.
 *
 * Combines device name, attribution label, and the trailing checkmark into
 * one ready-to-render string, or null when nothing meaningful can be shown.
 * Reused between `renderSaveFileRow` and `renderVersionRow`.
 */
export function formatAttributionSegment(
  uploadedByUs: boolean | null | undefined,
  deviceName: string | null | undefined,
): string | null {
  const label = attributionLabel(uploadedByUs);
  if (uploadedByUs === true) {
    return deviceName ? `${deviceName} ${label} ✓` : `${label} ✓`;
  }
  if (uploadedByUs === false) {
    // Intentionally no device name — lastSyncer is our own sync record, not the actual uploader
    return `${label} ✓`;
  }
  if (label === null) {
    return deviceName ? `${deviceName} ✓` : `✓`;
  }
  return null;
}

/**
 * Pick the toast body to surface when `get_slot_delete_info` returned
 * success=false. The frontend uses this to refuse the destructive confirm
 * modal and explain why — most importantly the `server_unreachable` branch,
 * which guards against confirming a wipe of a slot we never inspected.
 */
export function slotDeleteFailureToast(info: SlotDeleteInfo): string {
  if (info.reason === "active_slot" || info.is_active) {
    return "Cannot delete the active slot. Switch to a different slot first.";
  }
  if (info.reason === "server_unreachable") {
    return info.message ?? "Cannot inspect slot — RomM server is not reachable";
  }
  return info.message ?? "Cannot delete this slot";
}

/** Map a save file status to color and label */
export function statusLabel(status: string, lastSyncAt: string | null): { color: string; label: string } {
  switch (status) {
    case "synced":
    case "skip":
      return { color: "#5ba32b", label: "Synced" };
    case "upload":
      return { color: "#d4a72c", label: "Local changes" };
    case "download":
      return { color: "#1a9fff", label: "Server newer" };
    case "conflict":
      return { color: "#d94126", label: "Conflict" };
    case "unknown":
      return { color: "#8f98a0", label: "Status unknown" };
    default:
      if (lastSyncAt) return { color: "#5ba32b", label: "Synced" };
      return { color: "#8f98a0", label: "Not synced" };
  }
}

/**
 * Build the active slot's sync-summary header line.
 *
 * Returns the empty (null) state for inactive slots or missing status. When
 * the backend signals `server_query_failed`, short-circuits with a neutral
 * "Server unreachable" instead of running the matrix-derived classification
 * (which would otherwise read an empty server list as "ready to upload").
 */
export function computeSyncSummary(
  isActive: boolean,
  saveStatus: SaveStatus | null,
  conflicts: SyncConflict[],
): { syncSummaryText: string | null; syncSummaryColor: string } {
  if (!isActive || !saveStatus) return { syncSummaryText: null, syncSummaryColor: MUTED_COLOR };

  if (saveStatus.server_query_failed) {
    return { syncSummaryText: "Server unreachable", syncSummaryColor: MUTED_COLOR };
  }

  const hasConflict = conflicts.length > 0;
  const fileCount = saveStatus.files.length;

  if (hasConflict) return { syncSummaryText: "Conflict detected", syncSummaryColor: "#d94126" };
  if (fileCount > 0 && saveStatus.last_sync_check_at) {
    const rel = formatRelativeTime(saveStatus.last_sync_check_at);
    return { syncSummaryText: rel === "just now" ? "Synced just now" : `Synced ${rel}`, syncSummaryColor: "#5ba32b" };
  }
  if (fileCount > 0) return { syncSummaryText: "Not synced", syncSummaryColor: MUTED_COLOR };
  return { syncSummaryText: "No saves found", syncSummaryColor: MUTED_COLOR };
}
