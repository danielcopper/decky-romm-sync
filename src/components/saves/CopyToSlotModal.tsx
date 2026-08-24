/**
 * Picker for the "Copy to slot…" action. Lists the ROM's existing slots as copy
 * targets — minus the source slot (a same-slot copy is just a rollback) and
 * minus the read-only legacy no-slot bucket ("") — plus a "New slot…" text field
 * for creating a fresh named slot. Owns only its text-field state; the chosen
 * target is handed to `onSubmit` (trimmed, never empty) and the copy side effects
 * belong to the parent (`useCopyToSlot`).
 */

import { useState, FC, ChangeEvent, KeyboardEvent } from "react";
import { ModalRoot, DialogButton, TextField } from "@decky/ui";
import type { SaveSlotSummary } from "../../types";
import { displaySlot } from "./helpers";

export const CopyToSlotModal: FC<{
  availableSlots: SaveSlotSummary[];
  sourceSlot: string;
  onSubmit: (target: string) => void;
  closeModal?: () => void;
}> = ({ availableSlots, sourceSlot, onSubmit, closeModal }) => {
  const [newName, setNewName] = useState("");

  // Existing named slots eligible as targets: never the source (same-slot copy
  // == rollback) and never the legacy "" bucket (read-only source only). Dedupe
  // defensively so a repeated slot can't render two identical rows.
  const targetSlots = Array.from(
    new Set(availableSlots.map((s) => s.slot).filter((name) => name !== "" && name !== sourceSlot)),
  ).sort((a, b) => a.localeCompare(b));

  const pick = (target: string) => {
    const trimmed = target.trim();
    if (!trimmed) return;
    closeModal?.();
    onSubmit(trimmed);
  };

  const existingRows =
    targetSlots.length > 0
      ? targetSlots.map((name) => (
          <DialogButton
            key={`target-${name}`}
            style={{ padding: "6px 12px", width: "100%", textAlign: "left" as const }}
            onClick={() => {
              pick(name);
            }}
          >
            {displaySlot(name)}
          </DialogButton>
        ))
      : [
          <div
            key="no-existing"
            style={{ fontSize: "12px", color: "rgba(255,255,255,0.5)", fontStyle: "italic" as const }}
          >
            No other slots yet — create one below.
          </div>,
        ];

  return (
    <ModalRoot {...(closeModal !== undefined ? { closeModal } : {})}>
      <div style={{ padding: "16px", minWidth: "320px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", marginBottom: "4px", color: "#fff" }}>
          Copy save to slot
        </div>
        <div style={{ fontSize: "13px", color: "rgba(255,255,255,0.6)", marginBottom: "16px", lineHeight: "1.4" }}>
          Copies this save into the chosen slot, which becomes the active slot. The original save is kept.
        </div>
        {/* Existing-slot targets. */}
        <div style={{ display: "flex", flexDirection: "column" as const, gap: "8px" }}>{existingRows}</div>
        {/* New-slot creator. */}
        <div style={{ marginTop: "16px" }}>
          <TextField
            focusOnMount={false}
            label="New slot…"
            value={newName}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setNewName(e.target.value)}
            // Enter (the on-screen keyboard's "Eingabe" key) submits the typed name,
            // same as the "Create & copy here" button — pick() no-ops on an empty
            // name, so a stray Enter in a blank field does nothing.
            onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
              if (e.key === "Enter") pick(newName);
            }}
          />
          <DialogButton
            style={{ marginTop: "8px", padding: "6px 12px", width: "100%" }}
            disabled={newName.trim() === ""}
            onClick={() => {
              pick(newName);
            }}
          >
            Create & copy here
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};
