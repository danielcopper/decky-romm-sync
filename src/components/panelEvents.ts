/**
 * The game-detail panel's event lane: the listeners `RomMGameInfoPanel`
 * subscribes for the ROM it currently shows, and the per-event-type handlers
 * behind the `romm_data_changed` dispatch.
 *
 * Every handler reads the panel's live identity (`romIdRef` / `platformSlugRef`)
 * rather than a value captured at wiring time — these listeners outlive many
 * answers, and a version switch re-points the identity without re-subscribing
 * them.
 */

import { addEventListener, removeEventListener } from "@decky/api";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import {
  invalidateCachedGameDetail,
  getCachedGameDetail,
  getRomMetadata,
  checkPlatformBios,
  getPlatformCoreInfo,
  getSaveStatus,
  isCallableFailure,
  debugLog,
} from "../api/backend";
import type { BiosStatus, CoreInfo, RomMetadata, SaveStatus, SyncConflict, DownloadCompleteEvent } from "../types";
import type { RommDataChangedDetail } from "../types/events";
import { detach } from "../utils/detach";
import {
  bindRom,
  biosFieldsFromCache,
  refreshPanelCoreInfo,
  refreshCoverArtInBackground,
  refreshInstalledRomInBackground,
  refreshSlotState,
  saveStatusFromCache,
  takeReadTicket,
  type PanelReadSeqs,
  type PanelState,
  type RomBinding,
} from "./panelState";

/** Everything the event lane reaches into on the panel that wired it.
 *
 *  `cancelled` is asked when an answer LANDS, not when the lane was wired — a
 *  captured boolean would be read long after the panel that set it is gone. */
export interface PanelEventContext {
  readonly appId: number;
  readonly cancelled: () => boolean;
  readonly romIdRef: MutableRefObject<number | null>;
  /** The panel's own platform, so the broadcast `bios` handler can reject events
   *  for other platforms. bios events fan out to every mounted panel (#1082). */
  readonly platformSlugRef: MutableRefObject<string>;
  /** Load-once gate for the lazy SAVES tab data — a version switch releases it. */
  readonly slotsLoadedRef: MutableRefObject<boolean>;
  /** Orders two answers about the same ROM, which the rom binding admits — see
   *  `takeReadTicket`. Shared with the panel's loads and its slots lane, which
   *  race these handlers for the same fields. */
  readonly readSeqs: MutableRefObject<PanelReadSeqs>;
  readonly setState: Dispatch<SetStateAction<PanelState>>;
}

/** Bind a read this panel is issuing now for the ROM it currently shows — see
 *  `bindRom` for what the binding refuses and why. */
function bindCurrentRom(ctx: PanelEventContext, romId: number): RomBinding {
  return bindRom(romId, ctx.romIdRef, ctx.cancelled, ctx.setState);
}

async function handleSaveSyncSettingsChange(
  ctx: PanelEventContext,
  detail: Extract<RommDataChangedDetail, { type: "save_sync_settings" }>,
): Promise<void> {
  const enabled = detail.save_sync_enabled;
  if (!enabled) {
    // Switching save sync off is itself the newest word on it, so it takes the
    // sequence's next ticket: a status read an earlier event left in flight
    // would otherwise land afterwards and put the SAVES tab back.
    takeReadTicket(ctx.readSeqs, "saveStatus");
    ctx.setState((prev) => ({
      ...prev,
      saveSyncEnabled: false,
      // The SAVES tab's button is gated on the same flag, so switching it off
      // under a user standing on that tab would take the button away and leave
      // its pane below an unmarked strip. Send them back to the tab every ROM
      // has — the move `handleVersionSwitched` makes for BIOS.
      activeTab: prev.activeTab === "saves" ? "info" : prev.activeTab,
    }));
    return;
  }
  // Switching save sync ON is a fact about the setting, not an answer about this
  // rom, so it lands before the read and outside its fence. Carried along with
  // the status it would be lost whenever the read is overtaken or fails — and
  // the SAVES tab, gated on this flag, would stay hidden with nothing left to
  // re-issue it.
  ctx.setState((prev) => ({ ...prev, saveSyncEnabled: true }));
  const romId = ctx.romIdRef.current;
  if (!romId) return;
  const binding = bindCurrentRom(ctx, romId);
  const overtaken = takeReadTicket(ctx.readSeqs, "saveStatus");
  const result = await getSaveStatus(binding.romId).catch(() => null);
  if (result && isCallableFailure(result)) return;
  if (overtaken()) return;
  const updatedStatus: SaveStatus | null = result;
  const conflicts: SyncConflict[] = updatedStatus?.conflicts ?? [];
  binding.write((prev) => ({
    ...prev,
    saveStatus: updatedStatus,
    conflicts,
  }));
}

