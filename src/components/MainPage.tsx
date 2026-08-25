import { useState, useEffect, useRef, FC, ReactNode } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  Field,
  Focusable,
  ProgressBar,
  ToggleField,
  Spinner,
  DialogButton,
  ConfirmModal,
  showModal,
} from "@decky/ui";
import { FaCheckCircle, FaTimesCircle, FaExclamationTriangle } from "react-icons/fa";
import {
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
  getSessionBudgetStatus,
  getRetroDeckStatus,
  logError,
} from "../api/backend";
import { formatTimeAgo } from "../utils/formatters";
import { pluralize } from "../utils/pluralize";
import { formatDuration, previewApplySeconds } from "../utils/syncEstimate";
import {
  observeApplyProgress,
  displayedEtaSeconds,
  resetEta,
  formatEtaCountdown,
  latchedCoarseFraction,
} from "../utils/syncEta";
import {
  getSyncProgress,
  setSyncProgress as setStoredSyncProgress,
  onSyncProgressChange,
  withinUnitFraction,
} from "../utils/syncProgress";
import { useDownloads } from "../utils/downloadStore";
import { getMigrationState, onMigrationChange, setMigrationStatus } from "../utils/migrationStore";
import { getSettingsResetState, onSettingsResetChange } from "../utils/settingsResetStore";
import { getPlaytimeScopeState, onPlaytimeScopeChange, fetchPlaytimeScopeState } from "../utils/playtimeScopeStore";
import {
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
} from "../utils/saveSortMigrationStore";
import { reconcileStaleShortcuts, requestSyncCancel, isCancelRequested, resetSyncCancel } from "../utils/syncManager";
import { useConnectionProbe } from "../utils/connectionProbe";
import type { BackendFailed, ConnectionFailure } from "../utils/connectionProbe";
import { retroDeckBanner, type RetroDeckBanner } from "../utils/retrodeckHealth";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { WarningCard } from "./WarningCard";
import { DownloadProgressRow } from "./DownloadProgressRow";
import { MigrationBlockedPage } from "./MigrationBlockedPage";
import { SettingsResetBanner } from "./SettingsResetBanner";
import { PlaytimeScopeBanner } from "./PlaytimeScopeBanner";
import { SessionBudgetBanner, formatGb, formatSignedGb, memoryLevelColor } from "./SessionBudgetBanner";
import type {
  SyncProgress,
  SyncStage,
  SyncStats,
  SyncPreview,
  SyncPreviewSummary,
  SessionBudgetStatus,
  MigrationStatus,
  Page,
} from "../types";
import { detach } from "../utils/detach";
import { wrapText } from "../utils/textStyles";

interface MainPageProps {
  onNavigate: (page: Exclude<Page, "main">) => void;
}

/** The connection-row label for a failed probe, mapped from the backend's
 *  `{reason, message}`. The two `config_error` sub-cases are split by message
 *  text (the slug is shared). Anything unclassified falls back to the generic
 *  "Not connected". `version_error` is included for completeness even though a
 *  version failure short-circuits the whole panel to the VersionErrorCard. */
function connectionFailureLabel(failure: ConnectionFailure | null | undefined): string {
  const reason = failure?.reason;
  const message = failure?.message ?? "";
  switch (reason) {
    case "auth_failed":
      return "Sign-in rejected";
    case "server_unreachable":
      return "Server unreachable";
    case "version_error":
      return "Unsupported RomM version";
    case "config_error":
      if (/server url/i.test(message)) return "No server URL";
      if (/not signed in/i.test(message)) return "Not signed in";
      return "Not connected";
    default:
      return "Not connected";
  }
}

/** Counted segments — ``[[count, word], …]`` → ``"353 new / 800 updated"``: every
 *  segment spells its word out, zero counts dropped, joined with `` / ``. Empty
 *  when every count is zero. The old ``+``/``~``/``−`` sigils were a legend the
 *  panel never carried — on-device they read as noise, not as counts. */
function countedSegments(pairs: [number, string][]): string {
  return pairs
    .filter(([n]) => n > 0)
    .map(([n, word]) => `${n} ${word}`)
    .join(" / ");
}

export const ConnectionIndicator: FC<{
  connected: boolean | null | BackendFailed;
  failure?: ConnectionFailure | null;
}> = ({ connected, failure }) => {
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
      <span style={{ fontSize: "12px" }}>{connectionFailureLabel(failure)}</span>
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
  // The bare fine-detail message. The coarse "step/totalSteps" is already shown
  // on the bar row (sync-step), so it is not repeated here. The message wraps to
  // up to two lines in the QAM via CSS rather than being clipped mid-word.
  if (!progress) return "Syncing...";
  return progress.message || "Syncing...";
}

// The fine-detail line is clamped to two lines (``WebkitLineClamp``) and its box
// is reserved at exactly that height up front. Without the reservation a message
// that wraps 1→2 lines (or shrinks 2→1 at a unit boundary) reflows the ETA row
// and Cancel button below it — the visible jolt of the residual boundary flicker.
// ``minHeight`` in ``em`` ties to the element's own 12px font (``wrapText``), so
// two 1.4-line-height lines reserve 2.8em with no magic pixel value.
const FINE_DETAIL_LINE_HEIGHT = 1.4;
const FINE_DETAIL_CLAMP_LINES = 2;

