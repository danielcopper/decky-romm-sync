/**
 * Shared BIOS status-dot color mapping. The unknown/ok/partial/missing DECISION
 * is a single backend source of truth (`domain/bios_status.py::compute_bios_level`);
 * every surface that renders a BIOS status dot maps that level to a color
 * through this one helper so the colors never drift apart.
 *
 * Per-surface phrasing (the verbose label strings) stays in each component —
 * only the color mapping is shared here.
 */

import type { BiosLevel } from "../types";

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
      return "#d94126";
    case "unknown":
      return "#8f98a0";
    default:
      return "#8f98a0";
  }
}
