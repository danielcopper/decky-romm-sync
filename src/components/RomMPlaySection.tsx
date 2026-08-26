/**
 * RomMPlaySection — wraps CustomPlayButton and adds info items to its right,
 * mimicking Steam's native PlaySection layout:
 *
 *   [▶ Play ▾]   LAST PLAYED    PLAYTIME    ACHIEVEMENTS    SAVE SYNC    BIOS
 *                24. Jan.       14 Hours    To be impl.     ✅ 2h ago    🟢 OK
 *
 * Uses our own romm-play-section-row CSS class on the root.
 * Individual info items use our own romm-info-* CSS classes.
 * Save Sync and BIOS items only appear when relevant.
 */

import { useState, useEffect, FC, Fragment, type ReactElement } from "react";
import { showToast } from "../utils/toast";
import {
  ConfirmModal,
  DialogButton,
  Focusable,
  Menu,
  MenuItem,
  MenuSeparator,
  showContextMenu,
  showModal,
} from "@decky/ui";
import { basicAppDetailsSectionStylerClasses } from "../utils/deckyUiInternals";
import { FaGamepad, FaCog, FaMicrochip, FaExclamationTriangle } from "react-icons/fa";
import { CustomPlayButton } from "./CustomPlayButton";
import { DiscSelector } from "./DiscSelector";
import { VersionPicker } from "./VersionPicker";
import { WarningCard } from "./WarningCard";
import { SgdbGamePickerModalContent } from "./SgdbGamePickerModal";
import { applyArtwork, cancelArtworkApply } from "../utils/artwork";
import { hasAnySaveConflict } from "../utils/saveStatus";
import { saveSyncToastBody } from "../utils/saveSyncToast";
import { scrollToTop } from "../utils/scrollHelpers";
import { getEventTarget } from "../utils/events";
import {
  testConnection,
  probeReachability,
  getSgdbResolution,
  getRomMetadata,
  refreshCoverArtwork,
  removeRom,
  downloadAllFirmware,
  syncRomSaves,
  deleteLocalSaves,
  setGameCore,
  clearGameCore,
  reconcilePlaytime,
  debugLog,
} from "../api/backend";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseCancelled,
  isPruneLeaseCancellation,
  mountPruneLeaseOwner,
  releasePruneLeasesByOwner,
  withPruneLease,
  type PruneLeaseAdmission,
} from "../utils/pruneLease";
import { updatePlaytimeDisplay } from "../patches/metadataPatches";
import { buildEmulatorMenu } from "../utils/emulatorMenu";
import { formatBytes, formatLastPlayed, formatPlaytime } from "../utils/formatters";
import { biosColorForLevel } from "../utils/biosColor";
import { timeoutMs } from "../utils/playSection";
import {
  getGameDetail,
  noteSaveSyncDisplay,
  refreshBiosStatus,
  refreshCoreAndBios,
  refreshSaveStatus,
  useGameDetail,
} from "../utils/gameDetailStore";

/** Which rom_id each appId has had auto-artwork applied for this session. Keyed
 *  on the pair, not the appId alone: a version switch re-binds the appId to a
 *  new rom_id whose artwork has to be applied afresh (#1298 item 3). */
const artworkApplied = new Map<number, number>();

/** How long the authoritative connection check may run before the wait itself is
 *  worth a log line. It is NOT a deadline after which the server counts as
 *  unreachable — see the check below. */
const CONNECTION_CHECK_DEADLINE_MS = 5000;

/** Source of connection-check ids, in start order. Ids are handed out at module
 *  level rather than per component because the state they order is module-level
 *  too — two play sections briefly overlap while Steam swaps game pages. */
let connectionCheckSeq = 0;

/** Id of the newest check that has WRITTEN a verdict. An older check must not
 *  write over it — its answer is to a question a newer one has already answered.
 *  Ordering on the write rather than on the start is what keeps a check that is
 *  abandoned before it answers (its page closed while the server was still
 *  thinking) from silencing the verdict of a page that is still open. */
let lastSettledCheckId = 0;

/** Resolve the LAST PLAYED display, preferring our restored cross-device
 *  `last_played` (ISO-8601, from `reconcile_playtime` / native play sessions,
 *  #1294) over Steam's device-local `rt_last_time_played`. Steam synthesizes the
 *  latter to "now" after a device cutover, so the restored value wins whenever
 *  it parses; a null or unparseable restored value falls back to Steam's
 *  Unix-seconds value. Both route through `formatLastPlayed` so the rendered
 *  format is identical either way. */
function resolveLastPlayed(restoredIso: string | null, steamUnixSeconds: number): string {
  if (restoredIso) {
    const ms = Date.parse(restoredIso);
    if (!Number.isNaN(ms)) return formatLastPlayed(Math.floor(ms / 1000));
  }
  return formatLastPlayed(steamUnixSeconds);
}

/** Read this ROM's save status through the store and tell the other save-sync
 *  surfaces what came back.
 *
 *  The status travels WITH the notification. Every listener for this event — the
 *  store's fold, the info panel's — answers a payload-less one by reading the
 *  status itself, so a bare notification costs one more round-trip per listener
 *  for an answer already in hand (#1758).
 *
 *  Both callers reach the dispatch after an await, and `isCancelled` cannot be
 *  what keeps this page's answer apart from its predecessor's. The reconnect
 *  caller is keyed on the appId alone, so a version switch never tears it down
 *  at all; and where a switch DOES re-run a caller, React commits that teardown
 *  only after the re-render, so an answer arriving before the commit still
 *  passes (#1717). What decides is the answer's own `rom_id` against the rom
 *  bound to this appId NOW — the same gate the store's fold applies, which is
 *  also why the id on the wire is the answer's rather than a captured one. */
