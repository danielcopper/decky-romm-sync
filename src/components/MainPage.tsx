import { useState, useEffect, useRef, FC, ReactNode } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  ProgressBar,
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
  startSync,
  syncPreview,
  syncApplyDelta,
  syncCancelPreview,
  clearSyncCache,
  refreshMigrationState,
  getSyncStatus,
  logError,
} from "../api/backend";
import { formatBytes } from "../utils/formatters";
import { getSyncProgress, setSyncProgress as setStoredSyncProgress, onSyncProgressChange } from "../utils/syncProgress";
import { scrollToTop } from "../utils/scrollHelpers";
import { getDownloadState } from "../utils/downloadStore";
import { getMigrationState, onMigrationChange, setMigrationStatus } from "../utils/migrationStore";
import {
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
} from "../utils/saveSortMigrationStore";
import { requestSyncCancel } from "../utils/syncManager";
import { setVersionError } from "../utils/connectionState";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { MigrationBlockedPage } from "./MigrationBlockedPage";
import type {
  SyncProgress,
  SyncStage,
  SyncStats,
  SyncPreview,
  SyncPreviewSummary,
  DownloadItem,
  MigrationStatus,
} from "../types";
import { detach } from "../utils/detach";

type Page = "settings" | "library" | "data" | "downloads" | "system";

interface MainPageProps {
  onNavigate: (page: Page) => void;
}

function formatChanges(pairs: [number, string][]): string {
  return pairs
    .filter(([n]) => n > 0)
    .map(([n, label]) => `${n} ${label}`)
    .join(", ");
}

const ConnectionIndicator: FC<{ connected: boolean | null }> = ({ connected }) => {
  if (connected === null) {
    return (
      <>
        <Spinner width={14} height={14} />
        <span style={{ fontSize: "12px", opacity: 0.7 }}>Checking...</span>
      </>
    );
  }
  if (connected) {
    return (
      <>
        <FaCheckCircle style={{ color: "#59bf40", fontSize: "14px" }} />
        <span style={{ fontSize: "12px" }}>Connected</span>
      </>
    );
  }
  return (
    <>
      <FaTimesCircle style={{ color: "#d4343c", fontSize: "14px" }} />
      <span style={{ fontSize: "12px" }}>Not connected</span>
    </>
  );
};

const TERMINAL_STAGES: ReadonlySet<SyncStage> = new Set<SyncStage>(["done", "cancelled", "error"]);

function isTerminalStage(stage: SyncProgress["stage"]): boolean {
  return !!stage && TERMINAL_STAGES.has(stage);
}

const STAGE_LABELS: Record<SyncStage, string> = {
  discovering: "Discovering platforms",
  fetching: "Fetching library",
  applying: "Applying shortcuts",
  finalizing: "Finalizing",
  done: "Done",
  cancelled: "Cancelled",
  error: "Error",
};

function stageLabel(stage: SyncProgress["stage"]): string {
  return stage ? STAGE_LABELS[stage] : "Syncing";
}

function formatProgressText(progress: SyncProgress | null): string {
  if (!progress) return "Syncing...";
  const step = progress.step && progress.totalSteps ? `[${progress.step}/${progress.totalSteps}] ` : "";
  const msg = progress.message || "Syncing...";
  // Truncate to ~40 chars to prevent multi-line jumping in the QAM panel
  const maxLen = 40 - step.length;
  const truncated = msg.length > maxLen ? msg.slice(0, maxLen - 1) + "\u2026" : msg;
  return step + truncated;
}

function formatLastSync(iso: string | null): string {
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
}

