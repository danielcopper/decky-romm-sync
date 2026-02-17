import { useState, useEffect, useRef, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ProgressBarWithInfo,
  ToggleField,
} from "@decky/ui";
import {
  testConnection,
  startSync,
  cancelSync,
  getSyncStats,
  getSettings,
  saveDebugLogging,
  fixRetroarchInputDriver,
} from "../api/backend";
import { getSyncProgress } from "../utils/syncProgress";
import type { SyncProgress, SyncStats } from "../types";

type Page = "connection" | "platforms" | "danger" | "downloads" | "bios";

interface MainPageProps {
  onNavigate: (page: Page) => void;
}

export const MainPage: FC<MainPageProps> = ({ onNavigate }) => {
  const [stats, setStats] = useState<SyncStats | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [debugLogging, setDebugLogging] = useState(false);
  const [retroarchWarning, setRetroarchWarning] = useState<{ warning: boolean; current?: string } | null>(null);
  const [retroarchFixStatus, setRetroarchFixStatus] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const statusTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = () => {
    stopPolling();
    pollRef.current = setInterval(() => {
      // Read directly from module-level store — no async callable, no WebSocket
      const progress = getSyncProgress();
      setSyncProgress(progress);

      if (!progress.running) {
        stopPolling();
        setSyncing(false);
        setLoading(false);
        if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
        setStatus(progress.message || "Sync finished");
        statusTimeoutRef.current = setTimeout(() => setStatus(""), 8000);
        getSyncStats().then(setStats);
      }
    }, 250);
  };

  useEffect(() => {
    getSyncStats().then(setStats);
    testConnection().then((r) => setConnected(r.success));
    getSettings().then((s) => {
      setDebugLogging(s.debug_logging ?? false);
      if (s.retroarch_input_check) {
        setRetroarchWarning(s.retroarch_input_check);
      }
    });

    // Check if a sync is already in progress (handles QAM close/reopen)
    const progress = getSyncProgress();
    if (progress.running) {
      setSyncing(true);
      setLoading(true);
      setSyncProgress(progress);
      startPolling();
    }

    return () => {
      stopPolling();
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    };
  }, []);

  const handleSync = async () => {
    setLoading(true);
    setSyncing(true);
    setStatus("");
    setSyncProgress({ running: true, phase: "starting", message: "Starting sync..." });
    try {
      await startSync();
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

  const formatLastSync = (iso: string | null): string => {
    if (!iso) return "Never";
    try {
      const d = new Date(iso);
      const now = new Date();
      const diffMs = now.getTime() - d.getTime();
      const diffMins = Math.floor(diffMs / 60000);
      if (diffMins < 1) return "Just now";
      if (diffMins < 60) return `${diffMins}m ago`;
      const diffHours = Math.floor(diffMins / 60);
      if (diffHours < 24) return `${diffHours}h ago`;
      const diffDays = Math.floor(diffHours / 24);
      return `${diffDays}d ago`;
    } catch {
      return iso;
    }
  };

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field
            label="Connection"
            description={
              connected === null
                ? "Checking..."
                : connected
                  ? "Connected"
                  : "Not connected"
            }
          />
        </PanelSectionRow>
        {stats && (
          <>
            <PanelSectionRow>
              <Field
                label="Last sync"
                description={formatLastSync(stats.last_sync)}
              />
            </PanelSectionRow>
            {stats.roms > 0 && (
              <PanelSectionRow>
                <Field
                  label="Library"
                  description={`${stats.roms} ROMs from ${stats.platforms} platforms`}
                />
              </PanelSectionRow>
            )}
          </>
        )}
        {retroarchWarning && retroarchWarning.warning && (
          <PanelSectionRow>
            <Field
              label="RetroArch: input_driver issue"
              description={`Using "${retroarchWarning.current}" — controllers may not work in menus. See Warning section below.`}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Sync">
        {!syncing ? (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={handleSync}
              disabled={loading || connected === false}
            >
              Sync Library
            </ButtonItem>
          </PanelSectionRow>
        ) : (
          <>
            {syncProgress && (
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
            )}
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={handleCancel}>
                Cancel Sync
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
        {status && !syncing && (
          <PanelSectionRow>
            <Field label={status} />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("connection")}>
            Connection Settings
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("platforms")}>
            Platforms
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("downloads")}>
            Downloads
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("bios")}>
            BIOS Files
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("danger")}>
            Danger Zone
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {retroarchWarning && retroarchWarning.warning && (
        <PanelSection title="Warning">
          <PanelSectionRow>
            <Field
              label={`RetroArch input_driver: "${retroarchWarning.current}"`}
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
              Change to sdl2
            </ButtonItem>
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              label=""
              description="This modifies your RetroArch config. Use with caution — if controllers stop working, revert manually."
            />
          </PanelSectionRow>
          {retroarchFixStatus && (
            <PanelSectionRow>
              <Field label={retroarchFixStatus} />
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      <PanelSection title="Advanced">
        <PanelSectionRow>
          <ToggleField
            label="Debug Logging"
            description="Log additional details for troubleshooting"
            checked={debugLogging}
            onChange={(value) => {
              setDebugLogging(value);
              saveDebugLogging(value);
            }}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