async function readAndBroadcastSaveStatus(appId: number, isCancelled: () => boolean): Promise<void> {
  try {
    const saveStatus = await refreshSaveStatus(appId);
    if (isCancelled() || saveStatus?.rom_id !== getGameDetail(appId).romId) return;
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", {
        detail: {
          type: "save_sync",
          rom_id: saveStatus.rom_id,
          save_status: saveStatus,
          has_conflict: hasAnySaveConflict(saveStatus),
        },
      }),
    );
  } catch (e) {
    detach(debugLog(`RomMPlaySection: background save check error: ${e}`));
  }
}

interface RomMPlaySectionProps {
  appId: number;
}

/** The play section's own display state. Everything the game page's surfaces
 *  share — rom identity, install state, save sync, BIOS, core — lives in the
 *  per-appId game-detail store instead; what stays here is the PLAYTIME /
 *  LAST PLAYED pair, which is read from Steam's overview and belongs to this
 *  row alone. */
interface PlaytimeState {
  lastPlayed: string;
  /** Restored cross-device `last_played` (ISO-8601) from `reconcile_playtime`,
   *  or `null` until the server yields one. Preferred over Steam's device-local
   *  `rt_last_time_played` when rendering LAST PLAYED (#1294). */
  restoredLastPlayed: string | null;
  playtime: string;
}

import {
  onRommConnectionChange,
  setRommConnectionState,
  setVersionError,
  useRommConnectionState,
  type RommConnectionState,
} from "../utils/connectionState";
import { registerConnectionHeartbeat } from "../utils/connectionHeartbeat";
import { useVersionError } from "./VersionErrorCard";
import { useMigrationStatus } from "../utils/migrationStore";
import { detach } from "../utils/detach";