function formatPreviewDescription(s: SyncPreviewSummary): string {
  const sections: string[] = [];
  const romChanges = formatChanges([
    [s.new_count, "added"],
    [s.changed_count, "updated"],
    [s.remove_count, "removed"],
  ]);
  if (romChanges) sections.push(`ROMs: ${romChanges}`);
  const p = s.platform_collection_diff;
  if (p?.has_changes) {
    const platChanges = formatChanges([
      [p.added_count, "added"],
      [p.removed_count, "removed"],
    ]);
    if (platChanges) sections.push(`Platforms: ${platChanges}`);
  }
  const d = s.collection_diff;
  if (d?.has_changes) {
    const collChanges = formatChanges([
      [d.added.length, "added"],
      [d.removed.length, "removed"],
    ]);
    if (collChanges) sections.push(`Collections: ${collChanges}`);
  }
  return sections.length > 0 ? sections.join("; ") : "Everything is up to date.";
}

export const MainPage: FC<MainPageProps> = ({ onNavigate }) => {
  const [stats, setStats] = useState<SyncStats | null>(null);
  const [connected, setConnected] = useState<boolean | null>(null);
  const versionError = useVersionError();
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
  const statusTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const downloadPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const showTransientStatus = (msg: string) => {
    if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    setStatus(msg);
    statusTimeoutRef.current = setTimeout(() => setStatus(""), 8000);
  };

  useEffect(() => {
    refreshMigrationState()
      .then(({ retrodeck, save_sort }) => {
        setMigrationStatus(retrodeck);
        setSaveSortMigrationStatus(save_sort);
      })
      .catch((e) => logError(`Failed to refresh migration state: ${e}`));
    getSyncStats()
      .then(setStats)
      .catch((e) => logError(`Failed to load sync stats: ${e}`));
    testConnection()
      .then((r) => {
        setConnected(r.success);
        setVersionError(r.error_code === "version_error" ? r.message : null);
      })
      .catch((e) => logError(`Failed to test connection: ${e}`));
    getSettings()
      .then((s) => {
        if (s.retroarch_input_check) {
          setRetroarchWarning(s.retroarch_input_check);
        }
      })
      .catch((e) => logError(`Failed to load settings: ${e}`));

    // Backend is authoritative for in-flight sync state. Seed the module
    // store from get_sync_status() so a QAM close/reopen recovers the live
    // run rather than guessing from the event-fed store alone.
    getSyncStatus()
      .then((progress) => {
        setStoredSyncProgress(progress);
        if (progress.running) {
          setSyncing(true);
          setLoading(true);
          setSyncProgress(progress);
        }
      })
      .catch((e) => logError(`Failed to query sync status: ${e}`));

    // Subscribe to the module store — every backend sync_progress event and
    // every frontend updateSyncProgress notifies, driving a re-render. The
    // in-progress UI is torn down ONLY on a terminal stage, never on a bare
    // running:false (which can transiently race a fresh run's first event).
    const unsubProgress = onSyncProgressChange(() => {
      const progress = getSyncProgress();
      setSyncProgress(progress);
      if (isTerminalStage(progress.stage)) {
        setSyncing(false);
        setLoading(false);
        showTransientStatus(progress.message || "Sync finished");
        getSyncStats()
          .then(setStats)
          .catch((e) => logError(`Failed to refresh sync stats: ${e}`));
      }
    });

    // Poll download state for inline display
    downloadPollRef.current = setInterval(() => {
      setDownloads([...getDownloadState()]);
    }, 1000);

    const unsubMigration = onMigrationChange(() => setMigration(getMigrationState()));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortMigration(getSaveSortMigrationState()));
    return () => {
      unsubProgress();
      unsubMigration();
      unsubSaveSort();
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
      if (downloadPollRef.current) clearInterval(downloadPollRef.current);
    };
  }, []);

  // A start/apply call never reached a running backend sync (rejected up
  // front or threw). Reset both the local UI and the MODULE store so the
  // store mirrors reality — the optimistic running:true must not linger.
  const abortOptimisticSync = (msg: string) => {
    setStatus(msg);
    setSyncing(false);
    setLoading(false);
    setStoredSyncProgress({ running: false, stage: "" });
  };

  const handleSync = async () => {
    // Optimistically disable the button and show the in-progress UI before
    // the backend's first sync_progress event lands — writing running:true
    // into the MODULE store (the single source of truth the subscription
    // reads), not a shadowing local state.
    setLoading(true);
    setSyncing(true);
    setStatus("");
    setPreview(null);
    setStoredSyncProgress({ running: true, stage: "fetching", message: "Fetching library..." });
    try {
      // Skip Preview takes the per-unit pipeline (start_sync) — incremental
      // shortcut delivery, per-unit crash safety, no upfront full library
      // fetch. The legacy preview/apply path remains for users who want to
      // review changes before they apply.
      if (skipPreview) {
        const startResult = await startSync();
        if (!startResult.success) {
          abortOptimisticSync(startResult.message);
        }
        // On success the store subscription drives the UI from here.
        return;
      }
      const result = await syncPreview();
      if (result.success) {
        setPreview(result);
        setSyncing(false);
        setLoading(false);
      } else {
        abortOptimisticSync(result.message || "Preview failed");
      }
    } catch {
      abortOptimisticSync("Failed to start sync");
    }
  };

  const handleApply = async () => {
    if (!preview) return;
    const previewId = preview.preview_id;
    setPreview(null);
    setLoading(true);
    setSyncing(true);
    setStoredSyncProgress({ running: true, stage: "applying", message: "Applying changes..." });
    try {
      const result = await syncApplyDelta(previewId);
      if (!result.success) {
        abortOptimisticSync(result.message);
      }
      // On success the store subscription drives the UI from here.
    } catch {
      abortOptimisticSync("Failed to apply sync");
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

  const finishCancelWithStatus = (msg: string) => {
    setSyncing(false);
    setLoading(false);
    showTransientStatus(msg);
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
      finishCancelWithStatus(result.message);
    } catch {
      finishCancelWithStatus("Failed to cancel sync");
    }
  };

  // Two-level progress. The main determinate bar tracks COARSE unit
  // progress (step / totalSteps); 0/0 means the run hasn't reached a unit
  // yet, so the bar goes indeterminate. Steam's ProgressBarWithInfo
  // nProgress uses percentage (0-100), not fraction (0-1).
  const coarseFraction = syncProgress?.totalSteps
    ? ((syncProgress.step ?? 0) / syncProgress.totalSteps) * 100
    : undefined;
  const hasFineDetail = !!(syncProgress?.total && syncProgress.message);

  const activeDownloads = downloads.filter((d) => d.status === "queued" || d.status === "downloading");
  const completedDownloads = downloads.filter(
    (d) => d.status === "completed" || d.status === "failed" || d.status === "cancelled",
  );
  const hasDownloads = activeDownloads.length > 0 || completedDownloads.length > 0;

  if (versionError) {
    return <VersionErrorCard message={versionError} compact />;
  }

  if (migration.pending) {
    return <MigrationBlockedPage migration={migration} />;
  }

  let syncBody: ReactNode;
  if (preview) {
    const hasChanges =
      preview.summary.new_count + preview.summary.changed_count + preview.summary.remove_count > 0 ||
      preview.summary.collection_diff?.has_changes ||
      preview.summary.platform_collection_diff?.has_changes;
    syncBody = (
      <>
        <PanelSectionRow>
          <Field label="Preview" description={formatPreviewDescription(preview.summary)} />
        </PanelSectionRow>
        {hasChanges ? (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => {
                  detach(handleApply());
                }}
                // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
                onFocus={scrollToTop}
              >
                Apply Sync
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => {
                  detach(handleDismiss());
                }}
              >
                Cancel
              </ButtonItem>
            </PanelSectionRow>
          </>
        ) : (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => {
                detach(handleDismiss());
              }}
              // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
              onFocus={scrollToTop}
            >
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        )}
      </>
    );
  } else if (syncing) {
    const stepText = syncProgress?.totalSteps ? `${syncProgress.step ?? 0}/${syncProgress.totalSteps}` : "";
    syncBody = (
      <>
        <PanelSectionRow>
          {/* Own the caption in a full-width row and use the bare ProgressBar.
              ProgressBarWithInfo is a Steam Field (label column | bar column);
              with no label text the empty column shoves the bar into the right
              half and clips it (#751). The bare ProgressBar is just the bar and
              spans the full panel width. */}
          <div style={{ width: "100%" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: "12px",
                marginBottom: "4px",
              }}
            >
              <span data-testid="sync-stage">{stageLabel(syncProgress?.stage)}</span>
              {stepText && <span data-testid="sync-step">{stepText}</span>}
            </div>
            <ProgressBar
              indeterminate={coarseFraction === undefined}
              {...(coarseFraction !== undefined ? { nProgress: coarseFraction } : {})}
            />
          </div>
        </PanelSectionRow>
        {hasFineDetail && (
          <PanelSectionRow>
            <Field
              label={
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <Spinner width={14} height={14} />
                  <span style={{ fontSize: "12px" }}>{formatProgressText(syncProgress)}</span>
                </div>
              }
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              detach(handleCancel());
            }}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
          >
            Cancel Sync
          </ButtonItem>
        </PanelSectionRow>
      </>
    );
  } else {
    syncBody = (
      <>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              detach(handleSync());
            }}
            disabled={loading || connected === false}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
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
              onClick={() => {
                detach(
                  (async () => {
                    try {
                      const result = await clearSyncCache();
                      setStatus(result.message);
                    } catch {
                      setStatus("Failed to clear sync cache");
                    }
                    if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
                    statusTimeoutRef.current = setTimeout(() => setStatus(""), 8000);
                    getSyncStats()
                      .then(setStats)
                      .catch((e) => logError(`Failed to refresh sync stats: ${e}`));
                  })(),
                );
              }}
              disabled={loading || connected === false}
            >
              Force Full Sync
            </ButtonItem>
          </PanelSectionRow>
        )}
      </>
    );
  }

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field label="Connection">
            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              <ConnectionIndicator connected={connected} />
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
        {retroarchWarning?.warning && (
          <PanelSectionRow>
            <Field label="RetroArch: input_driver issue" description={`Using "${retroarchWarning.current}"`}>
              <DialogButton
                onClick={() =>
                  showModal(
                    <ConfirmModal
                      strTitle="Fix RetroArch input_driver?"
                      strDescription="This will change input_driver to sdl2 in your RetroArch config. Controllers should work better in RetroArch menus after this change."
                      strOKButtonText="Apply Fix"
                      strCancelButtonText="Cancel"
                      onOK={() => {
                        detach(
                          (async () => {
                            try {
                              const result = await fixRetroarchInputDriver();
                              if (result.success) {
                                setRetroarchWarning(null);
                              }
                            } catch {
                              // ignore
                            }
                          })(),
                        );
                      }}
                    />,
                  )
                }
                onFocus={scrollToTop}
              >
                Fix
              </DialogButton>
            </Field>
          </PanelSectionRow>
        )}
        {saveSortMigration.pending && (
          <>
            <PanelSectionRow>
              <div
                style={{
                  padding: "8px 12px",
                  backgroundColor: "rgba(212, 167, 44, 0.15)",
                  borderLeft: "3px solid #d4a72c",
                  borderRadius: "4px",
                  fontSize: "12px",
                }}
              >
                <div style={{ fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" }}>
                  {"\u26A0\uFE0F"} RetroArch save sorting changed
                </div>
                <div style={{ color: "rgba(255, 255, 255, 0.7)" }}>
                  {saveSortMigration.saves_count ?? 0} save file(s) to migrate
                </div>
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => onNavigate("settings")}
                // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
                onFocus={scrollToTop}
              >
                Go to Settings
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title="Sync">
        {syncBody}
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
                {...(item.total_bytes > 0 ? { nProgress: (item.bytes_downloaded / item.total_bytes) * 100 } : {})}
                indeterminate={item.total_bytes === 0}
                sOperationText={item.rom_name}
                sTimeRemaining={
                  item.total_bytes > 0
                    ? `${formatBytes(item.bytes_downloaded)} / ${formatBytes(item.total_bytes)}`
                    : formatBytes(item.bytes_downloaded)
                }
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
          <ButtonItem layout="below" onClick={() => onNavigate("system")}>
            System
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
