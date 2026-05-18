/**
 * Reusable text-input confirmation modal for SettingsPage edit flows.
 * Persists in-flight edits to module-level `pendingEdits` so that values
 * entered into the URL / username / password fields survive a QAM remount
 * triggered by closing the modal.
 */

import { useState, FC, ChangeEvent } from "react";
import { ConfirmModal, TextField } from "@decky/ui";

/** Module-level state survives component remounts (modal close can remount QAM) */
export const pendingEdits: { url?: string; username?: string; password?: string } = {};

interface TextInputModalProps {
  label: string;
  value: string;
  field?: "url" | "username" | "password";
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
  return (
    <ConfirmModal
      closeModal={closeModal}
      onOK={() => { if (field) { pendingEdits[field] = value; } onSubmit(value); }}
      strTitle={label}
      bDisableBackgroundDismiss={true}
    >
      <TextField
        focusOnMount={true}
        label={label}
        value={value}
        bIsPassword={bIsPassword}
        onChange={(e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value)}
      />
    </ConfirmModal>
  );
};
