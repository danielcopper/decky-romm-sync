/**
 * RomMGameInfoPanel — metadata and actions panel injected below the PlaySection
 * on RomM game detail pages.
 *
 * Layout:
 *   Game Info:    Platform, Description, Developer/Publisher, Genre tags, Release date
 *   ROM File:     Filename (only when installed)
 *   BIOS:         Status (only when platform needs BIOS)
 *   Save Sync:    Status (only when save sync enabled)
 *   Purely informational — all actions live in RomMPlaySection gear menu.
 *
 * Uses createElement throughout (no JSX) to match the RomMPlaySection pattern.
 * CSS classes prefixed with `romm-panel-` are injected separately by styleInjector.
 */

import { useState, useEffect, useRef, FC, createElement } from "react";
import { addEventListener, removeEventListener } from "@decky/api";
import { DialogButton, Focusable } from "@decky/ui";
import {
  getCachedGameDetail,
  invalidateCachedGameDetail,
  getRomMetadata,
  getInstalledRom,
  checkPlatformBios,
  getPlatformCoreInfo,
  getSaveStatus,
  isCallableFailure,
  getArtworkBase64,
  getSaveSlots,
  isSaveTrackingConfigured,
  debugLog,
  refreshMigrationState,
  logError,
} from "../api/backend";
import type { BiosAnswer } from "../api/backend";
import { SlotSetupWizard } from "./SlotSetupWizard";
import { SavesTab } from "./SavesTab";
import { AchievementsTab } from "./AchievementsTab";
import { BiosTab } from "./BiosTab";
import { infoRow, section } from "./panelSection";
import type {
  RomMetadata,
  InstalledRom,
  BiosStatus,
  CoreInfo,
  SaveStatus,
  SyncConflict,
  SaveSlotSummary,
  DownloadCompleteEvent,
} from "../types";
import type { RommDataChangedDetail } from "../types/events";
import { getMigrationState, onMigrationChange, setMigrationStatus } from "../utils/migrationStore";
import { getSettingsResetState, onSettingsResetChange } from "../utils/settingsResetStore";
import {
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
} from "../utils/saveSortMigrationStore";
import {
  beginServerLoad,
  reportServerReachable,
  setServerRetryProgress,
  settleServerLoad,
  useRommConnectionState,
} from "../utils/connectionState";
import { applyLoadSlotsResult, applyRefreshSlotResult } from "../utils/slotState";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { MigrationBlockedCard } from "./MigrationBlockedCard";
import { SettingsResetCard } from "./SettingsResetCard";
import { detach } from "../utils/detach";

interface RomMGameInfoPanelProps {
  appId: number;
}

interface PanelState {
  loading: boolean;
  romId: number | null;
  romName: string;
  platformName: string;
  platformSlug: string;
  installed: boolean;
  installedRom: InstalledRom | null;
  metadata: RomMetadata | null;
  coverBase64: string | null;
  biosStatus: BiosStatus | null;
  // unmanaged/ok/partial/missing classification — single source of truth is the
  // backend (`compute_bios_level`); both the cache path and the bios-change refresh
  // path thread `bios_level` straight off their respective payloads, never
  // re-deriving it. Drives the BIOS status-dot color, in `BiosTab`.
  // "unmanaged" (server files present, none registry-known) renders neutral grey.
  // null when no BIOS need.
  biosLevel: "ok" | "partial" | "missing" | "unmanaged" | null;
  // Core info comes from the dedicated get_platform_core_info path (#923), not
  // from biosStatus — the two concerns are decoupled. It stays here rather than
  // in the BIOS tab because it has to reach the render in the SAME update as
  // biosStatus: two updates would briefly highlight the previous core and name
  // it in the "Active Core" row against the new core's requirements.
  coreInfo: CoreInfo | null;
  saveSyncEnabled: boolean;
  saveStatus: SaveStatus | null;
  conflicts: SyncConflict[];
  error: boolean;
  activeTab: string;
  raId: number | null;
  slotConfirmed: boolean;
  activeSlot: string | null;
  availableSlots: SaveSlotSummary[];
  slotsLoading: boolean;
  // Region / Languages of the ACTIVE version (ADR-0021), rendered as GAME INFO
  // rows; empty arrays hide their row. Refreshed on a version switch.
  regions: string[];
  languages: string[];
}

/** A ROM identity paired with the only writer allowed to fold an answer read for
 *  it into panel state.
 *
 *  `write` drops the update when the panel that issued the read is gone, and
 *  when the panel has been re-bound to a different ROM since. Those are two
 *  separate ends, and neither covers the other: a version switch re-binds the
 *  shortcut to a new rom_id without changing the appId, so the `[appId]` effect
 *  never re-runs and its `cancelled` flag never fires for it (#1713).
 *
 *  Carrying the ROM alongside its writer is what keeps the two from drifting
 *  apart — a read issued off `binding.romId` cannot be folded in through a
 *  writer bound to some other version. */
interface RomBinding {
  readonly romId: number;
  readonly write: React.Dispatch<React.SetStateAction<PanelState>>;
}

