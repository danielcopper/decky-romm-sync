import { useEffect, useRef, useState, FC, Fragment } from "react";
import { toaster } from "@decky/api";
import {
  ButtonItem,
  DialogButton,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  ProgressBar,
  ToggleField,
  showModal,
} from "@decky/ui";
import {
  cancelPrune,
  getPrunePreview,
  startPrune,
  stagePruneInstalledSelection,
  logError,
  logInfo,
  logWarn,
  type PrunePreviewItem,
  type PrunePreviewRequest,
  type PrunePreviewResult,
  type PruneScope,
} from "../api/backend";
import { detach } from "../utils/detach";
import {
  beginPrunePreview,
  beginPruneRun,
  getPruneState,
  isPruneResultLost,
  onPruneStateChange,
  type PruneProgress,
} from "../utils/pruneStore";
import { scrollNearestToTop } from "../utils/scrollHelpers";
import { getSyncProgress, onSyncProgressChange } from "../utils/syncProgress";
import { withTimeout } from "../utils/withTimeout";

const PAGE_SIZE = 50;
const SELECTION_PAGE_SIZE = 100;
const PRUNE_CALLABLE_TIMEOUT_MS = 15000;
const RESULT_LOST_MESSAGE = "The cleanup result was lost — check your library and run the scan again.";
/** What Cancel can and cannot promise — the running group is never rolled back. */
const CANCEL_HINT = "Stops before the next game. The one being processed now finishes and reports what it changed.";
/** Shown once Stop was pressed, so a second press never looks necessary. */
const CANCELLING_HINT = "Stopping — finishing the current safe step, then reporting what changed.";

/**
 * Ask the backend to stop `runId`. Returns the message to surface, or null when
 * the request was accepted — the run's own terminal frame reports the outcome,
 * so a success here must not overwrite it with chatter.
 */
async function requestPruneCancel(runId: string): Promise<string | null> {
  logInfo(`[prune] Cancel pressed for run ${runId}`);
  try {
    const result = await withTimeout(cancelPrune(runId), PRUNE_CALLABLE_TIMEOUT_MS);
    if (!result.success) {
      logWarn(`[prune] cancelPrune refused: reason=${result.reason ?? "none"}`);
      return result.message;
    }
    logInfo(`[prune] cancelPrune accepted for run ${runId}`);
    return null;
  } catch (e) {
    logError(`[prune] cancelPrune threw for run ${runId}: ${e}`);
    return `Could not request cancellation: ${e}`;
  }
}

const plural = (count: number, singular: string, pluralForm: string): string => (count === 1 ? singular : pluralForm);

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

/**
 * The backend's stage slugs in the words the user reads — one entry per
 * `emit_progress` call site in `services/prune/executor.py`, and no others.
 * An unknown stage falls back to its slug with underscores opened up, so a new
 * backend stage degrades to something readable instead of vanishing; that
 * fallback is a safety net, not a licence to leave a real stage unmapped.
 *
 * Exported so a test can pin the key set against the backend's: an entry for a
 * stage that is never emitted hides a missing one behind it.
 */
export const STAGE_LABELS: Record<string, string> = {
  checking: "Checking with RomM",
  creating_recovery: "Backing up",
  recovery_sealed: "Backup complete",
  repointing: "Updating the Steam shortcut",
  removing_shortcut: "Removing the Steam shortcut",
  removing: "Removing local data",
  removed: "Done",
};

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replace(/_/g, " ");
}

/**
 * How full the bar is: the groups already behind the run, not the one in it.
 *
 * `current` names the group being worked on, so it is finished only once a later
 * frame moves past it. Counting it as done filled a one-group run before its
 * first byte moved, and a group publishes nothing during its longest phase — a
 * multi-hundred-megabyte content backup — so the bar sat at full for the whole
 * of it. Clamped: a frame that arrives late or out of order must not push the
 * fill past either end.
 */
function finishedGroupsPercent(current: number, total: number): number {
  return Math.min(100, Math.max(0, ((current - 1) / total) * 100));
}

/** Caption + bar for a running cleanup, shared by the modal and the Danger Zone. */
const CleanupProgress: FC<{ progress: PruneProgress }> = ({ progress }) => (
  <div style={{ width: "100%" }}>
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", fontSize: "12px" }}>
      <span style={{ overflowWrap: "anywhere" }}>
        {stageLabel(progress.stage)} — {progress.name}
      </span>
      <span style={{ flexShrink: 0, marginLeft: "8px" }}>
        {progress.current} / {progress.total}
      </span>
    </div>
    <ProgressBar
      indeterminate={progress.total <= 0}
      {...(progress.total > 0 ? { nProgress: finishedGroupsPercent(progress.current, progress.total) } : {})}
    />
  </div>
);

