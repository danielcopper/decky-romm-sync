/**
 * Hook driving the per-save "Copy to slot…" flow. Returns an opener that shows
 * the CopyToSlotModal for one source save, runs `copySaveToSlot`, and routes
 * every discriminated status to the right feedback: `ok` toasts and dispatches
 * `romm_data_changed` (so the parent re-fetches source AND target views);
 * `conflict_blocked` opens the standard sync-conflict modal (as
 * VersionHistoryPanel does); `target_slot_busy` and each refusal toast.
 */

import { useCallback } from "react";
import { showModal } from "@decky/ui";
import { showToast } from "../../utils/toast";
import { copySaveToSlot, debugLog } from "../../api/backend";
import type { CopySaveToSlotStatus, SaveSlotSummary } from "../../types";
import { showSyncConflictModal } from "../SyncConflictModal";
import { detach } from "../../utils/detach";
import { displaySlot } from "./helpers";
import { CopyToSlotModal } from "./CopyToSlotModal";
import type { CopyToSlotHandler } from "./CopyToSlotButton";

/** Refresh both the source and target slot views after a successful copy. */
function dispatchDataChanged(romId: number): void {
  globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
}

async function handleResult(result: CopySaveToSlotStatus, target: string, romId: number): Promise<void> {
  switch (result.status) {
    case "ok":
      showToast(`Save copied to slot '${displaySlot(target)}'`);
      dispatchDataChanged(romId);
      return;
    case "already_present":
      // Content-identical to a save already in the target slot — nothing was
      // copied (no churn), so no refresh either.
      showToast(`Already in slot '${displaySlot(target)}' as #${result.existing_id}`);
      return;
    case "conflict_blocked": {
      // A real conflict on the ROM's current slot must be resolved first. The
      // modal owns the resolution feedback — don't stack a toast on top of it
      // (mirrors VersionHistoryPanel). The empty-list case has no modal to show.
      const first = result.conflicts[0];
      if (first) {
        await showSyncConflictModal(first);
      } else {
        showToast("Copy blocked by a sync conflict. Sync this save, then try again.");
      }
      return;
    }
    case "target_slot_busy":
      showToast(`Slot '${displaySlot(target)}' has newer changes on another device — sync it first, then copy again.`);
      return;
    case "preflight_failed":
      showToast(`Sync failed before copy: ${result.errors[0] ?? "preflight error"}`);
      return;
    case "server_unreachable":
      showToast("Couldn't reach RomM. Check your connection and try again.");
      return;
    case "not_found":
      // The server answered — it has no such ROM or device id (#1560 family).
      // A retry can't help, so the copy must not send the user to check their
      // connection, and must not claim the saves are gone: the 404 can be the
      // device registration rather than the ROM (#1570).
      showToast("RomM couldn't find this game's save data — nothing was copied.");
      return;
    case "version_deleted":
      showToast("This save no longer exists on the server.");
      return;
    case "rom_not_installed":
      showToast("ROM is no longer installed locally. Reinstall and try again.");
      return;
    case "unsupported":
      showToast(
        result.reason === "savefiles_in_content_dir"
          ? "Save sync is off for this game (RetroArch writes saves next to the ROM)."
          : "Copying isn't available for multi-file saves yet.",
      );
      return;
    case "not_configured":
      showToast("Set up save slots for this game first, then copy.");
      return;
    case "copy_failed":
      showToast(`Couldn't copy the save: ${result.message}`);
      return;
    case "invalid_slot_name":
      showToast("Enter a valid slot name.");
      return;
  }
}

/** Returns an opener `openCopyModal(saveId, sourceSlot)` for the copy-to-slot flow. */
export function useCopyToSlot(romId: number, availableSlots: SaveSlotSummary[]): CopyToSlotHandler {
  return useCallback(
    (saveId: number, sourceSlot: string) => {
      const runCopy = async (target: string): Promise<void> => {
        try {
          const result = await copySaveToSlot(romId, saveId, target);
          await handleResult(result, target, romId);
        } catch (e) {
          detach(debugLog(`useCopyToSlot: copy failed for save ${saveId} into '${target}': ${e}`));
          showToast("Couldn't copy the save. Check your connection and try again.");
        }
      };
      showModal(
        <CopyToSlotModal
          availableSlots={availableSlots}
          sourceSlot={sourceSlot}
          onSubmit={(target: string) => {
            detach(runCopy(target));
          }}
        />,
      );
    },
    [romId, availableSlots],
  );
}
