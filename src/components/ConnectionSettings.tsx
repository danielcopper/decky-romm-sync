import { useState, useEffect, FC, ChangeEvent } from "react";
import {
  PanelSection,
  PanelSectionRow,
  TextField,
  ButtonItem,
  Field,
  DropdownItem,
  DialogButton,
  showModal,
  ModalRoot,
  Navigation,
} from "@decky/ui";
import { getSettings, saveSettings, testConnection, saveSteamInputSetting, applySteamInputSetting } from "../api/backend";

const TextInputModal: FC<{
  label: string;
  value: string;
  bIsPassword?: boolean;
  closeModal?: () => void;
  onSubmit: (value: string) => void;
}> = ({ label, value: initial, bIsPassword, closeModal, onSubmit }) => {
  const [value, setValue] = useState(initial);
  useEffect(() => {
    Navigation.CloseSideMenus();
  }, []);

  const submit = () => {
    onSubmit(value);
    closeModal?.();
  };

  return (
    <ModalRoot closeModal={closeModal} onOK={submit} onEscKeypress={closeModal}>
      <div style={{ padding: "16px" }}>
        <div style={{ fontSize: "16px", fontWeight: "bold", marginBottom: "12px", color: "#fff" }}>
          {label}
        </div>
        <TextField
          focusOnMount={true}
          label={label}
          value={value}
          bIsPassword={bIsPassword}
          onChange={(e: ChangeEvent<HTMLInputElement>) => setValue(e.target.value)}
        />
      </div>
    </ModalRoot>
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
  const [steamInputMode, setSteamInputMode] = useState("default");
  const [steamInputStatus, setSteamInputStatus] = useState("");
  const [retroarchWarning, setRetroarchWarning] = useState<{ warning: boolean; current?: string; config_path?: string } | null>(null);

  useEffect(() => {
    getSettings().then((s) => {
      setUrl(s.romm_url);
      setUsername(s.romm_user);
      setPassword(s.romm_pass_masked);
      setSteamInputMode(s.steam_input_mode || "default");
      if (s.retroarch_input_check) {
        setRetroarchWarning(s.retroarch_input_check);
      }
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
              <TextInputModal label="RomM URL" value={url} onSubmit={setUrl} />
            )}>
              Edit
            </DialogButton>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Username" description={username || "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal label="Username" value={username} onSubmit={setUsername} />
            )}>
              Edit
            </DialogButton>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Password" description={password ? "••••" : "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal label="Password" value="" bIsPassword onSubmit={setPassword} />
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
        {retroarchWarning && (
          <PanelSectionRow>
            <Field
              label={retroarchWarning.warning
                ? `RetroArch input_driver: "${retroarchWarning.current}" (not recommended)`
                : `RetroArch input_driver: "${retroarchWarning.current}"`}
              description={retroarchWarning.warning
                ? `Controller navigation in RetroArch menus may not work. Change input_driver to "sdl2" in: ${retroarchWarning.config_path}`
                : "Controller navigation in RetroArch menus should work correctly"}
            />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};
