/**
 * Renderer for the slot listing the device kept from the last time RomM
 * answered — what the SAVES tab shows while no live slot answer has landed.
 *
 * Read-only by construction: no DialogButton, no expand, no activate. Every
 * count and timestamp in it is as old as the snapshot, so the block leads with
 * a line saying so; the offline banner above stays the only word on
 * reachability (#1755).
 */

import type { ReactElement } from "react";
import type { LastKnownSlots, SaveSlotSummary } from "../../types";
import { formatTimestamp } from "../../utils/formatters";
import { displaySlot } from "./helpers";

/** The line that says what the list below is. It carries no date on purpose:
 *  nothing on the device records when the slot listing was last refreshed, and
 *  the nearest timestamp (the ROM's last sync check) moves on its own — it
 *  would date these numbers with a moment that is usually much newer. */
const STALE_NOTE = "Slots as RomM last reported them — counts and times are from that answer, not from now.";

function renderSlotRow(slot: SaveSlotSummary, isActive: boolean): ReactElement {
  const badges = [
    isActive ? (
      <span key="active" className="romm-slot-badge romm-slot-badge-active">
        active
      </span>
    ) : null,
    <span key="src" className={`romm-slot-badge romm-slot-badge-${slot.source}`}>
      {slot.source}
    </span>,
  ];

  return (
    <div
      key={`last-known-${slot.slot}`}
      data-testid={`last-known-slot-${slot.slot || "legacy"}`}
      className="romm-slot-panel"
    >
      <div className="romm-slot-header-static">
        <div className="romm-slot-header-left">
          <span className="romm-slot-name">{displaySlot(slot.slot)}</span>
          {badges}
        </div>
        <div className="romm-slot-header-right">
          <span className="romm-slot-count">{`${slot.count} save${slot.count === 1 ? "" : "s"}`}</span>
        </div>
      </div>
      {slot.latest_updated_at ? (
        <div className="romm-slot-stale-detail">{`Newest save: ${formatTimestamp(slot.latest_updated_at)}`}</div>
      ) : null}
    </div>
  );
}

/** Render the last-known slots, the one that was active among them marked. */
export function renderLastKnownSlots(lastKnown: LastKnownSlots): ReactElement {
  // The legacy bucket is keyed "" in the persisted map, which is what a null
  // active slot means there — compare in that key space, not against null.
  const activeKey = lastKnown.activeSlot ?? "";
  return (
    <div key="last-known-slots" data-testid="last-known-slots">
      <div className="romm-slot-stale-note">{STALE_NOTE}</div>
      <div className="romm-slot-stale">
        {lastKnown.slots.map((slot) => renderSlotRow(slot, slot.slot === activeKey))}
      </div>
    </div>
  );
}
