import { useState, useEffect, useRef, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ProgressBarWithInfo,
} from "@decky/ui";
import {
  testConnection,
  startSync,
  cancelSync,
  getSyncProgress,
  getSyncStats,
} from "../api/backend";
import type { SyncProgress, SyncStats } from "../types";

type Page = "connection" | "platforms" | "danger";

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
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    getSyncStats().then(setStats);
    testConnection().then((r) => setConnected(r.success));
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
          getSyncStats().then(setStats);
        }
      } catch {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        setSyncing(false);
        setLoading(false);
      }
    }, 2000);
  };

  const handleSync = async () => {
    setLoading(true);
    setSyncing(true);
    setStatus("");
    setSyncProgress(null);
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
          <ButtonItem layout="below" onClick={() => onNavigate("danger")}>
            Danger Zone
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
