/**
 * A single collapsible save slot in the SavesTab list. Owns expand/collapse,
 * lazy-loads slot saves for inactive slots, and drives the activate / delete
 * flows; the parent SavesTab handles slot creation and the offline banner.
 */

import { useState, useRef, useEffect, createElement, FC } from "react";
import { ConfirmModal, DialogButton, showModal } from "@decky/ui";
import { showToast } from "../../utils/toast";
import { getSlotSaves, switchSlot, debugLog, getSlotDeleteInfo, deleteSlot } from "../../api/backend";
import type {
  SaveStatus,
  SyncConflict,
  SaveSlotSummary,
  SlotSaveFile,
  SwitchSlotResponse,
  SlotDeleteInfo,
} from "../../types";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { reportServerReachable } from "../../utils/connectionState";
import { MUTED_COLOR, computeSyncSummary, displaySlot, slotDeleteFailureToast } from "./helpers";
import { renderSaveFileRow } from "./SaveFileRow";
import { InactiveSlotBody } from "./InactiveSlotBody";
import { VersionHistoryPanel } from "./VersionHistoryPanel";
import { renderCopyToSlotButton, type CopyToSlotHandler } from "./CopyToSlotButton";
import { detach } from "../../utils/detach";

/**
 * Multi-file note (#908 interim guard) — replaces the Previous-Versions /
 * rollback UI for a save that spans more than one file. Lists the N component
 * filenames and explains that per-version rollback isn't available yet.
 */
function renderMultiFileNote(componentFiles: string[]): ReturnType<typeof createElement> {
  const n = componentFiles.length;
  return createElement(
    "div",
    {
      key: "multi-file-note",
      style: { marginTop: "6px", marginLeft: "8px" },
    },
    createElement(
      "div",
      { style: { fontSize: "11px", color: "#8f98a0", fontWeight: 600 } },
      `Files in this save (${n})`,
    ),
    ...componentFiles.map((fn) =>
      createElement(
        "div",
        {
          key: `comp-${fn}`,
          style: {
            fontSize: "11px",
            color: "#8f98a0",
            fontFamily: "monospace",
            wordBreak: "break-all" as const,
            marginTop: "1px",
          },
        },
        fn,
      ),
    ),
    createElement(
      "div",
      { style: { fontSize: "11px", color: MUTED_COLOR, fontStyle: "italic" as const, marginTop: "4px" } },
      `This save spans ${n} files. Per-version rollback isn't available for multi-file saves yet.`,
    ),
  );
}

