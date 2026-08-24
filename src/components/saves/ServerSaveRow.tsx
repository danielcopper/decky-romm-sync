/**
 * Renderer for one server-side save entry inside an inactive slot panel —
 * a compact filename + (#id · size · updated-relative) line. No state, no I/O.
 */

import type { ReactElement } from "react";
import type { SlotSaveFile } from "../../types";
import { formatBytes } from "../../utils/formatters";
import { formatRelativeTime } from "./helpers";
import { renderCopyToSlotButton, type CopyToSlotRowProps } from "./CopyToSlotButton";

export function renderServerSaveRow(f: SlotSaveFile, copy?: CopyToSlotRowProps): ReactElement {
  // Lead with the server save id (#<id> · …), matching the version-history
  // header style, so a specific save can be identified across slots.
  const details: string[] = [`#${f.id}`];
  if (f.size != null) details.push(formatBytes(f.size));
  if (f.updated_at) details.push(`Updated ${formatRelativeTime(f.updated_at)}`);

  return (
    <div
      key={`server-${f.id}`}
      style={{
        padding: "4px 0",
        borderBottom: "1px solid rgba(255,255,255,0.04)",
        display: "flex",
        alignItems: "flex-start",
        gap: "8px",
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "12px", color: "#dcdedf", fontWeight: 500 }}>{f.filename}</div>
        {details.length > 0 ? (
          <div style={{ fontSize: "11px", color: "#8f98a0", marginTop: "2px" }}>{details.join(" · ")}</div>
        ) : null}
      </div>
      {copy ? renderCopyToSlotButton(`copy-${f.id}`, f.id, copy) : null}
    </div>
  );
}
