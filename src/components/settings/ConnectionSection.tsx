/**
 * RomM server connection settings — URL, account sign-in/token, SSL toggle,
 * and the "Test Connection" affordance. Pure renderer: the parent owns the
 * field values, the has-token flag, the status string, and the
 * save/sign-in/test logic.
 */

import { FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Field,
  showModal,
  ToggleField,
} from "@decky/ui";
import { TextInputModal } from "./TextInputModal";
import { ConnectModal } from "./ConnectModal";
import { isHttpsUrl } from "../../utils/serverUrl";

// Sign-out only forgets the token on this device; it never revokes it in RomM.
const SIGN_OUT_CONFIRM_DESCRIPTION =
  "This only forgets the RomM token on this device. The token itself stays valid in RomM — " +
  "revoke it there (Settings → API Tokens) if you no longer want it.";

interface ConnectionSectionProps {
  url: string;
  hasToken: boolean;
  allowInsecureSsl: boolean;
  status: string;
  loading: boolean;
  onUrlChange: (value: string) => void;
  onConnect: (username: string, password: string) => void;
  onConnectToken: (token: string) => void;
  onConnectPairing: (code: string) => void;
  onAllowInsecureSslChange: (value: boolean) => void;
  onTestConnection: () => void;
  onSignOut: () => void;
}

export const ConnectionSection: FC<ConnectionSectionProps> = ({
  url,
  hasToken,
  allowInsecureSsl,
  status,
  loading,
  onUrlChange,
  onConnect,
  onConnectToken,
  onConnectPairing,
  onAllowInsecureSslChange,
  onTestConnection,
  onSignOut,
}) => {
  return (
    <PanelSection title="Connection">
      <PanelSectionRow>
        <Field label="RomM URL" description={url || "(not set)"}>
          <DialogButton
            style={{ minWidth: "auto", width: "auto" }}
            onClick={() =>
              showModal(<TextInputModal label="RomM URL" value={url} field="url" onSubmit={onUrlChange} />)
            }
          >
            Edit
          </DialogButton>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="RomM Account" description={hasToken ? "Signed in" : "Not signed in"}>
          <DialogButton
            style={{ minWidth: "auto", width: "auto" }}
            onClick={() =>
              showModal(
                <ConnectModal
                  onConnect={onConnect}
                  onConnectToken={onConnectToken}
                  onConnectPairing={onConnectPairing}
                />,
              )
            }
          >
            {hasToken ? "Sign in again" : "Sign in"}
          </DialogButton>
        </Field>
      </PanelSectionRow>
      {hasToken && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description="Forgets the RomM token on this device. The token stays valid in RomM."
            onClick={() =>
              showModal(
                <ConfirmModal
                  strTitle="Sign out of RomM?"
                  strDescription={SIGN_OUT_CONFIRM_DESCRIPTION}
                  strOKButtonText="Sign out"
                  strCancelButtonText="Cancel"
                  onOK={onSignOut}
                />,
              )
            }
          >
            Sign out
          </ButtonItem>
        </PanelSectionRow>
      )}
      {isHttpsUrl(url) && (
        <PanelSectionRow>
          <ToggleField
            label="Allow Insecure SSL"
            description="Skip certificate verification for self-signed certs (LAN only)"
            checked={allowInsecureSsl}
            onChange={onAllowInsecureSslChange}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={onTestConnection}
          disabled={loading || !hasToken}
          description={hasToken ? undefined : "Sign in to RomM first to test the connection."}
        >
          Test Connection
        </ButtonItem>
      </PanelSectionRow>
      {status && (
        <PanelSectionRow>
          <Field label={status} />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};