// S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
// Prettier from relocating the trailing comment into the body (which would break the suppression).
// prettier-ignore
export const RomMPlaySection: FC<RomMPlaySectionProps> = ({ appId }) => { // NOSONAR(typescript:S3776) — React FC body; decomposed in #392. Holds Steam menu + achievements + save-sync row.
  // Subscribe to version error — re-renders when global state changes
  const versionError = useVersionError();
  const migration = useMigrationStatus();

  // Read playtime from Steam's own overview synchronously (already written by metadataPatches)
  // This avoids an unnecessary render from setting it inside the async effect.
  const overview = appStore.GetAppOverviewByAppID(appId);
  const initialLastPlayed = formatLastPlayed(overview?.rt_last_time_played ?? 0);
  const initialPlaytime = formatPlaytime(overview?.minutes_playtime_forever ?? 0);

  // Every field the game page's surfaces share — rom identity, install state,
  // save sync, BIOS, core selection — comes from the per-appId store, which owns
  // the reads and the romm_data_changed fold for all of them (#993).
  const detail = useGameDetail(appId);
  const [playtimeInfo, setPlaytimeInfo] = useState<PlaytimeState>({
    lastPlayed: initialLastPlayed,
    restoredLastPlayed: null,
    playtime: initialPlaytime,
  });
  // Badge derives from the shared store so it appears/disappears live on any
  // reachability signal (mount check, a failed/succeeded call, or the offline
  // recovery probe), not just at this mount's check (#1345).
  const connectionState = useRommConnectionState();
  const [actionPending, setActionPending] = useState<string | null>(null);

  // Drive the reachability heartbeat while this game page is mounted (#1345) —
  // it reports in both directions, so the badge follows the server going away as
  // well as coming back. A module-level guard keeps the ~30s re-probe
  // single-instance however many pages register it.
  useEffect(() => registerConnectionHeartbeat(), []);

  useEffect(() => {
    mountPruneLeaseOwner(`game-detail:${appId}`);
    return () => {
      detach(cancelArtworkApply(appId));
      detach(releasePruneLeasesByOwner(`game-detail:${appId}`));
    };
  }, [appId]);

  // Auto-apply SGDB artwork on first visit, once per (appId, rom_id) — so a
  // version switch re-applies for the newly bound rom_id (#1298 item 3). Only
  // marked applied after success, so a transient failure retries next visit.
  useEffect(() => {
    const romId = detail.romId;
    if (!romId || artworkApplied.get(appId) === romId) return;
    applyArtwork(romId, appId)
      .then(() => {
        artworkApplied.set(appId, romId);
      })
      .catch((e) => debugLog(`Auto-artwork error: ${e}`));
  }, [appId, detail.romId]);

  // Connection check — exactly one per mount, issued before this page's other
  // RomM reads rather than behind them. Keyed on appId alone: the verdict is
  // about the server, so nothing this page later learns about the ROM can change
  // it, and re-running the check when a store field settled meant the FIRST
  // (fast, idle-backend) answer was discarded as cancelled and the second one
  // queued behind the mount's own calls until it missed its deadline (#1670).
  useEffect(() => {
    const checkId = ++connectionCheckSeq;
    let cancelled = false;
    // Once testConnection() lands an authoritative verdict, the fast probe must
    // not override it (avoids a late probe clobbering a "connected" badge).
    let settled = false;

    /** An answer this check obtained is only still worth writing while nothing
     *  newer has answered: after unmount, and after a later check wrote its own
     *  verdict, writing it would put a stale verdict over a fresh one. A newer
     *  check that has merely STARTED does not supersede — it may yet be
     *  abandoned unanswered, and then this check's answer is the only one
     *  anybody is going to get. */
    const superseded = () => cancelled || checkId < lastSettledCheckId;

    /** Write a verdict and record this check as the newest one to have answered.
     *  The single place either happens, so the two cannot drift apart. */
    const settleWith = (next: RommConnectionState, reason: string) => {
      lastSettledCheckId = checkId;
      setRommConnectionState(next, reason);
    };

    // Fast offline-badge probe (ADR-0015): the slow `testConnection()` below
    // goes through the retrying heartbeat (3 attempts + backoff, up to ~90s on a
    // remote timeout), so the offline badge would otherwise lag the page open by
    // seconds. The fast `probeReachability()` (single attempt, ~3s) flips the
    // badge to "offline" the moment the server is unreachable. We only ACT on a
    // negative result here — a positive probe still defers to `testConnection()`
    // for the precise verdict (version gate, auth) so we never flash "connected"
    // when a version error is pending.
    const fastOfflineProbe = async () => {
      // Only an EXPLICIT `online === false` flips the badge offline here. A
      // throw, or any non-{online} payload, is "no signal" → defer to the
      // precise `testConnection()` verdict below rather than guess offline.
      const offline = await probeReachability()
        .then((r) => r.online === false)
        .catch((e) => {
          detach(debugLog(`RomMPlaySection: fast reachability probe failed (no signal — deferring to testConnection): ${e}`));
          return false;
        });
      // The fast probe is an EARLY hint, not the authority. Bail if testConnection
      // already settled an authoritative verdict (so a late probe can't clobber
      // "connected"), or if superseded / not offline. The shared store notifies
      // its subscribers on the change (#1345).
      if (superseded() || settled || !offline) return;
      settleWith("offline", "fast probe");
    };

    const applyVerdict = (result: Awaited<ReturnType<typeof testConnection>>) => {
      if (superseded()) return;
      settled = true; // authoritative verdict in hand — a late fast probe must not override it
      if (result.reason === "version_error") {
        setVersionError(result.message);
        settleWith("offline", "version error");
        return;
      }
      settleWith(result.success ? "connected" : "offline", "authoritative verdict");
    };

    /** Obtain the verdict and apply it. Declared async so a SYNCHRONOUS throw
     *  out of the callable bridge arrives here as a rejection instead of
     *  escaping into the fire-and-forget `check()` unlogged — a bridge that
     *  cannot be called at all is as much a reachability signal as a rejected
     *  promise. Only the CALL is guarded: a throw out of applyVerdict is a
     *  subscriber's defect, not a verdict, and must not be reported as one. */
    const runVerdict = async () => {
      let result: Awaited<ReturnType<typeof testConnection>>;
      try {
        result = await testConnection();
      } catch (e) {
        if (superseded()) return;
        settled = true;
        detach(debugLog(`RomMPlaySection(${appId}): connection check failed: ${e}`));
        settleWith("offline", "check failed");
        return;
      }
      applyVerdict(result);
    };

    const check = async () => {
      // Reset stale connection state immediately so downstream consumers
      // (e.g. CustomPlayButton) don't stay stuck on a previous "offline"
      setRommConnectionState("checking", "connection check started");

      // Snappy offline badge — runs concurrently with the precise check below.
      detach(fastOfflineProbe());

      // The verdict is consumed by runVerdict, not by the race below, so an
      // answer that arrives after the deadline is still applied.
      const verdict = runVerdict();

      // Missing the deadline means UNKNOWN, not unreachable: the badge stays at
      // "checking" and the outstanding call above settles it whenever it lands.
      // Reporting the deadline as a verdict is what put "RomM offline" on a page
      // whose server answered seconds later (#1670).
      await Promise.race([verdict, timeoutMs(CONNECTION_CHECK_DEADLINE_MS)]).catch(() => {
        if (superseded() || settled) return;
        detach(
          debugLog(
            `RomMPlaySection(${appId}): connection check unanswered after ${CONNECTION_CHECK_DEADLINE_MS}ms — holding "checking" until the verdict lands`,
          ),
        );
      });
    };
    detach(check());
    return () => {
      cancelled = true;
    };
  }, [appId]);

  // Background save-status check — detects new conflicts for a save-sync ROM.
  // The read itself and the state it produces belong to the store; what stays
  // here is the conflict notification for the other save-sync surfaces. Keyed on
  // the ROM identity and the save-sync flag, which is all it consumes: it is not
  // GATED on the connection verdict, because the read answers with the local half
  // even while the server is unreachable — that degraded answer is what the saves
  // surfaces render at all — and waiting behind a verdict only meant the read
  // landed after the store's own had settled, costing a second round-trip for the
  // same answer. The reconnect lane below adds a second OCCASION to run this
  // read; it is not a gate on this one. Playtime reconcile-on-view is a separate,
  // equally connectivity-independent effect (#1345).
  useEffect(() => {
    const romId = detail.romId;
    if (!romId || !detail.saveSyncEnabled) return;
    let cancelled = false;
    detach(readAndBroadcastSaveStatus(appId, () => cancelled));
    return () => {
      cancelled = true;
    };
  }, [appId, detail.romId, detail.saveSyncEnabled]);

  // Reconnect repair (#1758) — the server coming back while this page stays
  // open. The page's other server-fed lanes read the connection verdict and so
  // repair themselves (the achievements tab, the panel's slot load); the save
  // status has no such reader, and nothing else dispatches a save_sync event on
  // reconnect, so its degraded answer — server half nulled, `server_query_failed`
  // set — would stand until the page is closed and reopened.
  //
  // Subscribing to the transitions rather than depending on the rendered verdict
  // is what keeps a page open from reading twice: the store starts at "checking"
  // and this page's own mount check flips it to "connected", so a verdict in the
  // dep array above would re-run the read on EVERY page open. Here only a change
  // runs anything, only a change INTO a reachable server, and only when the
  // answer on hand is missing its server half — a read whose server half already
  // landed has nothing to repair, and a mount whose read is still in flight has
  // no answer to judge yet.
  useEffect(() => {
    let cancelled = false;
    const unsubscribe = onRommConnectionChange((next) => {
      if (next !== "connected") return;
      // Read the identity and the last answer at TRANSITION time, not at
      // subscribe time: this callback outlives many renders, and the answer it
      // has to judge is the one the store holds now.
      const { romId, saveSyncEnabled, saveStatus } = getGameDetail(appId);
      if (!romId || !saveSyncEnabled || !saveStatus?.server_query_failed) return;
      detach(readAndBroadcastSaveStatus(appId, () => cancelled));
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [appId]);

  // Reconcile-on-view (#868) — pull-only: folds RomM's play-session history into
  // the local total so a session played on another device shows up the moment the
  // detail page opens. INTENTIONALLY NOT gated on connectivity (#1345): reconcile
  // returns the LOCAL total even when the server is unreachable, so it re-injects
  // real playtime into a rebuilt/rebound Steam overview instead of leaving it at
  // "PLAYTIME None". Runs once romId is known (its own effect, so it fires after
  // the store resolves romId rather than racing the connection check). Pushes
  // the total through updatePlaytimeDisplay (the write-chokepoint), which emits
  // romm_playtime_changed; the reactive PLAYTIME effect (#869) refreshes the
  // display on the same mount. A 0 total no-ops in updatePlaytimeDisplay.
  useEffect(() => {
    const romId = detail.romId;
    if (!romId) return;
    let cancelled = false;

    async function doReconcilePlaytime(rid: number, isCancelled: () => boolean) {
      try {
        const result = await reconcilePlaytime(rid);
        if (isCancelled()) return;
        if ("success" in result) {
          detach(debugLog(`RomMPlaySection: playtime reconcile deferred: ${result.message}`));
          return;
        }
        if (!result.server_query_failed) {
          // Connected: adopt the restored cross-device last_played (#1294) and
          // refresh the display from it. Set BEFORE updatePlaytimeDisplay so the
          // synchronous romm_playtime_changed handler reads the new value; also
          // covers the sub-minute total case where updatePlaytimeDisplay emits no
          // signal. Skipped on a failed read — no cross-device push offline.
          const steamSecs = appStore.GetAppOverviewByAppID(appId)?.rt_last_time_played ?? 0;
          setPlaytimeInfo((prev) => ({
            ...prev,
            restoredLastPlayed: result.last_played,
            lastPlayed: resolveLastPlayed(result.last_played, steamSecs),
          }));
        }
        // Re-inject the local total regardless of connectivity (offline fix #1345).
        updatePlaytimeDisplay(appId, result.total_seconds, false);
      } catch (e) {
        detach(debugLog(`RomMPlaySection: playtime reconcile error: ${e}`));
      }
    }

    detach(doReconcilePlaytime(romId, () => cancelled));
    return () => {
      cancelled = true;
    };
  }, [detail.romId, appId]);

  // Reactive PLAYTIME display (#869) — re-read Steam's overview whenever the
  // playtime write-chokepoint (updatePlaytimeDisplay) fires romm_playtime_changed
  // for this appId. Drives the displayed PLAYTIME / LAST PLAYED from the source
  // of truth (the overview) instead of a mount-only snapshot, so a session end
  // (handleGameStop) or a multi-device reconcile-on-view refreshes the value on
  // the SAME mount — no navigate-away/back remount required.
  useEffect(() => {
    const onPlaytimeChanged = (e: Event) => {
      const payload = (e as CustomEvent<{ appId?: number } | null>).detail;
      if (payload?.appId !== appId) return;
      const ov = appStore.GetAppOverviewByAppID(appId);
      if (!ov) return;
      setPlaytimeInfo((prev) => ({
        ...prev,
        playtime: formatPlaytime(ov.minutes_playtime_forever ?? 0),
        lastPlayed: resolveLastPlayed(prev.restoredLastPlayed, ov.rt_last_time_played ?? 0),
      }));
    };
    globalThis.addEventListener("romm_playtime_changed", onPlaytimeChanged);
    return () => {
      globalThis.removeEventListener("romm_playtime_changed", onPlaytimeChanged);
    };
  }, [appId]);

  // Helper: create an info item with header and value (Steam's two-line pattern)
  const infoItem = (key: string, header: string, value: string, extraClass?: string) => (
    <div key={key} className={`romm-info-item ${extraClass || ""}`.trim()}>
      <div className="romm-info-header">{header}</div>
      <div className="romm-info-value">{value}</div>
    </div>
  );

  // --- Gear button action handlers ---

  const handleRefreshArtwork = async () => {
    if (actionPending) return;
    if (!detail.romId) {
      showToast("ROM info not loaded yet");
      return;
    }
    const romId = detail.romId;
    setActionPending("artwork");
    const admission = capturePruneLeaseAdmission(`game-detail:${appId}`);
    try {
      // Step 1: re-download the RomM cover, rename to {app_id}p.png, and
      // patch cover_path on the registry row so the game info panel can
      // render the refreshed image.
      const coverResult = await refreshCoverArtwork(romId).catch(
        (e): { success: boolean; reason?: string; message: string; cover_path?: string } => {
          detach(debugLog(`refreshCoverArtwork rejected: ${e}`));
          return { success: false, reason: "exception", message: String(e) };
        },
      );
      if (coverResult.success) {
        // Notify the game info panel so it can re-render the cover image.
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "cover_refreshed", rom_id: romId },
          }),
        );
      } else {
        detach(debugLog(`refreshCoverArtwork failed: ${coverResult.reason} — ${coverResult.message}`));
      }

      // Step 2: resolve which SGDB game id to use. The backend either picks
      // one automatically (RomM/IGDB) or hands back manual candidates.
      const resolution = await getSgdbResolution(romId).catch((e): null => {
        detach(debugLog(`getSgdbResolution rejected: ${e}`));
        return null;
      });
      if (!resolution) {
        showToast("Failed to refresh artwork");
        return;
      }

      switch (resolution.decision) {
        case "no_api_key":
          showToast("Set a SteamGridDB API key in settings first");
          break;
        case "resolved": {
          const applied = await applyArtwork(romId, appId);
          if (applied === -1) {
            showToast("Set a SteamGridDB API key in settings first");
          } else if (applied > 0) {
            showToast(`Artwork refreshed (${applied}/4 images applied)`);
          } else {
            showToast("No artwork available for this game");
          }
          break;
        }
        case "needs_pick":
          showModal(
            <SgdbGamePickerModalContent
              romId={romId}
              appId={appId}
              romName={detail.romName}
              candidates={resolution.candidates}
              onApplied={() => {}}
            />,
          );
          break;
      }
    } catch (e) {
      // Leaving the game page cancels the artwork continuation mid-apply — that is
      // teardown, not a refresh failure, so it must not toast at the next surface.
      if (isPruneLeaseCancellation(e, admission)) {
        detach(debugLog(`handleRefreshArtwork: continuation was cancelled: ${e}`));
        return;
      }
      showToast("Failed to refresh artwork");
    } finally {
      setActionPending(null);
    }
  };

  const handleRefreshMetadata = async () => {
    if (actionPending || !detail.romId) return;
    setActionPending("metadata");
    try {
      await getRomMetadata(detail.romId);
      showToast("Metadata refreshed");
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "metadata", rom_id: detail.romId } }),
      );
    } catch {
      showToast("Failed to refresh metadata");
    } finally {
      setActionPending(null);
    }
  };

  const handleSyncSaves = async () => {
    if (actionPending || !detail.romId) return;
    const romId = detail.romId;
    setActionPending("savesync");
    try {
      const result = await syncRomSaves(romId);
      if (result.success) {
        // Directional completion toast via the shared helper — the single source
        // of that copy across every save-sync surface (#1481).
        const directionalBody = saveSyncToastBody(result.uploaded, result.downloaded);
        const c = result.conflicts?.length ?? 0;
        if (directionalBody) {
          showToast(directionalBody);
        } else if (c === 0) {
          // Manual surface only (#1486): an explicit per-game "Sync Saves" click
          // that moved nothing and hit no conflicts gets a short acknowledgement,
          // so the click doesn't read as a no-op. The automatic surfaces
          // (pre-launch, post-exit) stay silent on this zero-case.
          showToast("Saves already up to date");
        }
        // Preserve the conflict signal as its own additive toast (mirroring the
        // post-exit conflicts_toast) — it must stay visible even when nothing
        // transferred. Gated above so "up to date" never contradicts pending
        // conflicts.
        if (c > 0) {
          showToast(`${c} conflict(s) need resolution`);
        }
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }));
        // Refresh save sync status — last_sync_check_at was just set by the backend
        noteSaveSyncDisplay(appId, romId, { status: "synced", label: "Just now", last_sync_check_at: null });
      } else {
        showToast(result.message || "Save sync failed");
      }
    } catch {
      showToast("Save sync failed");
    } finally {
      setActionPending(null);
    }
  };

  const handleDownloadBios = async () => {
    if (actionPending || !detail.platformSlug) return;
    setActionPending("bios");
    try {
      const result = await downloadAllFirmware(detail.platformSlug);
      if (result.success) {
        showToast(`BIOS downloaded (${result.downloaded ?? 0} files)`);
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", { detail: { type: "bios", platform_slug: detail.platformSlug } }),
        );
        // Refresh BIOS status — getBiosStatus ships pre-computed level/label so we don't re-derive.
        await refreshBiosStatus(appId);
      } else {
        showToast(result.message || "BIOS download failed");
      }
    } catch {
      showToast("BIOS download failed");
    } finally {
      setActionPending(null);
    }
  };

  const handleUninstall = async () => {
    if (actionPending || !detail.romId) return;
    setActionPending("uninstall");
    const admission = capturePruneLeaseAdmission(`game-detail:${appId}`);
    try {
      const result = await removeRom(detail.romId);
      if (result.success) {
        await withPruneLease(
          result.prune_lease_token,
          "Game detail uninstall",
          async (signal) => {
            if (isPruneLeaseCancelled(signal)) return;
            await setLaunchOptionsConfirmed(appId, "").catch(() => false);
            if (isPruneLeaseCancelled(signal)) return;
            globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: detail.romId } }));
          },
          `game-detail:${appId}`,
          admission,
        );
        showToast(`${detail.romName || "ROM"} uninstalled`);
      } else {
        showToast(result.message || "Uninstall failed");
      }
    } catch (e) {
      // The backend uninstall already committed before the continuation was torn
      // down; reporting it as a failure would be a lie the user can't act on.
      if (isPruneLeaseCancellation(e, admission)) {
        detach(debugLog(`handleUninstall: continuation was cancelled: ${e}`));
        return;
      }
      showToast("Uninstall failed");
    } finally {
      setActionPending(null);
    }
  };

  const handleDeleteSaves = () => {
    if (actionPending || !detail.romId) return;
    const romId = detail.romId;
    showModal(
      <ConfirmModal
        strTitle="Delete Local Saves"
        strDescription="This will delete local save files for this game. Make sure saves are synced to RomM first — the next sync will re-download them from the server."
        strOKButtonText="Delete"
        strCancelButtonText="Cancel"
        onOK={() => {
          detach(
            (async () => {
              setActionPending("deletesaves");
              try {
                const result = await deleteLocalSaves(romId);
                if (result.success) {
                  showToast(result.message);
                  // Directly update the shown status — no local saves remain
                  noteSaveSyncDisplay(appId, romId, { status: "none", label: "No saves", last_sync_check_at: null });
                  globalThis.dispatchEvent(
                    new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: romId } }),
                  );
                } else {
                  showToast(result.message || "Failed to delete saves");
                }
              } catch {
                showToast("Failed to delete saves");
              } finally {
                setActionPending(null);
              }
            })(),
          );
        }}
      />,
    );
  };

  /** Refresh the core badge + BIOS state from their dedicated paths and notify
   *  sibling components after a successful override pin/clear. */
  const refreshCoreDisplay = async (platformSlug: string) => {
    await refreshCoreAndBios(appId);
    globalThis.dispatchEvent(
      new CustomEvent("romm_data_changed", { detail: { type: "core_changed", platform_slug: platformSlug } }),
    );
  };

  /** Apply the result of a set/clear override call. The backend re-bakes the
   *  launch_options + returns the bound app_id for an installed ROM; we
   *  confirm-set them on the Steam shortcut BEFORE toasting success (R1). An
   *  unconfirmed bake gets a DISTINCT "restart Steam" toast and the DB row is
   *  KEPT — migration/re-sync re-bake from the pin. Uninstalled/unbound ROMs
   *  carry no launch_options/app_id: persist-only, no SetAppLaunchOptions. */
  const applyCoreResult = async (
    result: Awaited<ReturnType<typeof setGameCore>>,
    platformSlug: string,
    successBody: string,
    admission: PruneLeaseAdmission,
  ) => {
    await withPruneLease(result.prune_lease_token, "Core selection", async (signal) => {
      if (!result.success) {
        showToast(result.message || "Failed to set core");
        return;
      }
      // Installed + bound: confirm the re-baked launch_options landed before
      // claiming success. app_id can be null/undefined for an unbound ROM.
      if (result.launch_options !== undefined && result.app_id != null) {
        if (isPruneLeaseCancelled(signal)) return;
        const confirmed = await setLaunchOptionsConfirmed(result.app_id, result.launch_options);
        if (isPruneLeaseCancelled(signal)) return;
        if (!confirmed) {
          // Never toast success on an unconfirmed bake. Keep the DB row — a Steam
          // restart (or the next migration/re-sync) re-bakes from the override.
          showToast("Core saved — restart Steam to apply");
          return;
        }
      }
      // Confirmed (or uninstalled/unbound: nothing to confirm) → success.
      showToast(successBody);
      await refreshCoreDisplay(platformSlug);
    }, `game-detail:${appId}`, admission);
  };

  const handleChangeGameCore = async (coreLabel: string) => {
    const romId = detail.romId;
    if (!romId || !detail.platformSlug) return;
    const platformSlug = detail.platformSlug;
    detach(debugLog(`handleChangeGameCore: romId=${romId} coreLabel=${coreLabel}`));
    const admission = capturePruneLeaseAdmission(`game-detail:${appId}`);
    try {
      const result = await setGameCore(romId, coreLabel);
      detach(debugLog(`handleChangeGameCore: result success=${result.success}`));
      await applyCoreResult(result, platformSlug, `Core set to ${coreLabel}`, admission);
    } catch (e) {
      // The core pin is persisted before the Steam continuation runs, so a
      // teardown cancellation is not a "failed to set core" the user must see.
      if (isPruneLeaseCancellation(e, admission)) {
        detach(debugLog(`handleChangeGameCore: continuation was cancelled: ${e}`));
        return;
      }
      showToast("Failed to set core");
    }
  };

  const handleResetGameCore = async () => {
    const romId = detail.romId;
    if (!romId || !detail.platformSlug) return;
    const platformSlug = detail.platformSlug;
    detach(debugLog(`handleResetGameCore: romId=${romId}`));
    const admission = capturePruneLeaseAdmission(`game-detail:${appId}`);
    try {
      const result = await clearGameCore(romId);
      detach(debugLog(`handleResetGameCore: result success=${result.success}`));
      await applyCoreResult(result, platformSlug, "Now following the system core", admission);
    } catch (e) {
      if (isPruneLeaseCancellation(e, admission)) {
        detach(debugLog(`handleResetGameCore: continuation was cancelled: ${e}`));
        return;
      }
      showToast("Failed to reset core");
    }
  };

  const showCoreMenu = (e: Event) => {
    // Both pickers share the builder in utils/emulatorMenu. The game-detail menu
    // carries the "Use System Override" reset item (the only clear path, #211);
    // every bakeable emulator entry PINS the per-game override.
    showContextMenu(
      buildEmulatorMenu({
        emulators: detail.emulators,
        emulatorDataAvailable: detail.emulatorDataAvailable,
        activeLabel: detail.activeCoreLabel,
        platformCoreLabel: detail.platformCoreLabel,
        followSystem: {
          hasGameOverride: detail.hasGameOverride,
          onFollowSystem: () => {
            detach(handleResetGameCore());
          },
        },
        onPick: (label) => {
          detach(handleChangeGameCore(label));
        },
      }),
      getEventTarget(e),
    );
  };


  const showRomMMenu = (e: Event) => {
    showContextMenu(
      <Menu label="RomM Actions">
        <MenuItem
          key="refresh-artwork"
          onClick={() => {
            detach(handleRefreshArtwork());
          }}
        >
          Refresh Artwork
        </MenuItem>
        <MenuItem
          key="refresh-metadata"
          onClick={() => {
            detach(handleRefreshMetadata());
          }}
        >
          Refresh Metadata
        </MenuItem>
        <MenuItem
          key="sync-saves"
          onClick={() => {
            detach(handleSyncSaves());
          }}
        >
          Sync Save Files
        </MenuItem>
        <MenuItem
          key="download-bios"
          onClick={() => {
            detach(handleDownloadBios());
          }}
        >
          Download BIOS
        </MenuItem>
        <MenuSeparator key="sep" />
        <MenuItem key="delete-saves" tone="destructive" onClick={handleDeleteSaves}>
          Delete Local Saves
        </MenuItem>
        <MenuItem
          key="uninstall"
          tone="destructive"
          onClick={() => {
            detach(handleUninstall());
          }}
        >
          Uninstall
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  const showSteamMenu = (e: Event) => {
    showContextMenu(
      <Menu label="Steam">
        <MenuItem
          key="properties"
          onClick={() => {
            SteamClient.Apps.OpenAppSettingsDialog(appId, "general");
          }}
        >
          Properties
        </MenuItem>
      </Menu>,
      getEventTarget(e),
    );
  };

  // Version mismatch — render nothing (VersionErrorCard is shown in RomMGameInfoPanel instead)
  if (versionError) {
    return null;
  }

  // Pending RetroDECK migration — render nothing (MigrationBlockedCard is shown in RomMGameInfoPanel instead)
  if (migration.pending) {
    return null;
  }

  // Build info items array
  const infoItems: ReactElement[] = [];

  // Offline indicator (first — most prominent)
  if (connectionState === "offline") {
    infoItems.push(
      <div key="offline-indicator" className="romm-info-item">
        <div className="romm-info-header">
          <FaExclamationTriangle size={12} color="#ff8800" />
        </div>
        <div className="romm-info-value" style={{ color: "#ff8800" }}>
          RomM offline
        </div>
      </div>,
    );
  }

  // Space Required (#1395) — the download footprint, shown only for an
  // uninstalled ROM whose size is known. Placed FIRST to match Steam's native
  // game detail: an uninstalled title leads with "Space Required" ahead of Last
  // Played / Playtime, and the cell disappears once the ROM is on disk. Being
  // first also keeps it clear of the romm-info-items nowrap + overflow:hidden
  // clip, which drops cells from the right edge.
  if (!detail.installed && detail.fsSizeBytes != null) {
    infoItems.push(infoItem("space-required", "SPACE REQUIRED", formatBytes(detail.fsSizeBytes)));
  }

  // Last Played
  if (playtimeInfo.lastPlayed) {
    infoItems.push(infoItem("last-played", "LAST PLAYED", playtimeInfo.lastPlayed));
  }

  // Playtime
  if (playtimeInfo.playtime) {
    infoItems.push(infoItem("playtime", "PLAYTIME", playtimeInfo.playtime));
  }

  // Achievements badge (only when RA data available)
  if (detail.raId) {
    const hasEarned = detail.achievementEarned > 0;
    const countLabel =
      detail.achievementTotal > 0 ? `${detail.achievementEarned}/${detail.achievementTotal}` : `${detail.achievementEarned}`;

    // Generate sparkle dots at random fixed positions (only when earned > 0)
    // Positions are deterministic per-index so they don't shift on re-render
    const sparklePositions = [
      { top: "5%", left: "80%" },
      { top: "70%", left: "10%" },
      { top: "15%", left: "35%" },
      { top: "85%", left: "70%" },
      { top: "45%", left: "90%" },
    ];
    const sparkleDurs = [2.4, 3.5, 2.8, 3.8, 3.1];
    const sparkleDelays = [0, 0.9, 0.3, 1.6, 1.1];
    const sparkleDots = hasEarned
      ? sparklePositions.map((pos, i) => {
          // Hoisted and annotated, not inlined into `style={{...}}`: an inline
          // literal is excess-property checked against React's own
          // CSSProperties, which rejects the `--*` keys outright.
          const dotStyle: CSSPropertiesWithVars = {
            "--romm-sparkle-top": pos.top,
            "--romm-sparkle-left": pos.left,
            "--romm-sparkle-delay": `${sparkleDelays[i]}s`,
            "--romm-sparkle-dur": `${sparkleDurs[i]}s`,
          };
          return <span key={`sparkle-${pos.top}-${pos.left}`} className="romm-sparkle-dot" style={dotStyle} />;
        })
      : [];

    infoItems.push(
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- pointer-only shortcut into the ACHIEVEMENTS tab, which the tab bar's DialogButton already reaches from the focus ring; a role/tabIndex here would add a gamepad focus stop to the play row.
      <div
        key="achievements"
        className="romm-info-item romm-cheevo-badge"
        onClick={() => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
        }}
      >
        <div className="romm-info-header">ACHIEVEMENTS</div>
        <div className="romm-cheevo-badge-sparkle">
          {/* Trophy icon with sparkle container */}
          <span style={{ position: "relative", display: "inline-block" }}>
            <span className={hasEarned ? "romm-cheevo-trophy" : "romm-cheevo-trophy-none"}>{"\uD83C\uDFC6"}</span>
            {hasEarned ? <span className="romm-sparkle-container">{sparkleDots}</span> : null}
          </span>
          <span className="romm-cheevo-count">{countLabel}</span>
        </div>
      </div>,
    );
  }

  // Save Sync moved to dedicated tab — show legacy slot warning only
  if (detail.activeSlot == null && detail.saveSyncEnabled) {
    infoItems.push(
      <div key="legacy-slot-warning" className="romm-info-item">
        <div className="romm-info-header">SAVE SYNC</div>
        <div style={{ fontSize: "11px", color: "#ff8800", marginTop: "4px" }}>{"\u26A0 Legacy save slot"}</div>
      </div>,
    );
  }

  // BIOS warning (only when files are actually missing — "ok" and "unmanaged"
  // are non-actionable here and live in the BIOS tab, not on the play section)
  if (detail.biosNeeded && detail.biosStatus && detail.biosStatus !== "ok" && detail.biosStatus !== "unmanaged") {
    const biosColor = biosColorForLevel(detail.biosStatus);
    infoItems.push(
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions -- pointer-only shortcut into the BIOS tab, which the tab bar's DialogButton already reaches from the focus ring; a role/tabIndex here would add a gamepad focus stop to the play row.
      <div
        key="bios"
        className="romm-info-item"
        onClick={() => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        }}
        style={{ cursor: "pointer" }}
      >
        <div className="romm-info-header">BIOS</div>
        <div className="romm-info-value" style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <span className="romm-status-dot" style={{ backgroundColor: biosColor }} />
          {detail.biosLabel}
        </div>
      </div>,
    );
  }

  const playSectionRow = (
    <Focusable
      key="play-row"
      data-romm="true"
      className={`romm-play-section-row ${basicAppDetailsSectionStylerClasses?.PlaySection || ""}`.trim()}
      flow-children="right"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "20px",
        padding: "16px 2.8vw",
        background: "rgba(14, 20, 27, 0.33)",
        boxSizing: "border-box",
      }}
    >
      {/* Play button on the left */}
      <CustomPlayButton appId={appId} />
      {/* Disc picker for multi-disc ROMs — renders nothing otherwise (#865) */}
      <DiscSelector appId={appId} />
      {/* Version picker for multi-version sibling groups — renders nothing otherwise (#1297) */}
      <VersionPicker appId={appId} />
      {/* Info items row */}
      <div
        className="romm-info-items"
        style={{
          display: "flex",
          alignItems: "center",
          gap: "20px",
          flexWrap: "nowrap",
          overflow: "hidden",
        }}
      >
        {infoItems}
      </div>
      {/* Gear icon buttons pushed to the far right */}
      <div
        style={{
          marginLeft: "auto",
          display: "flex",
          alignItems: "center",
          gap: "8px",
          flexShrink: 0,
        }}
      >
        {/* RomM actions button */}
        <DialogButton className="romm-gear-btn" onClick={showRomMMenu} onFocus={scrollToTop} title="RomM Actions">
          <FaGamepad size={18} color="#553e98" />
        </DialogButton>
        {/* Core selection button (only when multiple emulators to choose between) */}
        {detail.emulators.length > 1 ? (
          <DialogButton
            key="core-btn"
            className="romm-gear-btn"
            onClick={showCoreMenu}
            onFocus={scrollToTop}
            title="Emulator Core"
          >
            <FaMicrochip size={18} color={detail.activeCoreIsDefault ? "#8f98a0" : "#d4a72c"} />
          </DialogButton>
        ) : null}
        {/* Steam properties button */}
        <DialogButton className="romm-gear-btn" onClick={showSteamMenu} onFocus={scrollToTop} title="Steam Properties">
          <FaCog size={18} color="#8f98a0" />
        </DialogButton>
      </div>
    </Focusable>
  );

  // Content-dir warning (#239) — prominent banner above the play row when
  // RetroArch writes saves to the content directory. The play row still renders
  // below it: the game remains fully playable, only save sync is unavailable.
  // The store holds the flag whether or not save sync is on (it is a fact about
  // the local RetroArch config, not about our setting); the banner asks the user
  // to change that config to re-enable save sync, so it is only worth showing to
  // someone who has save sync on.
  //
  // The banner is a keyed sibling under a Fragment, never a branch returning a
  // different root: the flag lands a moment after the row first paints, and a
  // root whose element type changes makes React unmount the whole row — the play
  // button drops back to "loading" and re-runs its init (#1682). A Fragment adds
  // no DOM node, so the row stays a direct child of the injected panel either way.
  return (
    <Fragment>
      {detail.savefilesInContentDir && detail.saveSyncEnabled ? (
        <WarningCard
          key="savefiles-content-dir-warning"
          title="Save sync off"
          message="RetroArch's 'Write Saves to Content Directory' is enabled, so saves go next to the ROM and can't be synced. Turn it off in RetroArch → Settings → Saving to re-enable save sync."
        />
      ) : null}
      {playSectionRow}
    </Fragment>
  );
};
