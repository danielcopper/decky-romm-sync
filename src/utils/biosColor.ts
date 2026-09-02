/**
 * Shared BIOS status-dot color mapping. The unknown/ok/partial/missing DECISION
 * is a single backend source of truth (`domain/bios_status.py::compute_bios_level`);
 * every surface that renders that four-valued verdict maps it to a color through
 * this one helper so the colors never drift apart.
 *
 * The play row's BIOS badge is not such a surface. It is a warning with one
 * appearance, raised by a local question rather than by the verdict, so it takes
 * {@link BIOS_MISSING_RED} directly.
 *
 * Per-surface phrasing (the verbose label strings) stays in each component —
 * only the color mapping is shared here.
 */

import type { BiosLevel } from "../types";

/** The red a missing BIOS requirement is drawn in. Named because one surface
 *  needs the colour WITHOUT the level: the play row's badge appears only when a
 *  file the active core requires is absent, so it has a single appearance and
 *  asks {@link biosColorForLevel} nothing. Sharing the spelling is what keeps
 *  that badge and the tab's red dot the same red. */
export const BIOS_MISSING_RED = "#d94126";

/** Map a backend BIOS level to the status-dot hex color.
 *  - `ok` → green
 *  - `partial` → amber
 *  - `missing` → red
 *  - `unknown` (no emulator's answer could be established) → neutral grey
 *  - `null` (no level data) → neutral grey */
export function biosColorForLevel(level: BiosLevel | null): string {
  switch (level) {
    case "ok":
      return "#5ba32b";
    case "partial":
      return "#d4a72c";
    case "missing":
      return BIOS_MISSING_RED;
    case "unknown":
      return "#8f98a0";
    default:
      return "#8f98a0";
  }
}
