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
import { FaCheckCircle, FaTimesCircle, FaExclamationTriangle } from "react-icons/fa";
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
  getRetroDeckStatus,
  logError,
} from "../api/backend";
import { formatBytes } from "../utils/formatters";
import { getSyncProgress, setSyncProgress as setStoredSyncProgress, onSyncProgressChange } from "../utils/syncProgress";
import { scrollToTop } from "../utils/scrollHelpers";
import { getDownloadState } from "../utils/downloadStore";
import { getMigrationState, onMigrationChange, setMigrationStatus } from "../utils/migrationStore";
import { getSettingsResetState, onSettingsResetChange } from "../utils/settingsResetStore";
import { getPlaytimeScopeState, onPlaytimeScopeChange, fetchPlaytimeScopeState } from "../utils/playtimeScopeStore";
import {
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
} from "../utils/saveSortMigrationStore";
import { reconcileStaleShortcuts, requestSyncCancel, isCancelRequested, resetSyncCancel } from "../utils/syncManager";
import { setVersionError } from "../utils/connectionState";
import { retroDeckBanner, type RetroDeckBanner } from "../utils/retrodeckHealth";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { WarningCard } from "./WarningCard";
import { MigrationBlockedPage } from "./MigrationBlockedPage";
import { SettingsResetBanner } from "./SettingsResetBanner";
import { PlaytimeScopeBanner } from "./PlaytimeScopeBanner";
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
import { withTimeout } from "../utils/withTimeout";

type Page = "settings" | "library" | "data" | "downloads" | "system";

interface MainPageProps {
  onNavigate: (page: Page) => void;
}

// The connection probe races each `test_connection()` attempt against a deadline
// (the callable never times out on its own and hangs forever when the backend
// isn't up yet) and retries across the slow-cold-boot window. A call that
// resolves — success OR "not connected" — is authoritative and ends the probe;
// only an exhausted retry budget means the plugin backend never came up
// (bootstrap aborted), which the connection row surfaces explicitly instead of
// an eternal "Checking…" spinner (#1045). The schedule mirrors the metadata init
// loop's tuned window in index.tsx (#1203).
const CONNECTION_RETRY_DELAYS = [2000, 5000, 10000, 15000, 20000];
const CONNECTION_CALLABLE_TIMEOUT = 5000;

/** Backend never answered after the retry budget — distinct from `false` ("not connected"). */
type BackendFailed = "backend_failed";

function formatChanges(pairs: [number, string][]): string {
  return pairs
    .filter(([n]) => n > 0)
    .map(([n, label]) => `${n} ${label}`)
    .join(", ");
}

