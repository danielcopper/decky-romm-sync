import { useState, useEffect, FC, ChangeEvent } from "react";
import {
  PanelSection,
  PanelSectionRow,
  TextField,
  ButtonItem,
  Field,
} from "@decky/ui";
import { getSettings, saveSettings, testConnection } from "../api/backend";

interface ConnectionSettingsProps {
  onBack: () => void;
}

export const ConnectionSettings: FC<ConnectionSettingsProps> = ({ onBack }) => {
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    getSettings().then((s) => {
      setUrl(s.romm_url);
      setUsername(s.romm_user);
      setPassword(s.romm_pass_masked);
    });
  }, []);

  const handleSave = async () => {
    setLoading(true);
    setStatus("");
    try {
      const result = await saveSettings(url, username, password);
      setStatus(result.message);
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
          <TextField
            label="RomM URL"
            value={url}
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              setUrl(e.target.value)
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Username"
            value={username}
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              setUsername(e.target.value)
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Password"
            bIsPassword
            value={password}
            onChange={(e: ChangeEvent<HTMLInputElement>) =>
              setPassword(e.target.value)
            }
          />
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
    </>
  );
};
