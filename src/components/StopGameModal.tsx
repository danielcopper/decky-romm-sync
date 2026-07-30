import { ConfirmModal, showModal } from "@decky/ui";

/**
 * "Stop Game" confirm. Shown before the running overlay's Stop Game action
 * terminates the live emulator. The copy promises nothing about the save: the
 * backend asks the emulator to exit and forces it if it refuses, so whether an
 * in-flight write completes is the emulator's business, not ours.
 *
 * Mirrors the `showModal(...)`-returns-a-Promise pattern of
 * `showCoreChangeModal` / `showFallbackLaunchModal`. Resolves `true` on
 * "Stop Game", `false` on Cancel (and on outside-click / X, which
 * `ConfirmModal` routes through `onCancel`).
 */
export function showStopGameModal(): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    showModal(
      <ConfirmModal
        strTitle="Stop Game?"
        strDescription="Any progress since the last in-game save may be lost."
        strOKButtonText="Stop Game"
        strCancelButtonText="Cancel"
        onOK={() => resolve(true)}
        onCancel={() => resolve(false)}
      />,
    );
  });
}
