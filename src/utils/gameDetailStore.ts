/**
 * Per-appId game-detail store — the shared state of one Steam game page.
 *
 * A game page mounts several RomM surfaces at once (the play section today, the
 * play button and the info panel next). Each used to fetch the cached detail,
 * keep its own `romId` / install / save-sync / BIOS / core copies, and run its
 * own near-duplicate `romm_data_changed` handlers, so the copies could disagree
 * (#993). Here there is one entry per appId: the first subscriber opens it,
 * loads the detail, and attaches the entry's event listeners; the last
 * unsubscribe tears the whole entry down.
 *
 * The DOM bus is unchanged — it still carries the notifications. What moved is
 * the state: one handler per appId folds an event into the entry, and every
 * subscriber re-renders from the same fold.
 *
 * Overlapping reads for one appId share a single request ({@link
 * refreshSaveStatus}), so a payload-less `save_sync` notification costs one
 * `get_save_status` round-trip however many surfaces are mounted.
 *
 * Follows the module-scope store shape of utils/connectionState.ts — state in
 * the module, a getter, a subscribe that hands back its own unsubscribe, and a
 * `use…()` hook over the same state — keyed by appId, and with the subscription
 * owning the entry's lifetime rather than only carrying a callback.
 */

import { useCallback, useSyncExternalStore, type Dispatch, type SetStateAction } from "react";
import { addEventListener, removeEventListener } from "@decky/api";
import {
  debugLog,
  getBiosStatus,
  getCachedGameDetail,
  getPlatformCoreInfo,
  getRomMetadata,
  getSaveStatus,
  invalidateCachedGameDetail,
  isCallableFailure,
  logError,
} from "../api/backend";
import type { DownloadCompleteEvent, SaveStatus, SaveSyncDisplay } from "../types";
import type { RommDataChangedDetail } from "../types/events";
import { detach } from "./detach";
import {
  applySaveSyncDisplay,
  extractBiosInfo,
  extractCoreInfo,
  resolveSaveSyncLabel,
  type BiosInfoFields,
  type CoreInfoFields,
} from "./playSection";
import {
  refreshAchievementsInBackground,
  refreshBiosInBackground,
  refreshCoreInfoInBackground,
} from "./sectionRefresh";

/** The state one game page's surfaces share. BIOS and core-selection fields are
 *  flat (rather than nested) because the background refresh helpers in
 *  sectionRefresh.ts merge themselves into any state carrying those fields. */
export interface GameDetailState extends BiosInfoFields, CoreInfoFields {
  /** The rom_id bound to this appId, or `null` while the cached detail is still
   *  in flight and for an appId RomM does not know. Every rom-keyed read and
   *  every incoming notification is matched against it. */
  romId: number | null;
  romName: string;
  platformSlug: string;
  installed: boolean;
  /** Server-reported ROM size in bytes, or `null` when unknown. */
  fsSizeBytes: number | null;
  saveSyncEnabled: boolean;
  /** The last save status read for this ROM, `null` until one lands. Carries the
   *  conflicts and slot detail the saves surfaces need; the resolved display
   *  pair below is the same payload projected for the badges. */
  saveStatus: SaveStatus | null;
  saveSyncStatus: "synced" | "pending" | "conflict" | "none" | null; // NOSONAR(typescript:S4323) — inline union inside GameDetailState; extracting an alias adds indirection for no reuse benefit.
  saveSyncLabel: string;
  /** RetroArch `savefiles_in_content_dir=true` — saves go next to the ROM and
   *  can't be synced (#239). Derived from a LOCAL retroarch.cfg read, so it is
   *  populated even while RomM is unreachable. */
  savefilesInContentDir: boolean;
  activeSlot: string | null;
  raId: number | null;
  achievementEarned: number;
  achievementTotal: number;
}

const DEFAULT_STATE: GameDetailState = {
  romId: null,
  romName: "",
  platformSlug: "",
  installed: false,
  fsSizeBytes: null,
  saveSyncEnabled: false,
  saveStatus: null,
  saveSyncStatus: null,
  saveSyncLabel: "",
  savefilesInContentDir: false,
  activeSlot: "default",
  raId: null,
  achievementEarned: 0,
  achievementTotal: 0,
  biosNeeded: false,
  biosStatus: null,
  biosLabel: "",
  activeCoreLabel: null,
  activeCoreIsDefault: true,
  emulators: [],
  emulatorDataAvailable: true,
  platformCoreLabel: null,
  hasGameOverride: false,
};

