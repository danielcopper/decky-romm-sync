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
import { DialogButton, Focusable, showModal } from "@decky/ui";
import { switchSlot, getVersionList, checkLocalDrift, debugLog, logWarn } from "../api/backend";
import { getRommConnectionState, onRommConnectionChange, reportServerReachable } from "../utils/connectionState";
import type { SaveStatus, SyncConflict, SaveSlotSummary, LastKnownSlots } from "../types";
import { scrollFocusedToCenter } from "../utils/scrollHelpers";
import { MUTED_COLOR } from "./saves/helpers";
import { renderLastKnownSlots } from "./saves/LastKnownSlotList";
import { NewSlotModal } from "./saves/NewSlotModal";
import { SlotPanel } from "./saves/SlotPanel";
import { ConnectingIndicator } from "./saves/ConnectingIndicator";
import { renderSaveFileRow } from "./saves/SaveFileRow";
import { useCopyToSlot } from "./saves/useCopyToSlot";
import { detach } from "../utils/detach";

interface SavesTabProps {
  /** The group's sticky Steam appId — drives the stranded-version drift check. */
  appId: number;
  romId: number;
  saveStatus: SaveStatus | null;
  conflicts: SyncConflict[];
  activeSlot: string | null;
  /** Whether `activeSlot` is an answer for this ROM. False while none has
   *  landed — the panel starts every ROM on a placeholder slot name, and this
   *  tab is what would otherwise render it as a fact (#1747). */
  activeSlotKnown: boolean;
  availableSlots: SaveSlotSummary[];
  /** The slots as of the last contact, shown only while no live answer has
   *  landed — read-only, and never a source for `activeSlot` (#1755). */
  lastKnownSlots: LastKnownSlots | null;
  slotsLoading: boolean;
  onSlotSwitched: (newSlot: string, newStatus: SaveStatus) => void;
}

