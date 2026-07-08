import { ConfirmModal, showModal } from "@decky/ui";

/** What the user chose in the unsynced-saves switch confirm (#1298). */
export type UnsyncedSavesChoice = "sync_and_switch" | "switch_anyway" | "cancel";

interface UnsyncedSavesModalArgs {
  /** Display name of the currently-bound version that would be stranded. */
  versionName: string;
  /** Whether RomM is reachable — decides if "Sync now & switch" is offered. */
  serverReachable: boolean;
}

/**
 * Unsynced-saves confirm for switching away from a downloaded version (#1298).
 * Shown by the version picker when the backend soft-blocks the switch
 * (`reason: "unsynced_saves"`): the bound install has local save changes that
 * were never uploaded to RomM.
 *
 * Two variants by reachability (one component, conditional middle/OK buttons),
 * mirroring `showOfflineDriftModal`'s `showModal(...)`-returns-a-Promise pattern:
 *
 * - Reachable (T4): OK = "Sync now & switch" (`sync_and_switch`), middle =
 *   "Switch anyway" (`switch_anyway`), Cancel (`cancel`).
 * - Offline (T5): OK = "Switch anyway" (`switch_anyway`), Cancel (`cancel`) — no
 *   sync option, since the saves can't be uploaded while RomM is unreachable.
 *
 * Outside-click / X routes through `ConfirmModal`'s `onCancel` → `cancel`.
 */
export function showUnsyncedSavesModal({
  versionName,
  serverReachable,
}: UnsyncedSavesModalArgs): Promise<UnsyncedSavesChoice> {
  return new Promise<UnsyncedSavesChoice>((resolve) => {
    const description = serverReachable
      ? `"${versionName}" has save changes that were never uploaded to RomM. They stay on disk, but won't sync until you switch back.`
      : `"${versionName}" has save changes that were never uploaded, and RomM is not reachable right now — so they can't be synced first. They stay on disk, but won't sync until you switch back.`;

    // Reachable: the middle button is the "strand it" escape hatch below the
    // primary "Sync now & switch". Offline: no middle button — OK IS "Switch
    // anyway" (spread nothing so the prop is absent, not `undefined`, which
    // `exactOptionalPropertyTypes` rejects).
    const middleButton = serverReachable
      ? { strMiddleButtonText: "Switch anyway", onMiddleButton: () => resolve("switch_anyway") }
      : {};

    showModal(
      <ConfirmModal
        strTitle="Unsynced saves"
        strDescription={description}
        strOKButtonText={serverReachable ? "Sync now & switch" : "Switch anyway"}
        strCancelButtonText="Cancel"
        onOK={() => resolve(serverReachable ? "sync_and_switch" : "switch_anyway")}
        onCancel={() => resolve("cancel")}
        {...middleButton}
      />,
    );
  });
}