type BiosStatusResult = Awaited<ReturnType<typeof getBiosStatus>>;

/** Stand-in for a BIOS read that failed, carrying the backend's own "no answer"
 *  flag. Every reader routes it through `extractBiosInfo`, which refuses to
 *  project it — so a failed read leaves the shown level exactly where it was,
 *  the same as an answer the backend itself could not determine (#1693). */
const UNKNOWN_BIOS_STATUS: BiosStatusResult = {
  bios_status: null,
  bios_level: null,
  bios_label: null,
  bios_status_unknown: true,
};

/** A save-status read still in flight, together with the rom identity it was
 *  issued for. A version switch re-keys the entry to a new rom_id without
 *  closing it, so the generation is unchanged and the identity is the only thing
 *  that distinguishes an answer about this page's ROM from one about its
 *  predecessor. */
interface InFlightSaveStatus {
  romId: number;
  promise: Promise<SaveStatus | null>;
}

interface Entry {
  state: GameDetailState;
  listeners: Set<(state: GameDetailState) => void>;
  /** Bumped when the entry is torn down, so a read that resolves afterwards
   *  cannot write into a state nobody is showing any more. */
  generation: number;
  /** Bumped by every load. Two loads for one appId can be open at once — a
   *  version switch and a re-read both re-derive the entry without closing it —
   *  and can finish in either order, so a load that no longer holds this number
   *  has been overtaken and writes nothing. */
  loadSeq: number;
  saveStatusInFlight: InFlightSaveStatus | null;
  detachListeners: () => void;
}

const _entries = new Map<number, Entry>();

/** Read this appId's shared state without subscribing. Returns the neutral
 *  default for an appId no surface is currently subscribed to. */
export function getGameDetail(appId: number): GameDetailState {
  return _entries.get(appId)?.state ?? DEFAULT_STATE;
}

/**
 * Subscribe to one appId's shared state. The first subscriber opens the entry —
 * loading the cached detail and attaching the entry's event listeners — and the
 * last unsubscribe closes it again, so nothing is left listening for a game page
 * that is no longer on screen.
 */
export function subscribeGameDetail(appId: number, cb: (state: GameDetailState) => void): () => void {
  const entry = openEntry(appId);
  entry.listeners.add(cb);
  return () => {
    entry.listeners.delete(cb);
    if (entry.listeners.size === 0 && _entries.get(appId) === entry) closeEntry(appId, entry);
  };
}

/** Subscribe to one appId's shared state from a component. Re-renders the
 *  caller on every change; drops its subscription on unmount. Reads through
 *  React's external-store primitive rather than mirroring into local state, so
 *  a component handed a different appId renders that appId's state on the same
 *  pass instead of its predecessor's. */
export function useGameDetail(appId: number): GameDetailState {
  const subscribe = useCallback((onChange: () => void) => subscribeGameDetail(appId, onChange), [appId]);
  const snapshot = useCallback(() => getGameDetail(appId), [appId]);
  return useSyncExternalStore(subscribe, snapshot);
}

function openEntry(appId: number): Entry {
  const existing = _entries.get(appId);
  if (existing) return existing;
  const entry: Entry = {
    state: DEFAULT_STATE,
    listeners: new Set(),
    generation: 0,
    loadSeq: 0,
    saveStatusInFlight: null,
    detachListeners: () => {},
  };
  _entries.set(appId, entry);
  entry.detachListeners = attachListeners(appId, entry);
  detach(loadDetail(appId, entry));
  return entry;
}

function closeEntry(appId: number, entry: Entry): void {
  entry.generation++;
  entry.saveStatusInFlight = null;
  entry.detachListeners();
  _entries.delete(appId);
}

/** A `setState`-shaped writer bound to one generation of one entry: it applies
 *  the update and notifies subscribers, and no-ops once that generation is gone.
 *  Shaped this way so the sectionRefresh helpers can write straight into the
 *  entry the way they write into a component's state. */
function writerFor(entry: Entry, generation: number): Dispatch<SetStateAction<GameDetailState>> {
  return (update) => {
    if (entry.generation !== generation) return;
    entry.state = typeof update === "function" ? update(entry.state) : update;
    entry.listeners.forEach((listener) => listener(entry.state));
  };
}

