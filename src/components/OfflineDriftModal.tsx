import { ConfirmModal, showModal } from "@decky/ui";

/**
 * Offline-drift confirm (ADR-0015). Shown by the launch gate's `offline_drift`
 * verdict: RomM is unreachable AND the local save has unsynced changes, so
 * playing now may create a conflict the user resolves later. Asks whether to
 * start anyway.
 *
 * Mirrors the `showModal(...)`-returns-a-Promise pattern of
 * `showCoreChangeModal` / `showSyncConflictModal`. Resolves `"start_anyway"` on
 * OK, `"cancel"` on Cancel (and on outside-click / X, which `ConfirmModal`
 * routes through `onCancel`).
 */
export function showOfflineDriftModal(): Promise<"start_anyway" | "cancel"> {
  return new Promise<"start_anyway" | "cancel">((resolve) => {
    showModal(
      <ConfirmModal
        strTitle="RomM Unreachable"
        strDescription="Your local save has unsynced changes. Playing now may create a conflict you'll resolve later. Start anyway?"
        strOKButtonText="Start Anyway"
        strCancelButtonText="Cancel"
        onOK={() => resolve("start_anyway")}
        onCancel={() => resolve("cancel")}
      />,
    );
  });
}