const ConnectionIndicator: FC<{ connected: boolean | null | BackendFailed }> = ({ connected }) => {
  if (connected === "backend_failed") {
    return (
      <>
        <FaExclamationTriangle style={{ color: "#d4a72c", fontSize: "14px" }} />
        <span style={{ fontSize: "12px" }}>Backend error</span>
      </>
    );
  }
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
  const [connected, setConnected] = useState<boolean | null | BackendFailed>(null);
  const versionError = useVersionError();
  const [syncing, setSyncing] = useState(false);
  // Disarmed "Cancelling…" state during the backend's RUNNING→CANCELLING→IDLE
  // drain. The Sync/Cancel button stays disabled until the terminal
  // sync_progress stage re-arms it, so a quick re-press can't hit the
  // sync_in_progress reject and look like an instant finish (#1202, RC-B).
  const [cancelling, setCancelling] = useState(false);
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);
  const [status, setStatus] = useState("");
  const [preview, setPreview] = useState<SyncPreview | null>(null);
  const [skipPreview, setSkipPreview] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retroarchWarning, setRetroarchWarning] = useState<{ warning: boolean; current?: string } | null>(null);
  const [retrodeckBanner, setRetrodeckBanner] = useState<RetroDeckBanner | null>(null);
  const [migration, setMigration] = useState<MigrationStatus>(getMigrationState());
  const [settingsReset, setSettingsReset] = useState(getSettingsResetState());
  const [playtimeScope, setPlaytimeScope] = useState(getPlaytimeScopeState());
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
    // Guards for the async connection probe below: `cancelled` stops the retry
    // loop from touching state after unmount; the timer ref lets cleanup clear a
    // pending backoff delay.
    let cancelled = false;
    let connectionRetryTimer: ReturnType<typeof setTimeout> | null = null;

    refreshMigrationState()
      .then(({ retrodeck, save_sort }) => {
        setMigrationStatus(retrodeck);
        setSaveSortMigrationStatus(save_sort);
      })
      .catch((e) => logError(`Failed to refresh migration state: ${e}`));
    getSyncStats()
      .then(setStats)
      .catch((e) => logError(`Failed to load sync stats: ${e}`));

    // Probe the backend for the connection row. Each attempt has a deadline
    // because the callable hangs (rather than rejects) while the backend is
    // still starting, and the retries ride out a slow cold boot. A resolved call
    // ends the probe — "not connected" (success:false) is an authoritative
    // answer, not a failure. Only an exhausted retry budget means the backend
    // never came up (bootstrap aborted); surface that explicitly (#1045).
    const probeConnection = async (isCancelled: () => boolean) => {
      for (let attempt = 0; !isCancelled(); attempt++) {
        try {
          const r = await withTimeout(testConnection(), CONNECTION_CALLABLE_TIMEOUT);
          if (isCancelled()) return;
          setConnected(r.success);
          setVersionError(r.reason === "version_error" ? r.message : null);
          return;
        } catch {
          if (isCancelled()) return;
          if (attempt >= CONNECTION_RETRY_DELAYS.length) {
            // Retry budget exhausted. test_connection() also waits out the
            // server round-trip — a hanging RomM server keeps the backend's
            // retrying heartbeat busy for up to ~90s, far past our per-attempt
            // deadline — so an exhausted budget alone can't tell a dead backend
            // from an unreachable server. Ping get_settings (a pure in-memory
            // read that resolves iff the backend RPC bridge is alive) to decide:
            // alive ⇒ the server is merely unreachable ("Not connected");
            // dead ⇒ the backend never came up ("Backend error").
            try {
              await withTimeout(getSettings(), CONNECTION_CALLABLE_TIMEOUT);
              if (isCancelled()) return;
              setConnected(false);
            } catch (pingErr) {
              if (isCancelled()) return;
              setConnected("backend_failed");
              // logError is itself a callable and would hang against a dead
              // backend — log to the console instead.
              console.error("[RomM] backend RPC bridge unreachable (get_settings ping failed):", pingErr);
            }
            return;
          }
          await new Promise<void>((resolve) => {
            connectionRetryTimer = setTimeout(resolve, CONNECTION_RETRY_DELAYS[attempt]);
          });
        }
      }
    };
    detach(probeConnection(() => cancelled));

    getSettings()
      .then((s) => {
        if (s.retroarch_input_check) {
          setRetroarchWarning(s.retroarch_input_check);
        }
      })
      .catch((e) => logError(`Failed to load settings: ${e}`));

    // RetroDECK path-resolution health — warn the user when the resolved roots
    // are likely wrong (retrodeck.json unreadable, or its home missing on
    // disk). "ok"/"absent" stay quiet (banner cleared to null).
    getRetroDeckStatus()
      .then((s) => setRetrodeckBanner(retroDeckBanner(s.status, s)))
      .catch((e) => logError(`Failed to query RetroDECK status: ${e}`));

    // Cross-device playtime scope notice. The backend sets a durable flag when a
    // playtime reconcile is rejected for a token missing `roms.user.read`; it
    // self-clears once a scoped token is minted, so we re-read it on every mount.
    fetchPlaytimeScopeState().catch((e) => logError(`Failed to check playtime scope notice: ${e}`));

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
        // True terminal reached — re-arm the button out of any "Cancelling…"
        // drain state (#1202, RC-B).
        setCancelling(false);
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
    const unsubSettingsReset = onSettingsResetChange(() => setSettingsReset(getSettingsResetState()));
    const unsubPlaytimeScope = onPlaytimeScopeChange(() => setPlaytimeScope(getPlaytimeScopeState()));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortMigration(getSaveSortMigrationState()));
    return () => {
      cancelled = true;
      if (connectionRetryTimer) clearTimeout(connectionRetryTimer);
      unsubProgress();
      unsubMigration();
      unsubSettingsReset();
      unsubPlaytimeScope();
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
    setCancelling(false);
    setStoredSyncProgress({ running: false, stage: "" });
  };

  const handleSync = async () => {
    // Clear any stale cancel flag from a prior run BEFORE this sync starts, so a
    // fresh sync never begins pre-cancelled (#1198). Run identity for a Cancel
    // click comes from the backend-fed sync_progress store now (#1202).
    resetSyncCancel();
    // Optimistically disable the button and show the in-progress UI before
    // the backend's first sync_progress event lands — writing running:true
    // into the MODULE store (the single source of truth the subscription
    // reads), not a shadowing local state.
    setLoading(true);
    setSyncing(true);
    setCancelling(false);
    setStatus("");
    setPreview(null);
    setStoredSyncProgress({ running: true, stage: "fetching", message: "Fetching library..." });
    try {
      // Reconcile shortcuts the user deleted via Steam's own UI BEFORE the work
      // queue is built (both sync paths fetch through it): unbind any dead
      // binding so the incremental skip re-fetches the platform and recreates
      // the missing shortcut (#1046). Best-effort — never blocks the sync.
      await reconcileStaleShortcuts();
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
      if (!result.success) {
        abortOptimisticSync(result.message || "Preview failed");
        return;
      }
      // RC-CANCEL-PREVIEW (#1202): a Cancel can land in the sub-second window
      // while syncPreview() is in flight. The backend returns a cancelled
      // result for a preview cancelled mid-loop, but a preview that finished
      // just before the cancel still resolves success — re-check the flag
      // before showing the phantom "Apply Sync". On a cancel, clear the flag
      // (so the next sync doesn't start pre-cancelled) and abort to idle.
      if (isCancelRequested()) {
        resetSyncCancel();
        abortOptimisticSync("Sync cancelled");
        return;
      }
      setPreview(result);
      setSyncing(false);
      setLoading(false);
    } catch {
      abortOptimisticSync("Failed to start sync");
    }
  };

  const handleApply = async () => {
    if (!preview) return;
    const previewId = preview.preview_id;
    // Clear any stale cancel flag before the apply run starts (#1198). A Cancel
    // in the apply window reads "" from the sync_progress store until the
    // backend stamps the run id, which the backend treats as an unconditional
    // cancel (#1202).
    resetSyncCancel();
    setPreview(null);
    setLoading(true);
    setSyncing(true);
    setCancelling(false);
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
    setCancelling(false);
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
    // RC-B (#1202): do NOT re-arm the Sync button here. The backend drains
    // RUNNING → CANCELLING → IDLE asynchronously; flipping back to enabled now
    // lets a quick re-press hit the sync_in_progress reject and look like an
    // "instant finish". Disarm into the "Cancelling…" state and wait for the
    // terminal sync_progress stage (the store subscription) to re-arm.
    setCancelling(true);
    requestSyncCancel();
    try {
      // Scope the cancel to the active run via the backend-fed run id; "" in the
      // pre-progress window → the backend's unconditional cancel (#1202).
      await cancelSync(getSyncProgress().runId ?? "");
      // Success: stay disarmed; the terminal stage tears the UI down and
      // re-arms, surfacing the backend's final message — no status here, so no
      // instant-finish flash during the drain.
    } catch {
      // The cancel call itself failed — no terminal will arrive from a cancel
      // that never landed, so re-arm and surface the failure for a retry.
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

  // Sync is unavailable when the server test failed OR the plugin backend never
  // started — both gate the Sync buttons off.
  const connectionUnavailable = connected === false || connected === "backend_failed";

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
            disabled={cancelling}
            onClick={() => {
              detach(handleCancel());
            }}
            // @ts-expect-error onFocus works at runtime; not in Decky's ButtonItem types
            onFocus={scrollToTop}
          >
            {cancelling ? "Cancelling…" : "Cancel Sync"}
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
            disabled={loading || connectionUnavailable}
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
              disabled={loading || connectionUnavailable}
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
      {settingsReset.pending && <SettingsResetBanner backedUpTo={settingsReset.backedUpTo} />}
      {playtimeScope.pending && <PlaytimeScopeBanner />}
      <PanelSection title="Status">
        {retrodeckBanner && (
          <PanelSectionRow>
            <WarningCard title={retrodeckBanner.title} message={retrodeckBanner.message} compact />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <Field
            label="Connection"
            description={
              connected === "backend_failed" ? "Plugin backend failed to start — check Decky logs." : undefined
            }
          >
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