/** A writer additionally bound to the rom identity a read was issued for: it
 *  refuses a fold once the entry has moved on to another ROM. A version switch
 *  re-keys the entry without closing it, so the generation is unchanged, and for
 *  a read issued outside a load — a BIOS re-read, a core-change handler — the
 *  identity is the only thing separating this page's answer from its
 *  predecessor's. A read issued by {@link loadDetail} is fenced by that load's
 *  sequence number too, which separates two answers about the SAME ROM.
 *
 *  The gate is the question, not the answer: it compares the rom the read was
 *  ISSUED for against the entry's rom now, where {@link applySaveStatus} can
 *  compare the answer's own `rom_id` and so also catch a payload about the wrong
 *  ROM. Neither `CoreInfo` nor the `get_bios_status` payload carries a rom id,
 *  so that stronger form is not available to the core and BIOS reads. */
function writerForRom(entry: Entry, generation: number, romId: number): Dispatch<SetStateAction<GameDetailState>> {
  const write = writerFor(entry, generation);
  return (update) => {
    if (entry.state.romId !== romId) return;
    write(update);
  };
}

/**
 * Cache-first load: resolve the cached game detail for this appId, fold it into
 * the entry, and fire the background refreshes (save status, metadata,
 * achievements, BIOS, core) whose results are merged in as they land — unless a
 * version switch re-keyed the entry to another ROM while one was open.
 *
 * Loads are ordered by {@link Entry.loadSeq} rather than by the order their
 * reads happen to resolve: a load a later one has overtaken writes nothing at
 * all. The identity write below is why that fence has to exist alongside the rom
 * binding — it is what INSTALLS the identity every other write compares itself
 * against, so binding it to a rom would refuse the very switch that re-keys the
 * entry (#1674). An overtaken load therefore leaves the newer load's identity
 * standing, including when that newer load found nothing to install: the older
 * answer describes a state the newer load has already been told is gone.
 */
async function loadDetail(appId: number, entry: Entry): Promise<void> {
  const generation = entry.generation;
  const loadSeq = ++entry.loadSeq;
  // Two separate ends: the generation covers an entry nobody is showing any
  // more, the sequence a load a later one has moved past.
  const cancelled = () => entry.generation !== generation || entry.loadSeq !== loadSeq;
  const write = writerFor(entry, generation);
  try {
    const cached = await getCachedGameDetail(appId);
    const romId = cached.rom_id;
    // A detail without a rom_id is nothing this store can key on — every read
    // below and every notification match runs off that identity.
    if (cancelled() || !cached.found || !romId) return;

    let saveSyncStatus: GameDetailState["saveSyncStatus"] = null;
    let saveSyncLabel = "";
    if (cached.save_sync_enabled && cached.save_sync_display) {
      saveSyncStatus = cached.save_sync_display.status;
      saveSyncLabel = resolveSaveSyncLabel(cached.save_sync_display);
    }

    write((prev) => ({
      ...prev,
      romId,
      romName: cached.rom_name || "",
      platformSlug: cached.platform_slug || "",
      installed: cached.installed ?? false,
      fsSizeBytes: cached.fs_size_bytes ?? null,
      saveSyncEnabled: cached.save_sync_enabled ?? false,
      saveSyncStatus,
      saveSyncLabel,
      raId: cached.ra_id ?? null,
      // Keep the last-known count when the re-derived cache has no achievement
      // summary (e.g. a version switch during an offline window, where the
      // backend progress cache is cold) — don't degrade a shown "7/70" to 0
      // (#1345). A genuine earned:0 is a real object, so it still shows through.
      achievementEarned: cached.achievement_summary?.earned ?? prev.achievementEarned,
      achievementTotal: cached.achievement_summary?.total ?? prev.achievementTotal,
    }));

    // Every background answer below is bound to the rom THIS load resolved, so a
    // version switch landing while one is open cannot fold it into a page that
    // has moved on — and, through `cancelled`, to this load, so an answer a
    // later load of the SAME rom has already superseded is dropped rather than
    // overwriting the newer one. Two writes stay on the unbound `write`: the
    // identity write above, which installs the very identity a rom binding would
    // check against, and the `cached.bios_status` fold below. Both run in the
    // same tick as the `cancelled()` check that admitted this load, with no
    // await in between for a newer load to land in.
    const writeForRom = writerForRom(entry, generation, romId);

    // The live save status carries what the cached detail does not: the active
    // slot, the content-dir flag, and the conflicts. Fire-and-forget — a failed
    // read leaves the cached display standing, and a caller that needs to know
    // reads it again itself.
    if (cached.save_sync_enabled) detach(refreshSaveStatus(appId));

    const staleFields = cached.stale_fields ?? [];

    if (staleFields.includes("metadata")) {
      getRomMetadata(romId).catch((e) => debugLog(`Background metadata fetch error: ${e}`));
    }

    if (cached.ra_id && staleFields.includes("achievements")) {
      refreshAchievementsInBackground(romId, cancelled, writeForRom);
    }

    // Folded whether or not the detail reports a requirement: an absent
    // `bios_status` is the backend answering "this version's core needs none",
    // and only an unconditional fold can take a shown requirement back (#1690).
    // A detail derived while the firmware cache was cold carries no answer at
    // all and is refused here instead (#1693) — the `bios` stale field below
    // then brings the live one.
    const cachedBios = extractBiosInfo(cached);
    if (cachedBios) write((prev) => ({ ...prev, ...cachedBios }));

    if (staleFields.includes("bios")) {
      refreshBiosInBackground(romId, cancelled, writeForRom);
    }

    // Core info is sourced from its OWN path (#923), independent of BIOS status,
    // and keyed on rom_id so the active core reflects a per-game DB override
    // (epic #945).
    refreshCoreInfoInBackground(romId, cancelled, writeForRom);
  } catch (e) {
    // Shared by the mount load and the version-switch re-derive — a failed cache
    // load leaves every subscribed surface stale either way, so surface at warn
    // level (debugLog is dropped at the default log level).
    logError(`gameDetailStore: load error: ${e}`);
  }
}

