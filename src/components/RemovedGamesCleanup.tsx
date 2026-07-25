import { useEffect, useState, FC } from "react";
import { toaster } from "@decky/api";
import {
  ButtonItem,
  DialogButton,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  showModal,
} from "@decky/ui";
import {
  getPrunePreview,
  startPrune,
  stagePruneInstalledSelection,
  logError,
  type PrunePreviewItem,
  type PrunePreviewRequest,
  type PrunePreviewResult,
  type PruneScope,
} from "../api/backend";
import { detach } from "../utils/detach";
import { beginPruneRun, getPruneState, onPruneStateChange, resetPruneState } from "../utils/pruneStore";
import { getSyncProgress, onSyncProgressChange } from "../utils/syncProgress";
import { withTimeout } from "../utils/withTimeout";

const PAGE_SIZE = 50;
const SELECTION_PAGE_SIZE = 100;
const PRUNE_CALLABLE_TIMEOUT_MS = 15000;

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

function requestFor(
  scope: PruneScope,
  romId: number | null,
  previewId: string | null,
  offset: number,
): PrunePreviewRequest {
  return { scope, rom_id: romId, preview_id: previewId, offset, limit: PAGE_SIZE };
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
  const [runStarted, setRunStarted] = useState(false);
  const [status, setStatus] = useState("");
  const [repoint, setRepoint] = useState(true);
  const [removeRows, setRemoveRows] = useState(true);
  const [removeDeadGames, setRemoveDeadGames] = useState(false);
  const [recovery, setRecovery] = useState(true);
  const [confirmWithoutRecovery, setConfirmWithoutRecovery] = useState(false);
  const [includedContent, setIncludedContent] = useState<Set<number>>(new Set());
  const [freeBytes, setFreeBytes] = useState(initial.free_bytes ?? 0);
  const [pruneState, setPruneState] = useState(getPruneState());

  useEffect(() => {
    const unsubscribe = onPruneStateChange(() => setPruneState(getPruneState()));
    return () => {
      unsubscribe();
    };
  }, []);

  const total = initial.total ?? items.length;
  const selectedBytes = items.reduce(
    (sum, item) => sum + (includedContent.has(item.rom_id) ? (item.installed_bytes ?? 0) : 0),
    0,
  );
  const unknownSelectedSize = items.some(
    (item) => includedContent.has(item.rom_id) && item.installed && item.installed_bytes === null,
  );
  const insufficientSpace = recovery && (unknownSelectedSize || selectedBytes > freeBytes);
  const destructiveConfirmed = recovery || confirmWithoutRecovery;
  const allEntriesLoaded = items.length === total;
  const progress = pruneState.progress;
  const complete = pruneState.complete;
  const runInFlight = starting || (runStarted && complete === null);
  const canStart =
    !runInFlight &&
    complete === null &&
    allEntriesLoaded &&
    !insufficientSpace &&
    destructiveConfirmed &&
    (repoint || removeRows || removeDeadGames);

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
    if (!canStart || !initial.preview_id) return;
    setStarting(true);
    setStatus(
      includedContent.size
        ? `Staging ${includedContent.size} installed-content selection(s)...`
        : "Starting cleanup...",
    );
    try {
      let selectionId: string | null = null;
      const selected = [...includedContent];
      for (let offset = 0; offset < selected.length; offset += SELECTION_PAGE_SIZE) {
        const page = selected.slice(offset, offset + SELECTION_PAGE_SIZE);
        const staged: {
          success: boolean;
          selection_id?: string;
          message?: string;
        } = await withTimeout(
          stagePruneInstalledSelection({
            preview_id: initial.preview_id,
            selection_id: selectionId,
            rom_ids: page,
            final: offset + page.length >= selected.length,
          }),
          PRUNE_CALLABLE_TIMEOUT_MS,
        );
        if (!staged.success || !staged.selection_id) {
          setStatus(staged.message ?? "Installed-content selections could not be staged.");
          return;
        }
        selectionId = staged.selection_id;
      }
      const result = await withTimeout(
        startPrune({
          preview_id: initial.preview_id,
          confirmed: true,
          repoint_shortcuts: repoint,
          remove_rows: removeRows,
          remove_fully_vanished: removeDeadGames,
          create_recovery_bundle: recovery,
          installed_selection_id: selectionId,
        }),
        PRUNE_CALLABLE_TIMEOUT_MS,
      );
      if (!result.success) {
        setStatus(result.message ?? "Cleanup could not start.");
        return;
      }
      beginPruneRun(result.run_id!);
      setRunStarted(true);
      setStatus("Cleanup running...");
    } catch (e) {
      setStatus(`Cleanup could not start: ${e}`);
    } finally {
      setStarting(false);
    }
  };

  return (
    <ModalRoot closeModal={closeModal}>
      <div style={{ minWidth: "440px", maxWidth: "720px", padding: "18px" }}>
        <div style={{ fontSize: "20px", fontWeight: 700, marginBottom: "6px" }}>Clean Up Removed RomM Games</div>
        <div style={{ color: "#c7d5e0", marginBottom: "14px" }}>
          {scope === "bulk"
            ? `${total} local group entr${total === 1 ? "y" : "ies"} may be affected by candidates absent from a completed RomM fetch.`
            : `${total} local group entr${total === 1 ? "y" : "ies"} may be affected by cleanup of the selected unavailable ROM.`}{" "}
          Every entry is checked again by exact ID; only a confirmed 404 can be removed.
        </div>

        <div style={{ maxHeight: "62vh", overflowY: "auto", paddingRight: "10px" }}>
          <ToggleField
            label="Repoint vanished shortcuts to the live Default"
            checked={repoint}
            disabled={runInFlight}
            onChange={setRepoint}
          />
          <ToggleField
            label="Remove confirmed rows and installed content from groups with a live version"
            checked={removeRows}
            disabled={runInFlight}
            onChange={setRemoveRows}
          />
          <ToggleField
            label="Remove fully vanished games and their Steam shortcut"
            description="Off by default. This removes the whole local game only when every local ID returns 404."
            checked={removeDeadGames}
            disabled={runInFlight}
            onChange={setRemoveDeadGames}
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

          <div style={{ margin: "14px 0 8px", fontWeight: 700 }}>Candidates</div>
          {items.map((item) => (
            <div key={item.rom_id} style={{ padding: "10px 0", borderTop: "1px solid rgba(255,255,255,0.10)" }}>
              <div style={{ fontWeight: 600 }}>{item.name || item.fs_name || `ROM ${item.rom_id}`}</div>
              {(item.name_truncated || item.fs_name_truncated || item.group_id_truncated || item.warning_truncated) && (
                <div style={{ color: "#e5a43b", fontSize: "12px" }}>
                  One or more display fields were shortened to keep this preview page within the Decky wire limit.
                </div>
              )}
              <div style={{ fontSize: "12px", color: "#8f98a0" }}>
                {item.platform_slug} · ROM {item.rom_id} · group of {item.group_size} ·{" "}
                {item.candidate ? "cleanup candidate" : "group member disclosed for whole-game removal"}
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
                  Installed content is not backed up but will still be deleted if this row is removed.
                </div>
              )}
            </div>
          ))}
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
          <div role="status" aria-live="polite" style={{ marginTop: "10px", color: "#c7d5e0" }}>
            {progress.stage.replace(/_/g, " ")} · {progress.current} of {progress.total} · {progress.name}
          </div>
        )}
        {complete && (
          <div style={{ marginTop: "10px", color: complete.success ? "#8fd18b" : "#ffcc66" }}>
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
              {complete.results
                .filter((item) => ["partial", "failed", "skipped"].includes(item.status))
                .map((item) => (
                  <div key={item.group_id} style={{ fontSize: "12px", marginTop: "4px" }}>
                    {item.group_id}: {item.message}
                    {item.message_truncated && <div>Detail was shortened to fit the Decky wire limit.</div>}
                    {item.warnings?.map((warning) => (
                      <div key={warning}>Warning: {warning}</div>
                    ))}
                    {(item.warnings_truncated || (item.warning_count ?? 0) > (item.warnings?.length ?? 0)) && (
                      <div>
                        {Math.max(0, (item.warning_count ?? item.warnings?.length ?? 0) - (item.warnings?.length ?? 0))}{" "}
                        additional warning(s) omitted or shortened.
                      </div>
                    )}
                  </div>
                ))}
            </Focusable>
          </div>
        )}
        {status && !complete && (
          <div role="status" aria-live="polite" style={{ marginTop: "10px", color: "#ffcc66" }}>
            {status}
          </div>
        )}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "16px" }}>
          <DialogButton disabled={starting} onClick={() => closeModal?.()}>
            {runStarted ? "Close" : "Cancel"}
          </DialogButton>
          <DialogButton disabled={!canStart} onClick={() => detach(start())}>
            {starting ? "Starting..." : progress ? `${progress.stage.replace(/_/g, " ")}...` : "Confirm Cleanup"}
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
  resetPruneState();
  showModal(<CleanupModal initial={result} scope={scope} romId={romId ?? null} />);
  return true;
}

