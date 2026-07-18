/**
 * Save-sync completion toast copy (#250) — the canonical per-direction wording
 * shared by the surfaces that confirm a sync. A run can move saves either way
 * (a pre-launch download OR a row-9 upload; a post-exit upload OR a row-5/10
 * adopt-download), so a single "synced" line hid which direction actually ran.
 *
 * ``saveSyncToastBody`` names the single direction when saves moved only one
 * way, both counts when a run went both ways, and returns ``null`` when nothing
 * transferred so a no-op sync fires no toast. The backend mirrors this verbatim
 * in ``services/session_lifecycle.py`` (``sync_toast_body``) for the post-exit
 * toast it renders itself; keep the two in lockstep.
 */

/**
 * The toast body for a completed save-sync, or ``null`` for a no-op run.
 *
 * @param uploaded - files POSTed to RomM this run (absent → 0).
 * @param downloaded - files pulled from RomM this run, including a 409-backstop
 *   download of an upload that lost the currency race (absent → 0).
 */
export function saveSyncToastBody(uploaded?: number, downloaded?: number): string | null {
  const up = uploaded ?? 0;
  const down = downloaded ?? 0;
  if (up > 0 && down > 0) return `Saves synced with RomM (${up} up, ${down} down)`;
  if (up > 0) return "Saves uploaded to RomM";
  if (down > 0) return "Saves downloaded from RomM";
  return null;
}
