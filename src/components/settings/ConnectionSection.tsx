/**
 * RomM server connection settings — URL, account sign-in/token, SSL toggle,
 * and the "Test Connection" affordance. Pure renderer: the parent owns the
 * field values, the has-token flag, the status string, and the
 * save/sign-in/test logic.
 */

import { FC } from "react";
import { PanelSection, PanelSectionRow, ButtonItem, DialogButton, Field, showModal, ToggleField } from "@decky/ui";
import { TextInputModal } from "./TextInputModal";
import { ConnectModal } from "./ConnectModal";

interface ConnectionSectionProps {
  url: string;
  hasToken: boolean;
  allowInsecureSsl: boolean;
  status: string;
  loading: boolean;
  onUrlChange: (value: string) => void;
  onConnect: (username: string, password: string) => void;
  onAllowInsecureSslChange: (value: boolean) => void;
  onTestConnection: () => void;
}

export const ConnectionSection: FC<ConnectionSectionProps> = ({
  url,
  hasToken,
  allowInsecureSsl,
  status,
  loading,
  onUrlChange,
  onConnect,
  onAllowInsecureSslChange,
  onTestConnection,
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
            onClick={() => showModal(<ConnectModal onConnect={onConnect} />)}
          >
            Sign in
          </DialogButton>
        </Field>
      </PanelSectionRow>
      {url.toLowerCase().startsWith("https") && (
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