/**
 * The bar's last frame, rendered beside the run's summary.
 *
 * Full means the run is over, not that it succeeded — the summary next to it
 * says how it ended. Without it the running bar, which stops one group short of
 * full, would simply vanish when the terminal frame lands.
 */
const FinishedCleanupBar: FC = () => <ProgressBar nProgress={100} />;

/** How many rows a group actually removed, however the frame reported it. */
function removedInGroup(item: { removed_count?: number; removed_rom_ids?: number[] }): number {
  return item.removed_count ?? item.removed_rom_ids?.length ?? 0;
}

function requestFor(
  scope: PruneScope,
  romId: number | null,
  previewId: string | null,
  offset: number,
): PrunePreviewRequest {
  return { scope, rom_id: romId, preview_id: previewId, offset, limit: PAGE_SIZE };
}

/**
 * Why Confirm is unavailable, in the user's words. Several of these conditions
 * already render a warning somewhere in the dialog, but the dialog scrolls —
 * a greyed button with its explanation off-screen reads as a dead control, and
 * a press that does nothing at all is indistinguishable from a broken plugin.
 */
function confirmBlockedReason(state: {
  completed: boolean;
  runInFlight: boolean;
  allEntriesLoaded: boolean;
  total: number;
  destructiveConfirmed: boolean;
  insufficientSpace: boolean;
  unknownSelectedSize: boolean;
  anyOptionChosen: boolean;
}): string | null {
  if (state.completed) return null;
  if (state.runInFlight) return "A cleanup is already running.";
  if (!state.allEntriesLoaded) return `Load all ${state.total} entries before confirming.`;
  if (!state.destructiveConfirmed) return "Confirm you understand there will be no recovery bundle.";
  if (state.insufficientSpace) {
    return state.unknownSelectedSize
      ? "A selected ROM's size can't be measured, so the recovery bundle can't be guaranteed to fit."
      : "The selected ROM content doesn't fit in the free space at the recovery target.";
  }
  if (!state.anyOptionChosen) return "Choose at least one cleanup option above.";
  return null;
}

/**
 * Stage the installed-content opt-ins page by page, chaining each page onto the
 * selection the previous one opened. A refused page reports itself and stops the
 * whole Confirm — a partially staged selection must never reach the run.
 *
 * Exported so a test can pin the page boundary directly: reaching it through the
 * dialog needs one rendered row and one click per selected id, which makes the
 * cost of proving it grow with the boundary it is proving.
 */
export async function stageInstalledSelections(
  previewId: string,
  selected: number[],
  setStatus: (message: string) => void,
): Promise<{ ok: true; selectionId: string | null } | { ok: false }> {
  let selectionId: string | null = null;
  for (let offset = 0; offset < selected.length; offset += SELECTION_PAGE_SIZE) {
    const page = selected.slice(offset, offset + SELECTION_PAGE_SIZE);
    const staged: {
      success: boolean;
      selection_id?: string;
      message?: string;
    } = await withTimeout(
      stagePruneInstalledSelection({
        preview_id: previewId,
        selection_id: selectionId,
        rom_ids: page,
        final: offset + page.length >= selected.length,
      }),
      PRUNE_CALLABLE_TIMEOUT_MS,
    );
    if (!staged.success || !staged.selection_id) {
      setStatus(staged.message ?? "Installed-content selections could not be staged.");
      logWarn(`[prune] Confirm aborted while staging installed content: ${staged.message ?? "no message"}`);
      return { ok: false };
    }
    selectionId = staged.selection_id;
  }
  return { ok: true, selectionId };
}

/** What the Confirm button says: pressed, then the run's current stage, then its resting label. */
function confirmButtonLabel(starting: boolean, progress: PruneProgress | null): string {
  if (starting) return "Starting...";
  if (progress) return `${progress.stage.replace(/_/g, " ")}...`;
  return "Confirm Cleanup";
}

/** Why the scan button is unavailable, or nothing when it is not. */
function scanButtonDescription(syncRunning: boolean, runActive: boolean): string | undefined {
  if (syncRunning) return "Unavailable while a library sync is running.";
  if (runActive) return "A cleanup is running. Its progress is shown below.";
  return undefined;
}

