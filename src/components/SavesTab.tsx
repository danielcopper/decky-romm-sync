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

import { useState, useRef, createElement, FC, ChangeEvent } from "react";
import { ConfirmModal, DialogButton, Focusable, TextField, showModal } from "@decky/ui";
import { getSlotSaves, switchSlot, debugLog } from "../api/backend";
import type { SaveStatus, PendingConflict, SaveSlotSummary, SaveFileStatus, SlotSaveFile, SwitchSlotResponse } from "../types";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";

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
// Device sync info helper
// ---------------------------------------------------------------------------

function renderDeviceSyncInfo(f: SaveFileStatus): (ReturnType<typeof createElement> | null)[] {
  if (!f.device_syncs || f.device_syncs.length === 0) return [];

  const lastSyncer = f.device_syncs.reduce((latest, ds) => {
    if (!latest) return ds;
    if (!ds.last_synced_at) return latest;
    if (!latest.last_synced_at) return ds;
    return ds.last_synced_at > latest.last_synced_at ? ds : latest;
  }, f.device_syncs[0]);

  const children: (ReturnType<typeof createElement> | null)[] = [];

  if (lastSyncer?.device_name) {
    children.push(createElement("span", {
      key: "device-info",
      style: { fontSize: "11px", color: "rgba(255,255,255,0.5)" },
    }, `Last sync: ${lastSyncer.device_name} \u2713`));
  }

  if (f.is_current === false) {
    children.push(createElement("span", {
      key: "not-current",
      style: { fontSize: "11px", color: "#d4a72c", marginLeft: "8px" },
    }, "Newer version available on server"));
  }

  if (children.length === 0) return [];
  return [createElement("div", { key: "device-sync", style: { marginTop: "2px" } }, ...children)];
}

// ---------------------------------------------------------------------------
// SaveFileRow — one row in the active slot body
// ---------------------------------------------------------------------------

function renderSaveFileRow(
  f: SaveFileStatus,
  conflict: PendingConflict | undefined,
  lastSyncCheckAt: string | null,
): ReturnType<typeof createElement> {
  const { color, label } = statusLabel(f.status, f.last_sync_at);
  const syncTime = lastSyncCheckAt || f.last_sync_at;

  const details: string[] = [];
  if (f.local_size != null) details.push(formatBytes(f.local_size));
  if (f.local_mtime) details.push(`Changed ${formatRelativeTime(f.local_mtime)}`);

  return createElement(DialogButton as any, {
    key: f.filename,
    style: {
      background: "transparent",
      border: "none",
      padding: "6px 0",
      textAlign: "left" as const,
      width: "100%",
      cursor: "default",
      display: "block",
    },
    noFocusRing: false,
    onFocus: scrollFocusedToCenter,
  },
    // Filename
    createElement("div", {
      style: { fontSize: "12px", color: "#dcdedf", fontWeight: 500, marginBottom: "3px" },
    }, f.filename),

    // Status label · size · changed time
    createElement("div", {
      style: { display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" as const },
    },
      createElement("span", {
        className: "romm-save-status-label",
        style: { color },
      }, label),
      details.length > 0
        ? createElement("span", { style: { fontSize: "11px", color: "#8f98a0" } },
            `\u00B7 ${details.join(" \u00B7 ")}`)
        : null,
      syncTime
        ? createElement("span", { style: { fontSize: "11px", color: "#8f98a0" } },
            `\u00B7 Synced ${formatRelativeTime(syncTime)}`)
        : null,
    ),

    // Device sync info (v4.7+)
    ...renderDeviceSyncInfo(f),

    // Conflict detail
    (f.status === "conflict" || conflict)
      ? createElement("div", {
          style: { fontSize: "11px", color: "#d94126", fontWeight: 600, marginTop: "2px" },
        }, "Conflict detected — resolve from the sync action")
      : null,

    // Local path
    f.local_path
      ? createElement("div", {
          className: "romm-panel-file-path",
          style: { marginTop: "3px" },
        }, f.local_path)
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
): (ReturnType<typeof createElement> | null)[] {
  if (saveStatus && saveStatus.files.length > 0) {
    return saveStatus.files.map((f) => {
      const conflict = conflicts.find((c) => c.filename === f.filename);
      return renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at);
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
        disabled: switching,
        onClick: handleActivate,
      }, switching ? "Switching..." : "Activate Slot"),
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
  // Callbacks
  onSlotSwitched: (newSlot: string, newStatus: SaveStatus) => void;
}

const SlotPanel: FC<SlotPanelProps> = ({
  romId,
  slot,
  isActive,
  defaultExpanded,
  saveStatus,
  conflicts,
  onSlotSwitched,
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
      ? renderActiveSlotBody(saveStatus, conflicts)
      : renderInactiveSlotBody(loadingSlot, slotFiles, switching, switchError, handleActivate);
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
    return createElement("div", { style: { fontSize: "13px", color: "#8f98a0", padding: "8px 0" } },
      "Loading slots...");
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
    legacyWarning,

    // Legacy mode: show save files directly above slot panels
    legacyFilesSection,

    // Slot panels — skip the "" (legacy) panel when already in legacy mode
    ...sorted
      .filter((s) => activeSlot !== null || s.slot !== "")
      .map((slot) => {
        const isActive = activeSlot !== null && slot.slot === activeSlot;
        return createElement(SlotPanel, {
          key: `panel-${slot.slot}`,
          romId,
          slot,
          isActive,
          defaultExpanded: isActive,
          saveStatus: isActive ? saveStatus : null,
          conflicts: isActive ? conflicts : [],
          onSlotSwitched,
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
