/**
 * Sync-time estimate — the pure cost model behind the always-on "Estimated
 * time" readout. Given counts of new, updated and cover-refreshed shortcuts it
 * returns an expected apply duration; anything that turns item counts into a
 * human-readable duration for the sync UI lives here. No I/O, no React.
 *
 * The model prices three INDEPENDENT terms, because they are three independent
 * phases of a run and a single blended per-item rate cannot describe a mix of
 * them: the per-item create walk, the per-item update walk, and the backend's
 * cover-download pass that runs between a unit's fetch and its first apply
 * chunk. Each constant is calibrated to its own measured mean with a ceiling
 * margin, so the estimate reads long rather than short whatever the mix. A flat
 * allowance covers the run's fixed overhead (the one-time shortcut scan, the
 * multi-page fetch, inter-chunk gaps, finalize).
 */

import type { SyncPlanUnit } from "../types/sync";

/**
 * Seconds per newly created shortcut — the AddShortcut + overview poll, the
 * Set* calls, the confirm poll, the inter-op cadence, and the per-item artwork
 * leg (DB read → file read → base64 → IPC → SetCustomArtworkForApp). Measured
 * at ~0.314 s/item (2026-07 on-device); carries a ~15% margin.
 * Excludes the backend cover DOWNLOAD, which is priced separately by
 * {@link COVER_DOWNLOAD_SEC} — it is a distinct phase, not part of the walk.
 */
export const NEW_ITEM_SEC = 0.36;

/**
 * Seconds per updated shortcut — the update path reuses the existing shortcut
 * (no AddShortcut, no overview poll, and no artwork: the apply loop gates cover
 * application on ``created``), so it is just the Set* calls and the confirm
 * poll. Measured at ~0.109 s/item (2026-07 on-device); carries a ~15% margin.
 */
export const UPDATED_ITEM_SEC = 0.13;

/**
 * Seconds per cover the backend must DOWNLOAD, in the per-unit cover pass that
 * runs before the unit's first apply chunk. Measured 0.132 s/cover cold
 * (2026-07 on-device). A cover already in the cache costs 0.0018 s — 73x
 * cheaper, and deliberately NOT a term: pricing warm covers would require a
 * backend cache probe in the preview path that we are not adding, and treating
 * every cover as cold is the safe direction.
 */
export const COVER_DOWNLOAD_SEC = 0.15;

/**
 * Flat allowance for the run's FIXED overhead — the one-time shortcut scan
 * (which scales with the existing library, not the delta), the multi-page ROM +
 * save fetches, the inter-chunk gaps, and finalize. Measured 17–24 s on-device
 * across a create-heavy and an update-heavy run; the margin here is wide because
 * the scan grows with library size.
 */
export const FETCH_ALLOWANCE_SEC = 45;

/**
 * Expected apply duration in seconds for *newCount* created and *changedCount*
 * updated shortcuts, plus *coverRefreshCount* covers refreshed on already-bound
 * shortcuts, plus the flat fixed-overhead allowance.
 *
 * Every create is priced as needing a cover download on top of the refreshes —
 * a deliberate upper bound: a create usually does need one, and the cache state
 * is not knowable without a backend probe, so over-reading is the safe
 * direction. Negative counts are clamped to zero (the allowance still applies).
 */
export function estimateApplySeconds(newCount: number, changedCount: number, coverRefreshCount = 0): number {
  const created = Math.max(0, newCount);
  const updated = Math.max(0, changedCount);
  const refreshed = Math.max(0, coverRefreshCount);
  return (
    created * NEW_ITEM_SEC +
    updated * UPDATED_ITEM_SEC +
    (created + refreshed) * COVER_DOWNLOAD_SEC +
    FETCH_ALLOWANCE_SEC
  );
}

/**
 * Expected apply duration in seconds for a whole ``sync_plan`` — the seed the
 * skip-preview path shows before any preview delta exists.
 *
 * Prices each unit by its COMPOSITION rather than pricing every planned item as
 * a create: a predicted-skip unit costs nothing, the unit's ROMs already bound
 * to a Steam shortcut (``bound_count``) take the cheap update path, and the
 * shortcuts that genuinely have to be minted (``new_shortcut_count``) take the
 * create path. This is what stops a re-sync — and, for platforms, every Force
 * Full Sync, which clears the completion stamps but unbinds nothing — from being
 * seeded at fresh-import prices.
 *
 * The two terms are read INDEPENDENTLY, never derived from each other by
 * subtracting from the unit's item weight (``collapsed_count ?? rom_count``).
 * That weight falls back to the pre-collapse ``rom_count`` whenever the platform
 * carries no completion stamp — exactly what a Force Full Sync leaves behind —
 * and on a platform with sibling groups (ADR-0021) the pre-collapse count
 * exceeds the real shortcut count, so the subtraction would price each collapsed
 * duplicate as a phantom create plus a cover download (#1517). A unit whose
 * backend omits ``new_shortcut_count`` (collections, older backends) keeps the
 * subtraction, which is the pre-#1517 behaviour.
 *
 * The ``bound_count`` fallback is not rare for collections: a collection's
 * membership is known only from its completion stamp, and a Force Full Sync
 * clears every stamp, so a forced run prices its collections as creates even
 * though their shortcuts survive. Deliberate — the alternative is asserting
 * membership the plan does not have — and it errs long, the safe direction.
 *
 * Cover downloads ride the create term; the plan carries no cover-refresh count,
 * and a refresh-only unit's covers are the cheap warm case.
 */
export function estimatePlanSeconds(units: readonly SyncPlanUnit[]): number {
  let created = 0;
  let updated = 0;
  for (const unit of units) {
    if (unit.predicted_skip) continue;
    const items = Math.max(0, unit.collapsed_count ?? unit.rom_count);
    // Clamp: bound rows are counted pre-collapse, so a sibling-heavy platform
    // could report more bound rows than the collapsed item total.
    const bound = Math.min(items, Math.max(0, unit.bound_count ?? 0));
    updated += bound;
    created += unit.new_shortcut_count !== undefined ? Math.max(0, unit.new_shortcut_count) : items - bound;
  }
  return estimateApplySeconds(created, updated);
}

/**
 * Shared coarse-duration renderer for the sync UI's two readouts. Renders
 * *seconds* as ``"< 1 min"`` under a minute, ``"N min"`` under an hour, and
 * ``"H h M min"`` / ``"H h"`` (on the hour) beyond, appending *suffix* to every
 * branch. *roundMinutes* controls the minute rounding: ``Math.round`` for the
 * neutral estimate, ``Math.ceil`` for the live countdown (which must never promise
 * less time than it expects). No ``~`` prefix — the coarse minute rounding already
 * reads as an estimate, and the tilde read as noise on-device.
 */
export function formatApproxDuration(seconds: number, roundMinutes: (n: number) => number, suffix: string): string {
  if (seconds < 60) return `< 1 min${suffix}`;
  const totalMinutes = roundMinutes(seconds / 60);
  if (totalMinutes < 60) return `${totalMinutes} min${suffix}`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes > 0 ? `${hours} h ${minutes} min${suffix}` : `${hours} h${suffix}`;
}

/**
 * Render *seconds* as a coarse, approximate duration for the UI:
 * ``"< 1 min"`` under a minute, ``"4 min"`` under an hour, ``"1 h 10 min"``
 * (or ``"1 h"`` on the hour) beyond. The neutral estimate rounds to the nearest
 * minute and carries no suffix.
 */
export function formatDuration(seconds: number): string {
  return formatApproxDuration(seconds, Math.round, "");
}
