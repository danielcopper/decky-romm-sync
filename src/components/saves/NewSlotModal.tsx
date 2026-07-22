/**
 * Modal that asks the user for a new save slot name. Owns its own text-field
 * state and submits the trimmed value via the `onSubmit` callback; all
 * slot-creation side effects belong in the parent.
 */

import { useState, createElement, FC, ChangeEvent, KeyboardEvent } from "react";
import { ConfirmModal, TextField } from "@decky/ui";

export const NewSlotModal: FC<{
  closeModal?: () => void;
  onSubmit: (name: string) => void;
}> = ({ closeModal, onSubmit }) => {
  const [value, setValue] = useState("");
  return createElement(
    ConfirmModal,
    {
      ...(closeModal !== undefined ? { closeModal } : {}),
      onOK: () => {
        onSubmit(value.trim());
      },
      strTitle: "New Save Slot",
      bDisableBackgroundDismiss: true,
      // Disable confirm on an empty/whitespace name (parent's no-op is the backstop).
      bOKDisabled: value.trim() === "",
    },
    createElement(TextField, {
      focusOnMount: true,
      label: "Slot Name",
      value,
      onChange: (e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value),
      // Enter (the on-screen keyboard's "Eingabe" key) confirms, same as the OK
      // button — which is disabled while blank, so guard identically: a blank
      // Enter is a no-op. ConfirmModal doesn't auto-close this manual path.
      onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => {
        if (e.key === "Enter" && value.trim() !== "") {
          onSubmit(value.trim());
          closeModal?.();
        }
      },
    }),
  );
};