async function handleSaveSyncChange(
  ctx: PanelEventContext,
  detail: Extract<RommDataChangedDetail, { type: "save_sync" }>,
): Promise<void> {
  if (detail.rom_id && detail.rom_id !== ctx.romIdRef.current) return;
  const romId = ctx.romIdRef.current;
  if (!romId) return;
  const binding = bindCurrentRom(ctx, romId);
  // A status carried on the event needs no read, but still takes the ticket: it
  // is the newer answer, so a read an earlier event left open must not land on
  // top of it.
  const overtaken = takeReadTicket(ctx.readSeqs, "saveStatus");
  const result = detail.save_status ?? (await getSaveStatus(binding.romId).catch(() => null));
  if (result && isCallableFailure(result)) return;
  const updatedStatus: SaveStatus | null = result;
  const conflicts: SyncConflict[] = updatedStatus?.conflicts ?? [];
  // Only the status fold is fenced. The slot refresh below issues its own reads
  // under their own sequences, so an overtaken run still re-checks them rather
  // than dropping the re-check on the floor.
  if (!overtaken()) {
    binding.write((prev) => ({
      ...prev,
      saveStatus: updatedStatus,
      conflicts,
    }));
  }
  // Also re-check slot configuration + refresh slot data
  refreshSlotState(binding, ctx.readSeqs);
}

async function handleBiosChange(
  ctx: PanelEventContext,
  detail: Extract<RommDataChangedDetail, { type: "bios" }>,
): Promise<void> {
  // bios events fan out to every mounted panel — ignore platforms other
  // than this panel's own, both to avoid cross-platform BIOS-list bleed
  // and to skip the wasted checkPlatformBios fetch (#1082). Read via ref
  // to avoid a stale closure.
  if (!detail.platform_slug || detail.platform_slug !== ctx.platformSlugRef.current) return;
  // A rejected check and one that could not determine the requirement are
  // both "we don't know" — writing either would drop the whole BIOS tab
  // while the play row above keeps its level (#1693). Only an ANSWER moves
  // the tab, in either direction.
  const updated = await checkPlatformBios(detail.platform_slug).catch((): BiosStatus | null => null);
  if (ctx.cancelled() || !updated || updated.bios_status_unknown) return;
  const biosLevel = updated.needs_bios ? (updated.bios_level ?? null) : null;
  ctx.setState((prev) => ({ ...prev, biosStatus: updated.needs_bios ? updated : null, biosLevel }));
}

async function handleCoreChange(
  ctx: PanelEventContext,
  _detail: Extract<RommDataChangedDetail, { type: "core_changed" }>,
): Promise<void> {
  // Re-fetch cached game detail to pick up the new core-aware BIOS status.
  invalidateCachedGameDetail(ctx.appId);
  const rid = ctx.romIdRef.current;
  if (!rid) return;
  // Core info comes from its own path (#923), keyed on the rom_id from a ref
  // to avoid a stale `state` closure. The active core reflects the per-game
  // DB override (epic #945). BIOS status is re-read from the (now core-free)
  // cache. Both land in ONE write: two writes would render the previous core
  // as the highlighted, active one against the new core's requirements.
  const binding = bindCurrentRom(ctx, rid);
  const [coreInfo, cached] = await Promise.all([
    getPlatformCoreInfo(binding.romId).catch((): CoreInfo | null => null),
    getCachedGameDetail(ctx.appId),
  ]);
  if (ctx.cancelled() || !cached.found) return;
  // A detail carrying no BIOS answer leaves the shown status alone — the
  // core switch invalidated the cached detail, but a cold firmware cache
  // makes the re-read a non-answer rather than a "needs none" (#1693).
  const biosFields = biosFieldsFromCache(cached);
  binding.write((prev) => ({ ...prev, ...biosFields, coreInfo: coreInfo ?? prev.coreInfo }));
}