/** Bind reads for `romId` to the panel showing it.
 *
 *  The check reads `romIdRef` when the answer LANDS, not when the read was
 *  issued: the version-switch handler re-points the ref the moment the switch
 *  resolves, so everything still in flight for the previous version is refused
 *  from that point on. */
function bindRom(
  romId: number,
  romIdRef: React.MutableRefObject<number | null>,
  cancelled: () => boolean,
  setter: React.Dispatch<React.SetStateAction<PanelState>>,
): RomBinding {
  return {
    romId,
    write: (update) => {
      if (cancelled() || romIdRef.current !== romId) return;
      setter(update);
    },
  };
}

/** Format a Unix timestamp (seconds) as a release date string (e.g. "15 Mar 2003") */
function formatReleaseDate(timestamp: number | null): string | null {
  if (!timestamp || timestamp <= 0) return null;
  const date = new Date(timestamp * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${date.getDate()} ${months[date.getMonth()]} ${date.getFullYear()}`;
}

/** Refresh slot configuration and available slots — extracted to reduce nesting depth. */
function refreshSlotState(binding: RomBinding): void {
  isSaveTrackingConfigured(binding.romId)
    .then((result) => binding.write((prev) => ({ ...prev, slotConfirmed: result.configured })))
    .catch(() => {});
  getSaveSlots(binding.romId)
    .then((slotResult) => applyRefreshSlotResult<PanelState>(slotResult, binding.write))
    .catch(() => {});
}

/** Fire-and-forget installed-rom fetch — kept at module scope to avoid nesting. */
function refreshInstalledRomInBackground(binding: RomBinding): Promise<void> {
  return getInstalledRom(binding.romId)
    .then((installed) => {
      if (installed) {
        binding.write((prev) => ({ ...prev, installedRom: installed }));
      }
    })
    .catch(() => {});
}

/** Fire-and-forget cover-art fetch — kept at module scope to avoid nesting. */
function refreshCoverArtInBackground(binding: RomBinding): Promise<void> {
  return getArtworkBase64(binding.romId)
    .then((result) => {
      if (result.base64) {
        binding.write((prev) => ({ ...prev, coverBase64: result.base64 }));
      }
    })
    .catch(() => {});
}

/** Fire-and-forget metadata fetch — kept at module scope to avoid nesting. */
function refreshMetadataInBackground(binding: RomBinding): Promise<void> {
  return getRomMetadata(binding.romId)
    .then((meta) => binding.write((prev) => ({ ...prev, metadata: meta })))
    .catch(() => {});
}

/** The panel's two BIOS fields as a cached game detail answers them, or `null`
 *  when the payload carries no BIOS answer — a detail derived while the firmware
 *  cache was cold, which every BIOS download and delete makes it. The caller then
 *  leaves the shown status standing instead of hiding the BIOS tab on a
 *  non-answer (#1693).
 *
 *  `bios_level` is computed by the backend (`compute_bios_level`) and threaded
 *  straight through, never re-derived; it is null whenever there is no
 *  requirement. */
function biosFieldsFromCache(cached: BiosAnswer): Pick<PanelState, "biosStatus" | "biosLevel"> | null {
  if (cached.bios_status_unknown) return null;
  if (!cached.bios_status) return { biosStatus: null, biosLevel: null };
  return {
    biosStatus: { needs_bios: true, ...cached.bios_status },
    biosLevel: cached.bios_level ?? null,
  };
}

/** Build a `SaveStatus` from a cached game detail's `save_status` field. */
function saveStatusFromCache(
  romId: number,
  cachedSave: NonNullable<Awaited<ReturnType<typeof getCachedGameDetail>>["save_status"]> | null | undefined,
): SaveStatus | null {
  if (!cachedSave) return null;
  return {
    rom_id: romId,
    files: cachedSave.files.map((f) => ({
      filename: f.filename,
      status: f.status as "skip" | "download" | "upload" | "conflict",
      local_path: null,
      local_hash: null,
      local_mtime: null,
      local_size: null,
      server_save_id: null,
      server_file_name: null,
      server_emulator: null,
      server_updated_at: null,
      server_size: null,
      last_sync_at: f.last_sync_at ?? null,
    })),
    playtime: {
      total_seconds: 0,
      session_count: 0,
      last_session_start: null,
      last_session_duration_sec: null,
      last_played: null,
    },
    device_id: "",
    last_sync_check_at: cachedSave.last_sync_check_at ?? null,
  };
}

/** Kick off background fetches that fill in fields not present in the cache:
 *  installed-rom details, cover art, and fresh metadata (if stale or missing). */
function startBackgroundRefreshes(
  cached: Awaited<ReturnType<typeof getCachedGameDetail>>,
  binding: RomBinding,
): Promise<void[]> {
  const bgPromises: Promise<void>[] = [];

  if (cached.installed) {
    bgPromises.push(refreshInstalledRomInBackground(binding));
  }

  bgPromises.push(refreshCoverArtInBackground(binding));

  const metaStale = cached.stale_fields?.includes("metadata") ?? true;
  if (!cached.metadata || metaStale) {
    bgPromises.push(refreshMetadataInBackground(binding));
  }

  // Core info from its own path (#923), decoupled from BIOS status.
  if (binding.romId) {
    bgPromises.push(refreshCoreInfoInBackground(binding));
  }

  return Promise.all(bgPromises);
}

/** Fetch active-core + available-cores for a ROM from the dedicated
 *  `get_platform_core_info` path (#923) and merge into panel state. Keyed on
 *  rom_id so the active core reflects a per-game DB override (epic #945) when
 *  one is pinned. */
function refreshCoreInfoInBackground(binding: RomBinding): Promise<void> {
  return getPlatformCoreInfo(binding.romId)
    .then((coreInfo) => binding.write((prev) => ({ ...prev, coreInfo })))
    .catch(() => {});
}

/** Cache-first initial render. Resolves the cached game detail for this appId,
 *  pushes it into PanelState, and fires the background refresh tasks whose
 *  results are merged in later. Module-scope so the FC body stays focused on
 *  rendering. */
async function loadData(
  appId: number,
  cancelled: () => boolean,
  romIdRef: React.MutableRefObject<number | null>,
  platformSlugRef: React.MutableRefObject<string>,
  setter: React.Dispatch<React.SetStateAction<PanelState>>,
): Promise<void> {
  try {
    // Phase 1: Cache-first — render instantly from cached data
    const cached = await getCachedGameDetail(appId);
    if (cancelled()) return;
    if (!cached.found) {
      setter((prev) => ({ ...prev, loading: false, error: true }));
      return;
    }

    const romId = cached.rom_id!;
    const romName = cached.rom_name || "";
    const platformName = cached.platform_name || "";
    const platformSlug = cached.platform_slug || "";

    romIdRef.current = romId;
    platformSlugRef.current = platformSlug;

    // Nothing is shown yet on a first render, so a payload carrying no BIOS
    // answer starts the panel out with none either.
    const { biosStatus, biosLevel } = biosFieldsFromCache(cached) ?? { biosStatus: null, biosLevel: null };
    const saveStatus = saveStatusFromCache(romId, cached.save_status);
    const conflicts: SyncConflict[] = cached.save_status?.conflicts ?? [];
    const raId = cached.ra_id ?? null;

    // Render immediately with cached data (metadata may be null — that's OK).
    // Unbound by construction: this is the write that INSTALLS the identity every
    // background fold below compares itself against.
    setter({
      loading: false,
      romId,
      romName,
      platformName,
      platformSlug,
      installed: cached.installed ?? false,
      installedRom: null, // Will be filled by background fetch if installed
      metadata: cached.metadata as RomMetadata | null,
      coverBase64: null, // Will be filled by background fetch
      biosStatus,
      biosLevel,
      coreInfo: null, // Will be filled by background fetch (get_platform_core_info)
      saveSyncEnabled: cached.save_sync_enabled ?? false,
      saveStatus,
      conflicts,
      error: false,
      activeTab: "info",
      raId,
      slotConfirmed: false,
      activeSlot: "default",
      availableSlots: [],
      slotsLoading: false,
      regions: cached.regions ?? [],
      languages: cached.languages ?? [],
    });

    const binding = bindRom(romId, romIdRef, cancelled, setter);

    if (cached.save_sync_enabled) {
      refreshSlotState(binding);
    }

    // Phase 2: Background fetch for data not available in cache
    await startBackgroundRefreshes(cached, binding);
  } catch (e) {
    detach(debugLog(`RomMGameInfoPanel: loadData error: ${e}`));
    if (!cancelled()) setter((prev) => ({ ...prev, loading: false, error: true }));
  }
}

// S3776 is raised on the declaration line, so its NOSONAR must stay there. prettier-ignore stops
// Prettier from relocating the trailing comment into the body (which would break the suppression).
// prettier-ignore
export const RomMGameInfoPanel: FC<RomMGameInfoPanelProps> = ({ appId }) => { // NOSONAR(typescript:S3776) — React FC fan-out; decomposed in #387/#391/Phase 7. Further split scatters handlers.
  // Subscribe to version error — re-renders when global state changes
  const versionError = useVersionError();
  // Subscribe to the shared connection state so the saves-tab slot load can take
  // the known-offline fast path and re-load automatically on reconnect (#1345).
  const isOffline = useRommConnectionState() === "offline";

  const [state, setState] = useState<PanelState>({
    loading: true,
    romId: null,
    romName: "",
    platformName: "",
    platformSlug: "",
    installed: false,
    installedRom: null,
    metadata: null,
    coverBase64: null,
    biosStatus: null,
    biosLevel: null,
    coreInfo: null,
    saveSyncEnabled: false,
    saveStatus: null,
    conflicts: [],
    error: false,
    activeTab: "info",
    raId: null,
    slotConfirmed: false,
    activeSlot: "default",
    availableSlots: [],
    slotsLoading: false,
    regions: [],
    languages: [],
  });
  const romIdRef = useRef<number | null>(null);
  // Tracks the panel's own platform so the broadcast `bios` data-changed
  // handler can reject events for other platforms without a stale closure
  // (mirrors romIdRef). bios events fan out to every mounted panel (#1082).
  const platformSlugRef = useRef<string>("");
  // Load-once gate for the lazy-loaded SAVES tab data. A version switch resets it
  // (in handleVersionSwitched) so the slots re-fetch for the newly-bound rom_id
  // instead of lingering from the previous version.
  const slotsLoadedRef = useRef(false);
  const [migration, setMigration] = useState(getMigrationState());
  const [settingsReset, setSettingsReset] = useState(getSettingsResetState());
  const [saveSortPending, setSaveSortPending] = useState(getSaveSortMigrationState().pending);

  useEffect(() => {
    const unsub = onMigrationChange(() => setMigration(getMigrationState()));
    const unsubSettingsReset = onSettingsResetChange(() => setSettingsReset(getSettingsResetState()));
    const unsubSaveSort = onSaveSortMigrationChange(() => setSaveSortPending(getSaveSortMigrationState().pending));
    return () => {
      unsub();
      unsubSettingsReset();
      unsubSaveSort();
    };
  }, []);

  useEffect(() => {
    refreshMigrationState()
      .then(({ retrodeck, save_sort }) => {
        setMigrationStatus(retrodeck);
        setSaveSortMigrationStatus(save_sort);
      })
      .catch((e) => logError(`Failed to refresh migration state: ${e}`));
  }, [appId]);

  useEffect(() => {
    let cancelled = false;

    detach(loadData(appId, () => cancelled, romIdRef, platformSlugRef, setState));

    /** Bind a read this panel is issuing now for the ROM it currently shows. */
    const bindCurrentRom = (romId: number): RomBinding => bindRom(romId, romIdRef, () => cancelled, setState);

    // Listen for uninstall events to update state (uses ref to avoid stale closure)
    const onUninstall = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.rom_id === romIdRef.current) {
        setState((prev) => ({ ...prev, installed: false, installedRom: null }));
      }
    };
    globalThis.addEventListener("romm_rom_uninstalled", onUninstall);

    // Listen for download completion — the install counterpart to onUninstall.
    // The "ROM File" section is gated on installed + installedRom; a fresh
    // download must flip both so the section (with the local path) appears
    // without a detail-page re-mount (#1340). Decky backend event (@decky/api),
    // not a DOM CustomEvent — mirrors DiscSelector's download_complete wiring.
    const onDownloadComplete = addEventListener<[DownloadCompleteEvent]>(
      "download_complete",
      (evt: DownloadCompleteEvent) => {
        if (evt.rom_id !== romIdRef.current) return;
        // Invalidate the cached detail so a fast re-mount inside the 3s TTL
        // doesn't briefly re-serve the stale installed:false.
        invalidateCachedGameDetail(appId);
        setState((prev) => ({ ...prev, installed: true }));
        detach(refreshInstalledRomInBackground(bindCurrentRom(evt.rom_id)));
      },
    );

    // Per-event-type handlers — each owns one branch of the data-changed dispatch.
    // Defined inside useEffect to share the cancelled/appId/romIdRef/setState closure.
    const handleSaveSyncSettingsChange = async (
      detail: Extract<RommDataChangedDetail, { type: "save_sync_settings" }>,
    ) => {
      const enabled = detail.save_sync_enabled;
      if (!enabled) {
        setState((prev) => ({ ...prev, saveSyncEnabled: false }));
        return;
      }
      const romId = romIdRef.current;
      if (!romId) return;
      const binding = bindCurrentRom(romId);
      const result = await getSaveStatus(binding.romId).catch(() => null);
      if (result && isCallableFailure(result)) return;
      const updatedStatus: SaveStatus | null = result;
      const conflicts: SyncConflict[] = updatedStatus?.conflicts ?? [];
      binding.write((prev) => ({
        ...prev,
        saveSyncEnabled: true,
        saveStatus: updatedStatus,
        conflicts,
      }));
    };

    const handleSaveSyncChange = async (detail: Extract<RommDataChangedDetail, { type: "save_sync" }>) => {
      if (detail.rom_id && detail.rom_id !== romIdRef.current) return;
      const romId = romIdRef.current;
      if (!romId) return;
      const binding = bindCurrentRom(romId);
      const result = detail.save_status ?? (await getSaveStatus(binding.romId).catch(() => null));
      if (result && isCallableFailure(result)) return;
      const updatedStatus: SaveStatus | null = result;
      const conflicts: SyncConflict[] = updatedStatus?.conflicts ?? [];
      binding.write((prev) => ({
        ...prev,
        saveStatus: updatedStatus,
        conflicts,
      }));
      // Also re-check slot configuration + refresh slot data
      refreshSlotState(binding);
    };

    const handleBiosChange = async (detail: Extract<RommDataChangedDetail, { type: "bios" }>) => {
      // bios events fan out to every mounted panel — ignore platforms other
      // than this panel's own, both to avoid cross-platform BIOS-list bleed
      // and to skip the wasted checkPlatformBios fetch (#1082). Read via ref
      // to avoid a stale closure.
      if (!detail.platform_slug || detail.platform_slug !== platformSlugRef.current) return;
      // A rejected check and one that could not determine the requirement are
      // both "we don't know" — writing either would drop the whole BIOS tab
      // while the play row above keeps its level (#1693). Only an ANSWER moves
      // the tab, in either direction.
      const updated = await checkPlatformBios(detail.platform_slug).catch((): BiosStatus | null => null);
      if (cancelled || !updated || updated.bios_status_unknown) return;
      const biosLevel = updated.needs_bios ? (updated.bios_level ?? null) : null;
      setState((prev) => ({ ...prev, biosStatus: updated.needs_bios ? updated : null, biosLevel }));
    };

    const handleCoreChange = async (_detail: Extract<RommDataChangedDetail, { type: "core_changed" }>) => {
      // Re-fetch cached game detail to pick up the new core-aware BIOS status.
      invalidateCachedGameDetail(appId);
      const rid = romIdRef.current;
      if (!rid) return;
      // Core info comes from its own path (#923), keyed on the rom_id from a ref
      // to avoid a stale `state` closure. The active core reflects the per-game
      // DB override (epic #945). BIOS status is re-read from the (now core-free)
      // cache. Both land in ONE write: two writes would render the previous core
      // as the highlighted, active one against the new core's requirements.
      const binding = bindCurrentRom(rid);
      const [coreInfo, cached] = await Promise.all([
        getPlatformCoreInfo(binding.romId).catch((): CoreInfo | null => null),
        getCachedGameDetail(appId),
      ]);
      if (cancelled || !cached.found) return;
      // A detail carrying no BIOS answer leaves the shown status alone — the
      // core switch invalidated the cached detail, but a cold firmware cache
      // makes the re-read a non-answer rather than a "needs none" (#1693).
      const biosFields = biosFieldsFromCache(cached);
      binding.write((prev) => ({ ...prev, ...biosFields, coreInfo: coreInfo ?? prev.coreInfo }));
    };

    const handleVersionSwitched = async (detail: Extract<RommDataChangedDetail, { type: "version_switched" }>) => {
      // A version switch moved the group's binding to a new rom_id. Re-read the
      // cached detail (invalidated by the picker) so the panel reflects the new
      // active version — its RomM name (the injected panel title), Region /
      // Languages rows, and cover — while the Steam hero title stays sticky.
      //
      // The per-rom TAB data must follow the new active version too. The SAVES
      // tab loads once behind slotsLoadedRef and holds per-rom state; without
      // re-keying it keeps showing the previous version's data. Reset the
      // load-once gate and re-key the tab state off the fresh cache (mirroring a
      // fresh mount) so the tab-activation effect re-fetches for the new rom_id —
      // covering both the sitting-on-a-tab case (romId changes → the effect
      // re-runs) and the open-a-tab-later case. The ACHIEVEMENTS tab re-keys
      // itself: its React key is the rom_id, so the new one remounts it.
      //
      // BIOS is the third per-rom tab: the requirement is core-dependent and the
      // core override is keyed on rom_id, so the new version's core may need
      // different files — or none, which hides the tab (#1681).
      if (detail.app_id !== appId) return;
      const cached = await getCachedGameDetail(appId);
      if (cancelled || !cached.found) return;
      const newRomId = cached.rom_id ?? romIdRef.current;
      romIdRef.current = newRomId;
      slotsLoadedRef.current = false;
      const saveStatus = newRomId != null ? saveStatusFromCache(newRomId, cached.save_status) : null;
      // A detail carrying no BIOS answer keeps the shown status: the switched-to
      // version's requirement is unread, not absent (#1693).
      const biosFields = biosFieldsFromCache(cached);
      setState((prev) => {
        const biosStatus = biosFields ? biosFields.biosStatus : prev.biosStatus;
        return {
          ...prev,
          romId: newRomId,
          romName: cached.rom_name || prev.romName,
          installed: cached.installed ?? false,
          regions: cached.regions ?? [],
          languages: cached.languages ?? [],
          // Re-key per-rom tab state so nothing lingers from the previous version.
          saveSyncEnabled: cached.save_sync_enabled ?? false,
          saveStatus,
          conflicts: cached.save_status?.conflicts ?? [],
          raId: cached.ra_id ?? null,
          activeSlot: "default",
          availableSlots: [],
          slotsLoading: false,
          ...biosFields,
          // The BIOS tab's button is gated on `biosStatus`, so clearing it while
          // the user stands on that tab would hide the button and leave the body
          // empty with nothing selected. Send them back to the tab every version
          // has.
          activeTab: !biosStatus && prev.activeTab === "bios" ? "info" : prev.activeTab,
        };
      });
      // Re-fetch slot configuration (slotConfirmed) + slots for the new rom_id,
      // mirroring loadData's save-sync branch — this is the authority that keeps
      // the SlotSetupWizard-vs-SavesTab gate correct across the switch.
      if (cached.save_sync_enabled && newRomId != null) {
        refreshSlotState(bindCurrentRom(newRomId));
      }
      if (newRomId) {
        const binding = bindCurrentRom(newRomId);
        await Promise.all([
          refreshCoverArtInBackground(binding),
          // The BIOS tab's "Active Core" row and its per-file core lines come
          // from the dedicated core-info path (#923), keyed on rom_id so a
          // per-game override follows the switch. A failed read keeps the
          // previous reading rather than blanking it.
          refreshCoreInfoInBackground(binding),
        ]);
      }
    };

    const handleMetadataChange = async (detail: Extract<RommDataChangedDetail, { type: "metadata" }>) => {
      if (detail.rom_id !== romIdRef.current) return;
      const romId = romIdRef.current;
      if (!romId) return;
      const binding = bindCurrentRom(romId);
      const meta = await getRomMetadata(binding.romId).catch((): RomMetadata | null => null);
      binding.write((prev) => ({ ...prev, metadata: meta }));
    };

    const handleCoverRefreshed = async (detail: Extract<RommDataChangedDetail, { type: "cover_refreshed" }>) => {
      if (detail.rom_id !== romIdRef.current) return;
      const romId = romIdRef.current;
      if (!romId) return;
      // Re-fetch the cover image. Backend has already patched the registry's
      // cover_path; getArtworkBase64 will now resolve the freshly-downloaded
      // file. Catch swallowed because the .then handles the success path —
      // the rejection branch surfaces via the empty-cover render.
      await refreshCoverArtInBackground(bindCurrentRom(romId));
    };

    const onDataChanged = (e: Event) => {
      detach(
        (async () => {
          try {
            const detail = (e as CustomEvent).detail;
            if (!romIdRef.current) return;
            switch (detail?.type) {
              case "save_sync_settings":
                await handleSaveSyncSettingsChange(detail);
                break;
              case "save_sync":
                await handleSaveSyncChange(detail);
                break;
              case "bios":
                await handleBiosChange(detail);
                break;
              case "core_changed":
                await handleCoreChange(detail);
                break;
              case "metadata":
                await handleMetadataChange(detail);
                break;
              case "cover_refreshed":
                await handleCoverRefreshed(detail);
                break;
              case "version_switched":
                await handleVersionSwitched(detail);
                break;
            }
          } catch (err) {
            detach(debugLog(`RomMGameInfoPanel: onDataChanged error: ${err}`));
          }
        })(),
      );
    };
    globalThis.addEventListener("romm_data_changed", onDataChanged);

    const onTabSwitch = (e: Event) => {
      const tab = (e as CustomEvent).detail?.tab;
      if (tab) setState((prev) => ({ ...prev, activeTab: tab }));
    };
    globalThis.addEventListener("romm_tab_switch", onTabSwitch);

    return () => {
      cancelled = true;
      globalThis.removeEventListener("romm_rom_uninstalled", onUninstall);
      removeEventListener("download_complete", onDownloadComplete);
      globalThis.removeEventListener("romm_data_changed", onDataChanged);
      globalThis.removeEventListener("romm_tab_switch", onTabSwitch);
    };
  }, [appId]);

  useEffect(() => {
    if (state.activeTab !== "saves" || !state.saveSyncEnabled || !state.romId) return;
    if (slotsLoadedRef.current) return;

    // Known-offline fast path (#1345): the server slot fetch runs through the
    // retry+backoff ladder, so on a known-unreachable server it would hang
    // "Loading slots…" for tens of seconds before the local degraded view (the
    // per-slot "Server unreachable" notices) renders. Skip the fetch entirely —
    // slotsLoading is still false here (the slotsLoadedRef guard above means the
    // connected path never set it), so SavesTab renders the degraded view now.
    // slotsLoadedRef stays false, so a flip back to connected re-runs this effect
    // (isOffline dep) and loads.
    if (isOffline) return;
    slotsLoadedRef.current = true;

    const load = beginServerLoad();
    let cancelled = false;
    let settled = false;

    async function loadSlots() {
      // Drop stale retry progress from a prior load so the SavesTab's
      // ConnectingIndicator starts at plain "Connecting to RomM…" (#1345).
      setServerRetryProgress(null);
      setState((prev) => ({ ...prev, slotsLoading: true }));
      try {
        if (!state.romId) return;
        const result = await getSaveSlots(state.romId);
        if (cancelled) return;
        settled = true;
        // A completed slot fetch is a reachability signal (#1345): a success
        // proves the server is reachable; an unreachable server drives the store
        // offline (which then renders the fast path above on the next dependent
        // run). Any OTHER failure reason is a server-side "no" (the server
        // answered), not a connectivity verdict — leave the store untouched.
        if (result.success) {
          reportServerReachable(true);
        } else if (result.reason === "server_unreachable") {
          reportServerReachable(false);
        }
        applyLoadSlotsResult<PanelState>(result, setState, slotsLoadedRef, (msg) => {
          detach(debugLog(msg));
        });
      } catch (e) {
        detach(debugLog(`Failed to load save slots: ${e}`));
        if (!cancelled) {
          settled = true;
          slotsLoadedRef.current = false;
          setState((prev) => ({ ...prev, slotsLoading: false }));
        }
      } finally {
        // Clear-on-settle in addition to clear-on-start (#1345 F2) — refused
        // when a newer load of any lane already owns the shared frame.
        settleServerLoad(load);
      }
    }

    detach(loadSlots());
    return () => {
      cancelled = true;
      // Torn down mid-flight (e.g. a concurrent call flipped the store offline,
      // re-running this effect) — release the load-once gate and drop the spinner
      // so the re-run isn't wedged behind a stuck slotsLoading/slotsLoadedRef, and
      // a later reconnect reliably reloads (#1345 F2). A settled load already set
      // its own final state, so leave it alone.
      if (!settled) {
        slotsLoadedRef.current = false;
        setState((prev) => (prev.slotsLoading ? { ...prev, slotsLoading: false } : prev));
      }
    };
  }, [state.activeTab, state.saveSyncEnabled, state.romId, isOffline]);

  // --- Version mismatch — replace entire panel with polished error card ---
  if (versionError) {
    return createElement("div", { "data-romm": "true" }, createElement(VersionErrorCard, { message: versionError }));
  }

  // --- Corrupt-settings reset — surface the reason instead of a bare "offline" ---
  if (settingsReset.pending) {
    return createElement(
      "div",
      { "data-romm": "true" },
      createElement(SettingsResetCard, { backedUpTo: settingsReset.backedUpTo, compact: true }),
    );
  }

  // --- Pending RetroDECK migration — block the page until resolved ---
  if (migration.pending) {
    return createElement("div", { "data-romm": "true" }, createElement(MigrationBlockedCard, {}));
  }

  // --- Loading state ---
  // Use minHeight so Steam's scroll container allocates enough space
  // before async data loads and expands the panel.
  if (state.loading) {
    return createElement(
      "div",
      {
        "data-romm": "true",
        className: "romm-panel-container",
        style: { minHeight: "500px" },
      },
      createElement("div", { className: "romm-panel-loading" }, "Loading..."),
    );
  }

  // --- Error / not found state ---
  if (state.error || !state.romId) {
    return null;
  }

  const romId = state.romId;
  const meta = state.metadata;

  // --- Game Info section ---
  const gameInfoChildren: ReturnType<typeof createElement>[] = [];

  // The RomM game name (distinct from the Steam shortcut hero title, which can differ).
  if (state.romName) {
    gameInfoChildren.push(createElement("div", { key: "rom-name", className: "romm-panel-rom-name" }, state.romName));
  }

  // Region / Languages of the active version (ADR-0021) — omitted when empty.
  if (state.regions.length > 0) {
    gameInfoChildren.push(infoRow("regions", "Region", state.regions.join("/")));
  }
  if (state.languages.length > 0) {
    gameInfoChildren.push(infoRow("languages", "Languages", state.languages.join(", ")));
  }

  if (meta) {
    if (meta.summary) {
      gameInfoChildren.push(createElement("div", { key: "summary", className: "romm-panel-summary" }, meta.summary));
    }

    // Platform after description
    if (state.platformName) {
      gameInfoChildren.push(infoRow("platform", "Platform", state.platformName));
    }

    if (meta.companies.length > 0) {
      gameInfoChildren.push(infoRow("companies", "Developer / Publisher", meta.companies.join(", ")));
    }

    if (meta.genres.length > 0) {
      gameInfoChildren.push(
        createElement(
          "div",
          { key: "genres", className: "romm-panel-info-row" },
          createElement("span", { className: "romm-panel-label" }, "Genres"),
          createElement(
            "div",
            { className: "romm-panel-tags" },
            ...meta.genres.map((g) => createElement("span", { key: g, className: "romm-panel-tag" }, g)),
          ),
        ),
      );
    }

    const releaseDate = formatReleaseDate(meta.first_release_date);
    if (releaseDate) {
      gameInfoChildren.push(infoRow("release-date", "Release Date", releaseDate));
    }

    if (meta.game_modes.length > 0) {
      gameInfoChildren.push(infoRow("game-modes", "Game Modes", meta.game_modes.join(", ")));
    }

    if (meta.player_count) {
      gameInfoChildren.push(infoRow("players", "Players", meta.player_count));
    }

    if (meta.average_rating != null && meta.average_rating > 0) {
      gameInfoChildren.push(infoRow("rating", "Rating", `${Math.round(meta.average_rating)}%`));
    }
  } else if (state.platformName) {
    // No metadata — still show platform
    gameInfoChildren.push(infoRow("platform", "Platform", state.platformName));
  }

  // "No metadata available" fires only when NO descriptive row was added (name,
  // region/languages, metadata, or platform). The version switcher no longer lives
  // here — it moved to the play-button section (#1297) — so this is a plain count
  // of the descriptive rows.
  if (gameInfoChildren.length === 0) {
    gameInfoChildren.push(createElement("div", { key: "no-meta", className: "romm-panel-muted" }, "No metadata available"));
  }
  const gameInfoContent = gameInfoChildren;

  const gameInfoSection = state.coverBase64
    ? section(
        "game-info",
        null,
        createElement(
          "div",
          {
            key: "game-info-row",
            style: { display: "flex", gap: "16px", alignItems: "flex-start" },
          },
          createElement("img", {
            key: "cover",
            src: `data:image/png;base64,${state.coverBase64}`,
            style: { width: "120px", borderRadius: "4px", flexShrink: 0, objectFit: "cover" as const },
          }),
          createElement("div", { key: "details", style: { flex: 1 } }, ...gameInfoContent),
        ),
      )
    : section("game-info", null, ...gameInfoContent);

  // --- ROM File section (only when installed) ---
  // A downloaded ROM the system cannot launch keeps its files and its row; only
  // the shortcut's launch command is withheld. This is the one place that says
  // so — without it the game simply never starts and nothing explains why.
  const noLaunchTargetNote =
    state.installedRom && !state.installedRom.launchable
      ? createElement(
          "div",
          { key: "no-launch-target", className: "romm-panel-muted", style: { marginTop: "4px" } },
          `Downloaded, but nothing here is a format ${state.installedRom.system} can launch — no launch ` +
            `command was set. The files are on disk; install them in the emulator to play.`,
        )
      : null;

  const romFileSection =
    state.installed && state.installedRom
      ? section(
          "rom-file",
          "ROM File",
          infoRow("filename", "Filename", state.installedRom.file_name),
          noLaunchTargetNote,
        )
      : null;

  // --- Tab bar ---
  const tabs: { id: string; label: string; visible: boolean }[] = [
    { id: "info", label: "GAME INFO", visible: true },
    { id: "achievements", label: "ACHIEVEMENTS", visible: !!state.raId },
    { id: "saves", label: "SAVES", visible: state.saveSyncEnabled },
    { id: "bios", label: "BIOS", visible: !!state.biosStatus },
  ];

  const tabBar = createElement(
    Focusable,
    {
      className: "romm-tab-bar",
      "flow-children": "right",
      "data-romm": "true",
    },
    ...tabs
      .filter((t) => t.visible)
      .map((t) =>
        createElement(
          DialogButton,
          {
            key: `tab-${t.id}`,
            className: `romm-tab ${state.activeTab === t.id ? "romm-tab-active" : ""}`,
            onClick: () => setState((prev) => ({ ...prev, activeTab: t.id })),
            style: {
              background: "transparent",
              border: "none",
              borderBottom: state.activeTab === t.id ? "2px solid #1a9fff" : "2px solid transparent",
              padding: "10px 16px",
              minWidth: "auto",
              width: "auto",
            },
            noFocusRing: false,
          },
          t.label,
        ),
      ),
  );

  const saveSortWarning = saveSortPending
    ? createElement(
        "div",
        {
          key: "save-sort-warning",
          style: {
            padding: "8px 12px",
            marginBottom: "12px",
            backgroundColor: "rgba(212, 167, 44, 0.15)",
            borderLeft: "3px solid #d4a72c",
            borderRadius: "4px",
          },
        },
        createElement(
          "div",
          {
            style: { fontSize: "13px", fontWeight: "bold", color: "#d4a72c", marginBottom: "4px" },
          },
          "\u26A0\uFE0F RetroArch save sorting changed",
        ),
        createElement(
          "div",
          {
            style: { fontSize: "12px", color: "rgba(255, 255, 255, 0.7)" },
          },
          "Save file paths may be incorrect. Go to Settings to migrate.",
        ),
      )
    : null;

  // --- Determine active tab content ---
  // The ACHIEVEMENTS and BIOS panes are not built here: they are mounted below
  // for every ROM and decide themselves whether to render.
  let activeTabContent: ReturnType<typeof createElement> | null = null;
  if (state.activeTab === "info") {
    activeTabContent = createElement("div", { key: "tab-info" }, gameInfoSection, romFileSection);
  } else if (state.activeTab === "saves") {
    if (state.saveSyncEnabled && !state.slotConfirmed) {
      activeTabContent = createElement(SlotSetupWizard, {
        romId: state.romId,
        onComplete: () => {
          // Refresh: mark as configured and reload save status
          setState((prev) => ({ ...prev, slotConfirmed: true }));
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "save_sync", rom_id: state.romId },
            }),
          );
        },
      });
    } else {
      activeTabContent = createElement(SavesTab, {
        appId,
        romId,
        saveStatus: state.saveStatus,
        conflicts: state.conflicts,
        activeSlot: state.activeSlot,
        availableSlots: state.availableSlots,
        slotsLoading: state.slotsLoading,
        onSlotSwitched: (newSlot, newStatus) => {
          setState((prev) => ({
            ...prev,
            activeSlot: newSlot === "" ? null : newSlot,
            saveStatus: newStatus,
            conflicts: newStatus.conflicts ?? [],
          }));
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "save_sync", rom_id: state.romId },
            }),
          );
        },
      });
    }
  }

  return createElement(
    "div",
    { "data-romm": "true" },
    saveSortWarning,
    tabBar,
    createElement(
      Focusable,
      {
        noFocusRing: true,
        className: "romm-tab-content",
        style: { paddingBottom: "48px" },
      },
      activeTabContent,
      // Mounted for every ROM and rendering nothing until their tab is active.
      // For achievements that is load-bearing: the list is fetched by the tab
      // itself, and unmounting it on a tab switch would re-fetch (and re-spinner)
      // on every visit. Its key is the ROM, so a version switch remounts it —
      // which is how its per-rom state and load-once gate re-key. BiosTab needs
      // no key: it renders from props and holds nothing of its own.
      createElement(AchievementsTab, {
        key: `achievements-${romId}`,
        romId,
        raId: state.raId,
        isActive: state.activeTab === "achievements",
      }),
      createElement(BiosTab, {
        biosStatus: state.biosStatus,
        biosLevel: state.biosLevel,
        coreInfo: state.coreInfo,
        isActive: state.activeTab === "bios",
      }),
    ),
  );
};