/**
 * Read this ROM's save status and fold it into the entry.
 *
 * Callers that overlap share one request: the second caller gets the first
 * caller's promise instead of a second `get_save_status` round-trip — but only
 * while it is a read of the rom_id bound to this appId now. A read left open
 * across a version switch is about the previous ROM, so a caller arriving after
 * the switch gets a fresh read rather than the answer to a question about
 * another game.
 *
 * Resolves to `null` — leaving the shown display untouched — when the identity
 * is not resolved yet, or when the backend refuses the read (a prune-active
 * refusal is not a save-status answer). Rejects when the call itself fails, so
 * each caller reports the failure in its own terms. Whether a ROM with save sync
 * switched off is worth reading at all is the caller's call, not this one's.
 */
export function refreshSaveStatus(appId: number): Promise<SaveStatus | null> {
  const entry = _entries.get(appId);
  if (!entry) return Promise.resolve(null);
  const romId = entry.state.romId;
  if (!romId) return Promise.resolve(null);
  const inFlight = entry.saveStatusInFlight;
  if (inFlight?.romId === romId) return inFlight.promise;

  const request = readSaveStatus(entry, romId, entry.generation);
  entry.saveStatusInFlight = { romId, promise: request };
  // Free the slot once this request settles, whichever way it settles. Both
  // arms of `then` are the same bookkeeping, and giving it a rejection arm is
  // what keeps this branch from surfacing as an unhandled rejection — the
  // rejection callers see is the one on `request` itself.
  const release = () => {
    if (entry.saveStatusInFlight?.promise === request) entry.saveStatusInFlight = null;
  };
  request.then(release, release);
  return request;
}

/** The read itself, as an async function so a synchronous throw comes back as a
 *  rejection like every other failure — a caller that gets a promise must not
 *  also have to guard the call. */
async function readSaveStatus(entry: Entry, romId: number, generation: number): Promise<SaveStatus | null> {
  const result = await getSaveStatus(romId);
  if (isCallableFailure(result)) {
    detach(debugLog(`gameDetailStore: save status refused: ${result.message}`));
    return null;
  }
  applySaveStatus(entry, generation, result);
  return result;
}

/** Fold a save status — freshly read or carried on a notification — into the
 *  entry. The single place a save status becomes shown state, so no two
 *  surfaces can derive it differently, and the last gate on whose status it is:
 *  a status whose `rom_id` is not the one bound to this appId is another game's
 *  (#975), whether it arrived on a notification carrying no `rom_id` of its own
 *  or from a read issued before a version switch re-keyed the entry. */
function applySaveStatus(entry: Entry, generation: number, status: SaveStatus): void {
  if (status.rom_id !== entry.state.romId) return;
  const { status: saveSyncStatus, label: saveSyncLabel } = applySaveSyncDisplay(status.save_sync_display, status);
  writerFor(
    entry,
    generation,
  )((prev) => ({
    ...prev,
    saveStatus: status,
    saveSyncStatus,
    saveSyncLabel,
    savefilesInContentDir: status.savefiles_in_content_dir === true,
    activeSlot: "active_slot" in status ? (status.active_slot ?? null) : prev.activeSlot,
  }));
}