export const RemovedGamesCleanupSection: FC = () => {
  const [scanning, setScanning] = useState(false);
  const [syncRunning, setSyncRunning] = useState(getSyncProgress().running);
  const [pruneState, setPruneState] = useState(getPruneState());

  useEffect(() => {
    const unsubscribeSync = onSyncProgressChange(() => setSyncRunning(getSyncProgress().running));
    const unsubscribePrune = onPruneStateChange(() => setPruneState(getPruneState()));
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
  return (
    <PanelSection title="Removed RomM Games">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={scanning || syncRunning || progress !== null}
          description={syncRunning ? "Unavailable while a library sync is running." : undefined}
          onClick={() => detach(scan())}
        >
          {scanning ? "Scanning..." : "Clean Up Removed RomM Games"}
        </ButtonItem>
      </PanelSectionRow>
      {progress && (
        <PanelSectionRow>
          <Field
            label={`${progress.stage.replace(/_/g, " ")} · ${progress.current} of ${progress.total} · ${progress.name}`}
            description={progress.bundle_path ? `Recovery sealed: ${progress.bundle_path}` : undefined}
          />
        </PanelSectionRow>
      )}
      {complete && (
        <PanelSectionRow>
          <Field
            label={`${complete.removed_count ?? complete.removed_rom_ids.length} removed; ${complete.problem_count ?? complete.results.filter((item) => ["partial", "failed", "skipped"].includes(item.status)).length} skipped, partial, or failed`}
            description={complete.results
              .filter((item) => item.status !== "removed")
              .map((item) => item.message)
              .join(" · ")}
          />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};
