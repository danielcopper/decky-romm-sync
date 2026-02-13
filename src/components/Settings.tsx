import { useState, useEffect, useRef, FC, ChangeEvent } from "react";
import { PanelSection, PanelSectionRow, TextField, ButtonItem, Field, ProgressBarWithInfo, showModal, ConfirmModal } from "@decky/ui";
import { getSettings, saveSettings, testConnection, startSync, getSyncProgress, cancelSync, removeAllShortcuts } from "../api/backend";
import { PlatformSync } from "./PlatformSync";
import type { SyncProgress } from "../types";

export const Settings: FC = () => {
  const [url, setUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [hasCredentials, setHasCredentials] = useState(false);
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    getSettings().then((s) => {
      setUrl(s.romm_url);
      setUsername(s.romm_user);
      setPassword(s.romm_pass_masked);
      setHasCredentials(s.has_credentials);
      setSettingsLoaded(true);
    });
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const startPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const progress = await getSyncProgress();
        setSyncProgress(progress);
        if (!progress.running) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setSyncing(false);
          setLoading(false);
          setStatus(progress.message || "Sync finished");
        }
      } catch {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        setSyncing(false);
        setLoading(false);
      }
    }, 2000);
  };

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

  const handleSync = async () => {
    setLoading(true);
    setSyncing(true);
    setStatus("");
    setSyncProgress(null);
    try {
      const result = await startSync();
      setStatus(result.message);
      startPolling();
    } catch {
      setStatus("Failed to start sync");
      setSyncing(false);
      setLoading(false);
    }
  };

  const handleCancel = async () => {
    try {
      const result = await cancelSync();
      setStatus(result.message);
    } catch {
      setStatus("Failed to cancel sync");
    }
  };

  const progressFraction = syncProgress?.total
    ? (syncProgress.current ?? 0) / syncProgress.total
    : undefined;

  return (
    <>
      <PanelSection title="RomM Connection">
        <PanelSectionRow>
          <TextField
            label="RomM URL"
            value={url}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setUrl(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Username"
            value={username}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setUsername(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Password"
            bIsPassword
            value={password}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setPassword(e.target.value)}
          />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Actions">
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
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleSync} disabled={loading}>
            Sync Library
          </ButtonItem>
        </PanelSectionRow>
        {syncing && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleCancel}>
              Cancel Sync
            </ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>
      {syncing && syncProgress && (
        <PanelSection title="Sync Progress">
          {syncProgress.phase && (
            <PanelSectionRow>
              <Field label="Phase" description={syncProgress.phase} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ProgressBarWithInfo
              indeterminate={progressFraction === undefined}
              nProgress={progressFraction}
              sOperationText={syncProgress.message || "Syncing..."}
              sTimeRemaining={
                syncProgress.total
                  ? `${syncProgress.current ?? 0} / ${syncProgress.total}`
                  : undefined
              }
            />
          </PanelSectionRow>
        </PanelSection>
      )}
      {settingsLoaded && hasCredentials && <PlatformSync />}
      <PanelSection title="Danger Zone">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={loading} onClick={() => {
            showModal(
              <ConfirmModal
                strTitle="Remove All Shortcuts"
                strDescription="Remove all RomM games from your Steam Library? Downloaded ROMs will not be deleted."
                strOKButtonText="Remove All"
                onOK={async () => {
                  const result = await removeAllShortcuts();
                  setStatus(result.message);
                }}
              />
            );
          }}>
            Remove All RomM Shortcuts
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      {status && (
        <PanelSection title="Status">
          <PanelSectionRow>
            <Field label={status} />
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
};
