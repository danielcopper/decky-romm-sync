import {
  fetchCoverBase64,
  invalidateCachedGameDetail,
  logError,
  logWarn,
  type SwitchVersionSuccess,
} from "../api/backend";
import { setLaunchOptionsConfirmed } from "./steamShortcuts";

export async function applyCommittedVersionSwitch(
  result: SwitchVersionSuccess,
  onCover?: (romId: number, cover: string) => void,
): Promise<boolean> {
  let confirmed = false;
  try {
    confirmed = await setLaunchOptionsConfirmed(result.app_id, result.launch_options);
  } catch (e) {
    logError(`Version switch: launch-options confirm threw for rom ${result.rom_id} (appId ${result.app_id}): ${e}`);
  }
  if (!confirmed) {
    logError(`Version switch: could not confirm launch options for rom ${result.rom_id} (appId ${result.app_id})`);
  }
  try {
    const cover = await fetchCoverBase64(result.rom_id);
    if (cover.base64) {
      onCover?.(result.rom_id, cover.base64);
      await SteamClient.Apps.SetCustomArtworkForApp(result.app_id, cover.base64, "png", 0);
    }
  } catch (e) {
    logWarn(`Version switch: cover apply after switch failed for rom ${result.rom_id}: ${e}`);
  }
  invalidateCachedGameDetail(result.app_id);
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", {
      detail: { type: "version_switched", app_id: result.app_id, rom_id: result.rom_id },
    }),
  );
  return confirmed;
}