interface CleanupModalProps {
  initial: PrunePreviewResult;
  scope: PruneScope;
  romId: number | null;
  closeModal?: () => void;
}

const CleanupModal: FC<CleanupModalProps> = ({ initial, scope, romId, closeModal }) => {
  const [items, setItems] = useState<PrunePreviewItem[]>(initial.items ?? []);
  const [loadingMore, setLoadingMore] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelRequestedFor, setCancelRequestedFor] = useState<string | null>(null);
  const [runStarted, setRunStarted] = useState(false);
  const [status, setStatus] = useState("");
  const [repoint, setRepoint] = useState(true);
  const [removeRows, setRemoveRows] = useState(true);
  const [removeDeadGames, setRemoveDeadGames] = useState(true);
  const [recovery, setRecovery] = useState(true);
  const [confirmWithoutRecovery, setConfirmWithoutRecovery] = useState(false);
  const [includedContent, setIncludedContent] = useState<Set<number>>(new Set());
  const [freeBytes, setFreeBytes] = useState(initial.free_bytes ?? 0);
  const [pruneState, setPruneState] = useState(getPruneState());
  const [resultLost, setResultLost] = useState(isPruneResultLost());

  useEffect(() => {
    const unsubscribe = onPruneStateChange(() => {
      setPruneState(getPruneState());
      setResultLost(isPruneResultLost());
    });
    return () => {
      unsubscribe();
    };
  }, []);

  const total = initial.total ?? items.length;
  // The headline count is the rows this run can remove on its own. `total` also
  // counts the siblings that are merely disclosed because a whole-game removal
  // could still take them, and leading with that number reads as a threat to
  // versions RomM still serves.
  const candidateTotal = initial.candidate_total ?? total;
  // A row that is not a candidate can only ever be deleted by a whole-game
  // removal (`selected_prune_ids` returns nothing else for it), so with that
  // option off it is disclosing a thing that cannot happen. Every page is still
  // fetched and the wire payload is untouched — this is what gets rendered.
  const visibleItems = removeDeadGames ? items : items.filter((item) => item.candidate);
  const selectedBytes = visibleItems.reduce(
    (sum, item) => sum + (includedContent.has(item.rom_id) ? (item.installed_bytes ?? 0) : 0),
    0,
  );
  const unknownSelectedSize = visibleItems.some(
    (item) => includedContent.has(item.rom_id) && item.installed && item.installed_bytes === null,
  );
  const insufficientSpace = recovery && (unknownSelectedSize || selectedBytes > freeBytes);
  const destructiveConfirmed = recovery || confirmWithoutRecovery;
  const allEntriesLoaded = items.length === total;
  const progress = pruneState.progress;
  const complete = pruneState.complete;
  const runInFlight = complete === null && (starting || pruneState.runId !== null);
  // Derived from the RUN's lifecycle, never the cancel callable's: the callable
  // resolving means the request was received, not that the run has stopped.
  // Tying the lock to it let seven presses through in three seconds (#1570 F19).
  // The exits are the terminal frame (complete), the run going away in the
  // store, and an outright refusal — the backend's idempotency is the safety
  // net behind that, not the mechanism.
  const cancelling = complete === null && cancelRequestedFor !== null && cancelRequestedFor === pruneState.runId;
  const blockedReason = confirmBlockedReason({
    completed: complete !== null,
    runInFlight,
    allEntriesLoaded,
    total,
    destructiveConfirmed,
    insufficientSpace,
    unknownSelectedSize,
    anyOptionChosen: repoint || removeRows || removeDeadGames,
  });
  // Disabled ONLY where a press is unsafe or meaningless: a run is already
  // going, or this dialog is showing a finished one. Every other reason to
  // refuse is explained when pressed — a greyed button whose explanation the
  // user has to hunt for is the shape the silent no-op took.
  const pressBlocked = complete !== null || runInFlight;

  const toggleContent = (romIdToToggle: number, checked: boolean): void => {
    setIncludedContent((current) => {
      const next = new Set(current);
      if (checked) next.add(romIdToToggle);
      else next.delete(romIdToToggle);
      return next;
    });
  };

  const loadMore = async (): Promise<void> => {
    if (!initial.preview_id || items.length >= total) return;
    setLoadingMore(true);
    try {
      const next = await withTimeout(
        getPrunePreview(requestFor(scope, romId, initial.preview_id, items.length)),
        PRUNE_CALLABLE_TIMEOUT_MS,
      );
      if (!next.success) {
        setStatus(next.message ?? "Could not load more candidates.");
        return;
      }
      setItems((current) => [...current, ...(next.items ?? [])]);
      if (typeof next.free_bytes === "number") setFreeBytes(next.free_bytes);
    } catch (e) {
      setStatus(`Could not load more candidates: ${e}`);
    } finally {
      setLoadingMore(false);
    }
  };

  const refreshFreeSpace = async (): Promise<void> => {
    if (!initial.preview_id) return;
    try {
      const refreshed = await withTimeout(
        getPrunePreview({
          scope,
          rom_id: romId,
          preview_id: initial.preview_id,
          offset: 0,
          limit: 0,
        }),
        PRUNE_CALLABLE_TIMEOUT_MS,
      );
      if (!refreshed.success || typeof refreshed.free_bytes !== "number") {
        setStatus(refreshed.message ?? "Could not refresh recovery space.");
        return;
      }
      setFreeBytes(refreshed.free_bytes);
    } catch (e) {
      setStatus(`Could not refresh recovery space: ${e}`);
    }
  };

  const start = async (): Promise<void> => {
    // Confirm is the destructive commit point, so every press is logged and
    // every outcome is visible in the dialog. A press that returns silently is
    // indistinguishable on device from a plugin that has stopped responding.
    logInfo(`[prune] Confirm pressed (preview=${initial.preview_id ?? "none"}, scope=${scope}, total=${total})`);
    if (!initial.preview_id) {
      setStatus("This cleanup preview has no id — close and scan again.");
      logError("[prune] Confirm aborted: the modal holds no preview id");
      return;
    }
    if (blockedReason !== null) {
      setStatus(`Cleanup did not start: ${blockedReason}`);
      logWarn(`[prune] Confirm refused locally: ${blockedReason}`);
      return;
    }
    setStarting(true);
    setStatus(
      includedContent.size
        ? `Staging ${includedContent.size} installed-content selection(s)...`
        : "Starting cleanup...",
    );
    try {
      const staged = await stageInstalledSelections(initial.preview_id, [...includedContent], setStatus);
      if (!staged.ok) return;
      const result = await withTimeout(
        startPrune({
          preview_id: initial.preview_id,
          confirmed: true,
          repoint_shortcuts: repoint,
          remove_rows: removeRows,
          remove_fully_vanished: removeDeadGames,
          create_recovery_bundle: recovery,
          installed_selection_id: staged.selectionId,
        }),
        PRUNE_CALLABLE_TIMEOUT_MS,
      );
      if (!result.success) {
        setStatus(result.message ?? "Cleanup could not start.");
        logWarn(`[prune] startPrune refused: reason=${result.reason ?? "none"} message=${result.message ?? "none"}`);
        return;
      }
      if (!result.run_id) {
        // A success without a run id can never be adopted by id — say so instead
        // of wedging frame admission on a run the store will never recognise.
        setStatus("Cleanup started but the backend response carried no run id.");
        logError("[prune] startPrune reported success with no run id");
        return;
      }
      setRunStarted(true);
      if (!beginPruneRun(result.run_id, initial.preview_id)) {
        // The run IS executing; this dialog just can't receive its frames, and
        // neither can the Danger Zone. Saying "running..." here would leave the
        // user watching a progress line that can never arrive.
        setStatus("Cleanup started, but this dialog lost track of it. Check the log, then re-scan to see the result.");
        logError(`[prune] run ${result.run_id} started but preview ${initial.preview_id} was no longer pending`);
        return;
      }
      setStatus("Cleanup running...");
      logInfo(`[prune] startPrune accepted: run=${result.run_id}`);
    } catch (e) {
      const adopted = getPruneState();
      if (adopted.runId !== null) {
        setRunStarted(true);
        setStatus(adopted.complete ? "Cleanup completed." : "Cleanup running...");
        logWarn(`[prune] startPrune response was lost; adopted run ${adopted.runId} from its frames instead: ${e}`);
      } else {
        setStatus(`Cleanup could not start: ${e}`);
        logError(`[prune] startPrune threw with no run adopted: ${e}`);
      }
    } finally {
      setStarting(false);
    }
  };

  return (
    <ModalRoot closeModal={closeModal}>
      {/* The whole dialog scrolls as one. A separate inner scroller for the list
          leaves the space estimate pinned over the last row, and gives the
          controller two scroll axes to choose between on every focus move. */}
      <div
        style={{
          minWidth: "440px",
          maxWidth: "720px",
          padding: "18px",
          paddingBottom: "28px",
          maxHeight: "76vh",
          overflowY: "auto",
        }}
      >
        <div style={{ fontSize: "20px", fontWeight: 700, marginBottom: "6px" }}>Clean Up Removed RomM Games</div>
        <div style={{ color: "#c7d5e0", marginBottom: "14px" }}>
          {`${candidateTotal} locally kept ${plural(candidateTotal, "version", "versions")}${
            scope === "bulk" ? "" : " of this game"
          } ${plural(candidateTotal, "is", "are")} no longer on your RomM server.`}{" "}
          Nothing is removed until each one is checked against the server again — only entries RomM confirms as gone can
          be deleted.
          {removeDeadGames && total > candidateTotal
            ? " Other versions of the same games are listed below; they stay, unless the check finds every version of a game gone."
            : ""}
        </div>

        <div>
          {/* Focusing the first control has to bring the intro above it back into
              view — Steam's focus engine only scrolls far enough to reveal the
              control itself, which strands the text off the top on a controller. */}
          <div onFocus={scrollNearestToTop}>
            <ToggleField
              label="Repoint vanished shortcuts to the live default version"
              checked={repoint}
              disabled={runInFlight}
              onChange={setRepoint}
            />
          </div>
          <ToggleField
            label="Remove confirmed rows and installed content from groups with a live version"
            checked={removeRows}
            disabled={runInFlight}
            onChange={setRemoveRows}
          />
          <ToggleField
            label="Remove fully vanished games, including any Steam shortcut"
            description="Only for games where the server confirms every single version is gone — those are removed whole, Steam shortcut included. The recovery bundle keeps the shortcut's Steam details so you can rebuild it by hand."
            checked={removeDeadGames}
            disabled={runInFlight}
            onChange={(checked: boolean) => {
              setRemoveDeadGames(checked);
              // Turning it off hides the rows only a whole-game removal could
              // take, so their content selections must not stay staged.
              if (!checked) {
                setIncludedContent((current) => {
                  const stillShown = new Set(items.filter((item) => item.candidate).map((item) => item.rom_id));
                  return new Set([...current].filter((romId) => stillShown.has(romId)));
                });
              }
            }}
          />
          <ToggleField
            label="Create recovery bundle"
            description={`Verified bundles are sealed under ${initial.recovery_root ?? "the recovery directory"}.`}
            checked={recovery}
            disabled={runInFlight}
            onChange={(checked: boolean) => {
              setRecovery(checked);
              if (checked) setConfirmWithoutRecovery(false);
              else setIncludedContent(new Set());
            }}
          />
          {!recovery && (
            <ToggleField
              label="I understand local database state and playtime will have no recovery bundle"
              checked={confirmWithoutRecovery}
              disabled={runInFlight}
              onChange={setConfirmWithoutRecovery}
            />
          )}

          <div style={{ margin: "14px 0 8px", fontWeight: 700 }}>Versions no longer on RomM</div>
          {visibleItems.map((item, index) => (
            <Fragment key={item.rom_id}>
              {/* The backend sorts candidates first, so the first non-candidate
                  row is where the disclosure block starts. */}
              {!item.candidate && (index === 0 || visibleItems[index - 1]!.candidate) && (
                <div style={{ margin: "18px 0 8px", fontWeight: 700 }}>Other versions of these games — kept</div>
              )}
              <div
                style={{
                  padding: "10px 0",
                  borderTop: "1px solid rgba(255,255,255,0.10)",
                  ...(item.candidate ? {} : { paddingLeft: "14px", opacity: 0.75 }),
                }}
              >
                <div style={{ fontWeight: item.candidate ? 600 : 400 }}>
                  {item.name || item.fs_name || `ROM ${item.rom_id}`}
                </div>
                {(item.name_truncated ||
                  item.fs_name_truncated ||
                  item.group_id_truncated ||
                  item.warning_truncated) && (
                  <div style={{ color: "#e5a43b", fontSize: "12px" }}>
                    One or more display fields were shortened to keep this preview page within the Decky wire limit.
                  </div>
                )}
                <div style={{ fontSize: "12px", color: "#8f98a0" }}>
                  {item.platform_slug} · ROM {item.rom_id}
                  {item.group_size > 1 ? ` · one of ${item.group_size} versions of this game` : ""}
                </div>
                <div style={{ fontSize: "12px", color: item.candidate ? "#c7d5e0" : "#8f98a0" }}>
                  {item.candidate
                    ? "Gone from RomM — removed once the server confirms it."
                    : "Still on RomM at your last sync. Removed only if the final check finds every version of this game gone — then the whole game goes, Steam shortcut included."}
                </div>
                {item.warning && <div style={{ color: "#e5a43b", fontSize: "12px" }}>{item.warning}</div>}
                {item.installed && (
                  <ToggleField
                    label={`Include installed ROM content (${item.installed_bytes === null ? "size unavailable" : formatBytes(item.installed_bytes)})`}
                    checked={includedContent.has(item.rom_id)}
                    disabled={runInFlight || !recovery}
                    onChange={(checked: boolean) => toggleContent(item.rom_id, checked)}
                  />
                )}
                {item.installed && (!recovery || !includedContent.has(item.rom_id)) && (
                  <div style={{ color: "#e5a43b", fontSize: "12px" }}>
                    Without a backup, the downloaded ROM file is deleted along with this version.
                  </div>
                )}
              </div>
            </Fragment>
          ))}
          {/* Without this the per-row "Include installed ROM content" checkbox is
              invisible on a library where nothing is downloaded, and the option
              reads as missing rather than as not applicable. Only claimed once
              every page is loaded — an unseen page could still hold one. */}
          {allEntriesLoaded && !visibleItems.some((item) => item.installed) && (
            <div style={{ padding: "10px 0", fontSize: "12px", color: "#8f98a0" }}>
              None of these versions has ROM files downloaded on this device, so there is nothing to back up.
            </div>
          )}
          {items.length < total && (
            <DialogButton disabled={loadingMore} onClick={() => detach(loadMore())}>
              {loadingMore ? "Loading..." : `Load more (${items.length} of ${total})`}
            </DialogButton>
          )}
          {!allEntriesLoaded && (
            <div style={{ color: "#ff8c6a", fontSize: "12px", marginTop: "8px" }}>
              Load every page before confirming so all potentially removed group members and installed content are
              disclosed.
            </div>
          )}
        </div>

        <div style={{ marginTop: "14px", padding: "10px", background: "rgba(0,0,0,0.20)" }}>
          Selected ROM-content recovery estimate: {formatBytes(selectedBytes)} · Free at target:{" "}
          {formatBytes(freeBytes)}
          <div style={{ color: "#8f98a0", fontSize: "12px", marginTop: "4px" }}>
            This is a lower bound. The backend remeasures mandatory saves, histories, caches, and Steam files before any
            mutation.
          </div>
          <div style={{ color: "#8f98a0", fontSize: "12px", marginTop: "4px" }}>
            Large installed-content selections are staged in bounded pages before cleanup; every checked item remains
            part of this run.
          </div>
          <DialogButton disabled={runInFlight} onClick={() => detach(refreshFreeSpace())}>
            Refresh free space
          </DialogButton>
          {insufficientSpace && (
            <div style={{ color: "#ff8c6a", marginTop: "4px" }}>
              {unknownSelectedSize ? "A selected installed ROM has no safe measurable size." : "Not enough free space."}
            </div>
          )}
        </div>
        {progress && (
          <div style={{ marginTop: "10px", color: "#c7d5e0" }}>
            <div role="status" aria-live="polite">
              <CleanupProgress progress={progress} />
            </div>
            <div style={{ color: "#8f98a0", fontSize: "12px", marginTop: "4px" }}>{CANCEL_HINT}</div>
            <DialogButton
              disabled={cancelling}
              onClick={() =>
                detach(
                  (async () => {
                    setCancelRequestedFor(progress.run_id);
                    const failure = await requestPruneCancel(progress.run_id);
                    if (failure !== null) {
                      // Refused means this run is not running, so no terminal
                      // frame is coming to re-open the control — do it here.
                      setCancelRequestedFor(null);
                      setStatus(failure);
                    }
                  })(),
                )
              }
            >
              {cancelling ? "Stopping..." : "Stop Cleanup"}
            </DialogButton>
            {cancelling && (
              <div role="status" aria-live="polite" style={{ color: "#8f98a0", fontSize: "12px", marginTop: "4px" }}>
                {CANCELLING_HINT}
              </div>
            )}
          </div>
        )}
        {complete && (
          <div style={{ marginTop: "10px", color: complete.success ? "#8fd18b" : "#ffcc66" }}>
            <FinishedCleanupBar />
            <div role="status" aria-live="polite">
              {complete.removed_count ?? complete.removed_rom_ids.length} removed;{" "}
              {complete.problem_count ??
                complete.results.filter((item) => ["partial", "failed", "skipped"].includes(item.status)).length}{" "}
              skipped, partial, or failed.
            </div>
            <Focusable
              role="region"
              aria-label="Cleanup details"
              tabIndex={0}
              style={{ maxHeight: "180px", overflowY: "auto", marginTop: "6px" }}
            >
              {complete.message && (
                <div style={{ fontSize: "12px", marginTop: "4px" }}>
                  {complete.reason ? `${complete.reason}: ` : ""}
                  {complete.message}
                </div>
              )}
              {complete.results
                .filter(
                  (item) =>
                    ["partial", "failed", "skipped"].includes(item.status) ||
                    (item.warnings?.length ?? 0) > 0 ||
                    item.warnings_omitted ||
                    item.warnings_truncated ||
                    // A sealed bundle that removed nothing leaves a folder on
                    // disk; saying so is what stops it being a mystery later.
                    (item.bundle_path !== undefined && removedInGroup(item) === 0),
                )
                .map((item) => (
                  <div key={item.group_id} style={{ fontSize: "12px", marginTop: "4px" }}>
                    {item.name || item.group_id}: {item.message}
                    {item.bundle_path !== undefined && removedInGroup(item) === 0 && (
                      <div>Backup created, nothing removed. The folder stays at {item.bundle_path}.</div>
                    )}
                    {item.message_truncated && <div>Detail was shortened to fit the Decky wire limit.</div>}
                    {item.warnings?.map((warning) => (
                      <div key={warning}>Warning: {warning}</div>
                    ))}
                    {item.warnings_omitted && (item.warning_count ?? 0) > (item.warnings?.length ?? 0) && (
                      <div>
                        {(item.warning_count ?? 0) - (item.warnings?.length ?? 0)} additional warning(s) omitted.
                      </div>
                    )}
                    {item.warnings_truncated && <div>One or more displayed warnings were shortened.</div>}
                  </div>
                ))}
            </Focusable>
          </div>
        )}
        {resultLost && (
          <div role="status" aria-live="polite" style={{ marginTop: "10px", color: "#ff8c6a" }}>
            {RESULT_LOST_MESSAGE}
          </div>
        )}
        {status && !complete && (
          <div role="status" aria-live="polite" style={{ marginTop: "10px", color: "#ffcc66" }}>
            {status}
          </div>
        )}
        {blockedReason !== null && !runInFlight && (
          <div style={{ marginTop: "10px", color: "#ff8c6a", fontSize: "12px" }}>{blockedReason}</div>
        )}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "16px" }}>
          <DialogButton disabled={starting && complete === null} onClick={() => closeModal?.()}>
            {runStarted || complete !== null ? "Close" : "Cancel"}
          </DialogButton>
          <DialogButton disabled={pressBlocked} onClick={() => detach(start())}>
            {confirmButtonLabel(starting, progress)}
          </DialogButton>
        </div>
      </div>
    </ModalRoot>
  );
};

