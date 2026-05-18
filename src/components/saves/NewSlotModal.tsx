/**
 * Modal that asks the user for a new save slot name. Owns its own text-field
 * state and submits the trimmed value via the `onSubmit` callback; all
 * slot-creation side effects belong in the parent.
 */

import { useState, createElement, FC, ChangeEvent } from "react";
import { ConfirmModal, TextField } from "@decky/ui";

export const NewSlotModal: FC<{
  closeModal?: () => void;
  onSubmit: (name: string) => void;
}> = ({ closeModal, onSubmit }) => {
  const [value, setValue] = useState("");
  return createElement(ConfirmModal, {
    closeModal,
    onOK: () => { onSubmit(value.trim()); },
    strTitle: "New Save Slot",
    bDisableBackgroundDismiss: true,
  },
    createElement(TextField, {
      focusOnMount: true,
      label: "Slot Name",
      value,
      onChange: (e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value),
    }),
  );
};
