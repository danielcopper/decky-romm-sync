/**
 * SGDB artwork apply — downloads the four SGDB asset types for a ROM and
 * writes them onto the Steam shortcut. Shared by RomMPlaySection (passive
 * auto-apply + Refresh Artwork action) and SgdbGamePickerModal (re-apply
 * after a manual game-id pick), so it lives here rather than on either
 * component to keep their import graph acyclic.
 */

import { getSgdbArtworkBase64, saveShortcutIcon, debugLog } from "../api/backend";
import { detach } from "./detach";

/**
 * Newest-apply-wins guard. Each appId's most recent applyArtwork call claims a
 * generation; an earlier call for the SAME appId that is still mid-flight when a
 * newer one starts stops writing rather than clobber the newer art. The hazard:
 * a version switch re-points the shortcut to a new rom and fires a fresh apply
 * while the old rom's (uncached, seconds-long) artwork fetch is still pending —
 * without this, the old apply's writes land last and revert the tile. Keyed by
 * appId so unrelated shortcuts never gate each other.
 */
const artworkGenerations = new Map<number, number>();

/** Fetch SGDB artwork (hero, logo, wide grid, icon) and apply to Steam.
 *  Returns count of successfully applied images, or -1 when no SGDB API
 *  key is configured. */
export async function applyArtwork(romId: number, appId: number): Promise<number> {
  const generation = (artworkGenerations.get(appId) ?? 0) + 1;
  artworkGenerations.set(appId, generation);
  const superseded = (): boolean => artworkGenerations.get(appId) !== generation;

  const results = await Promise.all([
    getSgdbArtworkBase64(romId, 1).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 2).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 3).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 4).catch(() => ({ base64: null, no_api_key: false })),
  ]);

  if (results.some((r) => r.no_api_key)) return -1;

  let applied = 0;
  // A later apply for this appId can start during any of the awaits above/below,
  // so re-check before every Steam write. Once superseded, go silent (log once,
  // keep whatever this call already wrote) instead of overwriting the newer art.
  const bail = (): number => {
    detach(debugLog(`applyArtwork: superseded for appId ${appId}, skipping stale writes`));
    return applied;
  };

  // SGDB type 1 = hero → Steam assetType 1
  if (results[0].base64) {
    if (superseded()) return bail();
    await SteamClient.Apps.SetCustomArtworkForApp(appId, results[0].base64, "png", 1);
    applied++;
  }
  // SGDB type 2 = logo → Steam assetType 2
  if (results[1].base64) {
    if (superseded()) return bail();
    await SteamClient.Apps.SetCustomArtworkForApp(appId, results[1].base64, "png", 2);
    applied++;
  }
  // SGDB type 3 = wide grid → Steam assetType 3
  if (results[2].base64) {
    if (superseded()) return bail();
    await SteamClient.Apps.SetCustomArtworkForApp(appId, results[2].base64, "png", 3);
    applied++;
  }
  // Type 4 = icon (VDF-based)
  if (results[3].base64) {
    if (superseded()) return bail();
    await saveShortcutIcon(appId, results[3].base64);
    applied++;
  }

  return applied;
}
