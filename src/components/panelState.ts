/**
 * The game-detail panel's state shape and the reads that fold answers into it.
 *
 * `PanelState` is what `RomMGameInfoPanel` renders; `RomBinding` is the only
 * writer allowed to fold a read issued for a ROM back into it. The cache-first
 * load below and the event lane (`panelEvents.ts`) both write through it, which
 * is why the shape lives here rather than with either of them.
 */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import {
  getCachedGameDetail,
  getRomMetadata,
  getInstalledRom,
  getPlatformCoreInfo,
  getArtworkBase64,
  getSaveSlots,
  isSaveTrackingConfigured,
  debugLog,
} from "../api/backend";
import type { BiosAnswer } from "../api/backend";
import type {
  RomMetadata,
  InstalledRom,
  BiosStatus,
  CoreInfo,
  SaveStatus,
  SyncConflict,
  SaveSlotSummary,
} from "../types";
import { applyRefreshSlotResult } from "../utils/slotState";
import { detach } from "../utils/detach";

export interface PanelState {
  loading: boolean;
  romId: number | null;
  romName: string;
  platformName: string;
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
export interface RomBinding {
  readonly romId: number;
  readonly write: Dispatch<SetStateAction<PanelState>>;
}

/** Bind reads for `romId` to the panel showing it.
 *
 *  The check reads `romIdRef` when the answer LANDS, not when the read was
 *  issued: the version-switch handler re-points the ref the moment the switch
 *  resolves, so everything still in flight for the previous version is refused
 *  from that point on. */
export function bindRom(
  romId: number,
  romIdRef: MutableRefObject<number | null>,
  cancelled: () => boolean,
  setter: Dispatch<SetStateAction<PanelState>>,
): RomBinding {
  return {
    romId,
    write: (update) => {
      if (cancelled() || romIdRef.current !== romId) return;
      setter(update);
    },
  };
}

/** Refresh slot configuration and available slots. */
export function refreshSlotState(binding: RomBinding): void {
  isSaveTrackingConfigured(binding.romId)
    .then((result) => binding.write((prev) => ({ ...prev, slotConfirmed: result.configured })))
    .catch(() => {});
  getSaveSlots(binding.romId)
    .then((slotResult) => applyRefreshSlotResult<PanelState>(slotResult, binding.write))
    .catch(() => {});
}

/** Fire-and-forget installed-rom fetch. */
export function refreshInstalledRomInBackground(binding: RomBinding): Promise<void> {
  return getInstalledRom(binding.romId)
    .then((installed) => {
      if (installed) {
        binding.write((prev) => ({ ...prev, installedRom: installed }));
      }
    })
    .catch(() => {});
}

/** Fire-and-forget cover-art fetch. */
export function refreshCoverArtInBackground(binding: RomBinding): Promise<void> {
  return getArtworkBase64(binding.romId)
    .then((result) => {
      if (result.base64) {
        binding.write((prev) => ({ ...prev, coverBase64: result.base64 }));
      }
    })
    .catch(() => {});
}

/** Fire-and-forget metadata fetch. */
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
export function biosFieldsFromCache(cached: BiosAnswer): Pick<PanelState, "biosStatus" | "biosLevel"> | null {
  if (cached.bios_status_unknown) return null;
  if (!cached.bios_status) return { biosStatus: null, biosLevel: null };
  return {
    biosStatus: { needs_bios: true, ...cached.bios_status },
    biosLevel: cached.bios_level ?? null,
  };
}

/** Build a `SaveStatus` from a cached game detail's `save_status` field. */
export function saveStatusFromCache(
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
    bgPromises.push(refreshPanelCoreInfo(binding));
  }

  return Promise.all(bgPromises);
}

/** Fetch active-core + available-cores for a ROM from the dedicated
 *  `get_platform_core_info` path (#923) and merge into panel state. Keyed on
 *  rom_id so the active core reflects a per-game DB override (epic #945) when
 *  one is pinned. */
export function refreshPanelCoreInfo(binding: RomBinding): Promise<void> {
  return getPlatformCoreInfo(binding.romId)
    .then((coreInfo) => binding.write((prev) => ({ ...prev, coreInfo })))
    .catch(() => {});
}

/** Cache-first initial render. Resolves the cached game detail for this appId,
 *  pushes it into PanelState, and fires the background refresh tasks whose
 *  results are merged in later. */
export async function loadData(
  appId: number,
  cancelled: () => boolean,
  romIdRef: MutableRefObject<number | null>,
  platformSlugRef: MutableRefObject<string>,
  setter: Dispatch<SetStateAction<PanelState>>,
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
