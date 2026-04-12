/**
 * SavesTab — slot-based collapsible save file browser.
 *
 * Replaces the old two-column (files left / slots right) layout in
 * RomMGameInfoPanel with a stacked list of collapsible slot panels.
 *
 * - Active slot expanded by default, inactive slots collapsed.
 * - Inactive slot bodies load lazily via getSlotSaves on first expand.
 * - Activate-slot via switchSlot (v4.7+) with inline error feedback.
 * - New-slot modal opens inline (same as old NewSlotModal in parent).
 */

import { useState, useEffect, useRef, createElement, FC, ChangeEvent } from "react";
import { ConfirmModal, DialogButton, Focusable, TextField, showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import { getSlotSaves, switchSlot, debugLog, savesSupportsVersionHistory, savesListFileVersions, savesRollbackToVersion } from "../api/backend";
import type { SaveVersionEntry, RollbackStatus } from "../api/backend";
import { getRommConnectionState } from "../utils/connectionState";
import type { SaveStatus, PendingConflict, SaveSlotSummary, SaveFileStatus, SlotSaveFile, SwitchSlotResponse, DeviceSyncInfo } from "../types";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import { formatTimestamp } from "../utils/formatters";

// --- Type re-exports needed internally ---

interface SavesTabProps {
  romId: number;
  saveStatus: SaveStatus | null;
  conflicts: PendingConflict[];
  activeSlot: string | null;
  availableSlots: SaveSlotSummary[];
  slotsLoading: boolean;
  onSlotSwitched: (newSlot: string, newStatus: SaveStatus) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Display a slot name, using "(no slot)" for null/empty values */
function displaySlot(slot: string | null | undefined): string {
  if (slot === null || slot === undefined || slot === "") return "(no slot)";
  return slot;
}

/** Format a byte count as a human-readable string (e.g. "12.4 KB") */
function formatBytes(bytes: number | null): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Format a relative time string (e.g. "5m ago", "2h ago") from an ISO string */
function formatRelativeTime(isoStr: string | null): string {
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
function pickLastSyncer(syncs: DeviceSyncInfo[] | undefined): DeviceSyncInfo | null {
  if (!syncs || syncs.length === 0) return null;
  return syncs.reduce<DeviceSyncInfo | null>((latest, ds) => {
    if (!latest) return ds;
    if (!ds.last_synced_at) return latest;
    if (!latest.last_synced_at) return ds;
    return ds.last_synced_at > latest.last_synced_at ? ds : latest;
  }, null);
}

/** Map a save file status to color and label */
function statusLabel(status: string, lastSyncAt: string | null): { color: string; label: string } {
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

// ---------------------------------------------------------------------------
// NewSlotModal
// ---------------------------------------------------------------------------

/** Modal for creating a new save slot — uses internal state for the text field. */
const NewSlotModal: FC<{
  closeModal?: () => void;
  onSubmit: (name: string) => void;
}> = ({ closeModal, onSubmit }) => {
  const [value, setValue] = useState("");
  return createElement(ConfirmModal, {
    closeModal,
    onOK: () => { onSubmit(value.trim()); },
    strTitle: "New Save Slot",
    bDisableBackgroundDismiss: true,
  },
    createElement(TextField, {
      focusOnMount: true,
      label: "Slot Name",
      value,
      onChange: (e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value),
    } as any),
  );
};

// ---------------------------------------------------------------------------
// VersionHistoryPanel — expandable sub-panel below a save file row
// ---------------------------------------------------------------------------

interface VersionHistoryPanelProps {
  romId: number;
  slot: string;
  filename: string;
  isOffline: boolean;
  onRestored: () => void;
}

const VersionHistoryPanel: FC<VersionHistoryPanelProps> = ({
  romId,
  slot,
  filename,
  isOffline,
  onRestored,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [versions, setVersions] = useState<SaveVersionEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState<number | null>(null);

  const handleToggle = async () => {
    const willExpand = !expanded;
    setExpanded(willExpand);
    if (willExpand && versions === null && !isOffline) {
      setLoading(true);
      try {
        const result = await savesListFileVersions(romId, slot, filename);
        setVersions(result);
      } catch (e) {
        debugLog(`VersionHistoryPanel: failed to load versions for ${filename}: ${e}`);
        setVersions([]);
      } finally {
        setLoading(false);
      }
    }
  };

  const handleRestore = async (version: SaveVersionEntry, force: boolean) => {
    setRestoring(version.id);
    // When the user needs to confirm via modal, we keep `restoring` set so the
    // Restore button stays disabled until the modal is resolved (prevents a
    // double-submit window between the first call's finally and the onOK
    // callback re-entering handleRestore with force=true).
    let awaitingModal = false;
    try {
      const result: RollbackStatus = await savesRollbackToVersion(romId, slot, filename, version.id, force);
      if (result.status === "ok") {
        toaster.toast({ title: "RomM Sync", body: `Save restored from ${formatRelativeTime(version.updated_at)}` });
        // Invalidate cache so next expand re-fetches
        setVersions(null);
        setExpanded(false);
        onRestored();
      } else if (result.status === "unsynced_changes") {
        awaitingModal = true;
        showModal(createElement(ConfirmModal, {
          strTitle: "Unsynced Local Changes",
          strDescription: "Your local save has changes that haven't been synced to the server. Rolling back will discard them. Continue?",
          strOKButtonText: "Roll Back",
          strCancelButtonText: "Cancel",
          onOK: () => { handleRestore(version, true); },
          onCancel: () => { setRestoring(null); },
        }));
      } else if (result.status === "tracked_missing") {
        awaitingModal = true;
        showModal(createElement(ConfirmModal, {
          strTitle: "Current save missing on server",
          strDescription: "The save currently tracked by this device no longer exists on the server. Rolling back will discard the reference to it. Continue?",
          strOKButtonText: "Roll Back",
          strCancelButtonText: "Cancel",
          onOK: () => { handleRestore(version, true); },
          onCancel: () => { setRestoring(null); },
        }));
      } else if (result.status === "not_found") {
        toaster.toast({ title: "RomM Sync", body: "This version no longer exists on the server" });
      } else if (result.status === "unsupported") {
        toaster.toast({ title: "RomM Sync", body: "Version history requires RomM 4.7+" });
      }
    } catch (e) {
      debugLog(`VersionHistoryPanel: restore error for save ${version.id}: ${e}`);
    } finally {
      if (!awaitingModal) setRestoring(null);
    }
  };

  const versionCount = versions?.length ?? 0;

  const renderVersionRow = (v: SaveVersionEntry): ReturnType<typeof createElement> => {
    const lastSyncer = pickLastSyncer(v.device_syncs);
    const deviceName = lastSyncer?.device_name ?? null;
    const isThisRestoring = restoring === v.id;

    // Line 1: #id · emulator · size
    const headerParts: string[] = [`#${v.id}`];
    if (v.emulator) headerParts.push(v.emulator);
    if (v.file_size_bytes != null) headerParts.push(formatBytes(v.file_size_bytes));

    // Line 2: Last updated: <timestamp>[ · <device>]
    const lastUpdatedParts: string[] = [formatTimestamp(v.updated_at)];
    if (deviceName) lastUpdatedParts.push(`${deviceName} \u2713`);

    return createElement("div", {
      key: `ver-${v.id}`,
      style: {
        display: "flex",
        alignItems: "flex-start",
        gap: "8px",
        padding: "6px 0",
        borderBottom: "1px solid rgba(255,255,255,0.06)",
      },
    },
      // Info column (grows)
      createElement("div", { style: { flex: 1, minWidth: 0 } },
        // Line 1: #id · emulator · size
        createElement("div", {
          style: { fontSize: "12px", color: "#c7cdd3", fontWeight: 600 },
        }, headerParts.join(" \u00B7 ")),
        // Line 2: last updated + device
        createElement("div", {
          style: {
            fontSize: "11px",
            color: "#8f98a0",
            marginTop: "2px",
          },
        },
          createElement("span", { style: { color: "#697075" } }, "Last updated: "),
          lastUpdatedParts.join(" \u00B7 "),
        ),
        // Line 3: server filename (technical, bottom)
        createElement("div", {
          style: {
            fontSize: "11px",
            color: "#8f98a0",
            fontFamily: "monospace",
            wordBreak: "break-all" as const,
            marginTop: "2px",
          },
        }, v.file_name),
      ),
      // Restore button (fixed right, disabled when offline)
      createElement(DialogButton as any, {
        style: {
          padding: "2px 8px",
          minWidth: "auto",
          fontSize: "11px",
          width: "auto",
          flexShrink: 0,
        },
        noFocusRing: false,
        onFocus: scrollFocusedToCenter,
        disabled: isThisRestoring || restoring !== null || isOffline,
        onClick: () => { handleRestore(v, false); },
      }, isThisRestoring ? "Restoring..." : "Restore"),
    );
  };

  const renderBody = (): ReturnType<typeof createElement> | ReturnType<typeof createElement>[] => {
    if (isOffline) {
      return createElement("div", {
        style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const },
      }, "Offline \u2014 versions unavailable");
    }
    if (loading) {
      return createElement("div", { style: { fontSize: "11px", color: "#8f98a0" } }, "Loading...");
    }
    if (versionCount === 0) {
      return createElement("div", {
        style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const },
      }, "No older versions available");
    }
    return (versions ?? []).map(renderVersionRow);
  };

  return createElement("div", {
    key: `history-${filename}`,
    style: { marginTop: "4px", marginLeft: "8px" },
  },
    // Expander toggle
    createElement(DialogButton as any, {
      style: {
        background: "transparent",
        border: "none",
        padding: "2px 0",
        textAlign: "left" as const,
        width: "100%",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        gap: "4px",
        fontSize: "11px",
        color: "#8f98a0",
      },
      noFocusRing: false,
      onFocus: scrollFocusedToCenter,
      onClick: handleToggle,
    },
      createElement("span", {}, expanded ? "\u25BE" : "\u25B8"),
      createElement("span", {}, expanded && versions !== null
        ? `Previous Versions (${versionCount})`
        : "Previous Versions"),
    ),

    // Version list (lazy-loaded)
    expanded
      ? createElement("div", { style: { marginTop: "4px" } }, renderBody())
      : null,
  );
};

// ---------------------------------------------------------------------------
// SaveFileRow — one row in the active slot body
// ---------------------------------------------------------------------------

// Label column width — keeps values aligned vertically across rows
const LABEL_WIDTH = "88px";

/** Render a labeled info row (label column + value column) inside the tracked save block */
function infoRow(
  key: string,
  label: string,
  value: ReturnType<typeof createElement> | string | null,
  valueColor = "#c7cdd3",
): ReturnType<typeof createElement> | null {
  if (value == null || value === "") return null;
  return createElement("div", {
    key,
    style: { display: "flex", alignItems: "flex-start", fontSize: "11px", marginTop: "2px" },
  },
    createElement("span", {
      style: { color: "#697075", width: LABEL_WIDTH, flexShrink: 0 },
    }, label),
    createElement("div", { style: { color: valueColor, flex: 1, minWidth: 0 } }, value),
  );
}

function renderSaveFileRow(
  f: SaveFileStatus,
  conflict: PendingConflict | undefined,
  lastSyncCheckAt: string | null,
): ReturnType<typeof createElement> {
  const { color, label } = statusLabel(f.status, f.last_sync_at);
  const syncTime = lastSyncCheckAt || f.last_sync_at;
  const lastSyncer = pickLastSyncer(f.device_syncs);
  const conflictActive = f.status === "conflict" || !!conflict;

  // Header value pieces (right-aligned meta: size + status)
  const headerMeta: (ReturnType<typeof createElement> | null)[] = [];
  if (f.local_size != null) {
    headerMeta.push(createElement("span", {
      key: "size",
      style: { fontSize: "11px", color: "#8f98a0" },
    }, formatBytes(f.local_size)));
  }
  headerMeta.push(createElement("span", {
    key: "status",
    className: "romm-save-status-label",
    style: { color, fontSize: "11px", fontWeight: 600 },
  }, label));

  // Last synced value: "just now · steamdeck ✓"
  const lastSyncedPieces: string[] = [];
  if (syncTime) {
    lastSyncedPieces.push(formatRelativeTime(syncTime) || "Never");
  } else {
    lastSyncedPieces.push("Never");
  }
  if (lastSyncer?.device_name) {
    lastSyncedPieces.push(`${lastSyncer.device_name} \u2713`);
  }
  if (f.is_current === false) {
    lastSyncedPieces.push("Newer version available on server");
  }
  const lastSyncedValue = lastSyncedPieces.join(" \u00B7 ");

  // Server save value — two lines: "#18 · retroarch-mgba" / "<server_file_name>"
  const serverValueLines: ReturnType<typeof createElement>[] = [];
  if (f.server_save_id != null) {
    const headerParts: string[] = [`#${f.server_save_id}`];
    if (f.server_emulator) headerParts.push(f.server_emulator);
    serverValueLines.push(createElement("div", {
      key: "srv-head",
      style: { color: "#c7cdd3" },
    }, headerParts.join(" \u00B7 ")));
    if (f.server_file_name) {
      serverValueLines.push(createElement("div", {
        key: "srv-fn",
        style: { color: "#8f98a0", fontFamily: "monospace", wordBreak: "break-all" as const, marginTop: "1px" },
      }, f.server_file_name));
    }
  }

  return createElement(DialogButton as any, {
    key: f.filename,
    style: {
      background: "transparent",
      border: "none",
      padding: "8px 0",
      textAlign: "left" as const,
      width: "100%",
      cursor: "default",
      display: "block",
    },
    noFocusRing: false,
    onFocus: scrollFocusedToCenter,
  },
    // Header row: filename (left) + size + status badge (right)
    createElement("div", {
      style: {
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "8px",
        marginBottom: "4px",
      },
    },
      createElement("div", {
        style: {
          fontSize: "13px",
          color: "#dcdedf",
          fontWeight: 600,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap" as const,
          flex: 1,
          minWidth: 0,
        },
      }, f.filename),
      createElement("div", {
        style: { display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 },
      }, ...headerMeta),
    ),

    // Conflict banner (prominent)
    conflictActive
      ? createElement("div", {
          style: { fontSize: "11px", color: "#d94126", fontWeight: 600, marginTop: "2px", marginBottom: "2px" },
        }, "Conflict detected \u2014 resolve from the sync action")
      : null,

    // Info rows
    infoRow("last-synced", "Last synced:", lastSyncedValue),
    infoRow(
      "last-updated",
      "Last updated:",
      f.server_updated_at ? formatTimestamp(f.server_updated_at) : null,
      "#8f98a0",
    ),
    serverValueLines.length > 0
      ? infoRow("server", "Server save:", createElement("div", {}, ...serverValueLines))
      : null,
    f.local_path
      ? infoRow(
          "path",
          "Local path:",
          createElement("span", {
            style: { fontFamily: "monospace", wordBreak: "break-all" as const },
          }, f.local_path),
          "#5a6066",
        )
      : null,
  );
}

// ---------------------------------------------------------------------------
// ServerSaveRow — one row in an inactive slot body
// ---------------------------------------------------------------------------

function renderServerSaveRow(f: SlotSaveFile): ReturnType<typeof createElement> {
  const details: string[] = [];
  if (f.size != null) details.push(formatBytes(f.size));
  if (f.updated_at) details.push(`Updated ${formatRelativeTime(f.updated_at)}`);

  return createElement("div", {
    key: `server-${f.id}`,
    style: { padding: "4px 0", borderBottom: "1px solid rgba(255,255,255,0.04)" },
  },
    createElement("div", {
      style: { fontSize: "12px", color: "#dcdedf", fontWeight: 500 },
    }, f.filename),
    details.length > 0
      ? createElement("div", {
          style: { fontSize: "11px", color: "#8f98a0", marginTop: "2px" },
        }, details.join(" \u00B7 "))
      : null,
  );
}

// ---------------------------------------------------------------------------
// SlotPanel — a single collapsible slot
// ---------------------------------------------------------------------------

const MUTED_COLOR = "#8f98a0";

function computeSyncSummary(
  isActive: boolean,
  saveStatus: SaveStatus | null,
  conflicts: PendingConflict[],
): { syncSummaryText: string | null; syncSummaryColor: string } {
  if (!isActive || !saveStatus) return { syncSummaryText: null, syncSummaryColor: MUTED_COLOR };

  const hasConflict = conflicts.length > 0;
  const fileCount = saveStatus.files?.length ?? 0;

  if (hasConflict) return { syncSummaryText: "Conflict detected", syncSummaryColor: "#d94126" };
  if (fileCount > 0 && saveStatus.last_sync_check_at) {
    const rel = formatRelativeTime(saveStatus.last_sync_check_at);
    return { syncSummaryText: rel === "just now" ? "Synced just now" : `Synced ${rel}`, syncSummaryColor: "#5ba32b" };
  }
  if (fileCount > 0) return { syncSummaryText: "Not synced", syncSummaryColor: MUTED_COLOR };
  return { syncSummaryText: "No saves found", syncSummaryColor: MUTED_COLOR };
}

function renderActiveSlotBody(
  saveStatus: SaveStatus | null,
  conflicts: PendingConflict[],
  romId: number,
  slot: string,
  supportsVersionHistory: boolean,
  isOffline: boolean,
  onVersionRestored: () => void,
): (ReturnType<typeof createElement> | null)[] {
  if (saveStatus && saveStatus.files.length > 0) {
    return saveStatus.files.map((f) => {
      const conflict = conflicts.find((c) => c.filename === f.filename);
      return createElement("div", { key: f.filename },
        renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at),
        supportsVersionHistory
          ? createElement(VersionHistoryPanel, {
              key: `vhp-${f.filename}`,
              romId,
              slot,
              filename: f.filename,
              isOffline,
              onRestored: onVersionRestored,
            })
          : null,
      );
    });
  }
  return [createElement("div", { key: "no-files", style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic" } },
    "No save files tracked yet")];
}

function renderInactiveSlotBody(
  loadingSlot: boolean,
  slotFiles: SlotSaveFile[] | null,
  switching: boolean,
  switchError: string | null,
  isOffline: boolean,
  handleActivate: () => void,
): (ReturnType<typeof createElement> | null)[] {
  const children: (ReturnType<typeof createElement> | null)[] = [];

  if (loadingSlot) {
    children.push(createElement("div", { key: "loading", style: { fontSize: "13px", color: MUTED_COLOR } }, "Loading..."));
  } else if (slotFiles && slotFiles.length > 0) {
    for (const f of slotFiles) {
      children.push(renderServerSaveRow(f));
    }
  } else if (slotFiles !== null) {
    children.push(createElement("div", { key: "no-server-files", style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic" } },
      "No saves in this slot"));
  }

  children.push(
    createElement("div", { key: "activate-row", style: { marginTop: "10px" } },
      createElement(DialogButton as any, {
        key: "activate-btn",
        style: { padding: "4px 12px", minWidth: "auto", fontSize: "12px", width: "auto" },
        noFocusRing: false,
        onFocus: scrollFocusedToCenter,
        disabled: switching || isOffline,
        onClick: handleActivate,
      }, switching ? "Switching..." : "Activate Slot"),
      isOffline
        ? createElement("div", {
            key: "offline-hint",
            style: { fontSize: "11px", color: "#8f98a0", fontStyle: "italic" as const, marginTop: "4px" },
          }, "Offline \u2014 slot switching unavailable")
        : null,
      switchError
        ? createElement("div", {
            key: "switch-error",
            style: { fontSize: "11px", color: "#d94126", marginTop: "4px" },
          }, switchError)
        : null,
    ),
  );

  return children;
}

interface SlotPanelProps {
  romId: number;
  slot: SaveSlotSummary;
  isActive: boolean;
  defaultExpanded: boolean;
  // Active slot data (only set when isActive === true)
  saveStatus: SaveStatus | null;
  conflicts: PendingConflict[];
  supportsVersionHistory: boolean;
  isOffline: boolean;
  // Callbacks
  onSlotSwitched: (newSlot: string, newStatus: SaveStatus) => void;
  onVersionRestored: () => void;
}

const SlotPanel: FC<SlotPanelProps> = ({
  romId,
  slot,
  isActive,
  defaultExpanded,
  saveStatus,
  conflicts,
  supportsVersionHistory,
  isOffline,
  onSlotSwitched,
  onVersionRestored,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [slotFiles, setSlotFiles] = useState<SlotSaveFile[] | null>(null);
  const [loadingSlot, setLoadingSlot] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const switchErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const slotName = slot.slot;

  const handleToggle = async () => {
    const willExpand = !expanded;
    setExpanded(willExpand);

    // Lazy-load slot saves for inactive slots on first expand
    if (willExpand && !isActive && slotFiles === null) {
      setLoadingSlot(true);
      try {
        const result = await getSlotSaves(romId, slotName);
        setSlotFiles(result.success ? result.saves : []);
      } catch (e) {
        debugLog(`SavesTab: failed to load slot saves for ${slotName}: ${e}`);
        setSlotFiles([]);
      } finally {
        setLoadingSlot(false);
      }
    }
  };

  const handleActivate = async () => {
    setSwitching(true);
    setSwitchError(null);
    try {
      const result: SwitchSlotResponse = await switchSlot(romId, slotName);
      if (result.success && result.save_status) {
        onSlotSwitched(slotName, result.save_status);
      } else {
        let msg = "Failed to switch slot";
        if (result.reason === "pending_uploads") {
          msg = "Sync your saves first — local changes haven't been uploaded";
        } else if (result.reason === "server_unreachable") {
          msg = "Can't switch — RomM server is not reachable";
        } else if (result.reason === "unresolved_conflicts") {
          msg = "Resolve conflicts before switching slots";
        }
        setSwitchError(msg);
        if (switchErrorTimerRef.current) clearTimeout(switchErrorTimerRef.current);
        switchErrorTimerRef.current = setTimeout(() => setSwitchError(null), 5000);
      }
    } catch (e) {
      debugLog(`SavesTab: switchSlot error: ${e}`);
      setSwitchError("An error occurred while switching slots");
      if (switchErrorTimerRef.current) clearTimeout(switchErrorTimerRef.current);
      switchErrorTimerRef.current = setTimeout(() => setSwitchError(null), 5000);
    } finally {
      setSwitching(false);
    }
  };

  const { syncSummaryText, syncSummaryColor } = computeSyncSummary(isActive, saveStatus, conflicts);

  const fileCount = isActive
    ? (saveStatus?.files?.length ?? 0)
    : (slotFiles?.length ?? slot.count);

  const panelClasses = ["romm-slot-panel", isActive ? "romm-slot-panel-active" : ""].filter(Boolean).join(" ");

  // --- Source badge ---
  const sourceBadge = slot.source === "local"
    ? createElement("span", { key: "src", className: "romm-slot-badge romm-slot-badge-local" }, "local")
    : createElement("span", { key: "src", className: "romm-slot-badge romm-slot-badge-server" }, "server");

  // --- Slot header ---
  const headerEl = createElement(DialogButton as any, {
    key: "header",
    className: "romm-slot-header",
    style: {
      background: "transparent",
      border: "none",
      padding: "10px 12px",
      textAlign: "left" as const,
      width: "100%",
      cursor: "pointer",
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
    },
    noFocusRing: false,
    onFocus: scrollFocusedToCenter,
    onClick: handleToggle,
  },
    // Left: slot name + badges
    createElement("div", { className: "romm-slot-header-left" },
      createElement("span", { className: "romm-slot-name" }, displaySlot(slotName)),
      isActive
        ? createElement("span", { key: "active", className: "romm-slot-badge romm-slot-badge-active" }, "active")
        : null,
      sourceBadge,
    ),
    // Right: file count + chevron
    createElement("div", { className: "romm-slot-header-right" },
      createElement("span", { className: "romm-slot-count" },
        `${fileCount} save${fileCount === 1 ? "" : "s"}`),
      createElement("span", { className: "romm-slot-chevron" }, expanded ? "\u25BE" : "\u25B8"),
    ),
  );

  // --- Sync summary line (active slot only) ---
  const syncSummaryEl = isActive && syncSummaryText
    ? createElement("div", {
        key: "sync-summary",
        className: "romm-slot-sync-summary",
        style: { color: syncSummaryColor },
      }, syncSummaryText)
    : null;

  // --- Slot body ---
  let bodyChildren: (ReturnType<typeof createElement> | null)[] = [];
  if (expanded) {
    bodyChildren = isActive
      ? renderActiveSlotBody(saveStatus, conflicts, romId, slotName, supportsVersionHistory, isOffline, onVersionRestored)
      : renderInactiveSlotBody(loadingSlot, slotFiles, switching, switchError, isOffline, handleActivate);
  }

  const bodyEl = expanded
    ? createElement("div", { key: "body", className: "romm-slot-body" },
        ...bodyChildren.filter(Boolean),
      )
    : null;

  return createElement("div", { key: `slot-${slotName}`, className: panelClasses },
    headerEl,
    syncSummaryEl,
    bodyEl,
  );
};

// ---------------------------------------------------------------------------
// SavesTab — main exported component
// ---------------------------------------------------------------------------

export const SavesTab: FC<SavesTabProps> = ({
  romId,
  saveStatus,
  conflicts,
  activeSlot,
  availableSlots,
  slotsLoading,
  onSlotSwitched,
}) => {
  const [newSlotError, setNewSlotError] = useState<string | null>(null);
  const newSlotErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [isOffline, setIsOffline] = useState(getRommConnectionState() === "offline");
  const [supportsVersionHistory, setSupportsVersionHistory] = useState(false);
  // Bumped to invalidate VersionHistoryPanel caches after a restore
  const [versionHistoryKey, setVersionHistoryKey] = useState(0);

  useEffect(() => {
    // Skip while offline — preserve last-known capability so we don't flicker
    // the UI off if the server is briefly unreachable. Re-fetches on reconnect
    // via the isOffline dep.
    if (isOffline) return;
    savesSupportsVersionHistory()
      .then((supported) => setSupportsVersionHistory(!!supported))
      .catch(() => setSupportsVersionHistory(false));
  }, [isOffline]);

  const handleVersionRestored = () => {
    setVersionHistoryKey((k) => k + 1);
    // Trigger parent refresh of saveStatus so the tracked save row reflects
    // the new tracked_save_id / server fields without leaving the page.
    globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
      detail: { type: "save_sync", rom_id: romId },
    }));
  };

  useEffect(() => {
    const onConnectionChanged = (e: Event) => {
      const connState = (e as CustomEvent).detail?.state;
      setIsOffline(connState === "offline");
    };
    globalThis.addEventListener("romm_connection_changed", onConnectionChanged);
    return () => {
      globalThis.removeEventListener("romm_connection_changed", onConnectionChanged);
    };
  }, []);

  // --- Offline banner ---
  const offlineBanner = isOffline
    ? createElement("div", {
        key: "offline-banner",
        style: {
          padding: "8px",
          background: "rgba(217, 65, 38, 0.15)",
          borderRadius: "4px",
          border: "1px solid rgba(217, 65, 38, 0.4)",
          marginBottom: "12px",
          fontSize: "12px",
          color: "#d94126",
        },
      }, "RomM is offline \u2014 slot switching is disabled until the server is reachable. This prevents save sync conflicts.")
    : null;

  // --- Legacy mode warning ---
  const legacyWarning = activeSlot === null
    ? createElement("div", {
        key: "legacy-warning",
        style: {
          padding: "8px",
          background: "rgba(255, 136, 0, 0.15)",
          borderRadius: "4px",
          border: "1px solid rgba(255, 136, 0, 0.3)",
          marginBottom: "12px",
          fontSize: "12px",
          color: "#ff8800",
        },
      }, "This game uses legacy mode (no slot). Only one save version per game is supported.")
    : null;

  // --- Loading state ---
  if (slotsLoading) {
    return createElement(Focusable as any, { noFocusRing: true },
      offlineBanner,
      createElement("div", { style: { fontSize: "13px", color: "#8f98a0", padding: "8px 0" } },
        "Loading slots..."),
    );
  }

  // --- Sort slots: active first, then alphabetically ---
  const sorted = [...availableSlots].sort((a, b) => {
    const aActive = a.slot === activeSlot ? 0 : 1;
    const bActive = b.slot === activeSlot ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return a.slot.localeCompare(b.slot);
  });

  // If active slot not in list yet, synthesize a placeholder entry
  const slotInList = sorted.some((s) => s.slot === activeSlot);
  if (!slotInList && activeSlot) {
    sorted.unshift({ slot: activeSlot, source: "local", count: 0, latest_updated_at: null });
  }

  // --- New Slot button handler ---
  const handleNewSlot = () => {
    showModal(
      createElement(NewSlotModal, {
        onSubmit: async (name: string) => {
          if (!name) {
            // Empty = legacy mode — show warning
            showModal(createElement(ConfirmModal, {
              strTitle: "Use Legacy Mode?",
              strDescription: "Legacy mode (no slot) limits saves to one version per game. Are you sure?",
              onOK: async () => {
                try {
                  const result = await switchSlot(romId, "");
                  if (result.success && result.save_status) {
                    onSlotSwitched("", result.save_status);
                  } else {
                    debugLog(`SavesTab: legacy switch failed: ${result.reason}`);
                  }
                } catch (e) {
                  debugLog(`SavesTab: legacy switch error: ${e}`);
                }
              },
            }));
            return;
          }
          // Named slot — also use switchSlot to do pre-checks + immediate download
          try {
            const result = await switchSlot(romId, name);
            if (result.success && result.save_status) {
              onSlotSwitched(name, result.save_status);
            } else {
              debugLog(`SavesTab: new slot switch failed: ${result.reason}`);
              let msg = "Failed to create slot";
              if (result.reason === "pending_uploads") {
                msg = "Sync your saves first — local changes haven't been uploaded";
              } else if (result.reason === "server_unreachable") {
                msg = "Can't switch — RomM server is not reachable";
              }
              setNewSlotError(msg);
              if (newSlotErrorTimerRef.current) clearTimeout(newSlotErrorTimerRef.current);
              newSlotErrorTimerRef.current = setTimeout(() => setNewSlotError(null), 5000);
            }
          } catch (e) {
            debugLog(`SavesTab: new slot switch error: ${e}`);
            setNewSlotError("An error occurred while creating the slot");
            if (newSlotErrorTimerRef.current) clearTimeout(newSlotErrorTimerRef.current);
            newSlotErrorTimerRef.current = setTimeout(() => setNewSlotError(null), 5000);
          }
        },
      }),
    );
  };

  // --- Legacy mode: show save files directly (not in a slot panel) ---
  let legacyFilesSection: ReturnType<typeof createElement> | null = null;
  if (activeSlot === null) {
    if (saveStatus && saveStatus.files.length > 0) {
      legacyFilesSection = createElement("div", { key: "legacy-files", style: { marginBottom: "12px" } },
        ...saveStatus.files.map((f) => {
          const conflict = conflicts.find((c) => c.filename === f.filename);
          return renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at);
        }),
      );
    } else {
      legacyFilesSection = createElement("div", {
        key: "no-files",
        style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic", marginBottom: "12px" },
      }, "No save files tracked yet");
    }
  }

  return createElement(Focusable as any, {
    noFocusRing: true,
    style: { display: "flex", flexDirection: "column" as const, gap: "0" },
  },
    offlineBanner,
    legacyWarning,

    // Legacy mode: show save files directly above slot panels
    legacyFilesSection,

    // Slot panels — skip the "" (legacy) panel when already in legacy mode
    ...sorted
      .filter((s) => activeSlot !== null || s.slot !== "")
      .map((slot) => {
        const isActive = activeSlot !== null && slot.slot === activeSlot;
        return createElement(SlotPanel, {
          key: `panel-${slot.slot}-${versionHistoryKey}`,
          romId,
          slot,
          isActive,
          defaultExpanded: isActive,
          saveStatus: isActive ? saveStatus : null,
          conflicts: isActive ? conflicts : [],
          supportsVersionHistory,
          isOffline,
          onSlotSwitched,
          onVersionRestored: handleVersionRestored,
        });
      }),

    // New Slot button + error feedback
    createElement("div", { key: "new-slot-area", style: { marginTop: "10px" } },
      createElement(DialogButton as any, {
        key: "new-slot-btn",
        style: {
          padding: "6px 12px",
          minWidth: "auto",
          fontSize: "12px",
          width: "auto",
        },
        noFocusRing: false,
        onFocus: scrollFocusedToCenter,
        onClick: handleNewSlot,
      }, "+ New Slot"),
      newSlotError
        ? createElement("div", {
            key: "new-slot-error",
            style: { fontSize: "11px", color: "#d94126", marginTop: "4px" },
          }, newSlotError)
        : null,
    ),
  );
};
