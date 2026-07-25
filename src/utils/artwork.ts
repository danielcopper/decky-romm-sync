/**
 * SGDB artwork apply — downloads the four SGDB asset types for a ROM and
 * writes them onto the Steam shortcut. Shared by RomMPlaySection (passive
 * auto-apply + Refresh Artwork action) and SgdbGamePickerModal (re-apply
 * after a manual game-id pick), so it lives here rather than on either
 * component to keep their import graph acyclic.
 */

import { getSgdbArtworkBase64, saveShortcutIcon, debugLog } from "../api/backend";
import { detach } from "./detach";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseCancelled,
  mountPruneLeaseOwner,
  releasePruneLeasesByOwner,
  withPruneLeases,
} from "./pruneLease";

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

/**
 * Apply the SGDB icon (type 4): the backend writes the PNG into Steam's grid dir
 * and returns its path; pointing the shortcut at it must go through SteamClient
 * (Steam owns shortcuts.vdf in memory and clobbers external writes). Returns
 * whether the icon was actually applied — a save failure or a missing path is not.
 */
async function applyIcon(appId: number, base64: string, signal: AbortSignal): Promise<boolean> {
  if (isPruneLeaseCancelled(signal)) return false;
  const iconResult = await saveShortcutIcon(appId, base64);
  if (isPruneLeaseCancelled(signal)) return false;
  if (iconResult.success && iconResult.icon_path) {
    SteamClient.Apps.SetShortcutIcon(appId, iconResult.icon_path);
    return true;
  }
  return false;
}

/** Fetch SGDB artwork (hero, logo, wide grid, icon) and apply to Steam.
 *  Returns count of successfully applied images, or -1 when no SGDB API
 *  key is configured. */
export async function applyArtwork(romId: number, appId: number): Promise<number> {
  const leaseOwner = `artwork:${appId}`;
  mountPruneLeaseOwner(leaseOwner);
  const admission = capturePruneLeaseAdmission(leaseOwner);
  const generation = (artworkGenerations.get(appId) ?? 0) + 1;
  artworkGenerations.set(appId, generation);
  const superseded = (): boolean => artworkGenerations.get(appId) !== generation;

  const results = await Promise.all([
    getSgdbArtworkBase64(romId, 1).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 2).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 3).catch(() => ({ base64: null, no_api_key: false })),
    getSgdbArtworkBase64(romId, 4).catch(() => ({ base64: null, no_api_key: false })),
  ]);

  const leaseTokens = results.map((result) => ("prune_lease_token" in result ? result.prune_lease_token : undefined));
  if (results.some((r) => r.no_api_key)) {
    return withPruneLeases(leaseTokens, "Artwork apply", async () => -1, leaseOwner, admission);
  }

  return withPruneLeases(
    leaseTokens,
    "Artwork apply",
    async (signal) => {
      let applied = 0;
      // A later apply for this appId can start during any of the awaits above/below,
      // so re-check before every Steam write. Once superseded, go silent (log once,
      // keep whatever this call already wrote) instead of overwriting the newer art.
      const bail = (): number => {
        detach(debugLog(`applyArtwork: superseded for appId ${appId}, skipping stale writes`));
        return applied;
      };

      // SGDB types 1-3 (hero / logo / wide grid) map 1:1 to Steam capsule assetTypes 1-3.
      const customArt: Array<[base64: string | null, assetType: number]> = [
        [results[0].base64, 1],
        [results[1].base64, 2],
        [results[2].base64, 3],
      ];
      for (const [base64, assetType] of customArt) {
        if (!base64) continue;
        if (isPruneLeaseCancelled(signal)) return applied;
        if (superseded()) return bail();
        await SteamClient.Apps.SetCustomArtworkForApp(appId, base64, "png", assetType);
        applied++;
      }

      // Type 4 = icon (a distinct apply path — see applyIcon).
      if (results[3].base64) {
        if (isPruneLeaseCancelled(signal)) return applied;
        if (superseded()) return bail();
        if (await applyIcon(appId, results[3].base64, signal)) applied++;
      }

      return applied;
    },
    leaseOwner,
    admission,
  );
}

export async function cancelArtworkApply(appId: number): Promise<void> {
  artworkGenerations.set(appId, (artworkGenerations.get(appId) ?? 0) + 1);
  await releasePruneLeasesByOwner(`artwork:${appId}`);
}
