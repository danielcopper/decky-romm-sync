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
  ToggleField,
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

// Module-level state survives component remounts (modal close can remount QAM)
const pendingEdits: { url?: string; username?: string; password?: string } = {};

/** Format a relative time string (e.g. "5m ago", "2h ago") from an ISO string */
function formatRelativeTime(isoStr: string | null): string {
  if (!isoStr) return "never";
  const date = new Date(isoStr);
  if (Number.isNaN(date.getTime())) return "unknown";
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffMin < 1440) return `${Math.floor(diffMin / 60)}h ago`;
  const d = date.getDate();
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d} ${months[date.getMonth()]}`;
}

const SHARED_ACCOUNT_NAMES = new Set(["admin", "romm", "user", "guest", "root"]);

function sortLabel(settings: { sort_by_content: boolean; sort_by_core: boolean }): string {
  return `Sort by content: ${settings.sort_by_content ? "ON" : "OFF"}, Sort by core: ${settings.sort_by_core ? "ON" : "OFF"}`;
}

function isSharedAccount(username: string): boolean {
  return SHARED_ACCOUNT_NAMES.has(username.trim().toLowerCase());
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

  const loadDevices = () => {
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
  };

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
        <PanelSection title="Save Sort Migration">
          <PanelSectionRow>
            <div style={{ padding: "8px 12px", backgroundColor: "rgba(212, 167, 44, 0.15)", borderLeft: "3px solid #d4a72c", borderRadius: "4px" }}>
              <div style={{ fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "6px" }}>
                {"\u26A0\uFE0F"} RetroArch save sorting changed
              </div>
              {saveSortMigration.old_settings && (
                <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "4px" }}>
                  From: {sortLabel(saveSortMigration.old_settings)}
                </div>
              )}
              {saveSortMigration.new_settings && (
                <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)", marginBottom: "4px" }}>
                  To: {sortLabel(saveSortMigration.new_settings)}
                </div>
              )}
              <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.9)" }}>
                {saveSortMigration.saves_count ?? 0} save file(s) to migrate
              </div>
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={saveSortMigrating}
              onClick={async () => {
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
              }}
            >
              {saveSortMigrating ? "Migrating..." : "Migrate Save Files"}
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={saveSortMigrating}
              onClick={async () => {
                try {
                  await dismissSaveSortMigration();
                  clearSaveSortMigration();
                } catch { /* ignore */ }
              }}
            >
              Dismiss (I migrated manually)
            </ButtonItem>
          </PanelSectionRow>
          {saveSortResult && (
            <PanelSectionRow>
              <Field label={saveSortResult} />
            </PanelSectionRow>
          )}
        </PanelSection>
      )}
      <PanelSection title="Connection">
        <PanelSectionRow>
          <Field label="RomM URL" description={url || "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal
                label="RomM URL"
                value={url}
                field="url"
                onSubmit={(value) => {
                  setUrl(value);
                  autoSaveSettings("url", value);
                }}
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
                onSubmit={(value) => {
                  setUsername(value);
                  autoSaveSettings("username", value);
                }}
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
          <Field label="Password" description={password ? "\u2022\u2022\u2022\u2022" : "(not set)"}>
            <DialogButton onClick={() => showModal(
              <TextInputModal
                label="Password"
                value=""
                field="password"
                bIsPassword
                onSubmit={(value) => {
                  setPassword(value);
                  autoSaveSettings("password", value);
                }}
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
              onChange={(val) => {
                setAllowInsecureSsl(val);
                // Auto-save with the new SSL setting
                saveSettings(url, username, password, val).catch(() => {
                  setStatus("Failed to save settings");
                });
              }}
            />
          </PanelSectionRow>
        )}
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
          <Field label="API Key" description={sgdbApiKey ? "\u2022\u2022\u2022\u2022" : "Not configured"}>
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
      <PanelSection title="Save Sync">
        {saveSyncSettings ? (
          <>
            <PanelSectionRow>
              <ToggleField
                key={saveSyncToggleKey}
                label="Enable Save Sync"
                description="Sync RetroArch saves between this device and RomM server"
                checked={saveSyncEnabled}
                onChange={handleToggleSaveSync}
              />
            </PanelSectionRow>
            {!saveSyncEnabled && (
              <PanelSectionRow>
                <Field label="Save sync is disabled" description="Enable above to configure sync settings" />
              </PanelSectionRow>
            )}
            {saveSyncEnabled && (
              <>
                {deviceInfo && (
                  <PanelSectionRow>
                    <Field
                      label="Device"
                      description={`Registered as "${deviceInfo.device_name}"`}
                    />
                  </PanelSectionRow>
                )}
                <PanelSectionRow>
                  <ToggleField
                    label="Sync before launch"
                    description="Download newer saves from server before starting a game"
                    checked={saveSyncSettings.sync_before_launch}
                    onChange={(value) => handleSaveSyncSettingChange({ sync_before_launch: value })}
                  />
                </PanelSectionRow>
                <PanelSectionRow>
                  <ToggleField
                    label="Sync after exit"
                    description="Upload changed saves to server after closing a game"
                    checked={saveSyncSettings.sync_after_exit}
                    onChange={(value) => handleSaveSyncSettingChange({ sync_after_exit: value })}
                  />
                </PanelSectionRow>
                <PanelSectionRow>
                  <Field
                    label="Default Save Slot"
                    description={`${saveSyncSettings.default_slot || "(no slot)"} — applies to new games and games without a per-game slot override`}
                  >
                    <DialogButton onClick={() => showModal(
                      <TextInputModal
                        label="Default Save Slot"
                        value={saveSyncSettings.default_slot ?? ""}
                        onSubmit={(value) => {
                          const trimmed = value.trim();
                          if (trimmed) {
                            setSaveSyncSettings((prev) => prev ? { ...prev, default_slot: trimmed } : prev);
                            handleSaveSyncSettingChange({ default_slot: trimmed });
                          } else {
                            confirmClearDefaultSlot();
                          }
                        }}
                      />
                    )}>
                      Edit
                    </DialogButton>
                  </Field>
                </PanelSectionRow>
                {saveSyncSettings.default_slot !== "default" && (
                  <PanelSectionRow>
                    <ButtonItem
                      layout="below"
                      onClick={() => {
                        setSaveSyncSettings((prev) => prev ? { ...prev, default_slot: "default" } : prev);
                        handleSaveSyncSettingChange({ default_slot: "default" });
                      }}
                    >
                      Reset to default
                    </ButtonItem>
                  </PanelSectionRow>
                )}
                {(saveSyncSettings.default_slot === null || saveSyncSettings.default_slot === "") && (
                  <PanelSectionRow>
                    <Field
                      label={<span style={{ color: "#ff8800" }}>Legacy mode (no slot)</span>}
                      description="Saves are limited to one version per game."
                    />
                  </PanelSectionRow>
                )}
                <PanelSectionRow>
                  <DropdownItem
                    label="Save History Limit"
                    description="Max save versions kept per slot on the server"
                    rgOptions={[
                      { data: 5, label: "5" },
                      { data: 10, label: "10 (Default)" },
                      { data: 20, label: "20" },
                      { data: 50, label: "50" },
                    ]}
                    selectedOption={saveSyncSettings.autocleanup_limit ?? 10}
                    onChange={(option) => handleSaveSyncSettingChange({ autocleanup_limit: option.data as number })}
                  />
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem layout="below" onClick={handleSyncAll} disabled={syncing}>
                    {syncing ? "Syncing..." : "Sync All Saves Now"}
                  </ButtonItem>
                </PanelSectionRow>
                {syncStatus && (
                  <PanelSectionRow>
                    <Field label={syncStatus} />
                  </PanelSectionRow>
                )}
              </>
            )}
          </>
        ) : (
          <PanelSectionRow>
            <Field label="Loading..." />
          </PanelSectionRow>
        )}
      </PanelSection>
      {saveSyncEnabled && (devicesLoading || registeredDevices !== null) && (
        <PanelSection title="Registered Devices">
          {devicesLoading && (
            <PanelSectionRow>
              <Field label="Loading..." />
            </PanelSectionRow>
          )}
          {!devicesLoading && devicesError && (
            <PanelSectionRow>
              <Field label="Could not load devices" description={devicesError} />
            </PanelSectionRow>
          )}
          {!devicesLoading && !devicesError && registeredDevices !== null && registeredDevices.length === 0 && (
            <PanelSectionRow>
              <Field label="No devices registered" />
            </PanelSectionRow>
          )}
          {!devicesLoading && !devicesError && registeredDevices !== null && registeredDevices.map((device, i) => {
            const parts: string[] = [
              `${device.client ?? "unknown client"} v${device.client_version ?? "?"}`,
              ...(device.platform ? [device.platform] : []),
              `last seen ${formatRelativeTime(device.last_seen)}`,
              `ID ${String(device.id ?? "").slice(0, 8) || "—"}`,
            ];
            return (
              <PanelSectionRow key={device.id || `idx-${i}`}>
                <Field
                  label={
                    <span>
                      {device.name ?? "(unnamed)"}
                      {device.is_current_device && (
                        <span style={{ color: "#6ab04c", marginLeft: "8px", fontSize: "12px" }}>(this device)</span>
                      )}
                    </span>
                  }
                  description={parts.join(" · ")}
                />
              </PanelSectionRow>
            );
          })}
        </PanelSection>
      )}
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
        {retroarchWarning?.warning && (
          <>
            <PanelSectionRow>
              <Field
                label={`RetroArch input_driver: "${retroarchWarning?.current}"`}
                description="Controller navigation in RetroArch menus may not work with this setting."
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => {
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
                }}
              >
                Fix input_driver to sdl2
              </ButtonItem>
            </PanelSectionRow>
            {retroarchFixStatus && (
              <PanelSectionRow>
                <Field label={retroarchFixStatus} />
              </PanelSectionRow>
            )}
          </>
        )}
      </PanelSection>
      <PanelSection title="Advanced">
        <PanelSectionRow>
          <DropdownItem
            label="Log Level"
            description="Controls how much detail is written to plugin logs"
            rgOptions={[
              { data: "error", label: "Error" },
              { data: "warn", label: "Warn" },
              { data: "info", label: "Info" },
              { data: "debug", label: "Debug" },
            ]}
            selectedOption={logLevel}
            onChange={(option) => {
              setLogLevel(option.data);
              saveLogLevel(option.data);
            }}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
