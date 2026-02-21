import { useState, useEffect, FC, ChangeEvent } from "react";
import {
  PanelSection,
  PanelSectionRow,
  TextField,
  ButtonItem,
  Field,
  DropdownItem,
  DialogButton,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import { getSettings, saveSettings, testConnection, saveSgdbApiKey, verifySgdbApiKey, saveSteamInputSetting, applySteamInputSetting } from "../api/backend";

// Module-level state survives component remounts (modal close can remount QAM)
const pendingEdits: { url?: string; username?: string; password?: string } = {};

const SHARED_ACCOUNT_NAMES = ["admin", "romm", "user", "guest", "root"];

function isSharedAccount(username: string): boolean {
  return SHARED_ACCOUNT_NAMES.includes(username.trim().toLowerCase());
}

const TextInputModal: FC<{
  label: string;
  value: string;
  field?: "url" | "username" | "password";
  bIsPassword?: boolean;
  closeModal?: () => void;
  onSubmit: (value: string) => void;
}> = ({ label, value: initial, field, bIsPassword, closeModal, onSubmit }) => {
  const [value, setValue] = useState(initial);
  return (
    <ConfirmModal
      closeModal={closeModal}
      onOK={() => { if (field) pendingEdits[field] = value; onSubmit(value); }}
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

interface ConnectionSettingsProps {
  onBack: () => void;
}

export const ConnectionSettings: FC<ConnectionSettingsProps> = ({ onBack }) => {
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [sgdbApiKey, setSgdbApiKey] = useState("");
  const [sgdbStatus, setSgdbStatus] = useState("");
  const [sgdbVerifying, setSgdbVerifying] = useState(false);
  const [steamInputMode, setSteamInputMode] = useState("default");
  const [steamInputStatus, setSteamInputStatus] = useState("");
  useEffect(() => {
    getSettings().then((s) => {
      // Apply any pending edits that survived a remount, fall back to backend values
      setUrl(pendingEdits.url ?? s.romm_url);
      setUsername(pendingEdits.username ?? s.romm_user);
      setPassword(pendingEdits.password ?? s.romm_pass_masked);
      setSgdbApiKey(s.sgdb_api_key_masked);
      setSteamInputMode(s.steam_input_mode || "default");
    }).catch((e) => {
      console.error("[RomM] Failed to load settings:", e);
      setStatus("Failed to load settings");
    });
  }, []);

  const handleSave = async () => {
    setLoading(true);
    setStatus("");
    try {
      const result = await saveSettings(url, username, password);
      setStatus(result.message);
      // Clear pending edits after successful save
      delete pendingEdits.url;
      delete pendingEdits.username;
      delete pendingEdits.password;
    } catch {
      setStatus("Failed to save settings");
    }
    setLoading(false);
  };

  const handleTest = async () => {
    setLoading(true);
    setStatus("");
    try {
      const result = await testConnection();
      setStatus(result.message);
    } catch {
      setStatus("Connection test failed");
    }
    setLoading(false);
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onBack}>
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Connection">
        <PanelSectionRow>
          <Field label="RomM URL" description={url || "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal label="RomM URL" value={url} field="url" onSubmit={setUrl} />
            )}>
              Edit
            </DialogButton>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Username" description={username || "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal label="Username" value={username} field="username" onSubmit={setUsername} />
            )}>
              Edit
            </DialogButton>
          </Field>
        </PanelSectionRow>
        {isSharedAccount(username) && (
          <PanelSectionRow>
            <Field
              label="Shared account detected"
              description={`"${username}" looks like a shared account. Save sync requires a personal RomM account per device to avoid overwriting other users' saves.`}
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <Field label="Password" description={password ? "••••" : "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal label="Password" value="" field="password" bIsPassword onSubmit={setPassword} />
            )}>
              Edit
            </DialogButton>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleSave} disabled={loading}>
            Save Settings
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleTest} disabled={loading}>
            Test Connection
          </ButtonItem>
        </PanelSectionRow>
        {status && (
          <PanelSectionRow>
            <Field label={status} />
          </PanelSectionRow>
        )}
      </PanelSection>
      <PanelSection title="SteamGridDB">
        <PanelSectionRow>
          <Field label="API Key" description={sgdbApiKey ? "••••" : "Not configured"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal
                label="SteamGridDB API Key"
                value=""
                bIsPassword
                onSubmit={async (value) => {
                  setSgdbStatus("");
                  try {
                    const result = await saveSgdbApiKey(value);
                    setSgdbApiKey(value ? "set" : "");
                    setSgdbStatus(result.message);
                  } catch {
                    setSgdbStatus("Failed to save API key");
                  }
                }}
              />
            )}>
              Edit
            </DialogButton>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              setSgdbVerifying(true);
              setSgdbStatus("");
              try {
                const result = await verifySgdbApiKey("");
                setSgdbStatus(result.success ? "Valid" : result.message);
              } catch {
                setSgdbStatus("Verification failed");
              }
              setSgdbVerifying(false);
            }}
            disabled={sgdbVerifying || !sgdbApiKey}
          >
            {sgdbVerifying ? "Verifying..." : "Verify Key"}
          </ButtonItem>
        </PanelSectionRow>
        {sgdbStatus && (
          <PanelSectionRow>
            <Field label={sgdbStatus} />
          </PanelSectionRow>
        )}
      </PanelSection>
      <PanelSection title="Controller">
        <PanelSectionRow>
          <DropdownItem
            label="Steam Input Mode"
            description="Controls how Steam handles controller input for ROM shortcuts"
            rgOptions={[
              { data: "default", label: "Default (Recommended)" },
              { data: "force_on", label: "Force On" },
              { data: "force_off", label: "Force Off" },
            ]}
            selectedOption={steamInputMode}
            onChange={(option) => {
              setSteamInputMode(option.data);
              saveSteamInputSetting(option.data);
              setSteamInputStatus("");
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              setSteamInputStatus("Applying...");
              try {
                const result = await applySteamInputSetting();
                setSteamInputStatus(result.message);
              } catch {
                setSteamInputStatus("Failed to apply");
              }
            }}
            disabled={loading}
          >
            Apply to All Shortcuts
          </ButtonItem>
        </PanelSectionRow>
        {steamInputStatus && (
          <PanelSectionRow>
            <Field label={steamInputStatus} />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};