export async function openRemovedGamesCleanupModal(romId?: number): Promise<boolean> {
  const scope: PruneScope = romId === undefined ? "bulk" : "rom";
  const result = await withTimeout(
    getPrunePreview(requestFor(scope, romId ?? null, null, 0)),
    PRUNE_CALLABLE_TIMEOUT_MS,
  );
  if (!result.success) throw new Error(result.message ?? "Cleanup scan failed.");
  if ((result.total ?? 0) === 0) return false;
  // Without a preview id nothing can admit this run's frames — surface the
  // malformed response rather than opening a modal that can never report.
  if (!result.preview_id) throw new Error("Cleanup scan response carried no preview id.");
  beginPrunePreview(result.preview_id);
  showModal(<CleanupModal initial={result} scope={scope} romId={romId ?? null} />);
  return true;
}

export const RemovedGamesCleanupSection: FC = () => {
  const [scanning, setScanning] = useState(false);
  const [cancelRequestedFor, setCancelRequestedFor] = useState<string | null>(null);
  const [cancelStatus, setCancelStatus] = useState<string | null>(null);
  const [syncRunning, setSyncRunning] = useState(getSyncProgress().running);
  const [pruneState, setPruneState] = useState(getPruneState());
  const [resultLost, setResultLost] = useState(isPruneResultLost());
  const lastRunIdRef = useRef(getPruneState().runId);

  useEffect(() => {
    const unsubscribeSync = onSyncProgressChange(() => setSyncRunning(getSyncProgress().running));
    const unsubscribePrune = onPruneStateChange(() => {
      const next = getPruneState();
      // A refusal message belongs to the run it was refused for; carrying it
      // into the next run would describe that run's Stop button with an
      // outcome that never happened to it. Tracked in a ref because this
      // subscription is registered once and would otherwise compare against
      // the state it closed over on mount.
      if (next.runId !== lastRunIdRef.current) {
        lastRunIdRef.current = next.runId;
        setCancelStatus(null);
      }
      setPruneState(next);
      setResultLost(isPruneResultLost());
    });
    return () => {
      unsubscribeSync();
      unsubscribePrune();
    };
  }, []);

  const scan = async (): Promise<void> => {
    setScanning(true);
    try {
      if (!(await openRemovedGamesCleanupModal())) {
        toaster.toast({ title: "RomM Sync", body: "No removed RomM entries were found." });
      }
    } catch (e) {
      logError(`Removed-game cleanup scan failed: ${e}`);
      toaster.toast({ title: "RomM Sync", body: "Could not scan removed RomM games." });
    } finally {
      setScanning(false);
    }
  };

  const progress = pruneState.progress;
  const complete = pruneState.complete;
  // A run that has started but not yet emitted its first progress frame is
  // still a run: gate on the run id, not just on progress, or the entry point
  // stays live during exactly the window where a second scan would collide.
  const runActive = complete === null && pruneState.runId !== null;
  const cancelling = runActive && cancelRequestedFor !== null && cancelRequestedFor === pruneState.runId;
  const runLabel = progress
    ? `${progress.stage.replace(/_/g, " ")} · ${progress.current} of ${progress.total} · ${progress.name}`
    : "Cleanup starting...";
  return (
    <PanelSection title="Removed RomM Games">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={scanning || syncRunning || runActive || progress !== null}
          description={scanButtonDescription(syncRunning, runActive)}
          onClick={() => detach(scan())}
        >
          {scanning ? "Scanning..." : "Clean Up Removed RomM Games"}
        </ButtonItem>
      </PanelSectionRow>
      {resultLost && (
        <PanelSectionRow>
          <Field label={RESULT_LOST_MESSAGE} />
        </PanelSectionRow>
      )}
      {runActive && (
        <>
          <PanelSectionRow>
            {progress ? <CleanupProgress progress={progress} /> : <Field label={runLabel} description={CANCEL_HINT} />}
          </PanelSectionRow>
          {progress?.bundle_path && (
            <PanelSectionRow>
              <Field label={`Recovery sealed: ${progress.bundle_path}`} />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={cancelling}
              description={cancelStatus ?? (cancelling ? CANCELLING_HINT : undefined)}
              onClick={() =>
                detach(
                  (async () => {
                    const runId = pruneState.runId!;
                    setCancelRequestedFor(runId);
                    const failure = await requestPruneCancel(runId);
                    setCancelStatus(failure);
                    // A refusal is the only outcome with no terminal frame
                    // behind it, so it is the only one that re-opens the button.
                    if (failure !== null) setCancelRequestedFor(null);
                  })(),
                )
              }
            >
              {cancelling ? "Stopping..." : "Stop Cleanup"}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
      {complete && (
        <>
          <PanelSectionRow>
            <FinishedCleanupBar />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              label={`${complete.removed_count ?? complete.removed_rom_ids.length} removed; ${complete.problem_count ?? complete.results.filter((item) => ["partial", "failed", "skipped"].includes(item.status)).length} skipped, partial, or failed`}
              description={[
                complete.message,
                ...complete.results
                  .filter((item) => item.status !== "removed" || (item.warnings?.length ?? 0) > 0)
                  .flatMap((item) => [item.message, ...(item.warnings ?? []).map((warning) => `Warning: ${warning}`)]),
              ]
                .filter(Boolean)
                .join(" · ")}
            />
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};
