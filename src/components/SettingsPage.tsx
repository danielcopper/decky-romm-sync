import { useState, useEffect, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import { toaster } from "@decky/api";
import {
  getSettings,
  saveSettings,
  testConnection,
  saveSgdbApiKey,
  verifySgdbApiKey,
  saveSteamInputSetting,
  applySteamInputSetting,
  getSaveSyncSettings,
  updateSaveSyncSettings,
  syncAllSaves,
  saveLogLevel,
  fixRetroarchInputDriver,
  ensureDeviceRegistered,
  listDevices,
  getSaveSortMigrationStatus,
  migrateSaveSortFiles,
  dismissSaveSortMigration,
  logError,
} from "../api/backend";
import type { SaveSortMigrationStatus, RegisteredDevice } from "../api/backend";
import { getSaveSortMigrationState, setSaveSortMigrationStatus as setStoreSaveSortStatus, clearSaveSortMigration, onSaveSortMigrationChange } from "../utils/saveSortMigrationStore";
import { scrollToTop } from "../utils/scrollHelpers";
import type { SaveSyncSettings as SaveSyncSettingsType, RetroArchInputCheck } from "../types";
import { pendingEdits } from "./settings/TextInputModal";
import { SaveSortMigrationSection } from "./settings/SaveSortMigrationSection";
import { ConnectionSection } from "./settings/ConnectionSection";
import { SteamGridDBSection } from "./settings/SteamGridDBSection";
import { SaveSyncSection } from "./settings/SaveSyncSection";
import { RegisteredDevicesSection } from "./settings/RegisteredDevicesSection";
import { ControllerSection } from "./settings/ControllerSection";
import { AdvancedSection } from "./settings/AdvancedSection";

interface SettingsPageProps {
  onBack: () => void;
}

export const SettingsPage: FC<SettingsPageProps> = ({ onBack }) => {
  // Connection state
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [allowInsecureSsl, setAllowInsecureSsl] = useState(false);

  // SteamGridDB state
  const [sgdbApiKey, setSgdbApiKey] = useState("");
  const [sgdbStatus, setSgdbStatus] = useState("");
  const [sgdbVerifying, setSgdbVerifying] = useState(false);

  // Save Sync state
  const [saveSyncSettings, setSaveSyncSettings] = useState<SaveSyncSettingsType | null>(null);
  const [saveSyncToggleKey, setSaveSyncToggleKey] = useState(0);
  const [deviceInfo, setDeviceInfo] = useState<{ device_id: string; device_name: string } | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncStatus, setSyncStatus] = useState("");

  // Registered devices state
  const [registeredDevices, setRegisteredDevices] = useState<RegisteredDevice[] | null>(null);
  const [devicesLoading, setDevicesLoading] = useState(false);
  const [devicesError, setDevicesError] = useState<string | null>(null);

  // Controller state
  const [steamInputMode, setSteamInputMode] = useState("default");
  const [steamInputStatus, setSteamInputStatus] = useState("");
  const [retroarchWarning, setRetroarchWarning] = useState<RetroArchInputCheck | null>(null);
  const [retroarchFixStatus, setRetroarchFixStatus] = useState("");

  // Save sort migration state
  const [saveSortMigration, setSaveSortMigration] = useState<SaveSortMigrationStatus>(getSaveSortMigrationState());
  const [saveSortMigrating, setSaveSortMigrating] = useState(false);
  const [saveSortResult, setSaveSortResult] = useState("");

  // Advanced state
  const [logLevel, setLogLevel] = useState("warn");

  useEffect(() => {
    getSettings().then((s) => {
      // Apply any pending edits that survived a remount, fall back to backend values
      setUrl(pendingEdits.url ?? s.romm_url);
      setUsername(pendingEdits.username ?? s.romm_user);
      setPassword(pendingEdits.password ?? s.romm_pass_masked);
      setAllowInsecureSsl(s.romm_allow_insecure_ssl ?? false);
      setSgdbApiKey(s.sgdb_api_key_masked);
      setSteamInputMode(s.steam_input_mode || "default");
      setLogLevel(s.log_level ?? "warn");
      if (s.retroarch_input_check) {
        setRetroarchWarning(s.retroarch_input_check);
      }
    }).catch((e) => {
      logError(`Failed to load settings: ${e}`);
      setStatus("Failed to load settings");
    });

    // Load save sync settings and conflicts
    getSaveSyncSettings()
      .then((settings) => {
        setSaveSyncSettings(settings);
        if (settings.save_sync_enabled) {
          ensureDeviceRegistered()
            .then((result) => {
              if (result.success) {
                setDeviceInfo({ device_id: result.device_id, device_name: result.device_name });
              }
            })
            .catch(() => {});
          loadDevices();
        }
      })
      .catch((e) => logError(`Failed to load save sync settings: ${e}`));

    getSaveSortMigrationStatus().then((s) => {
      if (s.pending) {
        setStoreSaveSortStatus(s);
        setSaveSortMigration(s);
      }
    }).catch(() => {});

    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortMigration(getSaveSortMigrationState()));
    return () => { unsubSaveSort(); };
  }, []);

  function loadDevices() {
    setDevicesLoading(true);
    setDevicesError(null);
    listDevices()
      .then((result) => {
        if (result.success) {
          setRegisteredDevices(result.devices);
        } else if (result.disabled) {
          setRegisteredDevices(null);
        } else {
          setDevicesError(result.error ?? "Failed to load devices");
          setRegisteredDevices([]);
        }
      })
      .catch((e: unknown) => {
        setDevicesError(e instanceof Error ? e.message : "Failed to load devices");
        setRegisteredDevices([]);
      })
      .finally(() => {
        setDevicesLoading(false);
      });
  }

  // Auto-save connection fields when a modal edit is confirmed
  const autoSaveSettings = async (field: "url" | "username" | "password", newValue: string) => {
    const currentUrl = field === "url" ? newValue : url;
    const currentUser = field === "username" ? newValue : username;
    const currentPass = field === "password" ? newValue : password;
    try {
      await saveSettings(currentUrl, currentUser, currentPass, allowInsecureSsl);
      delete pendingEdits[field];
    } catch {
      setStatus("Failed to save settings");
    }
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

  const handleSaveSyncSettingChange = async (partial: Partial<SaveSyncSettingsType>) => {
    if (!saveSyncSettings) return;
    const updated = { ...saveSyncSettings, ...partial };
    setSaveSyncSettings(updated);
    try {
      await updateSaveSyncSettings(updated);
      if ("save_sync_enabled" in partial) {
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", {
          detail: { type: "save_sync_settings", save_sync_enabled: updated.save_sync_enabled },
        }));
        if (updated.save_sync_enabled) {
          loadDevices();
        } else {
          setRegisteredDevices(null);
          setDevicesError(null);
        }
      }
    } catch (e) {
      logError(`Failed to save settings: ${e}`);
    }
  };

  const handleSyncAll = async () => {
    setSyncing(true);
    setSyncStatus("");
    try {
      const result = await syncAllSaves();
      setSyncStatus(result.message);
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync" } }));


    } catch {
      setSyncStatus("Sync failed");
    }
    setSyncing(false);
  };

  const handleEnableSaveSync = () => {
    showModal(
      <ConfirmModal
        strTitle="Enable Save Sync?"
        strDescription={
          "This will sync RetroArch save files (.srm) between this device and your RomM server.\n\n" +
          "Before enabling, please back up your local save files. " +
          "They are stored in your RetroArch/RetroDECK saves directory.\n\n" +
          "IMPORTANT: Save sync requires RetroArch's save sorting to be set to " +
          "\"Sort Saves into Folders by Content Directory = ON\" and " +
          "\"Sort Saves into Folders by Core Name = OFF\" (RetroDECK default). " +
          "If you changed these settings, save sync will not find your save files.\n\n" +
          "Also make sure you are not using this on a shared RomM account " +
          "(e.g. admin, romm, guest) - unless you know what you are doing. " +
          "Save sync is intended for single user accounts.\n\n" +
          "Are you sure you want to proceed?"
        }
        strOKButtonText="I am sure"
        strCancelButtonText="Cancel"
        onOK={() => handleSaveSyncSettingChange({ save_sync_enabled: true })}
        onCancel={() => {
          setSaveSyncToggleKey((k) => k + 1);
        }}
      />,
    );
  };

  const handleDisableSaveSync = () => {
    handleSaveSyncSettingChange({ save_sync_enabled: false });
  };

  const handleToggleSaveSync = (value: boolean) => {
    if (value) { handleEnableSaveSync(); } else { handleDisableSaveSync(); } // NOSONAR — enable shows confirmation modal
  };

  const saveSyncEnabled = saveSyncSettings?.save_sync_enabled ?? false;

  /** Show confirmation modal and clear the default slot on OK. */
  function confirmClearDefaultSlot(): void {
    showModal(
      <ConfirmModal
        strTitle="Clear Default Slot?"
        strDescription="Clearing the default slot enables legacy mode. New games will not use a slot, which limits saves to one version per game. Are you sure?"
        strOKButtonText="Clear Slot"
        strCancelButtonText="Cancel"
        onOK={() => {
          setSaveSyncSettings((prev) => prev ? { ...prev, default_slot: null } : prev);
          handleSaveSyncSettingChange({ default_slot: null });
        }}
      />,
    );
  }

  // --- Connection handlers wired into ConnectionSection ---
  const handleUrlSubmit = (value: string) => {
    setUrl(value);
    autoSaveSettings("url", value);
  };
  const handleUsernameSubmit = (value: string) => {
    setUsername(value);
    autoSaveSettings("username", value);
  };
  const handlePasswordSubmit = (value: string) => {
    setPassword(value);
    autoSaveSettings("password", value);
  };
  const handleAllowInsecureSslChange = (val: boolean) => {
    setAllowInsecureSsl(val);
    // Auto-save with the new SSL setting
    saveSettings(url, username, password, val).catch(() => {
      setStatus("Failed to save settings");
    });
  };

  // --- SteamGridDB handlers ---
  const handleSgdbKeySubmit = async (value: string) => {
    setSgdbStatus("");
    try {
      const result = await saveSgdbApiKey(value);
      setSgdbApiKey(value ? "set" : "");
      setSgdbStatus(result.message);
    } catch {
      setSgdbStatus("Failed to save API key");
    }
  };
  const handleSgdbVerify = async () => {
    setSgdbVerifying(true);
    setSgdbStatus("");
    try {
      const result = await verifySgdbApiKey("");
      setSgdbStatus(result.success ? "Valid" : result.message);
    } catch {
      setSgdbStatus("Verification failed");
    }
    setSgdbVerifying(false);
  };

  // --- Save-sync default-slot handlers ---
  const handleDefaultSlotSubmit = (value: string) => {
    const trimmed = value.trim();
    if (trimmed) {
      setSaveSyncSettings((prev) => prev ? { ...prev, default_slot: trimmed } : prev);
      handleSaveSyncSettingChange({ default_slot: trimmed });
    } else {
      confirmClearDefaultSlot();
    }
  };
  const handleResetDefaultSlot = () => {
    setSaveSyncSettings((prev) => prev ? { ...prev, default_slot: "default" } : prev);
    handleSaveSyncSettingChange({ default_slot: "default" });
  };

  // --- Controller handlers ---
  const handleSteamInputModeChange = (mode: string) => {
    setSteamInputMode(mode);
    saveSteamInputSetting(mode);
    setSteamInputStatus("");
  };
  const handleApplySteamInput = async () => {
    setSteamInputStatus("Applying...");
    try {
      const result = await applySteamInputSetting();
      setSteamInputStatus(result.message);
    } catch {
      setSteamInputStatus("Failed to apply");
    }
  };
  const handleFixInputDriver = async () => {
    setRetroarchFixStatus("Applying...");
    try {
      const result = await fixRetroarchInputDriver();
      setRetroarchFixStatus(result.message);
      if (result.success) {
        setRetroarchWarning(null);
      }
    } catch {
      setRetroarchFixStatus("Failed to apply fix");
    }
  };

  // --- Advanced handlers ---
  const handleLogLevelChange = (level: string) => {
    setLogLevel(level);
    saveLogLevel(level);
  };

  // --- Save sort migration handlers ---
  const handleMigrateSaveSort = async () => {
    setSaveSortMigrating(true);
    setSaveSortResult("");
    try {
      const result = await migrateSaveSortFiles(null);
      setSaveSortResult(result.message);
      if (result.success) {
        clearSaveSortMigration();
        toaster.toast({
          title: "RomM Sync",
          body: result.message || "Migration complete.",
        });
      }
    } catch {
      setSaveSortResult("Migration failed");
    }
    setSaveSortMigrating(false);
  };
  const handleDismissSaveSort = async () => {
    try {
      await dismissSaveSortMigration();
      clearSaveSortMigration();
    } catch { /* ignore */ }
  };

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={onBack}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
          >
            Back
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      {saveSortMigration.pending && (
        <SaveSortMigrationSection
          migration={saveSortMigration}
          migrating={saveSortMigrating}
          result={saveSortResult}
          onMigrate={handleMigrateSaveSort}
          onDismiss={handleDismissSaveSort}
        />
      )}
      <ConnectionSection
        url={url}
        username={username}
        password={password}
        allowInsecureSsl={allowInsecureSsl}
        status={status}
        loading={loading}
        onUrlSubmit={handleUrlSubmit}
        onUsernameSubmit={handleUsernameSubmit}
        onPasswordSubmit={handlePasswordSubmit}
        onAllowInsecureSslChange={handleAllowInsecureSslChange}
        onTestConnection={handleTest}
      />
      <SteamGridDBSection
        sgdbApiKey={sgdbApiKey}
        sgdbStatus={sgdbStatus}
        sgdbVerifying={sgdbVerifying}
        onSubmitKey={handleSgdbKeySubmit}
        onVerifyKey={handleSgdbVerify}
      />
      <SaveSyncSection
        saveSyncSettings={saveSyncSettings}
        saveSyncToggleKey={saveSyncToggleKey}
        deviceInfo={deviceInfo}
        syncing={syncing}
        syncStatus={syncStatus}
        onToggleSaveSync={handleToggleSaveSync}
        onSettingChange={handleSaveSyncSettingChange}
        onDefaultSlotSubmit={handleDefaultSlotSubmit}
        onResetDefaultSlot={handleResetDefaultSlot}
        onSyncAll={handleSyncAll}
      />
      {saveSyncEnabled && (devicesLoading || registeredDevices !== null) && (
        <RegisteredDevicesSection
          devicesLoading={devicesLoading}
          devicesError={devicesError}
          registeredDevices={registeredDevices}
        />
      )}
      <ControllerSection
        steamInputMode={steamInputMode}
        steamInputStatus={steamInputStatus}
        retroarchWarning={retroarchWarning}
        retroarchFixStatus={retroarchFixStatus}
        loading={loading}
        onModeChange={handleSteamInputModeChange}
        onApplyMode={handleApplySteamInput}
        onFixInputDriver={handleFixInputDriver}
      />
      <AdvancedSection
        logLevel={logLevel}
        onLogLevelChange={handleLogLevelChange}
      />
    </>
  );
};
