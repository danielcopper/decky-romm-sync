/**
 * A single collapsible save slot in the SavesTab list. Owns expand/collapse,
 * lazy-loads slot saves for inactive slots, and drives the activate / delete
 * flows; the parent SavesTab handles slot creation and the offline banner.
 */

import { useState, useRef, createElement, FC } from "react";
import { ConfirmModal, DialogButton, showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import { getSlotSaves, switchSlot, debugLog, getSlotDeleteInfo, deleteSlot } from "../../api/backend";
import type { SlotDeleteInfo } from "../../api/backend";
import type { SaveStatus, SyncConflict, SaveSlotSummary, SlotSaveFile, SwitchSlotResponse } from "../../types";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { computeSyncSummary, displaySlot, slotDeleteFailureToast } from "./helpers";
import { renderSaveFileRow } from "./SaveFileRow";
import { InactiveSlotBody } from "./InactiveSlotBody";
import { VersionHistoryPanel } from "./VersionHistoryPanel";

const MUTED_COLOR = "#8f98a0";

function renderActiveSlotBody(
  saveStatus: SaveStatus | null,
  conflicts: SyncConflict[],
  romId: number,
  slot: string,
  isOffline: boolean,
  onVersionRestored: () => void,
): (ReturnType<typeof createElement> | null)[] {
  if (saveStatus && saveStatus.files.length > 0) {
    return saveStatus.files.map((f) => {
      const conflict = conflicts.find((c) => c.filename === f.filename);
      return createElement("div", { key: f.filename },
        renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at),
        createElement(VersionHistoryPanel, {
          key: `vhp-${f.filename}`,
          romId,
          slot,
          filename: f.filename,
          isOffline,
          onRestored: onVersionRestored,
        }),
      );
    });
  }
  return [createElement("div", { key: "no-files", style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic" } },
    "No save files tracked yet")];
}

interface SlotPanelProps {
  romId: number;
  slot: SaveSlotSummary;
  isActive: boolean;
  defaultExpanded: boolean;
  // Active slot data (only set when isActive === true)
  saveStatus: SaveStatus | null;
  conflicts: SyncConflict[];
  isOffline: boolean;
  // Callbacks
  onSlotSwitched: (newSlot: string, newStatus: SaveStatus) => void;
  onVersionRestored: () => void;
  onSlotDeleted: () => void;
}

export const SlotPanel: FC<SlotPanelProps> = ({
  romId,
  slot,
  isActive,
  defaultExpanded,
  saveStatus,
  conflicts,
  isOffline,
  onSlotSwitched,
  onVersionRestored,
  onSlotDeleted,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [slotFiles, setSlotFiles] = useState<SlotSaveFile[] | null>(null);
  const [loadingSlot, setLoadingSlot] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
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

  const handleDelete = async () => {
    setDeleting(true);
    try {
      const info: SlotDeleteInfo = await getSlotDeleteInfo(romId, slotName);
      if (!info.success) {
        toaster.toast({ title: "RomM Sync", body: slotDeleteFailureToast(info) });
        return;
      }

      // Build confirmation message
      const lines: string[] = [];
      if (info.source === "server" && (info.server_save_count ?? 0) > 0) {
        const n = info.server_save_count ?? 0;
        lines.push(`This will permanently delete ${n} save${n === 1 ? "" : "s"} from slot '${info.slot}' on the RomM server.`);
      } else {
        lines.push(`This will remove slot '${info.slot}' from your local configuration.`);
      }
      if ((info.local_file_count ?? 0) > 0) {
        const n = info.local_file_count ?? 0;
        lines.push(`${n} tracked file${n === 1 ? "" : "s"} will be unlinked.`);
      }
      lines.push("This cannot be undone.");

      showModal(createElement(ConfirmModal, {
        strTitle: "Delete Slot",
        strDescription: lines.join("\n\n"),
        strOKButtonText: "Delete",
        strCancelButtonText: "Cancel",
        onOK: async () => {
          try {
            const result = await deleteSlot(romId, slotName);
            if (result.success) {
              toaster.toast({ title: "RomM Sync", body: `Slot '${slotName}' deleted` });
              onSlotDeleted();
            } else {
              toaster.toast({ title: "RomM Sync", body: result.message ?? "Failed to delete slot" });
            }
          } catch (e) {
            debugLog(`SavesTab: deleteSlot error: ${e}`);
            toaster.toast({ title: "RomM Sync", body: "An error occurred while deleting the slot" });
          }
        },
      }));
    } catch (e) {
      debugLog(`SavesTab: getSlotDeleteInfo error: ${e}`);
      toaster.toast({ title: "RomM Sync", body: "Failed to load slot info" });
    } finally {
      setDeleting(false);
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
  const headerEl = createElement(DialogButton, {
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
      createElement("span", { className: "romm-slot-chevron" }, expanded ? "▾" : "▸"),
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
  let bodyEl: ReturnType<typeof createElement> | null = null;
  if (expanded) {
    bodyEl = isActive
      ? createElement("div", { key: "body", className: "romm-slot-body" },
          ...renderActiveSlotBody(saveStatus, conflicts, romId, slotName, isOffline, onVersionRestored).filter(Boolean),
        )
      // eslint-disable-next-line react-hooks/refs -- createElement of an FC in a ternary branch trips the new react-hooks/refs rule; the component itself takes no ref.
      : createElement(InactiveSlotBody, {
          key: "body",
          loadingSlot, slotFiles, switching, switchError, isOffline, handleActivate, handleDelete, deleting,
        });
  }

  return createElement("div", { key: `slot-${slotName}`, className: panelClasses },
    headerEl,
    syncSummaryEl,
    bodyEl,
  );
};
