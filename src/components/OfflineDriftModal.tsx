import { ConfirmModal, showModal } from "@decky/ui";

/**
 * Offline-drift confirm (ADR-0015). Shown by the launch gate's `offline_drift`
 * verdict: RomM is unreachable AND the local save has unsynced changes, so
 * playing now may create a conflict the user resolves later. Asks whether to
 * start anyway, retry the connection, or cancel.
 *
 * Mirrors the `showModal(...)`-returns-a-Promise pattern of
 * `showCoreChangeModal` / `showSyncConflictModal`. Resolves `"start_anyway"` on
 * OK, `"retry"` on the middle button (re-run the launch gate, which re-probes
 * via the fast reachability check), and `"cancel"` on Cancel (and on
 * outside-click / X, which `ConfirmModal` routes through `onCancel`).
 */
export function showOfflineDriftModal(): Promise<"start_anyway" | "retry" | "cancel"> {
  return new Promise<"start_anyway" | "retry" | "cancel">((resolve) => {
    showModal(
      <ConfirmModal
        strTitle="RomM Unreachable"
        strDescription="Your local save has unsynced changes. Playing now may create a conflict you'll resolve later. Start anyway?"
        strOKButtonText="Start Anyway"
        strMiddleButtonText="Retry connection"
        strCancelButtonText="Cancel"
        onOK={() => resolve("start_anyway")}
        onMiddleButton={() => resolve("retry")}
        onCancel={() => resolve("cancel")}
      />,
    );
  });
}
