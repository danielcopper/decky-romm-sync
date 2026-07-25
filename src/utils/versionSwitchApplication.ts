import {
  fetchCoverBase64,
  invalidateCachedGameDetail,
  logError,
  logWarn,
  type SwitchVersionSuccess,
} from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";
import { isPruneLeaseCancelled, withPruneLease, type PruneLeaseAdmission } from "./pruneLease";

export async function applyCommittedVersionSwitch(
  result: SwitchVersionSuccess,
  onCover?: (romId: number, cover: string) => void,
  admission?: PruneLeaseAdmission,
): Promise<boolean> {
  return withPruneLease(
    result.prune_lease_token,
    "Version switch",
    async (signal) => {
      let confirmed = false;
      if (isPruneLeaseCancelled(signal)) return confirmed;
      try {
        confirmed = await setLaunchOptionsConfirmed(result.app_id, result.launch_options);
      } catch (e) {
        logError(
          `Version switch: launch-options confirm threw for rom ${result.rom_id} (appId ${result.app_id}): ${e}`,
        );
      }
      if (!confirmed) {
        logError(`Version switch: could not confirm launch options for rom ${result.rom_id} (appId ${result.app_id})`);
      }
      if (!isPruneLeaseCancelled(signal)) {
        await publishCommittedVersionSwitch(result.app_id, result.rom_id, onCover, signal);
      }
      return confirmed;
    },
    `version-picker:${result.app_id}`,
    admission,
  );
}

export async function publishCommittedVersionSwitch(
  appId: number,
  romId: number,
  onCover?: (romId: number, cover: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  try {
    const cover = await fetchCoverBase64(romId);
    if (signal?.aborted) return;
    if (cover.base64) {
      onCover?.(romId, cover.base64);
      await SteamClient.Apps.SetCustomArtworkForApp(appId, cover.base64, "png", 0);
    }
  } catch (e) {
    logWarn(`Version switch: cover apply after switch failed for rom ${romId}: ${e}`);
  }
  if (signal?.aborted) return;
  invalidateCachedGameDetail(appId);
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", {
      detail: { type: "version_switched", app_id: appId, rom_id: romId },
    }),
  );
}
