import { ConfirmModal, showModal } from "@decky/ui";

/**
 * "Save Sync Unavailable" fallback confirm (ADR-0015). Shown when an online
 * pre-launch sync failed without surfacing a conflict — asks whether to launch
 * with local saves anyway. Shared by both launch surfaces (the Play button's
 * `sync_failed` verdict and the global watcher) so the copy stays identical.
 *
 * Mirrors the `showModal(...)`-returns-a-Promise pattern of
 * `showCoreChangeModal` / `showOfflineDriftModal`. Resolves `true` on
 * "Launch Anyway", `false` on Cancel (and on outside-click / X, which
 * `ConfirmModal` routes through `onCancel`).
 */
export function showFallbackLaunchModal(message?: string): Promise<boolean> {
  const description = message?.trim()
    ? `${message} — launch with local saves?`
    : "Couldn't sync saves with RomM server. Launch with local saves?";
  return new Promise<boolean>((resolve) => {
    showModal(
      <ConfirmModal
        strTitle="Save Sync Unavailable"
        strDescription={description}
        strOKButtonText="Launch Anyway"
        strCancelButtonText="Cancel"
        onOK={() => resolve(true)}
        onCancel={() => resolve(false)}
      />,
    );
  });
}
