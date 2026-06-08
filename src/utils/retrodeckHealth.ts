/**
 * User-facing copy and the loud/quiet decision for the RetroDECK
 * path-resolution health banner. The backend's `get_retrodeck_status`
 * returns only the discriminant plus paths; the human-readable banner copy
 * lives here so the component stays presentation-only. Anything that maps a
 * `RetroDeckHealth` status to banner text belongs in this module.
 */

import type { RetroDeckHealth } from "../types";

/** Title + body for a loud RetroDECK health banner. */
export interface RetroDeckBanner {
  title: string;
  message: string;
}

/**
 * Map a RetroDECK health status to banner copy, or `null` when nothing should
 * be shown. `ok` and `absent` stay quiet: `ok` is healthy, and `absent` is the
 * legitimate fresh-install case (RetroDECK's own `~/retrodeck` default). Only
 * `unreadable` and `root_missing` are loud — both mean the resolved roots are
 * likely wrong, so syncs and downloads may target the wrong location.
 */
export function retroDeckBanner(
  status: RetroDeckHealth,
  paths: { config_path: string; resolved_home: string },
): RetroDeckBanner | null {
  switch (status) {
    case "unreadable":
      return {
        title: "RetroDECK configuration unreadable",
        message:
          "Couldn't read RetroDECK's configuration — syncs and downloads may target the wrong location. " +
          "Check that RetroDECK is installed correctly. " +
          `Looked at: ${paths.config_path}`,
      };
    case "root_missing":
      return {
        title: "RetroDECK library not found",
        message:
          "RetroDECK's library folder doesn't exist on disk — if it's on an SD card, make sure the card is inserted. " +
          `Expected at: ${paths.resolved_home}`,
      };
    default:
      // "ok" and "absent" stay quiet.
      return null;
  }
}