function renderActiveSlotBody(
  saveStatus: SaveStatus | null,
  conflicts: SyncConflict[],
  romId: number,
  slot: string,
  isOffline: boolean,
  onVersionRestored: () => void,
  onCopy: CopyToSlotHandler,
): (ReturnType<typeof createElement> | null)[] {
  if (saveStatus && saveStatus.files.length > 0) {
    // Multi-file save (#908 guard): the slot's current save is one game state
    // spread across N files. The siblings are components, not prior versions,
    // so suppress the per-file VersionHistoryPanel/rollback AND the copy action
    // (copying one component would produce an incoherent set) and show the
    // component list + a calm note instead.
    if (saveStatus.multi_file) {
      const componentFiles = saveStatus.component_files ?? [];
      return [
        ...saveStatus.files.map((f) => {
          const conflict = conflicts.find((c) => c.filename === f.filename);
          return createElement(
            "div",
            { key: f.filename },
            renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at),
          );
        }),
        renderMultiFileNote(componentFiles),
      ];
    }
    return saveStatus.files.map((f) => {
      const conflict = conflicts.find((c) => c.filename === f.filename);
      return createElement(
        "div",
        { key: f.filename },
        renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at),
        // Copy-to-slot on the active slot's current save (source = this slot).
        // Rendered as a sibling of the save-file row (never nested in its
        // DialogButton) so Deck focus stays flat. Only when it has a server save.
        f.server_save_id != null
          ? createElement(
              "div",
              {
                key: `copy-active-${f.filename}`,
                style: { display: "flex", justifyContent: "flex-end" as const, marginTop: "4px" },
              },
              renderCopyToSlotButton(`copy-active-${f.filename}`, f.server_save_id, {
                onCopy,
                sourceSlot: slot,
                isOffline,
              }),
            )
          : null,
        createElement(VersionHistoryPanel, {
          key: `vhp-${f.filename}`,
          romId,
          slot,
          filename: f.filename,
          isOffline,
          onRestored: onVersionRestored,
          onCopy,
        }),
      );
    });
  }
  return [
    createElement(
      "div",
      { key: "no-files", style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic" } },
      "No save files tracked yet",
    ),
  ];
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
  /** Opens the copy-to-slot picker for a save row in this slot. */
  onCopy: CopyToSlotHandler;
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
  onCopy,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [slotFiles, setSlotFiles] = useState<SlotSaveFile[] | null>(null);
  const [loadingSlot, setLoadingSlot] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const switchErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (switchErrorTimerRef.current) clearTimeout(switchErrorTimerRef.current);
    };
  }, []);

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
        detach(debugLog(`SavesTab: failed to load slot saves for ${slotName}: ${e}`));
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
        reportServerReachable(true);
        onSlotSwitched(slotName, result.save_status);
      } else {
        let msg = "Failed to switch slot";
        if (result.reason === "pending_uploads") {
          msg = "Sync your saves first — local changes haven't been uploaded";
        } else if (result.reason === "server_unreachable") {
          reportServerReachable(false);
          msg = "Can't switch — RomM server is not reachable";
        } else if (result.reason === "not_installed") {
          msg = "Can't switch — download the game first";
        }
        setSwitchError(msg);
        if (switchErrorTimerRef.current) clearTimeout(switchErrorTimerRef.current);
        switchErrorTimerRef.current = setTimeout(() => setSwitchError(null), 5000);
      }
    } catch (e) {
      detach(debugLog(`SavesTab: switchSlot error: ${e}`));
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
        showToast(slotDeleteFailureToast(info));
        return;
      }

      // Build confirmation message
      const lines: string[] = [];
      if (info.source === "server" && (info.server_save_count ?? 0) > 0) {
        const n = info.server_save_count ?? 0;
        lines.push(
          `This will permanently delete ${n} save${n === 1 ? "" : "s"} from slot '${info.slot}' on the RomM server.`,
        );
      } else {
        lines.push(`This will remove slot '${info.slot}' from your local configuration.`);
      }
      if ((info.local_file_count ?? 0) > 0) {
        const n = info.local_file_count ?? 0;
        lines.push(`${n} tracked file${n === 1 ? "" : "s"} will be unlinked.`);
      }
      lines.push("This cannot be undone.");

      showModal(
        createElement(ConfirmModal, {
          strTitle: "Delete Slot",
          strDescription: lines.join("\n\n"),
          strOKButtonText: "Delete",
          strCancelButtonText: "Cancel",
          onOK: () => {
            detach(
              (async () => {
                try {
                  const result = await deleteSlot(romId, slotName);
                  if (result.success) {
                    showToast(`Slot '${slotName}' deleted`);
                    onSlotDeleted();
                  } else {
                    showToast(result.message ?? "Failed to delete slot");
                  }
                } catch (e) {
                  detach(debugLog(`SavesTab: deleteSlot error: ${e}`));
                  showToast("An error occurred while deleting the slot");
                }
              })(),
            );
          },
        }),
      );
    } catch (e) {
      detach(debugLog(`SavesTab: getSlotDeleteInfo error: ${e}`));
      showToast("Failed to load slot info");
    } finally {
      setDeleting(false);
    }
  };

  const { syncSummaryText, syncSummaryColor } = computeSyncSummary(isActive, saveStatus, conflicts);

  // Active slot: prefer the server-side save count (SaveSlotSummary.count, kept
  // fresh by the parent's romm_data_changed → getSaveSlots refresh) so the header
  // reflects every save in the slot — a copy adds a version that the tracked-file
  // count (files.length, usually 1) would never show. Fall back to the tracked
  // file count when the summary count is absent (0 — e.g. the synthesized
  // active-slot placeholder before getSaveSlots resolves).
  const fileCount = isActive ? slot.count || (saveStatus?.files.length ?? 0) : (slotFiles?.length ?? slot.count);

  // The slot-less legacy (web-player) bucket is read-only (#1276, #1478) and
  // demoted: muted styling + a read-only note; it sorts last (in SavesTab).
  const isLegacy = slotName === "";

  const panelClasses = [
    "romm-slot-panel",
    isActive ? "romm-slot-panel-active" : "",
    isLegacy ? "romm-slot-panel-legacy" : "",
  ]
    .filter(Boolean)
    .join(" ");

  // --- Source badge ---
  const sourceBadge =
    slot.source === "local"
      ? createElement("span", { key: "src", className: "romm-slot-badge romm-slot-badge-local" }, "local")
      : createElement("span", { key: "src", className: "romm-slot-badge romm-slot-badge-server" }, "server");

  // --- Slot header ---
  const headerEl = createElement(
    DialogButton,
    {
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
      onClick: () => {
        detach(handleToggle());
      },
    },
    // Left: slot name + badges
    createElement(
      "div",
      { className: "romm-slot-header-left" },
      createElement("span", { className: "romm-slot-name" }, displaySlot(slotName)),
      isActive
        ? createElement("span", { key: "active", className: "romm-slot-badge romm-slot-badge-active" }, "active")
        : null,
      sourceBadge,
    ),
    // Right: file count + chevron
    createElement(
      "div",
      { className: "romm-slot-header-right" },
      createElement("span", { className: "romm-slot-count" }, `${fileCount} save${fileCount === 1 ? "" : "s"}`),
      createElement("span", { className: "romm-slot-chevron" }, expanded ? "▾" : "▸"),
    ),
  );

  // --- Sync summary line (active slot only) ---
  const syncSummaryEl =
    isActive && syncSummaryText
      ? createElement(
          "div",
          {
            key: "sync-summary",
            className: "romm-slot-sync-summary",
            style: { color: syncSummaryColor },
          },
          syncSummaryText,
        )
      : null;

  // --- Read-only note (legacy bucket only, always visible) ---
  const legacyNoteEl = isLegacy
    ? createElement(
        "div",
        {
          key: "legacy-note",
          className: "romm-slot-legacy-note",
        },
        "Used by the RomM web player. Read-only here — manage in the RomM web app.",
      )
    : null;

  // --- Slot body ---
  let bodyEl: ReturnType<typeof createElement> | null = null;
  if (expanded) {
    bodyEl = isActive
      ? createElement(
          "div",
          { key: "body", className: "romm-slot-body" },
          ...renderActiveSlotBody(saveStatus, conflicts, romId, slotName, isOffline, onVersionRestored, onCopy).filter(
            Boolean,
          ),
        )
      : // eslint-disable-next-line react-hooks/refs -- createElement of an FC in a ternary branch trips the new react-hooks/refs rule; the component itself takes no ref.
        createElement(InactiveSlotBody, {
          key: "body",
          loadingSlot,
          slotFiles,
          switching,
          switchError,
          isOffline,
          // Legacy bucket is read-only (#1276 / #1478) — no Activate/Delete.
          isLegacy,
          deleting,
          handleActivate: () => {
            detach(handleActivate());
          },
          handleDelete: () => {
            detach(handleDelete());
          },
          // Copy source = this (inactive/legacy) slot; the picker excludes it as a target.
          copy: { onCopy, sourceSlot: slotName, isOffline },
        });
  }

  return createElement(
    "div",
    { key: `slot-${slotName}`, className: panelClasses },
    headerEl,
    syncSummaryEl,
    legacyNoteEl,
    bodyEl,
  );
};
