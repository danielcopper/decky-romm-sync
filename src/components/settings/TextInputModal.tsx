/**
 * Reusable text-input confirmation modal for SettingsPage edit flows.
 * Persists in-flight edits to module-level `pendingEdits` so that the value
 * entered into the URL field survives a QAM remount triggered by closing
 * the modal.
 */

import { useState, FC, ChangeEvent, KeyboardEvent } from "react";
import { ConfirmModal, TextField } from "@decky/ui";

/** Module-level state survives component remounts (modal close can remount QAM) */
export const pendingEdits: { url?: string } = {};

interface TextInputModalProps {
  label: string;
  value: string;
  field?: "url";
  bIsPassword?: boolean;
  closeModal?: () => void;
  onSubmit: (value: string) => void;
}

export const TextInputModal: FC<TextInputModalProps> = ({
  label,
  value: initial,
  field,
  bIsPassword,
  closeModal,
  onSubmit,
}) => {
  const [value, setValue] = useState(initial);
  const handleOK = () => {
    if (field) {
      pendingEdits[field] = value;
    }
    onSubmit(value);
  };
  return (
    <ConfirmModal
      {...(closeModal !== undefined ? { closeModal } : {})}
      onOK={handleOK}
      strTitle={label}
      bDisableBackgroundDismiss={true}
    >
      <TextField
        focusOnMount={true}
        label={label}
        value={value}
        {...(bIsPassword !== undefined ? { bIsPassword } : {})}
        onChange={(e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value)}
        // Enter (the on-screen keyboard's "Eingabe" key) confirms, same as the OK
        // button. ConfirmModal auto-closes on its OK button but not on this manual
        // path, so close here too — handleOK is shared so the two never drift.
        onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
          if (e.key === "Enter") {
            handleOK();
            closeModal?.();
          }
        }}
      />
    </ConfirmModal>
  );
};
