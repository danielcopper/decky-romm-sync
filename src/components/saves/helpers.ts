/**
 * Pure formatters and selectors for the SavesTab UI. Anything that takes
 * inputs and returns outputs without touching component state or React
 * belongs here; rendering helpers live alongside their components.
 */

import type { DeviceSyncInfo } from "../../types";

/** Display a slot name, using "(no slot)" for null/empty values */
export function displaySlot(slot: string | null | undefined): string {
  if (slot === null || slot === undefined || slot === "") return "(no slot)";
  return slot;
}

/** Format a byte count as a human-readable string (e.g. "12.4 KB") */
export function formatBytes(bytes: number | null): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
    default:
      if (lastSyncAt) return { color: "#5ba32b", label: "Synced" };
      return { color: "#8f98a0", label: "Not synced" };
  }
}
