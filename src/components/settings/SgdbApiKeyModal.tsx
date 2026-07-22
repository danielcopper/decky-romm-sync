/**
 * Prompt for entering a SteamGridDB API key, validated inline before it is
 * saved. On submit the entered key is first tested against SteamGridDB
 * (``onVerify``); only a valid key is persisted (``onSave``) and the modal
 * closes. A rejected key keeps the modal open and shows the returned message so
 * the user can correct and retry — the generic TextInputModal saved without
 * validating, which let an invalid key sit silently. The key is write-only:
 * never pre-filled, never echoed back. This modal owns only the in-flight field
 * value and the in-flight/error UI state; ``onVerify`` / ``onSave`` own the
 * backend calls and persistence.
 */

import { FC, useState, ChangeEvent, KeyboardEvent } from "react";
import { TextField, Focusable } from "@decky/ui";
import { ValidatingModalShell } from "./ValidatingModalShell";

/** The subset of the verify result the modal needs to decide whether to save
 *  (success) or surface an error and stay open (failure). */
export interface VerifyKeyResult {
  success: boolean;
  message: string;
}

interface SgdbApiKeyModalProps {
  closeModal?: () => void;
  /** Tests the entered key against SteamGridDB without persisting it. */
  onVerify: (key: string) => Promise<VerifyKeyResult>;
  /** Persists the entered key. Only called after ``onVerify`` succeeds. */
  onSave: (key: string) => Promise<void>;
}

const helperTextStyle = { fontSize: "12px", marginBottom: "12px", color: "rgba(255,255,255,0.6)" } as const;

const GENERIC_VERIFY_ERROR = "Could not verify the key. Check your connection and try again.";
const GENERIC_SAVE_ERROR = "The key is valid, but saving it failed. Check your connection and try again.";

export const SgdbApiKeyModal: FC<SgdbApiKeyModalProps> = ({ closeModal, onVerify, onSave }) => {
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Trimmed so a whitespace-only value never counts as submittable.
  const canSubmit = value.trim() !== "";

  // Verify the entered key; on success persist it and close, on failure keep the
  // modal open with the returned message so the user can correct and retry.
  const submit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      let check: VerifyKeyResult;
      try {
        check = await onVerify(value);
      } catch {
        setError(GENERIC_VERIFY_ERROR);
        return;
      }
      if (!check.success) {
        setError(check.message);
        return;
      }
      // The key is valid; persist it. A save failure is distinct from a verify
      // failure, so it must not claim the key couldn't be verified.
      try {
        await onSave(value);
      } catch {
        setError(GENERIC_SAVE_ERROR);
        return;
      }
      closeModal?.();
    } finally {
      setSubmitting(false);
    }
  };

  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    setValue(e.target.value);
    // Any edit clears a stale error so it doesn't linger past a correction.
    setError(null);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter") return;
    // Consume the event so Steam's ModalRoot cannot fire its own default
    // confirm/close on an incomplete field — regardless of whether we submit.
    e.preventDefault();
    e.stopPropagation();
    if (canSubmit) void submit();
  };

  return (
    <ValidatingModalShell
      {...(closeModal === undefined ? {} : { closeModal })}
      title="SteamGridDB API Key"
      error={error}
      errorTestId="sgdb-key-error"
      submitLabel={submitting ? "Verifying…" : "Save"}
      submitDisabled={!canSubmit || submitting}
      onSubmit={() => {
        void submit();
      }}
    >
      <div style={helperTextStyle}>
        Paste the key from your SteamGridDB profile (Preferences &rarr; API). It is checked against SteamGridDB before
        it is saved.
      </div>
      {/* Wrapped in <Focusable> like ConnectModal's token field: on the Deck,
          R2/OSK-Enter on an unwrapped single field otherwise closes the
          ModalRoot even when the Enter handler no-ops on an incomplete field. */}
      <Focusable>
        <TextField
          focusOnMount={true}
          label="API Key"
          value={value}
          bIsPassword
          onChange={handleChange}
          onKeyDown={handleKeyDown}
        />
      </Focusable>
    </ValidatingModalShell>
  );
};
