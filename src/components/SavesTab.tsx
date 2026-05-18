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

import { useState, useEffect, useRef, createElement, FC } from "react";
import { ConfirmModal, DialogButton, Focusable, showModal } from "@decky/ui";
import { switchSlot, debugLog } from "../api/backend";
import { getRommConnectionState } from "../utils/connectionState";
import type { SaveStatus, SyncConflict, SaveSlotSummary } from "../types";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import { NewSlotModal } from "./saves/NewSlotModal";
import { SlotPanel } from "./saves/SlotPanel";
import { renderSaveFileRow } from "./saves/SaveFileRow";

interface SavesTabProps {
  romId: number;
  saveStatus: SaveStatus | null;
  conflicts: SyncConflict[];
  activeSlot: string | null;
  availableSlots: SaveSlotSummary[];
  slotsLoading: boolean;
  onSlotSwitched: (newSlot: string, newStatus: SaveStatus) => void;
}

const MUTED_COLOR = "#8f98a0";

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
  // Bumped to invalidate VersionHistoryPanel caches after a restore
  const [versionHistoryKey, setVersionHistoryKey] = useState(0);

  const handleVersionRestored = () => {
    setVersionHistoryKey((k) => k + 1);
    // Trigger parent refresh of saveStatus so the tracked save row reflects
    // the new tracked_save_id / server fields without leaving the page.
    globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
      detail: { type: "save_sync", rom_id: romId },
    }));
  };

  const handleSlotDeleted = () => {
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
      }, "RomM is offline — slot switching is disabled until the server is reachable. This prevents save sync conflicts.")
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
    return createElement(Focusable, { noFocusRing: true },
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

  return createElement(Focusable, {
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
          isOffline,
          onSlotSwitched,
          onVersionRestored: handleVersionRestored,
          onSlotDeleted: handleSlotDeleted,
        });
      }),

    // New Slot button + error feedback
    createElement("div", { key: "new-slot-area", style: { marginTop: "10px" } },
      // eslint-disable-next-line react-hooks/refs -- react-hooks/refs flags createElement of forwardRef components in ternary/conditional positions; @decky/ui's DialogButton extends RefAttributes via DialogCommonProps. Module-augmentation in src/types/decky-ui-augmentation.d.ts eliminated the `as any` cast but the refs rule fires independently.
      createElement(DialogButton, {
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