async function handleVersionSwitched(
  ctx: PanelEventContext,
  detail: Extract<RommDataChangedDetail, { type: "version_switched" }>,
): Promise<void> {
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
  //
  // Two switches in quick succession read the cache twice and can finish in
  // either order, so this read is ordered like the mount load's — see `loadData`.
  if (detail.app_id !== ctx.appId) return;
  const overtaken = takeReadTicket(ctx.readSeqs, "detail");
  const cached = await getCachedGameDetail(ctx.appId);
  if (ctx.cancelled() || overtaken() || !cached.found) return;
  const newRomId = cached.rom_id ?? ctx.romIdRef.current;
  ctx.romIdRef.current = newRomId;
  ctx.slotsLoadedRef.current = false;
  const saveStatus = newRomId != null ? saveStatusFromCache(newRomId, cached.save_status) : null;
  // A detail carrying no BIOS answer keeps the shown status: the switched-to
  // version's requirement is unread, not absent (#1693).
  const biosFields = biosFieldsFromCache(cached);
  const saveSyncEnabled = cached.save_sync_enabled ?? false;
  ctx.setState((prev) => {
    const biosStatus = biosFields ? biosFields.biosStatus : prev.biosStatus;
    // Both gated tabs can lose their button to this switch, and a user standing
    // on one is then looking at a body with nothing marked in the strip above
    // it. Asked as one question because the answer is the same either way and
    // only one tab is active: back to the tab every version has.
    const activeTabGone =
      (prev.activeTab === "bios" && !biosStatus) || (prev.activeTab === "saves" && !saveSyncEnabled);
    return {
      ...prev,
      romId: newRomId,
      romName: cached.rom_name || prev.romName,
      installed: cached.installed ?? false,
      // Cleared rather than overwritten: `refreshInstalledRomInBackground`
      // writes only on a truthy answer, so a failed or empty read would leave
      // the previous version's file name standing under this version's
      // `installed` until a remount. The record describes ONE version, and a
      // sibling download supersedes the install it names without the panel
      // hearing about it (#1742). Re-read below.
      installedRom: null,
      regions: cached.regions ?? [],
      languages: cached.languages ?? [],
      // Re-key per-rom tab state so nothing lingers from the previous version.
      saveSyncEnabled,
      saveStatus,
      conflicts: cached.save_status?.conflicts ?? [],
      raId: cached.ra_id ?? null,
      activeSlot: "default",
      activeSlotKnown: false,
      availableSlots: [],
      slotsLoading: false,
      ...biosFields,
      activeTab: activeTabGone ? "info" : prev.activeTab,
    };
  });
  // Re-fetch slot configuration (slotConfirmed) + slots for the new rom_id,
  // mirroring loadData's save-sync branch — this is the authority that keeps
  // the SlotSetupWizard-vs-SavesTab gate correct across the switch.
  if (saveSyncEnabled && newRomId != null) {
    refreshSlotState(bindCurrentRom(ctx, newRomId), ctx.readSeqs);
  }
  if (newRomId) {
    const binding = bindCurrentRom(ctx, newRomId);
    const reads = [
      refreshCoverArtInBackground(binding),
      // The BIOS tab's "Active Core" row and its per-file core lines come
      // from the dedicated core-info path (#923), keyed on rom_id so a
      // per-game override follows the switch. A failed read keeps the
      // previous reading rather than blanking it.
      refreshPanelCoreInfo(binding),
    ];
    // Read only when this version has an install to describe, mirroring the
    // mount load — the ROM File row and its launch-target note are hidden
    // either way, and an uninstalled version has no record to fetch.
    if (cached.installed) reads.push(refreshInstalledRomInBackground(binding));
    await Promise.all(reads);
  }
}

