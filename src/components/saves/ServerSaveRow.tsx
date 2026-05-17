/**
 * Renderer for one server-side save entry inside an inactive slot panel —
 * a compact filename + (size · updated-relative) line. No state, no I/O.
 */

import { createElement } from "react";
import type { SlotSaveFile } from "../../types";
import { formatBytes, formatRelativeTime } from "./helpers";

export function renderServerSaveRow(f: SlotSaveFile): ReturnType<typeof createElement> {
  const details: string[] = [];
  if (f.size != null) details.push(formatBytes(f.size));
  if (f.updated_at) details.push(`Updated ${formatRelativeTime(f.updated_at)}`);

  return createElement("div", {
    key: `server-${f.id}`,
    style: { padding: "4px 0", borderBottom: "1px solid rgba(255,255,255,0.04)" },
  },
    createElement("div", {
      style: { fontSize: "12px", color: "#dcdedf", fontWeight: 500 },
    }, f.filename),
    details.length > 0
      ? createElement("div", {
          style: { fontSize: "11px", color: "#8f98a0", marginTop: "2px" },
        }, details.join(" · "))
      : null,
  );
}
