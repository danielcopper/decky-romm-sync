/**
 * The shared shell for the plugin's inline-validating prompt modals (ConnectModal,
 * SgdbApiKeyModal): a bare {@link ModalRoot} carrying a title, the caller's field
 * content, an inline error line above the footer, and a footer with a primary
 * submit button plus a Cancel button. Owns only the shell chrome and its style
 * constants — the field content, gating, and submit/validation logic stay with
 * each modal, passed in as `children` and the labelled props. A modal that shows
 * fields, gates a primary action, and surfaces a returned error inline belongs
 * here rather than re-copying the scaffolding.
 */

import { FC, ReactNode } from "react";
import { ModalRoot, DialogButton } from "@decky/ui";

// ModalRoot renders only the shell + a close affordance (unlike ConfirmModal, it
// has no strTitle / OK button), so the title, body, error line, and footer are
// all rendered as content here.
const modalBodyStyle = { padding: "16px", minWidth: "360px" } as const;
const titleStyle = { fontSize: "16px", fontWeight: "bold", marginBottom: "12px", color: "#fff" } as const;
// A failed submit surfaces here, above the footer, in a red-ish tone.
const errorStyle = { color: "#d94126", fontSize: "13px", marginTop: "12px", marginBottom: "4px" } as const;
const footerStyle = { display: "flex", gap: "8px", marginTop: "16px" } as const;
const footerButtonStyle = { flex: "1 1 0px", minWidth: 0 } as const;

interface ValidatingModalShellProps {
  closeModal?: () => void;
  /** Heading shown at the top of the modal body. */
  title: string;
  /** The returned error message to surface above the footer, or null to hide it. */
  error: string | null;
  /** `data-testid` for the inline error line (per-modal, e.g. "signin-error"). */
  errorTestId: string;
  /** Label for the primary button (typically flips to an in-flight variant). */
  submitLabel: string;
  /** Whether the primary button is disabled (gate incomplete or in flight). */
  submitDisabled: boolean;
  /** Invoked when the primary button is clicked. */
  onSubmit: () => void;
  /** The modal's field content, rendered between the title and the error line. */
  children: ReactNode;
}

export const ValidatingModalShell: FC<ValidatingModalShellProps> = ({
  closeModal,
  title,
  error,
  errorTestId,
  submitLabel,
  submitDisabled,
  onSubmit,
  children,
}) => (
  <ModalRoot {...(closeModal === undefined ? {} : { closeModal })}>
    <div style={modalBodyStyle}>
      <div style={titleStyle}>{title}</div>
      {children}
      {error !== null && (
        <div style={errorStyle} data-testid={errorTestId}>
          {error}
        </div>
      )}
      <div style={footerStyle}>
        <DialogButton style={footerButtonStyle} disabled={submitDisabled} onClick={onSubmit}>
          {submitLabel}
        </DialogButton>
        <DialogButton
          style={footerButtonStyle}
          onClick={() => {
            closeModal?.();
          }}
        >
          Cancel
        </DialogButton>
      </div>
    </div>
  </ModalRoot>
);