async function handleMetadataChange(
  ctx: PanelEventContext,
  detail: Extract<RommDataChangedDetail, { type: "metadata" }>,
): Promise<void> {
  if (detail.rom_id !== ctx.romIdRef.current) return;
  const romId = ctx.romIdRef.current;
  if (!romId) return;
  const binding = bindCurrentRom(ctx, romId);
  const meta = await getRomMetadata(binding.romId).catch((): RomMetadata | null => null);
  binding.write((prev) => ({ ...prev, metadata: meta }));
}

async function handleCoverRefreshed(
  ctx: PanelEventContext,
  detail: Extract<RommDataChangedDetail, { type: "cover_refreshed" }>,
): Promise<void> {
  if (detail.rom_id !== ctx.romIdRef.current) return;
  const romId = ctx.romIdRef.current;
  if (!romId) return;
  // Re-fetch the cover image. Backend has already patched the registry's
  // cover_path; getArtworkBase64 will now resolve the freshly-downloaded
  // file. Catch swallowed because the .then handles the success path —
  // the rejection branch surfaces via the empty-cover render.
  await refreshCoverArtInBackground(bindCurrentRom(ctx, romId));
}

/** Route one `romm_data_changed` payload to the handler that owns its type.
 *
 *  Nothing is dispatched before the panel's identity has landed: every handler
 *  below reads `romIdRef` as the ROM it answers for.
 *
 *  Returns the chosen handler's own promise rather than awaiting it, so the
 *  caller's `await` resolves on the same turn it did when the switch sat in the
 *  caller. */
function dispatchDataChanged(ctx: PanelEventContext, detail: RommDataChangedDetail | undefined): Promise<void> {
  if (!ctx.romIdRef.current) return Promise.resolve();
  switch (detail?.type) {
    case "save_sync_settings":
      return handleSaveSyncSettingsChange(ctx, detail);
    case "save_sync":
      return handleSaveSyncChange(ctx, detail);
    case "bios":
      return handleBiosChange(ctx, detail);
    case "core_changed":
      return handleCoreChange(ctx, detail);
    case "metadata":
      return handleMetadataChange(ctx, detail);
    case "cover_refreshed":
      return handleCoverRefreshed(ctx, detail);
    case "version_switched":
      return handleVersionSwitched(ctx, detail);
    default:
      return Promise.resolve();
  }
}

/** Subscribe the panel's event listeners; the returned teardown unsubscribes
 *  them all. */
export function wirePanelEvents(ctx: PanelEventContext): () => void {
  // Listen for uninstall events to update state (uses ref to avoid stale closure)
  const onUninstall = (e: Event) => {
    const detail = (e as CustomEvent).detail;
    if (detail?.rom_id === ctx.romIdRef.current) {
      ctx.setState((prev) => ({ ...prev, installed: false, installedRom: null }));
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
      if (evt.rom_id !== ctx.romIdRef.current) return;
      // Invalidate the cached detail so a fast re-mount inside the 3s TTL
      // doesn't briefly re-serve the stale installed:false.
      invalidateCachedGameDetail(ctx.appId);
      ctx.setState((prev) => ({ ...prev, installed: true }));
      detach(refreshInstalledRomInBackground(bindCurrentRom(ctx, evt.rom_id)));
    },
  );

  const onDataChanged = (e: Event) => {
    detach(
      (async () => {
        try {
          await dispatchDataChanged(ctx, (e as CustomEvent).detail);
        } catch (err) {
          detach(debugLog(`RomMGameInfoPanel: onDataChanged error: ${err}`));
        }
      })(),
    );
  };
  globalThis.addEventListener("romm_data_changed", onDataChanged);

  const onTabSwitch = (e: Event) => {
    const tab = (e as CustomEvent).detail?.tab;
    if (tab) ctx.setState((prev) => ({ ...prev, activeTab: tab }));
  };
  globalThis.addEventListener("romm_tab_switch", onTabSwitch);

  return () => {
    globalThis.removeEventListener("romm_rom_uninstalled", onUninstall);
    removeEventListener("download_complete", onDownloadComplete);
    globalThis.removeEventListener("romm_data_changed", onDataChanged);
    globalThis.removeEventListener("romm_tab_switch", onTabSwitch);
  };
}
