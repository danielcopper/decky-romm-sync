/**
 * Body content for an inactive (collapsed-by-default, lazy-loaded) save slot.
 * Renders the slot's saved files plus the Activate/Delete controls, with
 * inline switch-error and offline-hint feedback. Owned exclusively by SlotPanel.
 */

import { createElement, FC } from "react";
import { DialogButton, Focusable } from "@decky/ui";
import type { SlotSaveFile } from "../../types";
import { scrollFocusedToCenter } from "../../utils/scrollHelpers";
import { MUTED_COLOR } from "./helpers";
import { renderServerSaveRow } from "./ServerSaveRow";

export interface InactiveSlotBodyProps {
  loadingSlot: boolean;
  slotFiles: SlotSaveFile[] | null;
  switching: boolean;
  switchError: string | null;
  isOffline: boolean;
  /**
   * Whether this slot may be activated. The slot-less legacy bucket is
   * view-only (#1276): switching into it is retired, so its panel drops the
   * Activate button and the switch-only hints while keeping the file list and
   * Delete control.
   */
  canActivate: boolean;
  handleActivate: () => void;
  handleDelete: () => void;
  deleting: boolean;
}

export const InactiveSlotBody: FC<InactiveSlotBodyProps> = ({
  loadingSlot,
  slotFiles,
  switching,
  switchError,
  isOffline,
  canActivate,
  handleActivate,
  handleDelete,
  deleting,
}) => {
  const children: (ReturnType<typeof createElement> | null)[] = [];

  if (loadingSlot) {
    children.push(
      createElement("div", { key: "loading", style: { fontSize: "13px", color: MUTED_COLOR } }, "Loading..."),
    );
  } else if (slotFiles && slotFiles.length > 0) {
    for (const f of slotFiles) {
      children.push(renderServerSaveRow(f));
    }
  } else if (slotFiles !== null) {
    children.push(
      createElement(
        "div",
        { key: "no-server-files", style: { fontSize: "13px", color: MUTED_COLOR, fontStyle: "italic" } },
        "No saves in this slot",
      ),
    );
  }

  const activateLabel = switching ? "Switching..." : "Activate Slot";
  const deleteLabel = deleting ? "Deleting..." : "Delete Slot";

  const activateButton = canActivate
    ? createElement(
        DialogButton,
        {
          key: "activate-btn",
          style: { padding: "4px 12px", minWidth: "auto", fontSize: "12px", width: "auto" },
          noFocusRing: false,
          onFocus: scrollFocusedToCenter,
          disabled: switching || isOffline,
          onClick: handleActivate,
        },
        activateLabel,
      )
    : null;

  children.push(
    createElement(
      Focusable as never,
      {
        key: "activate-row",
        "flow-children": "right",
        style: { marginTop: "10px", display: "flex", gap: "8px", alignItems: "center" },
      },
      activateButton,
      createElement(
        DialogButton,
        {
          key: "delete-btn",
          style: { padding: "4px 12px", minWidth: "auto", fontSize: "12px", width: "auto", color: "#d94126" },
          noFocusRing: false,
          onFocus: scrollFocusedToCenter,
          disabled: deleting || switching,
          onClick: handleDelete,
        },
        deleteLabel,
      ),
    ),
    // Offline hint + switch error relate to the activate flow, so a view-only
    // legacy panel (canActivate === false) drops them.
    canActivate && isOffline
      ? createElement(
          "div",
          {
            key: "offline-hint",
            style: { fontSize: "11px", color: MUTED_COLOR, fontStyle: "italic" as const, marginTop: "4px" },
          },
          "Offline — slot switching unavailable",
        )
      : null,
    canActivate && switchError
      ? createElement(
          "div",
          {
            key: "switch-error",
            style: { fontSize: "11px", color: "#d94126", marginTop: "4px" },
          },
          switchError,
        )
      : null,
  );

  return createElement("div", { className: "romm-slot-body" }, ...children.filter(Boolean));
};