/** Wall-clock ``HH:MM`` for the "last attempt" hint; the raw ISO on a bad parse. */
function formatClockTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** The "Last sync" field value: the completed run's relative time on line 1,
 *  and (when a newer attempt did not complete) the attempt on a second
 *  right-aligned line — INSIDE the field, so the focus highlight covers both
 *  lines like the Library row's. Needs ``childrenContainerWidth="max"`` on the
 *  field: the default children column is too narrow and wrapped the attempt
 *  line mid-text. With no completed run ever, the cancelled/crashed attempt is
 *  surfaced as line 1 so it never reads a bare "Never" after thousands of
 *  games synced (#1367); otherwise "Never". */
function lastSyncValue(stats: SyncStats): ReactNode {
  if (stats.last_sync) {
    return (
      <span style={{ fontSize: "12px", display: "flex", flexDirection: "column", alignItems: "flex-end" }}>
        <span>{formatTimeAgo(stats.last_sync) ?? stats.last_sync}</span>
        {stats.last_attempt && (
          <span style={{ opacity: 0.6 }}>
            last attempt: {formatClockTime(stats.last_attempt.finished_at)} ({stats.last_attempt.status})
          </span>
        )}
      </span>
    );
  }
  if (stats.last_attempt) {
    return (
      <span style={{ fontSize: "12px" }}>
        {formatClockTime(stats.last_attempt.finished_at)} ({stats.last_attempt.status})
      </span>
    );
  }
  return <span style={{ fontSize: "12px" }}>Never</span>;
}

/**
 * The preview's change categories — e.g.
 * ``["Games: 353 new / 800 updated / 1200 removed", "Platforms: 2 new", "Collections: 2 new"]``.
 * Every segment spells out what happens to those games ("updated" = the shortcut
 * exists and gets rewritten, not recreated); a zero segment is omitted and a
 * wholly-unchanged category is dropped. Empty when nothing differs.
 */
function previewChangeSegments(s: SyncPreviewSummary): string[] {
  const categories: string[] = [];
  const games = countedSegments([
    [s.new_count, "new"],
    [s.changed_count, "updated"],
    [s.remove_count, "removed"],
  ]);
  if (games) categories.push(`Games: ${games}`);
  const p = s.platform_collection_diff;
  if (p?.has_changes) {
    const platforms = countedSegments([
      [p.added_count, "new"],
      [p.removed_count, "removed"],
    ]);
    if (platforms) categories.push(`Platforms: ${platforms}`);
  }
  const d = s.collection_diff;
  if (d?.has_changes) {
    const collections = countedSegments([
      [d.added.length, "new"],
      [d.removed.length, "removed"],
    ]);
    if (collections) categories.push(`Collections: ${collections}`);
  }
  return categories;
}

/**
 * True when every platform this run spans is being re-fetched AND re-applied —
 * the derived "Force Full Sync" signal (#1318). After Force Full Sync every
 * platform loses its completion stamp, so ``restamp_platform_count`` (unstamped
 * enabled platforms) equals ``sync_platform_count`` (platforms in the work
 * queue); and the recorded launch options are cleared, so the whole library
 * counts as ``changed`` (``changed_count > 0``). Both ride the preview summary,
 * so no new backend flag is needed. The ``changed_count`` leg is what separates
 * a force from a first-ever sync — a fresh install is all-unstamped too, but its
 * delta is pure ``new_count`` (nothing to "re-fetch"), so the odd wording is
 * suppressed there. A partial resume (only some platforms unstamped) reads
 * unequal; an absent count (older backend) is 0; both return false.
 */
function isFullResync(s: SyncPreviewSummary): boolean {
  const platforms = s.sync_platform_count ?? 0;
  return platforms > 0 && (s.restamp_platform_count ?? 0) === platforms && s.changed_count > 0;
}

/**
 * The change line — categories joined with `` · ``, each category unbreakable.
 * A category is a nowrap span, so a line break can only land on a `` · ``
 * separator: "Platforms: 2 new" never splits across two lines the way plain
 * text wrapping split it at the narrow QAM width. An empty shortcut delta with
 * pending cover work (#1386) names that work — the preview still proceeds to
 * Apply so the cover refreshes actually run; only a fully-empty preview falls
 * back to the unchanged message. When the delta is non-empty AND every platform
 * is being re-fetched (Force Full Sync, #1318), a context line above the
 * segments names the full re-sync so "Games: N updated" isn't read as a normal
 * incremental delta.
 */
const PreviewChanges: FC<{ summary: SyncPreviewSummary }> = ({ summary }) => {
  const segments = previewChangeSegments(summary);
  if (segments.length === 0) {
    const covers = summary.cover_refresh_count ?? 0;
    if (covers > 0) return <>No shortcut changes — {pluralize(covers, "cover update")}.</>;
    // An unstamped platform is complete but carries no completion stamp (#1416) —
    // a late-ack recovery, a pre-stamp-era install, or a zero-ROM platform: the
    // delta is empty, but the apply must still run once to re-stamp it and heal
    // the lingering "interrupted" status.
    if ((summary.restamp_platform_count ?? 0) > 0) return <>No changes — finishing a previous sync.</>;
    return <>Everything is up to date.</>;
  }
  return (
    <>
      {isFullResync(summary) && (
        <div data-testid="sync-full-resync" style={{ marginBottom: "2px", opacity: 0.8 }}>
          Full re-sync — all platforms re-fetched.
        </div>
      )}
      {segments.map((segment, i) => (
        <span key={segment}>
          {i > 0 ? " · " : ""}
          <span style={{ whiteSpace: "nowrap" }}>{segment}</span>
        </span>
      ))}
    </>
  );
};

/**
 * Informational scope line for the preview — "N platforms · M collections" — the
 * count of enabled platforms/collections the run spans, shown independent of the
 * change diffs (#29). Each part is omitted when its count is 0, so a
 * collections-only run reads "3 collections" and a platforms-only run "5
 * platforms". Empty when both counts are 0 (an older backend that omits them) —
 * the caller then shows the estimate alone rather than a misleading "0 platforms".
 */
function formatSyncScope(s: SyncPreviewSummary): string {
  const platforms = s.sync_platform_count ?? 0;
  const collections = s.sync_collection_count ?? 0;
  const parts: string[] = [];
  if (platforms > 0) parts.push(pluralize(platforms, "platform"));
  if (collections > 0) parts.push(pluralize(collections, "collection"));
  return parts.join(" · ");
}

/**
 * The Library row's one-line summary — "N games · M platforms · K collections" —
 * each part correctly singular/plural, zero parts omitted. Games is always
 * present (the row renders only when ``roms > 0``).
 */
function formatLibraryLine(stats: SyncStats): string {
  const parts = [pluralize(stats.roms, "game")];
  if (stats.platforms > 0) parts.push(pluralize(stats.platforms, "platform"));
  const collections = stats.collections ?? 0;
  if (collections > 0) parts.push(pluralize(collections, "collection"));
  return parts.join(" · ");
}

/** Preview apply-time (seconds) at/above which the hint appends the sleep-pause
 *  caveat. Below ~10 minutes a sync finishes fast enough that the sleep/resume
 *  note is noise rather than useful guidance; 10 min = 600 s. */
const LONG_SYNC_HINT_THRESHOLD_SEC = 600;

/**
 * Thin horizontal rule dividing the panel's blocks (status | sync | menu).
 * The panel carries no section headings — these rules are the only block
 * boundaries.
 */
const BlockSeparator: FC = () => (
  <PanelSectionRow>
    <div data-testid="block-separator" style={{ height: "1px", backgroundColor: "rgba(255, 255, 255, 0.12)" }} />
  </PanelSectionRow>
);

/** The affirmative green — matches the connection checkmark and the healthy
 *  memory level — used only for a cleanly-finished sync's status line. */
const STATUS_SUCCESS_COLOR = "#59bf40";

/** How long the transient status line lingers before auto-clearing. Kept long
 *  enough to still be readable after a glance away from a just-finished sync. */
const STATUS_CLEAR_MS = 15000;

/** Tone of the transient status line. Only a clean sync finish is affirmative
 *  (green); a cancel/error/other keeps the neutral panel-text look. */
type StatusTone = "success" | "neutral";

interface TransientStatus {
  text: string;
  tone: StatusTone;
}

/** A terminal sync stage's status tone — green only on a clean finish; a
 *  cancel or error stays neutral so green never reads as "all good". */
function terminalStatusTone(stage: SyncProgress["stage"]): StatusTone {
  return stage === "done" ? "success" : "neutral";
}

export const MainPage: FC<MainPageProps> = ({ onNavigate }) => {
  const [stats, setStats] = useState<SyncStats | null>(null);
  const [budgetStatus, setBudgetStatus] = useState<SessionBudgetStatus | null>(null);
  // `failure` classifies a resolved-but-failed probe so the connection row can
  // show a specific label (auth rejected / server unreachable / no URL / not
  // signed in). Null for every non-failed state and for a probe that never
  // resolved. The probe itself lives outside this component so a QAM close does
  // not abandon a run that has not reached a verdict yet.
  const { connected, failure: connectionFailure } = useConnectionProbe();
  const versionError = useVersionError();
  const [syncing, setSyncing] = useState(false);
  // Disarmed "Cancelling…" state during the backend's RUNNING→CANCELLING→IDLE
  // drain. The Sync/Cancel button stays disabled until the terminal
  // sync_progress stage re-arms it, so a quick re-press can't hit the
  // sync_in_progress reject and look like an instant finish (#1202, RC-B).
  const [cancelling, setCancelling] = useState(false);
  const [syncProgress, setSyncProgress] = useState<SyncProgress | null>(null);
  // Last non-empty fine-detail line, carried across unit-boundary anchor frames
  // so the fine-detail row (and its inline spinner) stay MOUNTED when the next
  // unit's FETCHING anchor frame resets current/total to 0 (#1415) — otherwise
  // the row unmounts for a frame and the panel flickers. Populated by the store
  // subscriber from any frame with real fine detail; reset to null when the run
  // ends, so a terminal/idle state never surfaces a stale line.
  const [carriedFineDetail, setCarriedFineDetail] = useState<string | null>(null);
  // A dumb mirror of syncEta's live countdown (seconds), or null when not measured
  // yet / between runs. syncEta owns the sticky deadline; the impure now-read that
  // resolves it to seconds lives in the store subscriber (an event handler), NOT
  // the render — the render must stay pure. Progress frames drive the subscriber,
  // so the countdown ticks per frame exactly as before.
  const [liveEtaDisplay, setLiveEtaDisplay] = useState<number | null>(null);
  const [status, setStatus] = useState<TransientStatus | null>(null);
  const [preview, setPreview] = useState<SyncPreview | null>(null);
  const [skipPreview, setSkipPreview] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retroarchWarning, setRetroarchWarning] = useState<{ warning: boolean; current?: string } | null>(null);
  const [retrodeckBanner, setRetrodeckBanner] = useState<RetroDeckBanner | null>(null);
  const [migration, setMigration] = useState<MigrationStatus>(getMigrationState());
  const [settingsReset, setSettingsReset] = useState(getSettingsResetState());
  const [playtimeScope, setPlaytimeScope] = useState(getPlaytimeScopeState());
  const [saveSortMigration, setSaveSortMigration] = useState(getSaveSortMigrationState());
  const downloads = useDownloads();
  const statusTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showTransientStatus = (text: string, tone: StatusTone = "neutral") => {
    if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    setStatus({ text, tone });
    statusTimeoutRef.current = setTimeout(() => setStatus(null), STATUS_CLEAR_MS);
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
    // Live renderer-heap reading for the session-budget banners (#1383). Fail-open:
    // the backend always resolves (rss_kb null when unreadable), so the banners
    // degrade to text-only rather than erroring.
    getSessionBudgetStatus()
      .then(setBudgetStatus)
      .catch((e) => logError(`Failed to load session budget status: ${e}`));

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
    //
    // But the backend snapshot is COARSE mid-apply: the fine within-unit
    // counters (current/total/message) are advanced frontend-side per item and
    // never round-trip to the backend, and etaSeconds is frontend-computed from
    // sync_plan (never sent by the backend). A blind replace on remount would
    // wipe the fine line + ETA until the next chunk boundary. So when the
    // backend reports the SAME in-flight run the module store already tracks,
    // MERGE: keep the store's fine fields + etaSeconds, take the backend's
    // authoritative running/stage/runId. Run identity is compared via runId when
    // both sides carry it; when the backend is idle or the runs differ, keep the
    // replace behavior (the store holds nothing worth preserving).
    getSyncStatus()
      .then((backendProgress) => {
        const stored = getSyncProgress();
        const sameRun = backendProgress.runId && stored.runId ? backendProgress.runId === stored.runId : true;
        const isSameLiveRun = backendProgress.running && stored.running && sameRun;
        // Same live run: spread the store (keeping its fine fields + etaSeconds)
        // and overlay the backend's authoritative running/stage/runId. The
        // conditional spreads keep the optional stage/runId out when the backend
        // omits them (exactOptionalPropertyTypes). One exception: "applying" is
        // frontend-authoritative (the backend never emits it — its last frame is
        // the fetch anchor), so a stored applying stage survives the seed; taking
        // the backend's stale "fetching" would drop the coarse-bar interpolation
        // and flip the label until the next per-item update. Otherwise replace
        // wholesale.
        const backendStage = stored.stage === "applying" ? undefined : backendProgress.stage;
        const progress: SyncProgress = isSameLiveRun
          ? {
              ...stored,
              running: backendProgress.running,
              ...(backendStage !== undefined ? { stage: backendStage } : {}),
              ...(backendProgress.runId !== undefined ? { runId: backendProgress.runId } : {}),
            }
          : backendProgress;
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
      // The local mirror must update FIRST and unconditionally — it is what
      // drives the re-render. Everything after it is derived work (terminal
      // teardown, estimator feeding, ETA state) that must never be able to break
      // the re-render chain (on-device freeze, cause not yet reproduced in tests).
      const progress = getSyncProgress();
      setSyncProgress(progress);
      // Carry the fine-detail line so the row survives a unit boundary's anchor
      // frame (which resets current/total, #1415); drop it the moment the run
      // ends so the next run starts clean. Kept outside the try below so the
      // reset can never be skipped by a subscriber throw.
      //
      // The carry is refreshed by any running frame that has a message AND a
      // position: a real fine-detail frame (``total`` > 0) OR a unit-boundary
      // FETCHING anchor (``total`` 0 but ``totalSteps`` > 0). At the boundary the
      // anchor's own message ("Fetching <next unit>") REPLACES the previous
      // unit's carried text, so the fine line names the new unit immediately
      // instead of lagging on the old one until the next real frame lands a
      // network RTT later. The initial optimistic "Fetching library…" start (no
      // total, no totalSteps) is excluded, so it keeps the stage-label spinner;
      // an empty-message frame never clears the carry (replace, never remove).
      if (!progress.running) {
        setCarriedFineDetail(null);
      } else if (progress.message && (progress.total || progress.totalSteps)) {
        setCarriedFineDetail(progress.message);
      }
      try {
        if (isTerminalStage(progress.stage)) {
          // Tear down the run's live-ETA state (deadline included) so the next run
          // measures fresh, and clear the display mirror.
          resetEta();
          setLiveEtaDisplay(null);
          setSyncing(false);
          setLoading(false);
          // True terminal reached — re-arm the button out of any "Cancelling…"
          // drain state (#1202, RC-B).
          setCancelling(false);
          showTransientStatus(progress.message || "Sync finished", terminalStatusTone(progress.stage));
          getSyncStats()
            .then(setStats)
            .catch((e) => logError(`Failed to refresh sync stats: ${e}`));
          // Refresh the live heap reading so the paused / high-heap banner reflects
          // the run's end state (a pause leaves it high; a completed run may too).
          getSessionBudgetStatus()
            .then(setBudgetStatus)
            .catch((e) => logError(`Failed to refresh session budget status: ${e}`));
        } else {
          // Feed the live-rate estimator from applying frames that carry ITEM
          // progress only — fetch frames carry page/cover counters, and an
          // applying-stage cover-refresh frame (``coverRefresh``, #1456) carries a
          // cover counter, not item progress, so both must be skipped. syncEta
          // re-anchors its sticky deadline internally; then mirror the current
          // countdown into state here — the impure now-read must live in this
          // subscriber (an event handler), never in render, which must stay pure.
          if (
            progress.stage === "applying" &&
            progress.step !== undefined &&
            progress.current !== undefined &&
            !progress.coverRefresh
          ) {
            observeApplyProgress(progress.step, progress.current, Date.now());
          }
          setLiveEtaDisplay(displayedEtaSeconds(Date.now()));
        }
      } catch (e) {
        logError(`sync-progress subscriber failed: ${e}`);
      }
    });

    const unsubMigration = onMigrationChange(() => setMigration(getMigrationState()));
    const unsubSettingsReset = onSettingsResetChange(() => setSettingsReset(getSettingsResetState()));
    const unsubPlaytimeScope = onPlaytimeScopeChange(() => setPlaytimeScope(getPlaytimeScopeState()));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortMigration(getSaveSortMigrationState()));
    return () => {
      unsubProgress();
      unsubMigration();
      unsubSettingsReset();
      unsubPlaytimeScope();
      unsubSaveSort();
      if (statusTimeoutRef.current) clearTimeout(statusTimeoutRef.current);
    };
  }, []);

  // Poll the live renderer-heap reading while it can still change: during a sync (so
  // the "Steam memory" row tracks the climbing RSS mid-apply) AND while the last run
  // is paused (so the paused banner notices once a Steam restart frees memory and
  // ``resume_ready`` flips — otherwise it sits stale after the restart). One dumb
  // interval, faster during a sync than while merely waiting for a restart; torn down
  // when neither condition holds or on unmount.
  const lastRunPaused = stats?.last_attempt?.status === "paused";
  useEffect(() => {
    if (!syncing && !lastRunPaused) return;
    const id = setInterval(
      () => {
        getSessionBudgetStatus()
          .then(setBudgetStatus)
          .catch((e) => logError(`Failed to poll session budget status: ${e}`));
        // Belt-and-braces on top of the backend emit-last fix (#39): while the paused
        // banner is showing (idle), also re-read stats so the "Last sync" line + the
        // paused banner (which keys on last_attempt) recover if the one-shot terminal
        // refetch was ever missed/dropped. Then this poll self-stops (last_attempt is
        // no longer paused).
        if (!syncing) {
          getSyncStats()
            .then(setStats)
            .catch((e) => logError(`Failed to poll sync stats: ${e}`));
        }
      },
      syncing ? 5000 : 10000,
    );
    return () => clearInterval(id);
  }, [syncing, lastRunPaused]);

  // A start/apply call never reached a running backend sync (rejected up
  // front or threw). Reset both the local UI and the MODULE store so the
  // store mirrors reality — the optimistic running:true must not linger.
  const abortOptimisticSync = (msg: string) => {
    setStatus({ text: msg, tone: "neutral" });
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
    setStatus(null);
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
    // Seed the apply ETA from the walk cost (shared with the preview row via
    // previewApplySeconds) so the number the user approved is the run's seed. It
    // lands in the optimistic store write below, so the sync_plan listener sees an
    // etaSeconds already present and leaves its cruder total_roms bound off.
    const etaSeconds = previewApplySeconds(preview.summary);
    // Clear any stale cancel flag before the apply run starts (#1198). A Cancel
    // in the apply window reads "" from the sync_progress store until the
    // backend stamps the run id, which the backend treats as an unconditional
    // cancel (#1202).
    resetSyncCancel();
    setPreview(null);
    setLoading(true);
    setSyncing(true);
    setCancelling(false);
    setStoredSyncProgress({ running: true, stage: "applying", message: "Applying changes...", etaSeconds });
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
    setStatus(null);
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

  // Two-level progress. The main determinate bar tracks COARSE unit progress
  // but INTERPOLATES within the running unit so a large unit (e.g. 2091 items at
  // step 2/8) doesn't sit frozen: the bar fills from the step's floor toward the
  // next notch as the unit is worked. Notch positions come from the plan's
  // per-unit item weights when measured (#1382), else each unit is an equal
  // 1/totalSteps slice. 0/0 totalSteps means the run hasn't reached a unit yet →
  // indeterminate. ``nProgress`` is a percentage (0-100), not a fraction.
  //
  // While actively working a unit (``fetching``/``applying``) the current unit
  // is not yet done, so the completed count is ``step - 1``; the terminal-ish
  // stages (``finalizing``/``done``) carry ``step == totalSteps`` as a
  // completed count, so they keep the full ``step`` and the bar reads 100%.
  // The within-unit fill splits the running unit's width into three monotonic
  // sub-slices — fetch → covers → apply (#1407, ``withinUnitFraction``) — each
  // filling by its own phase's ``current/total`` within a strictly-higher band
  // than the phase before. So the fetch and cover frames now DO advance the bar
  // (within their own sub-slice), never backwards at a phase boundary even
  // though each phase restarts ``current/total`` from zero. An old backend that
  // sends no sub-stage falls back to resting at the unit floor during fetch.
  const step = syncProgress?.step ?? 0;
  const activeUnit = syncProgress?.stage === "fetching" || syncProgress?.stage === "applying";
  const completedSteps = activeUnit ? Math.max(0, step - 1) : step;
  const withinUnit = withinUnitFraction(syncProgress);
  // Weight the bar by the plan's per-unit item weights (#1382) — the same
  // skip-aware, delta-corrected weights the countdown uses — so a
  // predicted-skip unit takes no width and a huge platform takes its real
  // share — except a run's LEADING zero-weight units, which still refresh
  // covers and so claim an equal index slice rather than pinning the bar to
  // empty (#1506). The latched wrapper adds a run-scoped high-water floor so a
  // mid-run upward weight correction (observeUnitTotal on a mispredicted
  // trailing skip) can't retract shown width (#1509). Falls back to
  // equal-per-unit index weighting when no plan is measured (QAM opened mid-run
  // before any sync_plan, old backend) or the plan can't apportion (unit-count
  // mismatch, all-zero weights).
  const weightedFraction = syncProgress?.totalSteps
    ? latchedCoarseFraction(completedSteps, withinUnit, syncProgress.totalSteps)
    : null;
  const coarseFraction = syncProgress?.totalSteps
    ? Math.max(0, Math.min(100, (weightedFraction ?? (completedSteps + withinUnit) / syncProgress.totalSteps) * 100))
    : undefined;
  const currentHasFineDetail = !!(syncProgress?.total && syncProgress.message);
  // Keep the fine-detail row mounted across unit boundaries: the next unit's
  // FETCHING anchor frame resets current/total (#1415), so fall back to the last
  // non-empty fine detail carried by the store subscriber. Cleared when the run
  // ends, so terminal/idle states never surface a stale line. The bar's own
  // within-unit fill still reads the live current/total (never the carry), so
  // this affects only which rows mount, not the bar (#1407).
  const hasFineDetail = currentHasFineDetail || carriedFineDetail !== null;
  const fineDetailText = currentHasFineDetail ? formatProgressText(syncProgress) : (carriedFineDetail ?? "");

  // Estimated-time readout for the in-flight run. Prefer the live measured
  // countdown ("9 min left") once the estimator has a rate; before that, fall
  // back to the static seed carried on the store as an upper bound ("up to
  // X min"). Absent both, the row is omitted (honest silence).
  const staticEtaSeconds = syncProgress?.etaSeconds;
  let etaText: string | null = null;
  if (liveEtaDisplay !== null) {
    etaText = formatEtaCountdown(liveEtaDisplay);
  } else if (staticEtaSeconds !== undefined) {
    etaText = `up to ${formatDuration(staticEtaSeconds)}`;
  }

  const activeDownloads = downloads.filter((d) => d.status === "queued" || d.status === "downloading");
  const completedDownloads = downloads.filter(
    (d) => d.status === "completed" || d.status === "failed" || d.status === "cancelled",
  );
  const hasDownloads = activeDownloads.length > 0 || completedDownloads.length > 0;

  // Sync is unavailable when the server test failed OR the plugin backend never
  // started — both gate the Sync buttons off.
  const connectionUnavailable = connected === false || connected === "backend_failed";

  // The stamp/chunk sync model makes every re-sync an effective resume: a
  // cancelled or interrupted run's committed chunks survive on disk, so the next
  // run's incremental skip picks up where it stopped. ``last_attempt`` is
  // non-null exactly when the newest terminal run did NOT complete. "errored"
  // stays "Sync Library": an errored run often fails before applying anything
  // (e.g. a config error), so "resume" isn't the right mental model. A completed
  // sync clears last_attempt on the stats refresh, flipping the label back.
  //
  // But "resume" only holds while partial progress actually exists on disk. If
  // the user removed all shortcuts after an incomplete run (e.g. DangerZone
  // "remove all"), there are zero bound shortcuts and the next run is a full
  // fresh import — nothing to resume — so the button must honestly read "Sync
  // Library" again. ``stats.roms`` is the bound-shortcut count (registry-derived).
  const incompleteAttempt =
    stats?.last_attempt?.status === "interrupted" ||
    stats?.last_attempt?.status === "cancelled" ||
    stats?.last_attempt?.status === "paused";
  // ``incompleteAttempt`` being true narrows ``stats`` non-null (it dereferenced
  // stats.last_attempt), and ``roms`` is a required number — no ``?.``/``??`` needed.
  const canResume = incompleteAttempt && stats.roms > 0;
  const syncButtonLabel = canResume ? "Resume Sync" : "Sync Library";

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
      !!(preview.summary.collection_diff?.added.length || preview.summary.collection_diff?.removed.length) ||
      preview.summary.platform_collection_diff?.has_changes ||
      // Cover-only work (#1386): the refresh pass runs inside the apply, so an
      // empty shortcut delta with pending cover updates must still offer Apply —
      // the old "no changes" short-circuit stranded changed covers forever.
      (preview.summary.cover_refresh_count ?? 0) > 0 ||
      // Unstamped platforms (#1416): a late-ack-recovered platform needs a
      // 0-delta apply run to re-stamp itself and heal the lingering
      // "interrupted" status, so offer Apply even when every change count is zero.
      (preview.summary.restamp_platform_count ?? 0) > 0;
    // Walk cost, shared with the handleApply seed (previewApplySeconds) so the
    // approved number equals the run's seed. Delta-only pricing here read "2 min"
    // for a resume whose apply walked ~3100 items.
    const applySeconds = previewApplySeconds(preview.summary);
    const estimateText = formatDuration(applySeconds);
    // Coverage and duration each own a line — at the QAM width they wrapped as
    // one row anyway, and the break landed mid-phrase. An older backend that
    // omits the scope counts leaves scopeText empty; the duration line then
    // stands alone.
    const scopeText = formatSyncScope(preview.summary);
    // The sleep-pause caveat is only worth the extra line for a genuinely long run.
    const hintText =
      "Progress is saved about every 200 games — cancelling is safe." +
      (applySeconds >= LONG_SYNC_HINT_THRESHOLD_SEC ? " Long syncs pause during sleep; keep the Deck powered." : "");
    syncBody = (
      <>
        {/* One block: WHAT changes, then what the run covers and how long — the
            coverage/estimate and the progress-is-saved hint describe the run the
            Apply button would start, so with an empty delta only "Everything is
            up to date." + Dismiss stand alone. */}
        <PanelSectionRow>
          <Field
            label="Changes"
            description={
              <>
                <div data-testid="sync-changes">
                  <PreviewChanges summary={preview.summary} />
                </div>
                {hasChanges && scopeText && (
                  <div data-testid="sync-scope" style={{ marginTop: "4px" }}>
                    Syncing {scopeText}
                  </div>
                )}
                {hasChanges && (
                  <div data-testid="sync-estimate" style={{ marginTop: scopeText ? undefined : "4px" }}>
                    Estimated duration: {estimateText}
                  </div>
                )}
              </>
            }
            focusable={true}
            bottomSeparator="none"
          />
        </PanelSectionRow>
        {hasChanges && (
          <PanelSectionRow>
            <Focusable>
              <div style={{ fontSize: "12px", color: "rgba(255, 255, 255, 0.6)", padding: "4px 0" }}>{hintText}</div>
            </Focusable>
          </PanelSectionRow>
        )}
        {preview.pause_likely ? (
          <PanelSectionRow>
            <Focusable>
              <div
                data-testid="budget-advisory"
                style={{
                  fontSize: "12px",
                  color: "#7fbcff",
                  borderLeft: "3px solid rgba(61, 157, 246, 0.6)",
                  paddingLeft: "8px",
                  margin: "4px 0",
                  lineHeight: 1.4,
                }}
              >
                Will likely pause partway to protect Steam&apos;s memory — normal for large syncs. Restart Steam when
                prompted, then Resume Sync.
              </div>
            </Focusable>
          </PanelSectionRow>
        ) : null}
        {hasChanges ? (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                bottomSeparator="none"
                onClick={() => {
                  detach(handleApply());
                }}
              >
                Apply Sync
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                bottomSeparator="none"
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
              bottomSeparator="none"
              onClick={() => {
                detach(handleDismiss());
              }}
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
              spans the full panel width. NOT focusable: the syncing rows sit
              between the focusable status rows and the Cancel button, so they
              are always in view — a focus highlight here only fakes
              interactivity. */}
          <div style={{ width: "100%" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: "12px",
                marginBottom: "4px",
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                {/* During silent phases (the initial "Fetching library" anchor
                    frame, before any narrated fine-detail page frame) there is
                    no fine line to carry a spinner, so the panel looks hung.
                    Show the spinner inline with the stage label so a running
                    sync always has motion. When the fine line is present it
                    already has its own spinner — don't show two. */}
                {!hasFineDetail && <Spinner width={14} height={14} />}
                <span data-testid="sync-stage">{stageLabel(syncProgress?.stage)}</span>
              </span>
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
              bottomSeparator="none"
              label={
                <div style={{ display: "flex", alignItems: "flex-start", gap: "8px" }}>
                  <Spinner width={14} height={14} />
                  {/* Wrap the narrated messages ("Fetching Game Boy Advance
                      (page 4/62)") on word boundaries instead of clipping them
                      mid-parenthesis (shared wrap rule). The clamp caps this
                      live-updating line at two lines so a long platform name
                      can't grow the row unboundedly. */}
                  <span
                    data-testid="sync-fine"
                    style={{
                      ...wrapText,
                      display: "-webkit-box",
                      WebkitLineClamp: FINE_DETAIL_CLAMP_LINES,
                      WebkitBoxOrient: "vertical",
                      overflow: "hidden",
                      lineHeight: FINE_DETAIL_LINE_HEIGHT,
                      // Reserve the full two-line clamp box so a 1↔2-line wrap
                      // change never reflows the ETA row / Cancel button below.
                      minHeight: `${FINE_DETAIL_CLAMP_LINES * FINE_DETAIL_LINE_HEIGHT}em`,
                    }}
                  >
                    {fineDetailText}
                  </span>
                </div>
              }
            />
          </PanelSectionRow>
        )}
        {etaText !== null && (
          <PanelSectionRow>
            <Field label="Estimated time" bottomSeparator="none">
              <span data-testid="estimate-time" style={{ fontSize: "12px" }}>
                {etaText}
              </span>
            </Field>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            bottomSeparator="none"
            disabled={cancelling}
            onClick={() => {
              detach(handleCancel());
            }}
          >
            {cancelling ? "Cancelling…" : "Cancel Sync"}
          </ButtonItem>
        </PanelSectionRow>
      </>
    );
  } else {
    syncBody = (
      <>
        {/* Persistent session-budget banner (#1383): blue while the last run was
            paused (restart Steam, then Resume Sync), or yellow when the live heap
            is high after a completed run. Only in the idle state, so it clears the
            moment a resume/new sync starts. */}
        <SessionBudgetBanner
          lastAttemptStatus={stats?.last_attempt?.status}
          rssKb={budgetStatus?.rss_kb ?? null}
          resumeReady={budgetStatus?.resume_ready ?? null}
          restartDisabled={loading || connectionUnavailable}
          runDoneItems={budgetStatus?.run_done_items ?? null}
          runTotalItems={budgetStatus?.run_total_items ?? null}
        />
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            bottomSeparator="none"
            onClick={() => {
              detach(handleSync());
            }}
            disabled={loading || connectionUnavailable}
          >
            {syncButtonLabel}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField label="Skip Preview" checked={skipPreview} onChange={setSkipPreview} bottomSeparator="none" />
        </PanelSectionRow>
        {/* Visible whenever ANY terminal run is recorded — a completed run OR a
            cancelled/interrupted/errored attempt. A resume (last_attempt set,
            last_sync null) is exactly when the user may want a forced fresh
            start, so gating on last_sync alone would hide the button in a
            resume situation. Still hidden on a pristine install (neither
            recorded). Pressing it clears the per-platform stamps + recorded
            launch options (arming a full re-fetch + re-apply) but PRESERVES the
            run history (#1318), so the Last-sync line and this button both stay
            put; the button is idempotent — pressing it again just re-clears the
            already-cleared stamps. The stats refresh below keeps the display
            truthful rather than blanking it to "Never". */}
        {(stats?.last_sync || stats?.last_attempt) && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              bottomSeparator="none"
              description="Clear cached sync data to re-fetch all platforms"
              onClick={() => {
                detach(
                  (async () => {
                    try {
                      const result = await clearSyncCache();
                      showTransientStatus(result.message);
                    } catch {
                      showTransientStatus("Failed to clear sync cache");
                    }
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
      {/* Untitled status block (Connection / Last sync / Library / Steam memory)
          leads the panel — the following PanelSection titles provide the block
          breaks, so no "Status" title is needed. */}
      <PanelSection>
        {retrodeckBanner && (
          <PanelSectionRow>
            {/* WarningCard is shared with the game-detail context, so it carries no
                focusable child of its own — wrap it here (QAM-only) so gamepad focus
                can reach it. */}
            <Focusable>
              <WarningCard title={retrodeckBanner.title} message={retrodeckBanner.message} compact />
            </Focusable>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <Field
            label="Connection"
            focusable={true}
            bottomSeparator="none"
            description={
              connected === "backend_failed" ? "Plugin backend failed to start — check Decky logs." : undefined
            }
          >
            <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
              <ConnectionIndicator connected={connected} failure={connectionFailure} />
            </div>
          </Field>
        </PanelSectionRow>
        {stats && (
          <>
            <PanelSectionRow>
              <Field label="Last sync" focusable={true} bottomSeparator="none" childrenContainerWidth="max">
                {lastSyncValue(stats)}
              </Field>
            </PanelSectionRow>
            {stats.roms > 0 && (
              <PanelSectionRow>
                <Field
                  label="Library"
                  description={
                    <div style={{ width: "100%", textAlign: "right", fontSize: "12px" }}>
                      {formatLibraryLine(stats)}
                    </div>
                  }
                  focusable={true}
                  bottomSeparator="none"
                />
              </PanelSectionRow>
            )}
          </>
        )}
        {/* Steam renderer memory (#1383): the live RSS as an always-on info row,
            plus the last completed sync's signed growth. Omitted entirely when the
            reading is unavailable (rss_kb null) rather than shown as a blank. */}
        {budgetStatus?.rss_kb != null && (
          <PanelSectionRow>
            <Field label="Steam memory" focusable={true} bottomSeparator="none">
              <span data-testid="steam-memory" style={{ fontSize: "12px" }}>
                {/* Only the value gets traffic-light colouring (green/yellow/red),
                    driven by the payload thresholds; the delta stays muted. Both sit
                    on one line: "0.6 GB · last run +0.7". */}
                <span
                  data-testid="steam-memory-value"
                  style={{
                    color: memoryLevelColor(budgetStatus.rss_kb, budgetStatus.warn_kb, budgetStatus.ceiling_kb),
                  }}
                >
                  {formatGb(budgetStatus.rss_kb)}
                </span>
                {budgetStatus.memory_delta_kb != null && (
                  <span data-testid="steam-memory-delta" style={{ opacity: 0.6 }}>
                    {" · last run "}
                    {formatSignedGb(budgetStatus.memory_delta_kb)}
                  </span>
                )}
              </span>
            </Field>
          </PanelSectionRow>
        )}
        {retroarchWarning?.warning && (
          <PanelSectionRow>
            <Field
              label="RetroArch: input_driver issue"
              description={`Using "${retroarchWarning.current}"`}
              bottomSeparator="none"
            >
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
              >
                Fix
              </DialogButton>
            </Field>
          </PanelSectionRow>
        )}
        {saveSortMigration.pending && (
          <>
            <PanelSectionRow>
              <Focusable>
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
              </Focusable>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" bottomSeparator="none" onClick={() => onNavigate("settings")}>
                Go to Settings
              </ButtonItem>
            </PanelSectionRow>
          </>
        )}
        <BlockSeparator />
      </PanelSection>

      <PanelSection>
        {syncBody}
        {status?.text && !syncing && !preview && (
          <PanelSectionRow>
            <Field
              label={
                <span
                  data-testid="sync-status"
                  style={{
                    ...wrapText,
                    ...(status.tone === "success" ? { color: STATUS_SUCCESS_COLOR } : {}),
                  }}
                >
                  {status.text}
                </span>
              }
              focusable={true}
              bottomSeparator="none"
            />
          </PanelSectionRow>
        )}
        <BlockSeparator />
      </PanelSection>

      {hasDownloads && (
        <PanelSection>
          {activeDownloads.slice(0, 2).map((item) => (
            <DownloadProgressRow
              key={item.rom_id}
              caption={item.rom_name}
              bytesDownloaded={item.bytes_downloaded}
              totalBytes={item.total_bytes}
            />
          ))}
          {activeDownloads.length > 2 && (
            <PanelSectionRow>
              <Field
                label={`+${activeDownloads.length - 2} more downloading`}
                focusable={true}
                bottomSeparator="none"
              />
            </PanelSectionRow>
          )}
          {completedDownloads.length > 0 && (
            <PanelSectionRow>
              {/* Self-describing — the downloads block carries no heading, so a
                  bare "1 completed" floats without context. */}
              <Field
                label={`${pluralize(completedDownloads.length, "download")} completed`}
                focusable={true}
                bottomSeparator="none"
              />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" bottomSeparator="none" onClick={() => onNavigate("downloads")}>
              View All
            </ButtonItem>
          </PanelSectionRow>
          <BlockSeparator />
        </PanelSection>
      )}

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" bottomSeparator="none" onClick={() => onNavigate("library")}>
            Library
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" bottomSeparator="none" onClick={() => onNavigate("system")}>
            System
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" bottomSeparator="none" onClick={() => onNavigate("settings")}>
            Settings
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" bottomSeparator="none" onClick={() => onNavigate("data")}>
            Data Management
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
