import { useState, useEffect, useRef, FC } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ProgressBarWithInfo,
  ToggleField,
  Spinner,
  DialogButton,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import { FaCheckCircle, FaTimesCircle } from "react-icons/fa";
import {
  testConnection,
  cancelSync,
  getSyncStats,
  getSettings,
  fixRetroarchInputDriver,
  syncPreview,
  syncApplyDelta,
  syncCancelPreview,
  clearSyncCache,
} from "../api/backend";
import { getSyncProgress } from "../utils/syncProgress";
import { getDownloadState } from "../utils/downloadStore";
import { getMigrationState, onMigrationChange } from "../utils/migrationStore";
import { getSaveSortMigrationState, onSaveSortMigrationChange } from "../utils/saveSortMigrationStore";
import { requestSyncCancel } from "../utils/syncManager";
import type { SyncProgress, SyncStats, SyncPreview, SyncPreviewSummary, DownloadItem } from "../types";
import type { MigrationStatus } from "../api/backend";

type Page = "settings" | "library" | "data" | "downloads";

interface MainPageProps {
  onNavigate: (page: Page) => void;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatChanges(pairs: [number, string][]): string {
  return pairs.filter(([n]) => n > 0).map(([n, label]) => `${n} ${label}`).join(", ");
}

function formatPreviewDescription(s: SyncPreviewSummary): string {
  const sections: string[] = [];
  const romChanges = formatChanges([[s.new_count, "added"], [s.changed_count, "updated"], [s.remove_count, "removed"]]);
  if (romChanges) sections.push(`ROMs: ${romChanges}`);
  const p = s.platform_collection_diff;
  if (p?.has_changes) {
    const platChanges = formatChanges([[p.added_count, "added"], [p.removed_count, "removed"]]);
    if (platChanges) sections.push(`Platforms: ${platChanges}`);
  }
  const d = s.collection_diff;
  if (d?.has_changes) {
    const collChanges = formatChanges([[d.added.length, "added"], [d.removed.length, "removed"]]);
    if (collChanges) sections.push(`Collections: ${collChanges}`);
  }
  return sections.length > 0 ? sections.join("; ") : "Everything is up to date.";
}

export const MainPage: FC<MainPageProps> = ({ onNavigate }) => {
  const [stats, setStats] = useState<SyncStats | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);
  const [status, setStatus] = useState("");
  const [preview, setPreview] = useState<SyncPreview | null>(null);
  const [skipPreview, setSkipPreview] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retroarchWarning, setRetroarchWarning] = useState<{ warning: boolean; current?: string } | null>(null);
  const [migration, setMigration] = useState<MigrationStatus>(getMigrationState());
  const [saveSortMigration, setSaveSortMigration] = useState(getSaveSortMigrationState());
  const [downloads, setDownloads] = useState<DownloadItem[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const statusTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const downloadPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = (progressOnly = false) => {
    stopPolling();
    pollRef.current = setInterval(() => {
      // Read directly from module-level store — no async callable, no WebSocket
      const progress = getSyncProgress();
      setSyncProgress(progress);

      if (!progressOnly && !progress.running) {
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

    // Poll download state for inline display
    downloadPollRef.current = setInterval(() => {
      setDownloads([...getDownloadState()]);
    }, 1000);

    const unsubMigration = onMigrationChange(() => setMigration(getMigrationState()));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortMigration(getSaveSortMigrationState()));
    return () => {
      stopPolling();
      unsubMigration();
      unsubSaveSort();
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      if (downloadPollRef.current) clearInterval(downloadPollRef.current);
    };
  }, []);

  const handleSync = async () => {
    setLoading(true);
    setSyncing(true);
    setStatus("");
    setPreview(null);
    setSyncProgress({ running: true, phase: "fetching", message: "Fetching library..." });
    startPolling(true);
    try {
      const result = await syncPreview();
      stopPolling();
      if (result.success) {
        const hasChanges = result.summary.new_count + result.summary.changed_count + result.summary.remove_count > 0 || !!result.summary.collection_diff?.has_changes || !!result.summary.platform_collection_diff?.has_changes;
        if (skipPreview && hasChanges) {
          // Auto-apply: skip preview UI
          setSyncProgress({ running: true, phase: "applying", message: "Applying changes..." });
          const applyResult = await syncApplyDelta(result.preview_id);
          if (applyResult.success) {
            startPolling();
          } else {
            setStatus(applyResult.message);
            setSyncing(false);
            setLoading(false);
          }
        } else if (skipPreview) {
          // Nothing to sync — show brief status
          await syncCancelPreview();
          setStatus("Everything is up to date");
          setSyncing(false);
          setLoading(false);
        } else {
          setPreview(result);
          setSyncing(false);
          setLoading(false);
        }
      } else {
        setStatus(result.message || "Preview failed");
        setSyncing(false);
        setLoading(false);
      }
    } catch {
      stopPolling();
      setStatus("Failed to start sync");
      setSyncing(false);
      setLoading(false);
    }
  };

  const handleApply = async () => {
    if (!preview) return;
    const previewId = preview.preview_id;
    setPreview(null);
    setLoading(true);
    setSyncing(true);
    setSyncProgress({ running: true, phase: "applying", message: "Applying changes..." });
    try {
      const result = await syncApplyDelta(previewId);
      if (result.success) {
        startPolling();
      } else {
        setStatus(result.message);
        setSyncing(false);
        setLoading(false);
      }
    } catch {
      setStatus("Failed to apply sync");
      setSyncing(false);
      setLoading(false);
    }
  };

  const handleDismiss = async () => {
    setPreview(null);
    setStatus("");
    try {
      await syncCancelPreview();
    } catch {
      // ignore
    }
  };

  const handleCancel = async () => {
    if (preview) {
      await handleDismiss();
      setSyncing(false);
      setLoading(false);
      return;
    }
    try {
      requestSyncCancel();
      const result = await cancelSync();
      setStatus(result.message);
    } catch {
      setStatus("Failed to cancel sync");
    }
  };

  // Steam's ProgressBarWithInfo nProgress uses percentage (0-100), not fraction (0-1)
  const progressFraction = syncProgress?.total
    ? ((syncProgress.current ?? 0) / syncProgress.total) * 100
    : undefined;

  const formatProgressText = (progress: SyncProgress | null): string => {
    if (!progress) return "Syncing...";
    const step = progress.step && progress.totalSteps
      ? `[${progress.step}/${progress.totalSteps}] `
      : "";
    const msg = progress.message || "Syncing...";
    // Truncate to ~40 chars to prevent multi-line jumping in the QAM panel
    const maxLen = 40 - step.length;
    const truncated = msg.length > maxLen ? msg.slice(0, maxLen - 1) + "\u2026" : msg;
    return step + truncated;
  };

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

  const activeDownloads = downloads.filter(d => d.status === "queued" || d.status === "downloading");
  const completedDownloads = downloads.filter(d => d.status === "completed" || d.status === "failed" || d.status === "cancelled");
  const hasDownloads = activeDownloads.length > 0 || completedDownloads.length > 0;

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field
            label="Connection"
          >
            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              {connected === null ? (
                <>
                  <Spinner width={14} height={14} />
                  <span style={{ fontSize: "12px", opacity: 0.7 }}>Checking...</span>
                </>
              ) : connected ? (
                <>
                  <FaCheckCircle style={{ color: "#59bf40", fontSize: "14px" }} />
                  <span style={{ fontSize: "12px" }}>Connected</span>
                </>
              ) : (
                <>
                  <FaTimesCircle style={{ color: "#d4343c", fontSize: "14px" }} />
                  <span style={{ fontSize: "12px" }}>Not connected</span>
                </>
              )}
            </div>
          </Field>
        </PanelSectionRow>
        {stats && (
          <>
            <PanelSectionRow>
              <Field label="Last sync">
                <span style={{ fontSize: "12px" }}>{formatLastSync(stats.last_sync)}</span>
              </Field>
            </PanelSectionRow>
            {stats.roms > 0 && (
              <PanelSectionRow>
                <Field label="Library">
                  <span style={{ fontSize: "12px" }}>
                    {stats.roms} ROMs
                    {stats.platforms > 0 ? ` · ${stats.platforms} platforms` : ""}
                    {(stats.collections ?? 0) > 0 ? ` · ${stats.collections} collections` : ""}
                  </span>
                </Field>
              </PanelSectionRow>
            )}
          </>
        )}
        {retroarchWarning && retroarchWarning.warning && (
          <PanelSectionRow>
            <Field
              label="RetroArch: input_driver issue"
              description={`Using "${retroarchWarning.current}"`}
            >
              <DialogButton onClick={() => showModal(
                <ConfirmModal
                  strTitle="Fix RetroArch input_driver?"
                  strDescription="This will change input_driver to sdl2 in your RetroArch config. Controllers should work better in RetroArch menus after this change."
                  strOKButtonText="Apply Fix"
                  strCancelButtonText="Cancel"
                  onOK={async () => {
                    try {
                      const result = await fixRetroarchInputDriver();
                      if (result.success) {
                        setRetroarchWarning(null);
                      }
                    } catch {
                      // ignore
                    }
                  }}
                />
              )}>
                Fix
              </DialogButton>
            </Field>
          </PanelSectionRow>
        )}
        {migration.pending && (
          <>
            <PanelSectionRow>
              <div style={{ padding: "8px 12px", backgroundColor: "rgba(212, 167, 44, 0.15)", borderLeft: "3px solid #d4a72c", borderRadius: "4px" }}>
                <div style={{ fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" }}>
                  {"\u26A0\uFE0F"} RetroDECK location changed
                </div>
                <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" }}>
                  {(migration.roms_count ?? 0) + (migration.bios_count ?? 0) + (migration.saves_count ?? 0)} file(s) need migration ({migration.roms_count ?? 0} ROMs, {migration.bios_count ?? 0} BIOS, {migration.saves_count ?? 0} saves)
                </div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => onNavigate("settings")}>
                Go to Settings
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
        {saveSortMigration.pending && (
          <>
            <PanelSectionRow>
              <div style={{
                padding: "8px 12px",
                backgroundColor: "rgba(212, 167, 44, 0.15)",
                borderLeft: "3px solid #d4a72c",
                borderRadius: "4px",
                fontSize: "12px",
              }}>
                <div style={{ fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" }}>
                  {"\u26A0\uFE0F"} RetroArch save sorting changed
                </div>
                <div style={{ color: "rgba(255, 255, 255, 0.7)" }}>
                  {saveSortMigration.saves_count ?? 0} save file(s) to migrate
                </div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => onNavigate("settings")}>
                Go to Settings
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title="Sync">
        {preview ? (
          <>
            <PanelSectionRow>
              <Field
                label="Preview"
                description={formatPreviewDescription(preview.summary)}
              />
            </PanelSectionRow>
            {preview.summary.new_count + preview.summary.changed_count + preview.summary.remove_count > 0 || preview.summary.collection_diff?.has_changes || preview.summary.platform_collection_diff?.has_changes ? (
              <>
                <PanelSectionRow>
                  <ButtonItem layout="below" onClick={handleApply}>
                    Apply Sync
                  </ButtonItem>
                </PanelSectionRow>
                <PanelSectionRow>
                  <ButtonItem layout="below" onClick={handleDismiss}>
                    Cancel
                  </ButtonItem>
                </PanelSectionRow>
              </>
            ) : (
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={handleDismiss}>
                  Dismiss
                </ButtonItem>
              </PanelSectionRow>
            )}
          </>
        ) : !syncing ? (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={handleSync}
                disabled={loading || connected === false}
              >
                Sync Library
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Skip Preview"
                description="Apply changes immediately without preview"
                checked={skipPreview}
                onChange={setSkipPreview}
              />
            </PanelSectionRow>
            {stats?.last_sync && (
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  description="Clear cached sync data to re-fetch all platforms"
                  onClick={async () => {
                    const result = await clearSyncCache();
                    setStatus(result.message);
                    if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
                    statusTimeoutRef.current = setTimeout(() => setStatus(""), 8000);
                    getSyncStats().then(setStats);
                  }}
                  disabled={loading || connected === false}
                >
                  Force Full Sync
                </ButtonItem>
              </PanelSectionRow>
            )}
          </>
        ) : (
          <>
            {syncProgress && syncProgress.step && syncProgress.totalSteps ? (
              <PanelSectionRow>
                <ProgressBarWithInfo
                  indeterminate={progressFraction === undefined}
                  nProgress={progressFraction}
                  sOperationText={formatProgressText(syncProgress)}
                />
              </PanelSectionRow>
            ) : (
              <PanelSectionRow>
                <Field
                  label={
                    <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                      <Spinner width={16} height={16} />
                      {syncProgress?.message || "Fetching..."}
                    </div>
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
        {status && !syncing && !preview && (
          <PanelSectionRow>
            <Field label={status} />
          </PanelSectionRow>
        )}
      </PanelSection>

      {hasDownloads && (
        <PanelSection title="Downloads">
          {activeDownloads.slice(0, 2).map((item) => (
            <PanelSectionRow key={item.rom_id}>
              <ProgressBarWithInfo
                nProgress={item.total_bytes > 0 ? (item.bytes_downloaded / item.total_bytes) * 100 : undefined}
                indeterminate={item.total_bytes === 0}
                sOperationText={item.rom_name}
                sTimeRemaining={item.total_bytes > 0 ? `${formatBytes(item.bytes_downloaded)} / ${formatBytes(item.total_bytes)}` : formatBytes(item.bytes_downloaded)}
              />
            </PanelSectionRow>
          ))}
          {activeDownloads.length > 2 && (
            <PanelSectionRow>
              <Field label={`+${activeDownloads.length - 2} more downloading`} />
            </PanelSectionRow>
          )}
          {completedDownloads.length > 0 && (
            <PanelSectionRow>
              <Field label={`${completedDownloads.length} completed`} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => onNavigate("downloads")}>
              View All
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Settings">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("library")}>
            Library
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("settings")}>
            Settings
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => onNavigate("data")}>
            Data Management
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
