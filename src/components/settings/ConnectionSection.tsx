/**
 * RomM server connection settings — URL, username, password, SSL toggle, and
 * the "Test Connection" affordance. Pure renderer: the parent owns the field
 * values plus the test-status string and auto-save logic.
 */

import { FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  DialogButton,
  showModal,
  ToggleField,
} from "@decky/ui";
import { TextInputModal } from "./TextInputModal";
import { isSharedAccount } from "./helpers";

interface ConnectionSectionProps {
  url: string;
  username: string;
  password: string;
  allowInsecureSsl: boolean;
  status: string;
  loading: boolean;
  onUrlSubmit: (value: string) => void;
  onUsernameSubmit: (value: string) => void;
  onPasswordSubmit: (value: string) => void;
  onAllowInsecureSslChange: (value: boolean) => void;
  onTestConnection: () => void;
}

export const ConnectionSection: FC<ConnectionSectionProps> = ({
  url,
  username,
  password,
  allowInsecureSsl,
  status,
  loading,
  onUrlSubmit,
  onUsernameSubmit,
  onPasswordSubmit,
  onAllowInsecureSslChange,
  onTestConnection,
}) => {
  return (
    <PanelSection title="Connection">
      <PanelSectionRow>
        <Field label="RomM URL" description={url || "(not set)"}>
          <DialogButton onClick={() => showModal(
            <TextInputModal
              label="RomM URL"
              value={url}
              field="url"
              onSubmit={onUrlSubmit}
            />
          )}>
            Edit
          </DialogButton>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field label="Username" description={username || "(not set)"}>
          <DialogButton onClick={() => showModal(
            <TextInputModal
              label="Username"
              value={username}
              field="username"
              onSubmit={onUsernameSubmit}
            />
          )}>
            Edit
          </DialogButton>
        </Field>
      </PanelSectionRow>
      {isSharedAccount(username) && (
        <PanelSectionRow>
          <Field
            label={<span style={{ color: "#ff8800" }}>Shared account detected</span>}
            description={`"${username}" looks like a shared account. Save sync requires a personal RomM account per device to avoid overwriting other users' saves.`}
          />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <Field label="Password" description={password ? "••••" : "(not set)"}>
          <DialogButton onClick={() => showModal(
            <TextInputModal
              label="Password"
              value=""
              field="password"
              bIsPassword
              onSubmit={onPasswordSubmit}
            />
          )}>
            Edit
          </DialogButton>
        </Field>
      </PanelSectionRow>
      {(url.toLowerCase().startsWith("https")) && (
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
        <ButtonItem layout="below" onClick={onTestConnection} disabled={loading}>
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