/** Record a save-sync display a surface already knows to be true about `romId` —
 *  the "Just now" after a manual sync, the "No saves" after a local delete —
 *  without waiting for a round-trip to confirm it. Refused once the entry has
 *  moved on to another ROM: every caller reaches this after an await, so a
 *  version switch landing in that window would otherwise put a display on the
 *  page of a ROM that never earned it (#1673). */
export function noteSaveSyncDisplay(appId: number, romId: number, display: SaveSyncDisplay): void {
  const entry = _entries.get(appId);
  if (!entry) return;
  writerForRom(
    entry,
    entry.generation,
    romId,
  )((prev) => ({
    ...prev,
    saveSyncStatus: display.status,
    saveSyncLabel: resolveSaveSyncLabel(display),
  }));
}

/** Re-read the BIOS answer after a firmware download. Leaves what is shown
 *  alone when the read fails, when it reports no BIOS need, or when a version
 *  switch re-keyed the entry while the read was open. Downloading firmware
 *  cannot remove a requirement — it only fills one, or reveals one the cache had
 *  not seen — so this path adopts a refreshed answer and never clears one. */
export async function refreshBiosStatus(appId: number): Promise<void> {
  const entry = _entries.get(appId);
  const romId = entry?.state.romId;
  if (!entry || !romId) return;
  const generation = entry.generation;
  const refreshed = await getBiosStatus(romId).catch(() => UNKNOWN_BIOS_STATUS);
  const biosInfo = extractBiosInfo(refreshed);
  if (!biosInfo?.biosNeeded) return;
  writerForRom(entry, generation, romId)((prev) => ({ ...prev, ...biosInfo }));
}

/**
 * Re-read core selection and BIOS after an override was pinned or cleared, and
 * drop the cached detail so the next read sees the new core.
 *
 * The BIOS fields are re-derived from an ANSWER rather than kept: the active
 * core just changed, so the requirement may have changed with it (#923). A read
 * that could not answer is not that — it leaves the shown level standing while
 * the core half of the fold still lands, which is the difference between a core
 * switch that reports its new BIOS state and one that silently drops a
 * missing-BIOS warning off a game that still needs it (#1693).
 *
 * A version switch landing while the two reads are open re-keys the entry, and
 * the answers are then about the ROM it moved off — so the fold is refused. The
 * cache drop still runs either way: it only forces the next read to go to the
 * backend, so it cannot show anything wrong, while skipping it would make the
 * cache's correctness depend on the switch path having dropped it first.
 */
export async function refreshCoreAndBios(appId: number): Promise<void> {
  const entry = _entries.get(appId);
  const romId = entry?.state.romId;
  if (!entry || !romId) return;
  const generation = entry.generation;
  const [coreInfo, refreshed] = await Promise.all([
    getPlatformCoreInfo(romId),
    getBiosStatus(romId).catch(() => UNKNOWN_BIOS_STATUS),
  ]);
  const biosInfo = extractBiosInfo(refreshed);
  writerForRom(
    entry,
    generation,
    romId,
  )((prev) => ({
    ...prev,
    ...extractCoreInfo(coreInfo),
    ...biosInfo,
  }));
  invalidateCachedGameDetail(appId);
}

/** Re-read the cached detail after something invalidated it. A re-read is a new
 *  load and so takes the newest sequence number at the moment it is issued: no
 *  load issued before it can undo it. It is not exempt from the fence — a load
 *  issued after it refuses it in turn, which is what keeps two re-reads in
 *  order. */
async function reloadDetail(appId: number, entry: Entry): Promise<void> {
  invalidateCachedGameDetail(appId);
  await loadDetail(appId, entry);
}

async function handleSaveSyncSettingsChange(
  appId: number,
  entry: Entry,
  detail: Extract<RommDataChangedDetail, { type: "save_sync_settings" }>,
): Promise<void> {
  if (!detail.save_sync_enabled) {
    // Only the sync-derived display is cleared. `savefilesInContentDir` stays as
    // read: it describes the local RetroArch config, which the setting does not
    // change, and each surface decides for itself whether the fact is worth
    // showing while sync is off.
    writerFor(
      entry,
      entry.generation,
    )((prev) => ({
      ...prev,
      saveSyncEnabled: false,
      saveSyncStatus: null,
      saveSyncLabel: "",
    }));
    return;
  }
  writerFor(entry, entry.generation)((prev) => ({ ...prev, saveSyncEnabled: true }));
  await refreshSaveStatus(appId).catch(() => null);
}

