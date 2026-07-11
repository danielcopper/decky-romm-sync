/**
 * Micro-batched cover-mtime stamp — the single writer of Steam overviews'
 * ``rt_custom_image_mtime``, shared by every stamping path (syncManager's
 * per-chunk stamp, index.tsx's immediate sweep, and the post-sync heal poll).
 *
 * Each assignment mutates a MobX observable in Steam's app store, so a synchronous
 * burst of up to a full platform's chunk (~368) triggers a reaction-flush storm
 * that visibly flickers the QAM at chunk/platform boundaries (on-device
 * 2026-07-10). Slicing the writes and yielding to the event loop between slices
 * gives Steam's reactions air to flush in small batches instead of one storm.
 */

import { logInfo, logError } from "../api/backend";
import { stateTransaction } from "./steamState";

// Stamp appIds in slices of this size, yielding once between slices.
const STAMP_BATCH_SIZE = 25;

/**
 * Batched write of ``rt_custom_image_mtime`` over *appIds* — one mtime for the
 * whole call, applied in {@link STAMP_BATCH_SIZE} slices with a ``setTimeout(0)``
 * yield between them so the MobX-reaction flush is spread out. Returns the counts;
 * the caller owns the summary log (the sweep and heal paths word it differently).
 * May throw if a lookup throws — callers wrap it fail-soft.
 */
async function writeBatched(appIds: number[]): Promise<{ stamped: number; noOverview: number }> {
  const mtime = Math.floor(Date.now() / 1000);
  let stamped = 0;
  let noOverview = 0;
  for (let i = 0; i < appIds.length; i += STAMP_BATCH_SIZE) {
    // Wrap each slice's observable writes in a mobx state transaction (synchronous;
    // the yield stays outside it) so they land on a strict-actions build too.
    stateTransaction(() => {
      for (const appId of appIds.slice(i, i + STAMP_BATCH_SIZE)) {
        const overview = appStore.GetAppOverviewByAppID(appId);
        if (overview) {
          overview.rt_custom_image_mtime = mtime;
          stamped++;
        } else {
          noOverview++;
        }
      }
    });
    // Yield BETWEEN slices (not after the last) so reactions flush in batches.
    if (i + STAMP_BATCH_SIZE < appIds.length) {
      await new Promise<void>((r) => setTimeout(r, 0));
    }
  }
  return { stamped, noOverview };
}

/**
 * Stamp ``rt_custom_image_mtime`` on each appId's Steam overview so a freshly
 * written grid cover shows on the tile's next render (the per-app cache-buster for
 * ``/customimage/{appid}?v={mtime}``), micro-batched to avoid a QAM-flickering
 * reaction storm. Fail-soft: a missing overview or a throw is summarized and never
 * propagates. ``label`` distinguishes the call site in the single summary log
 * (``" (chunk)"`` / ``""``). A no-op for an empty list. Callers ``void`` this (it
 * catches internally) so the per-chunk apply loop is never delayed and the
 * end-of-run sweep never blocks teardown.
 */
export async function stampCoverMtimes(appIds: number[], label: string): Promise<void> {
  if (appIds.length === 0) return;
  try {
    const { stamped, noOverview } = await writeBatched(appIds);
    logInfo(`[FE] cover mtime nudge${label}: ${stamped} stamped, ${noOverview} no overview`);
  } catch (e) {
    logError(`[FE] cover mtime nudge${label} failed for ${appIds.length} appIds: ${e}`);
  }
}

/**
 * Re-stamp a specific set of appIds whose stamp went missing — the heal path uses
 * the same micro-batched writer but emits NO summary line of its own (the poll
 * loop owns the ``cover mtime heal`` log). Fail-soft. A no-op for an empty list.
 */
export async function healCoverMtimes(appIds: number[]): Promise<void> {
  if (appIds.length === 0) return;
  try {
    await writeBatched(appIds);
  } catch (e) {
    logError(`[FE] cover mtime heal failed for ${appIds.length} appIds: ${e}`);
  }
}
