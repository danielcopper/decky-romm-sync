/**
 * Body content for an inactive (collapsed-by-default, lazy-loaded) save slot.
 * Renders the slot's saved files plus the Activate/Delete controls, with
 * inline switch-error and offline-hint feedback. Owned exclusively by SlotPanel.
 */

import { FC, type ReactElement } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import type { SlotSaveFile } from "../../types";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { MUTED_COLOR } from "./helpers";
import { renderServerSaveRow } from "./ServerSaveRow";
import type { CopyToSlotRowProps } from "./CopyToSlotButton";

export interface InactiveSlotBodyProps {
  loadingSlot: boolean;
  slotFiles: SlotSaveFile[] | null;
  switching: boolean;
  switchError: string | null;
  isOffline: boolean;
  /**
   * The slot-less legacy (RomM web-player) bucket — fully read-only (#1276,
   * #1478). Drops the Activate/Delete controls and switch hints, keeping only
   * the file list. Note + muted styling live in SlotPanel.
   */
  isLegacy: boolean;
  handleActivate: () => void;
  handleDelete: () => void;
  deleting: boolean;
  /** Copy-to-slot binding for each server save row (source = this slot). */
  copy: CopyToSlotRowProps;
}

export const InactiveSlotBody: FC<InactiveSlotBodyProps> = ({
  loadingSlot,
  slotFiles,
  switching,
  switchError,
  isOffline,
  isLegacy,
  handleActivate,
  handleDelete,
  deleting,
  copy,
}) => {
  const children: (ReactElement | null)[] = [];

  if (loadingSlot) {
    children.push(
      <div key="loading" style={{ fontSize: "13px", color: MUTED_COLOR }}>
        Loading...
      </div>,
    );
  } else if (slotFiles && slotFiles.length > 0) {
    for (const f of slotFiles) {
      children.push(renderServerSaveRow(f, copy));
    }
  } else if (slotFiles !== null) {
    children.push(
      <div key="no-server-files" style={{ fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic" }}>
        No saves in this slot
      </div>,
    );
  }

  // Legacy web-player bucket: read-only — no Activate/Delete, no switch hints.
  if (!isLegacy) {
    const activateLabel = switching ? "Switching..." : "Activate Slot";
    const deleteLabel = deleting ? "Deleting..." : "Delete Slot";

    children.push(
      <Focusable
        key="activate-row"
        flow-children="right"
        style={{ marginTop: "10px", display: "flex", gap: "8px", alignItems: "center" }}
      >
        <DialogButton
          key="activate-btn"
          style={{ padding: "4px 12px", minWidth: "auto", fontSize: "12px", width: "auto" }}
          noFocusRing={false}
          onFocus={scrollFocusedToCenter}
          disabled={switching || isOffline}
          onClick={handleActivate}
        >
          {activateLabel}
        </DialogButton>
        <DialogButton
          key="delete-btn"
          style={{ padding: "4px 12px", minWidth: "auto", fontSize: "12px", width: "auto", color: "#d94126" }}
          noFocusRing={false}
          onFocus={scrollFocusedToCenter}
          disabled={deleting || switching}
          onClick={handleDelete}
        >
          {deleteLabel}
        </DialogButton>
      </Focusable>,
      isOffline ? (
        <div
          key="offline-hint"
          style={{ fontSize: "11px", color: MUTED_COLOR, fontStyle: "italic" as const, marginTop: "4px" }}
        >
          Offline — slot switching unavailable
        </div>
      ) : null,
      switchError ? (
        <div key="switch-error" style={{ fontSize: "11px", color: "#d94126", marginTop: "4px" }}>
          {switchError}
        </div>
      ) : null,
    );
  }

  return <div className="romm-slot-body">{children.filter(Boolean)}</div>;
};