async function handleSaveSyncChange(
  appId: number,
  entry: Entry,
  detail: Extract<RommDataChangedDetail, { type: "save_sync" }>,
): Promise<void> {
  const romId = entry.state.romId;
  // An event that cannot be proven to be about THIS game is dropped, and an
  // unresolved rom_id proves nothing (#975): adopting the event's own rom_id
  // here would show another game's save status on this page. Nothing is lost —
  // the load in flight brings this ROM's status with it.
  if (!romId) return;
  if (detail.rom_id && detail.rom_id !== romId) return;
  if (detail.save_status) {
    applySaveStatus(entry, entry.generation, detail.save_status);
    return;
  }
  await refreshSaveStatus(appId).catch(() => null);
}

async function handleCoreChange(entry: Entry): Promise<void> {
  const romId = entry.state.romId;
  if (!romId) return;
  const generation = entry.generation;
  // Core data comes from the dedicated core-info path (#923), keyed on rom_id so
  // the active core reflects a per-game DB override (epic #945). BIOS
  // level/label still come from the (now core-free) BIOS status — the active
  // core just switched, so the BIOS requirements may have changed.
  //
  // Same user action as refreshCoreAndBios, so the same answer to a BIOS read
  // that failed: the core half still folds, and the shown level stands rather
  // than the whole fold being lost to the handler's catch (#1693).
  const [coreInfo, biosResult] = await Promise.all([
    getPlatformCoreInfo(romId),
    getBiosStatus(romId).catch(() => UNKNOWN_BIOS_STATUS),
  ]);
  const biosInfo = extractBiosInfo(biosResult);
  writerForRom(
    entry,
    generation,
    romId,
  )((prev) => ({
    ...prev,
    ...extractCoreInfo(coreInfo),
    ...biosInfo,
  }));
}

function attachListeners(appId: number, entry: Entry): () => void {
  const onDataChanged = (e: Event) => {
    detach(
      (async () => {
        try {
          const detail = (e as CustomEvent).detail;
          switch (detail?.type) {
            case "save_sync_settings":
              await handleSaveSyncSettingsChange(appId, entry, detail);
              break;
            case "core_changed":
              await handleCoreChange(entry);
              break;
            case "save_sync":
              await handleSaveSyncChange(appId, entry, detail);
              break;
            case "version_switched":
              // A version switch re-bound this appId to a new rom_id. The picker
              // already invalidated the cached detail, so re-deriving the whole
              // entry is enough to re-key every rom_id-driven read.
              if (detail.app_id === appId) await loadDetail(appId, entry);
              break;
            case "rom_adopted":
              // Adoption writes an install record without a download, so no
              // `download_complete` fires — but `installed` and `fs_size_bytes`
              // changed just the same and the cached detail must be dropped.
              if (detail.rom_id === entry.state.romId) await reloadDetail(appId, entry);
              break;
          }
        } catch (err) {
          detach(debugLog(`gameDetailStore: onDataChanged error: ${err}`));
        }
      })(),
    );
  };
  globalThis.addEventListener("romm_data_changed", onDataChanged);

  // Install-state reactivity (#1395): a download or an uninstall changes
  // `installed` + `fs_size_bytes`, so the cached detail is dropped first and the
  // whole entry re-derived — reading it back inside the 3s TTL would return the
  // pre-change state.
  const onDownloadComplete = addEventListener<[DownloadCompleteEvent]>("download_complete", (evt) => {
    if (evt.rom_id !== entry.state.romId) return;
    detach(reloadDetail(appId, entry));
  });

  const onUninstalled = (e: Event) => {
    const romId = (e as CustomEvent).detail?.rom_id;
    if (romId !== entry.state.romId) return;
    detach(reloadDetail(appId, entry));
  };
  globalThis.addEventListener("romm_rom_uninstalled", onUninstalled);

  return () => {
    globalThis.removeEventListener("romm_data_changed", onDataChanged);
    removeEventListener("download_complete", onDownloadComplete);
    globalThis.removeEventListener("romm_rom_uninstalled", onUninstalled);
  };
}