export const SavesTab: FC<SavesTabProps> = ({
  appId,
  romId,
  saveStatus,
  conflicts,
  activeSlot,
  activeSlotKnown,
  availableSlots,
  lastKnownSlots,
  slotsLoading,
  onSlotSwitched,
}) => {
  const [newSlotError, setNewSlotError] = useState<string | null>(null);
  const newSlotErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [isOffline, setIsOffline] = useState(getRommConnectionState() === "offline");
  // Bumped to invalidate VersionHistoryPanel caches after a restore
  const [versionHistoryKey, setVersionHistoryKey] = useState(0);
  // Display name of an INACTIVE sibling version that is still on disk and has
  // unsynced save drift (#1298) — null when none. Reminds the user to switch back
  // and sync before those saves are lost.
  const [strandedVersion, setStrandedVersion] = useState<string | null>(null);

  const handleVersionRestored = () => {
    setVersionHistoryKey((k) => k + 1);
    // Trigger parent refresh of saveStatus so the tracked save row reflects
    // the new tracked_save_id / server fields without leaving the page.
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", {
        detail: { type: "save_sync", rom_id: romId },
      }),
    );
  };

  const handleSlotDeleted = () => {
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", {
        detail: { type: "save_sync", rom_id: romId },
      }),
    );
  };

  // Offline banner reacts live to any reachability signal via the shared store
  // (#1345) — appears when a call reports the server unreachable, clears when the
  // recovery probe or a successful call reconnects, without a tab re-entry.
  useEffect(() => onRommConnectionChange((s) => setIsOffline(s === "offline")), []);

  useEffect(() => {
    return () => {
      if (newSlotErrorTimerRef.current) clearTimeout(newSlotErrorTimerRef.current);
    };
  }, []);

  // Stranded-saves reminder (#1298): after a version switch the previously-bound
  // install becomes an INACTIVE sibling that still holds its (possibly unsynced)
  // saves on disk. Surface a banner when such a version has local drift so the
  // user knows to switch back and sync before those saves are lost. Re-runs on
  // saves-tab load (mount) and on a version switch (romId changes).
  useEffect(() => {
    let cancelled = false;
    // Getter, not a bare read: the two post-await `cancelled` checks would
    // otherwise be narrowed to "always false" by no-unnecessary-condition (the
    // cleanup mutation lives in another closure it can't model).
    const isCancelled = () => cancelled;
    const check = async (): Promise<void> => {
      try {
        const list = await getVersionList(appId);
        if (isCancelled()) return;
        const inactiveInstalled = list.multi_version
          ? (list.versions ?? []).filter((v) => v.installed && !v.active && !v.vanished)
          : [];
        // Probe every inactive-installed sibling (any of them can hold stranded
        // saves), and surface the first one that actually drifted.
        let stranded: string | null = null;
        for (const v of inactiveInstalled) {
          const drift = await checkLocalDrift(v.rom_id);
          if (isCancelled()) return;
          if (drift.drifted) {
            stranded = v.label || v.name || String(v.rom_id);
            break;
          }
        }
        setStrandedVersion(stranded);
      } catch (e) {
        if (!isCancelled()) setStrandedVersion(null);
        logWarn(`SavesTab: stranded-version check failed for appId ${appId}: ${e}`);
      }
    };
    detach(check());
    return () => {
      cancelled = true;
    };
  }, [appId, romId]);

  // Copy-to-slot opener, shared by every save row across the slot panels. The
  // picker's target list is derived from availableSlots (minus the source slot
  // and the legacy "" bucket); on success it dispatches romm_data_changed so the
  // parent re-fetches and both the source and target views refresh.
  const openCopyModal = useCopyToSlot(romId, availableSlots);

  // --- Offline banner ---
  const offlineBanner = isOffline
    ? createElement(
        "div",
        {
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
        },
        "RomM is offline — slot switching is disabled until the server is reachable. This prevents save sync conflicts.",
      )
    : null;

  // --- Stranded-version reminder banner (#1298) ---
  const strandedBanner = strandedVersion
    ? createElement(
        "div",
        {
          key: "stranded-banner",
          style: {
            padding: "8px",
            background: "rgba(255, 136, 0, 0.15)",
            borderRadius: "4px",
            border: "1px solid rgba(255, 136, 0, 0.3)",
            marginBottom: "12px",
            fontSize: "12px",
            color: "#ff8800",
          },
        },
        `Version "${strandedVersion}" has saves that were never uploaded — switch back to sync them.`,
      )
    : null;

  // --- Legacy mode warning ---
  const legacyWarning =
    activeSlot === null
      ? createElement(
          "div",
          {
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
          },
          "This game uses legacy mode (no slot). Only one save version per game is supported.",
        )
      : null;

  // --- Loading state ---
  // A spinner + live retry progress (#1345) instead of bare italic text — the
  // slot fetch pays the backend retry ladder, so surface "Connecting to RomM…
  // (attempt N/M)" while it is in flight.
  if (slotsLoading) {
    return createElement(
      Focusable,
      { noFocusRing: true },
      offlineBanner,
      strandedBanner,
      createElement(ConnectingIndicator, { key: "connecting" }),
    );
  }

  // --- Sort: active first, named alphabetically, legacy "" bucket last (#1478) ---
  const slotRank = (s: SaveSlotSummary): number => {
    if (s.slot === activeSlot) return 0;
    if (s.slot === "") return 2;
    return 1;
  };
  const sorted = [...availableSlots].sort((a, b) => {
    const rankDiff = slotRank(a) - slotRank(b);
    if (rankDiff !== 0) return rankDiff;
    return a.slot.localeCompare(b.slot);
  });

  // An active slot the list doesn't carry still gets a panel — it is a slot the
  // user is on. Only for a slot something ANSWERED, though: synthesised off the
  // panel's placeholder it puts a slot name on screen that nothing ever said
  // (#1747), and `default` is a real name here, so it reads as a fact.
  const slotInList = sorted.some((s) => s.slot === activeSlot);
  if (!slotInList && activeSlot && activeSlotKnown) {
    sorted.unshift({ slot: activeSlot, source: "local", count: 0, latest_updated_at: null });
  }

  // --- New Slot button handler ---
  const handleNewSlot = () => {
    showModal(
      createElement(NewSlotModal, {
        onSubmit: (name: string) => {
          // Switching into the slot-less legacy bucket is retired (#1276): an
          // empty name is not a valid slot target, so ignore it rather than
          // offering legacy mode. Legacy saves stay viewable in their own panel.
          if (!name) return;
          detach(
            (async () => {
              // Named slot — use switchSlot to do pre-checks + immediate download
              try {
                const result = await switchSlot(romId, name);
                if (result.success && result.save_status) {
                  reportServerReachable(true);
                  onSlotSwitched(name, result.save_status);
                } else {
                  detach(debugLog(`SavesTab: new slot switch failed: ${result.reason}`));
                  let msg = "Failed to create slot";
                  if (result.reason === "pending_uploads") {
                    msg = "Sync your saves first — local changes haven't been uploaded";
                  } else if (result.reason === "server_unreachable") {
                    reportServerReachable(false);
                    msg = "Can't switch — RomM server is not reachable";
                  }
                  setNewSlotError(msg);
                  if (newSlotErrorTimerRef.current) clearTimeout(newSlotErrorTimerRef.current);
                  newSlotErrorTimerRef.current = setTimeout(() => setNewSlotError(null), 5000);
                }
              } catch (e) {
                detach(debugLog(`SavesTab: new slot switch error: ${e}`));
                setNewSlotError("An error occurred while creating the slot");
                if (newSlotErrorTimerRef.current) clearTimeout(newSlotErrorTimerRef.current);
                newSlotErrorTimerRef.current = setTimeout(() => setNewSlotError(null), 5000);
              }
            })(),
          );
        },
      }),
    );
  };

  // --- The slots as of the last contact ---
  // Only while nothing live has landed, and only as history: the rows are not
  // pressable and nothing here feeds `activeSlot`, so the tab still names no
  // slot as the current one (#1747, #1755).
  const lastKnownSection = !activeSlotKnown && lastKnownSlots ? renderLastKnownSlots(lastKnownSlots) : null;

  // --- Save files with no slot panel to live under ---
  // Legacy mode has no slot; an unanswered active slot has none this tab may
  // name (#1747). Either way the locally tracked files are still what the user
  // has, so they are shown — just not filed under a slot.
  let unslottedFilesSection: ReturnType<typeof createElement> | null = null;
  if (activeSlot === null || !activeSlotKnown) {
    if (saveStatus && saveStatus.files.length > 0) {
      unslottedFilesSection = createElement(
        "div",
        { key: "unslotted-files", style: { marginBottom: "12px" } },
        ...saveStatus.files.map((f) => {
          const conflict = conflicts.find((c) => c.filename === f.filename);
          return renderSaveFileRow(f, conflict, saveStatus.last_sync_check_at);
        }),
      );
    } else {
      unslottedFilesSection = createElement(
        "div",
        {
          key: "no-files",
          style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic", marginBottom: "12px" },
        },
        "No save files tracked yet",
      );
    }
  }

  return createElement(
    Focusable,
    {
      noFocusRing: true,
      style: { display: "flex", flexDirection: "column" as const, gap: "0" },
    },
    offlineBanner,
    strandedBanner,
    legacyWarning,

    // Save files that belong to no slot panel — above the panels, if any.
    unslottedFilesSection,

    // The last contact's slots, where there are no live panels to show.
    lastKnownSection,

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
          onCopy: openCopyModal,
        });
      }),

    // New Slot button + error feedback
    createElement(
      "div",
      { key: "new-slot-area", style: { marginTop: "10px" } },
      createElement(
        DialogButton,
        // eslint-disable-next-line react-hooks/refs -- react-hooks/refs flags createElement of forwardRef components in ternary/conditional positions; @decky/ui's DialogButton extends RefAttributes via DialogCommonProps. Module-augmentation in src/types/decky-ui-augmentation.d.ts eliminated the `as any` cast but the refs rule fires independently.
        {
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
          // Creating a slot is a server write, so offline it can only end in the
          // failure the banner above already predicts — after the full retry
          // ladder. The sibling actions on this tab (slot switch, copy-to-slot)
          // are disabled the same way.
          disabled: isOffline,
        },
        "+ New Slot",
      ),
      newSlotError
        ? createElement(
            "div",
            {
              key: "new-slot-error",
              style: { fontSize: "11px", color: "#d94126", marginTop: "4px" },
            },
            newSlotError,
          )
        : null,
    ),
  );
};
