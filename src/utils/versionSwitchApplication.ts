import {
  fetchCoverBase64,
  invalidateCachedGameDetail,
  logError,
  logWarn,
  type SwitchVersionSuccess,
} from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";
import { withPruneLease } from "./pruneLease";

export async function applyCommittedVersionSwitch(
  result: SwitchVersionSuccess,
  onCover?: (romId: number, cover: string) => void,
): Promise<boolean> {
  return withPruneLease(result.prune_lease_token, "Version switch", async () => {
    let confirmed = false;
    try {
      confirmed = await setLaunchOptionsConfirmed(result.app_id, result.launch_options);
    } catch (e) {
      logError(`Version switch: launch-options confirm threw for rom ${result.rom_id} (appId ${result.app_id}): ${e}`);
    }
    if (!confirmed) {
      logError(`Version switch: could not confirm launch options for rom ${result.rom_id} (appId ${result.app_id})`);
    }
    await publishCommittedVersionSwitch(result.app_id, result.rom_id, onCover);
    return confirmed;
  });
}

export async function publishCommittedVersionSwitch(
  appId: number,
  romId: number,
  onCover?: (romId: number, cover: string) => void,
): Promise<void> {
  try {
    const cover = await fetchCoverBase64(romId);
    if (cover.base64) {
      onCover?.(romId, cover.base64);
      await SteamClient.Apps.SetCustomArtworkForApp(appId, cover.base64, "png", 0);
    }
  } catch (e) {
    logWarn(`Version switch: cover apply after switch failed for rom ${romId}: ${e}`);
  }
  invalidateCachedGameDetail(appId);
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", {
      detail: { type: "version_switched", app_id: appId, rom_id: romId },
    }),
  );
}
