import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import * as backend from "../api/backend";
import { _resetSharedReadsForTests } from "../api/sharedReads";
import * as cachedStore from "./cachedGameDetailStore";
import {
  getGameDetail,
  noteSaveSyncDisplay,
  refreshBiosStatus,
  refreshCoreAndBios,
  refreshSaveStatus,
  subscribeGameDetail,
  useGameDetail,
} from "./gameDetailStore";
import {
  installDomEventListenerSpy,
  uninstallDomEventListenerSpy,
  domListenerCount,
} from "../test-utils/dom-event-listener-spy";
import { deckyEventListenerCount, emitDeckyEvent } from "../test-utils/decky-api-mock";
import type { CachedGameDetail } from "../api/backend";
import type { CoreInfo, DownloadCompleteEvent, SaveStatus } from "../types";

type BiosStatusResult = Awaited<ReturnType<typeof backend.getBiosStatus>>;
type AchievementProgressResult = Awaited<ReturnType<typeof backend.getAchievementProgress>>;

// getCachedGameDetail / invalidateCachedGameDetail are re-exported through
// backend.ts but their canonical home is utils — mock the store so both import
// paths land on the same vi.fn.
vi.mock("./cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn(),
  invalidateCachedGameDetail: vi.fn(),
}));

// Each test works on its own appId so one test's entry can never be another's.
let appIdSeq = 5000;
let nextAppId = 0;

// Every subscription opened by a test is released afterwards — an entry with a
// live subscriber keeps its DOM listeners attached.
const openSubscriptions: Array<() => void> = [];

function subscribe(appId: number, cb: () => void = () => {}): () => void {
  const unsubscribe = subscribeGameDetail(appId, cb);
  openSubscriptions.push(unsubscribe);
  return unsubscribe;
}

const flush = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

function saveStatus(overrides: Partial<SaveStatus> = {}): SaveStatus {
  return {
    rom_id: 42,
    files: [],
    playtime: {
      total_seconds: 0,
      session_count: 0,
      last_session_start: null,
      last_session_duration_sec: null,
      last_played: null,
    },
    device_id: "device",
    last_sync_check_at: null,
    save_sync_display: { status: "synced", label: "Up to date", last_sync_check_at: null },
    ...overrides,
  };
}

/** A deferred stand-in for a callable, so a test can hold a read open across an
 *  event and settle it afterwards. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (e: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Re-key an entry to rom 43 the way the version picker does — without closing
 *  it, so the generation is unchanged and only the rom identity separates an
 *  answer read for the previous ROM from the entry's new one. */
const dispatchVersionSwitch = (appId: number) =>
  globalThis.dispatchEvent(
    new CustomEvent("romm_data_changed", { detail: { type: "version_switched", app_id: appId, rom_id: 43 } }),
  );

const downloadComplete = (romId: number): DownloadCompleteEvent => ({
  rom_id: romId,
  rom_name: "Test ROM",
  platform_name: "SNES",
  file_path: "/roms/test.sfc",
  app_id: null,
  launch_options: "",
});

const coreInfo: CoreInfo = {
  active_core: "snes9x.so",
  active_core_label: "Snes9x",
  platform_core_label: null,
  has_game_override: false,
  emulator_data_available: true,
  emulators: [
    {
      label: "Snes9x",
      kind: "libretro",
      core_so: "snes9x.so",
      is_default: true,
      bakeable: true,
      reason: null,
    },
  ],
};

/** The answer a second core read returns — after a version switch, or after a
 *  re-read of the same rom — distinct from {@link coreInfo} so a fold of the
 *  first read's answer is visible. */
const laterCoreInfo: CoreInfo = {
  active_core: "genesis_plus_gx.so",
  active_core_label: "Genesis Plus GX",
  platform_core_label: null,
  has_game_override: false,
  emulator_data_available: true,
  emulators: [
    {
      label: "Genesis Plus GX",
      kind: "libretro",
      core_so: "genesis_plus_gx.so",
      is_default: true,
      bakeable: true,
      reason: null,
    },
  ],
};

const achievementSummary = (earned: number, total: number) => ({ earned, total, earned_hardcore: 0 });

const biosMissing: BiosStatusResult = {
  bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
  bios_level: "missing",
  bios_label: "0/3",
};

/** The answer a second BIOS read returns, distinct from {@link biosMissing} so a
 *  fold of the first read's answer is visible in the shown level. */
const biosAllPresent: BiosStatusResult = {
  bios_status: { platform_slug: "snes", server_count: 3, local_count: 3, all_downloaded: true },
  bios_level: "ok",
  bios_label: "3/3",
};

/** The cached detail of the ROM a version switch moves the entry to, carrying a
 *  BIOS level the previous ROM's read would overwrite if it were still folded. */
const switchedDetail: Partial<CachedGameDetail> = {
  rom_id: 43,
  bios_status: { platform_slug: "genesis", server_count: 3, local_count: 3, all_downloaded: true },
  bios_level: "ok",
  bios_label: "3/3",
};

describe("gameDetailStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    nextAppId = ++appIdSeq;
    installDomEventListenerSpy();
    // The core-info read is shared with the info panel's load, and a shared
    // request only releases itself by settling — a test that holds one open
    // would hand it to the next test that reads the same rom.
    _resetSharedReadsForTests();

    vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({ found: false });
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus());
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfo);
    vi.mocked(backend.getBiosStatus).mockResolvedValue({ bios_status: null, bios_level: null, bios_label: null });
    vi.mocked(backend.getAchievementProgress).mockResolvedValue({
      success: true,
      earned: 0,
      total: 0,
      earned_achievements: [],
    });
    vi.mocked(backend.getRomMetadata).mockResolvedValue({} as never);
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
  });

  afterEach(() => {
    openSubscriptions.splice(0).forEach((unsubscribe) => unsubscribe());
    uninstallDomEventListenerSpy();
  });

  const found = (overrides: Partial<CachedGameDetail> = {}): CachedGameDetail => ({
    found: true,
    rom_id: 42,
    rom_name: "Test ROM",
    platform_slug: "snes",
    ...overrides,
  });

  describe("subscription lifecycle", () => {
    it("reports the neutral default for an appId nobody is subscribed to", () => {
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: null, installed: false, activeSlot: "default" });
    });

    it("loads the cached detail on first subscribe and notifies with the folded state", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          installed: true,
          fs_size_bytes: 4096,
          ra_id: 7,
          achievement_summary: { earned: 3, total: 50, earned_hardcore: 0 },
        }),
      );
      const notified = vi.fn();
      subscribe(nextAppId, notified);
      await flush();

      expect(vi.mocked(cachedStore.getCachedGameDetail)).toHaveBeenCalledWith(nextAppId);
      expect(notified).toHaveBeenCalled();
      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 42,
        romName: "Test ROM",
        platformSlug: "snes",
        installed: true,
        fsSizeBytes: 4096,
        raId: 7,
        achievementEarned: 3,
        achievementTotal: 50,
      });
    });

    it("serves a second subscriber from the same entry — one cached-detail read, both notified", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      const first = vi.fn();
      const second = vi.fn();
      subscribe(nextAppId, first);
      subscribe(nextAppId, second);
      await flush();

      expect(vi.mocked(cachedStore.getCachedGameDetail)).toHaveBeenCalledTimes(1);
      expect(first).toHaveBeenCalled();
      expect(second).toHaveBeenCalled();
    });

    it("stops notifying an unsubscribed caller even when its load is still in flight", async () => {
      const cached = deferred<CachedGameDetail>();
      vi.mocked(cachedStore.getCachedGameDetail).mockReturnValue(cached.promise);
      const notified = vi.fn();
      const unsubscribe = subscribe(nextAppId, notified);

      unsubscribe();
      cached.resolve(found());
      await flush();

      expect(notified).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId).romId).toBeNull();
    });

    it("attaches the entry's listeners on the first subscribe and detaches them on the last unsubscribe", async () => {
      const dataChangedBefore = domListenerCount("romm_data_changed");
      const uninstalledBefore = domListenerCount("romm_rom_uninstalled");
      const downloadBefore = deckyEventListenerCount("download_complete");

      const first = subscribe(nextAppId);
      const second = subscribe(nextAppId);
      await flush();
      expect(domListenerCount("romm_data_changed")).toBe(dataChangedBefore + 1);
      expect(domListenerCount("romm_rom_uninstalled")).toBe(uninstalledBefore + 1);
      expect(deckyEventListenerCount("download_complete")).toBe(downloadBefore + 1);

      first();
      expect(domListenerCount("romm_data_changed")).toBe(dataChangedBefore + 1);
      second();
      expect(domListenerCount("romm_data_changed")).toBe(dataChangedBefore);
      expect(domListenerCount("romm_rom_uninstalled")).toBe(uninstalledBefore);
      expect(deckyEventListenerCount("download_complete")).toBe(downloadBefore);
    });

    it("logs at warn level when the cached-detail read rejects", async () => {
      const logError = vi.spyOn(backend, "logError").mockImplementation(() => {});
      try {
        vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValue(new Error("boom"));
        subscribe(nextAppId);
        await flush();
        expect(logError).toHaveBeenCalledWith(expect.stringContaining("load error"));
      } finally {
        logError.mockRestore();
      }
    });

    it("useGameDetail renders the current state and re-renders on every change", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true }));
      const { result, unmount } = renderHook(() => useGameDetail(nextAppId));
      expect(result.current.romId).toBeNull();

      await flush();
      expect(result.current).toMatchObject({ romId: 42, installed: true });

      act(() => {
        noteSaveSyncDisplay(nextAppId, 42, { status: "none", label: "No saves", last_sync_check_at: null });
      });
      expect(result.current.saveSyncLabel).toBe("No saves");

      unmount();
      expect(domListenerCount("romm_data_changed")).toBe(0);
    });
  });

  describe("refreshSaveStatus", () => {
    beforeEach(() => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ save_sync_enabled: true }));
    });

    it("folds the read into the shared state", async () => {
      subscribe(nextAppId);
      vi.mocked(backend.getSaveStatus).mockResolvedValue(
        saveStatus({ active_slot: "slot-a", savefiles_in_content_dir: true }),
      );
      await flush();

      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(42);
      expect(getGameDetail(nextAppId)).toMatchObject({
        activeSlot: "slot-a",
        savefilesInContentDir: true,
        saveSyncStatus: "synced",
        saveSyncLabel: "Up to date",
      });
      expect(getGameDetail(nextAppId).saveStatus).not.toBeNull();
    });

    it("shares one request between overlapping callers", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();
      const pending = deferred<SaveStatus>();
      vi.mocked(backend.getSaveStatus).mockReturnValue(pending.promise);

      const first = refreshSaveStatus(nextAppId);
      const second = refreshSaveStatus(nextAppId);
      pending.resolve(saveStatus({ active_slot: "shared" }));
      const [firstStatus, secondStatus] = await Promise.all([first, second]);

      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledTimes(1);
      expect(firstStatus).toBe(secondStatus);
      expect(getGameDetail(nextAppId).activeSlot).toBe("shared");
    });

    it("issues a fresh request once the shared one has settled", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();

      await refreshSaveStatus(nextAppId);
      await refreshSaveStatus(nextAppId);

      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledTimes(2);
    });

    // A version switch re-keys the entry to a new rom_id without closing it, so
    // the generation is unchanged and the open read is the only thing that could
    // still speak for the previous ROM.
    it("does not serve a read left open across a version switch to the new rom", async () => {
      const previousRom = deferred<SaveStatus>();
      vi.mocked(backend.getSaveStatus).mockReturnValueOnce(previousRom.promise);
      subscribe(nextAppId);
      await flush();
      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(42);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ rom_id: 43, save_sync_enabled: true }));
      vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ rom_id: 43, active_slot: "new-version" }));

      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: nextAppId, rom_id: 43 },
          }),
        );
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(43);
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, activeSlot: "new-version" });

      previousRom.resolve(saveStatus({ rom_id: 42, active_slot: "old-version", savefiles_in_content_dir: true }));
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        activeSlot: "new-version",
        savefilesInContentDir: false,
      });
    });

    it("leaves the shown display untouched when the backend refuses the read", async () => {
      subscribe(nextAppId);
      await flush();
      const before = getGameDetail(nextAppId);
      vi.mocked(backend.getSaveStatus).mockResolvedValue({
        success: false,
        reason: "prune_active",
        message: "Cleanup is active.",
      });

      await expect(refreshSaveStatus(nextAppId)).resolves.toBeNull();
      expect(getGameDetail(nextAppId).saveSyncLabel).toBe(before.saveSyncLabel);
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("save status refused"));
    });

    it("rejects on a failed call and frees the shared slot for the next caller", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockRejectedValueOnce(new Error("offline"));

      await expect(refreshSaveStatus(nextAppId)).rejects.toThrow("offline");

      vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ active_slot: "after-retry" }));
      await refreshSaveStatus(nextAppId);
      expect(getGameDetail(nextAppId).activeSlot).toBe("after-retry");
    });

    it("does nothing while the rom identity is unresolved", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({ found: false });
      subscribe(nextAppId);
      await flush();

      await expect(refreshSaveStatus(nextAppId)).resolves.toBeNull();
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });
  });

  describe("save_sync notifications", () => {
    const dispatchSaveSync = (detail: Record<string, unknown>) =>
      globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", ...detail } }));

    it("re-reads the status for a matching rom_id", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ active_slot: "re-read" }));

      await act(async () => {
        dispatchSaveSync({ rom_id: 42 });
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(42);
      expect(getGameDetail(nextAppId).activeSlot).toBe("re-read");
    });

    it("ignores a notification for another rom_id", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();

      await act(async () => {
        dispatchSaveSync({ rom_id: 999 });
        await Promise.resolve();
      });

      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });

    // #975 — the cross-game bleed. The old per-component handler fell back to
    // the EVENT's rom_id while its own was still null, so a notification for
    // another game fetched and displayed that game's save status here.
    it("#975: drops a foreign rom_id while this appId's identity is still unresolved", async () => {
      const cached = deferred<CachedGameDetail>();
      vi.mocked(cachedStore.getCachedGameDetail).mockReturnValue(cached.promise);
      subscribe(nextAppId);
      await flush();
      expect(getGameDetail(nextAppId).romId).toBeNull();

      const foreign = saveStatus({ rom_id: 999, active_slot: null, savefiles_in_content_dir: true });
      vi.mocked(backend.getSaveStatus).mockResolvedValue(foreign);
      await act(async () => {
        dispatchSaveSync({ rom_id: 999 });
        // The same event carrying the other game's status inline — the shape
        // that would land without a read at all.
        dispatchSaveSync({ rom_id: 999, save_status: foreign });
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: null,
        activeSlot: "default",
        savefilesInContentDir: false,
        saveStatus: null,
      });

      // The load that was in flight all along still brings this ROM's own state.
      cached.resolve(found({ save_sync_enabled: true }));
      vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ active_slot: "ours" }));
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 42, activeSlot: "ours" });
    });

    // `rom_id` and `save_status` are independently optional on the event, so a
    // dispatch carrying a status but no rom_id would pass the handler's identity
    // check on the event itself — the shape #975 took.
    it("drops an inline status for another rom when the notification carries no rom_id", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();

      await act(async () => {
        dispatchSaveSync({
          save_status: saveStatus({ rom_id: 999, active_slot: "foreign", savefiles_in_content_dir: true }),
        });
        await Promise.resolve();
      });

      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 42,
        activeSlot: "default",
        savefilesInContentDir: false,
        saveStatus: null,
      });
    });

    it("uses a status carried on the notification instead of re-reading it", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();

      await act(async () => {
        dispatchSaveSync({ rom_id: 42, save_status: saveStatus({ active_slot: "inline" }) });
        await Promise.resolve();
      });

      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId).activeSlot).toBe("inline");
    });
  });

  describe("save_sync_settings notifications", () => {
    const dispatchSettings = (enabled: boolean) =>
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", {
          detail: { type: "save_sync_settings", save_sync_enabled: enabled },
        }),
      );

    it("enabling re-reads the status", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getSaveStatus).mockClear();

      await act(async () => {
        dispatchSettings(true);
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(42);
      expect(getGameDetail(nextAppId).saveSyncEnabled).toBe(true);
    });

    it("disabling clears the sync-derived display without a read, keeping the content-dir fact", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ save_sync_enabled: true }));
      vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ savefiles_in_content_dir: true }));
      subscribe(nextAppId);
      await flush();
      expect(getGameDetail(nextAppId).savefilesInContentDir).toBe(true);
      vi.mocked(backend.getSaveStatus).mockClear();

      await act(async () => {
        dispatchSettings(false);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId)).toMatchObject({
        saveSyncEnabled: false,
        saveSyncStatus: null,
        saveSyncLabel: "",
        // The local RetroArch config did not change with the setting, so the
        // fact stands; whether it is worth showing is each surface's call.
        savefilesInContentDir: true,
      });
    });
  });

  describe("core_changed notifications", () => {
    const dispatchCoreChanged = () =>
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "core_changed", platform_slug: "snes" } }),
      );

    it("re-reads core info and BIOS keyed on the rom_id", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      vi.mocked(backend.getBiosStatus).mockClear();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
        bios_level: "missing",
        bios_label: "0/3",
      });

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(42);
      expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledWith(42);
      expect(getGameDetail(nextAppId)).toMatchObject({
        activeCoreLabel: "Snes9x",
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
    });

    it("skips the reads while the rom identity is unresolved", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      vi.mocked(backend.getBiosStatus).mockClear();

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });

      expect(vi.mocked(backend.getPlatformCoreInfo)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getBiosStatus)).not.toHaveBeenCalled();
    });

    // The entry is re-keyed, not closed, so the generation fence cannot see this:
    // the core and BIOS answers below were read for the ROM the page has left.
    it("does not fold a core or BIOS answer read for the rom the entry has since left", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      const previousRomCore = deferred<CoreInfo>();
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(laterCoreInfo);
      vi.mocked(backend.getPlatformCoreInfo).mockReturnValueOnce(previousRomCore.promise);
      vi.mocked(backend.getBiosStatus).mockResolvedValue(biosMissing);

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledExactlyOnceWith(42);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found(switchedDetail));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, activeCoreLabel: "Genesis Plus GX" });

      previousRomCore.resolve(coreInfo);
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        activeCoreLabel: "Genesis Plus GX",
        biosStatus: "ok",
        biosLabel: "3/3",
      });
    });

    it("surfaces a failed read through debugLog instead of leaving it unhandled", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getPlatformCoreInfo).mockRejectedValue(new Error("handler-boom"));
      vi.mocked(backend.debugLog).mockClear();

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
    });

    // The same user action as refreshCoreAndBios, and it has to answer the same
    // way: a failed BIOS read is "we don't know", and the core answer that DID
    // land is still worth showing (#1693).
    it("folds the core answer and keeps the shown BIOS need when the BIOS read fails", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(laterCoreInfo);
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("offline"));
      vi.mocked(backend.debugLog).mockClear();

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        activeCoreLabel: "Genesis Plus GX",
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
      // Non-vacuous: the BIOS read's own catch handled it, so the handler-level
      // catch never fired.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
    });

    it("keeps the shown BIOS need when the BIOS read carries no answer", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: null,
        bios_level: null,
        bios_label: null,
        bios_status_unknown: true,
      });

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        activeCoreLabel: "Snes9x",
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
    });

    it("clears the BIOS need when the re-read reports the new core needs none", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({ bios_status: null, bios_level: null, bios_label: null });

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ biosNeeded: false, biosStatus: null, biosLabel: "" });
    });
  });

  describe("version_switched notifications", () => {
    it("re-derives the entry for this appId", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ rom_id: 43, rom_name: "Other version" }));

      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, romName: "Other version" });
    });

    // #1690 — the BIOS requirement is core-dependent and the core override is
    // keyed on rom_id, so two versions of one game genuinely can differ here.
    // The switched-to detail below carries no `stale_fields`, so the cached fold
    // is the only writer in play.
    it("clears the BIOS requirement when the switched-to version's core needs none", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ biosNeeded: true, biosStatus: "missing", biosLabel: "0/3" });

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ rom_id: 43 }));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        biosNeeded: false,
        biosStatus: null,
        biosLabel: "",
      });
    });

    it("keeps the requirement when the switched-to version's core needs BIOS too", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found(switchedDetail));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "3/3",
      });
    });

    // The cold-cache window (#1693): every firmware download and every platform
    // BIOS delete invalidates the in-memory firmware cache, and a detail derived
    // while it is cold carries no BIOS answer at all. It ships the same absent
    // `bios_status` as the clear above, so only the flag keeps the badge on.
    it("keeps the shown requirement when the re-derived detail carries no BIOS answer", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({ rom_id: 43, bios_status_unknown: true, stale_fields: [] }),
      );
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
    });

    it("clears the requirement when the switched-to version's live BIOS read reports none", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      // The switched-to detail still carries the previous version's cached
      // requirement and marks it stale, so the live re-read is what answers.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({ ...switchedDetail, stale_fields: ["bios"] }),
      );
      vi.mocked(backend.getBiosStatus).mockResolvedValue({ bios_status: null, bios_level: null, bios_label: null });

      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledWith(43);
      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        biosNeeded: false,
        biosStatus: null,
        biosLabel: "",
      });
    });

    // The over-fix this change invites: a read that FAILED is "we don't know",
    // not "no BIOS need", so it may not blank the cell (#1690).
    it("leaves the shown level standing when the switched-to version's BIOS re-read fails", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({ ...switchedDetail, stale_fields: ["bios"] }),
      );
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("offline"));

      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "3/3",
      });
    });

    it("ignores a switch for another appId", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockClear();

      await act(async () => {
        dispatchVersionSwitch(nextAppId + 1);
        await Promise.resolve();
      });

      expect(vi.mocked(cachedStore.getCachedGameDetail)).not.toHaveBeenCalled();
    });
  });

  // #1674 — a version switch and a re-read both re-derive the entry without
  // closing it, so two loads can be open at once and can finish in either order.
  // The identity write is the one write no rom binding can fence, because it is
  // what installs the identity such a binding would compare against.
  describe("out-of-order loads", () => {
    /** What the overtaken mount load resolves: a different rom, differing in
     *  every field of the identity write so a fold of it is visible whichever
     *  field is read. */
    const overtakenDetail = found({
      rom_id: 42,
      rom_name: "Mount ROM",
      platform_slug: "snes",
      installed: true,
      fs_size_bytes: 4096,
      save_sync_enabled: true,
      save_sync_display: { status: "conflict", label: "Conflict", last_sync_check_at: null },
      ra_id: 7,
      achievement_summary: achievementSummary(7, 70),
    });

    const switchedIdentity = found({
      rom_id: 43,
      rom_name: "Other version",
      platform_slug: "genesis",
      installed: false,
      fs_size_bytes: 2048,
      ra_id: 9,
      achievement_summary: achievementSummary(3, 30),
    });

    it("refuses the identity write of a load the version switch overtook", async () => {
      const mountLoad = deferred<CachedGameDetail>();
      vi.mocked(cachedStore.getCachedGameDetail).mockReturnValueOnce(mountLoad.promise);
      subscribe(nextAppId);
      await flush();
      expect(getGameDetail(nextAppId).romId).toBeNull();

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(switchedIdentity);
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId).romId).toBe(43);

      mountLoad.resolve(overtakenDetail);
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        romName: "Other version",
        platformSlug: "genesis",
        installed: false,
        fsSizeBytes: 2048,
        saveSyncEnabled: false,
        saveSyncStatus: null,
        saveSyncLabel: "",
        raId: 9,
        achievementEarned: 3,
        achievementTotal: 30,
      });
      // The overtaken load is abandoned whole, not merely refused its write: the
      // save-sync read its own detail asked for is never issued either.
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });

    // The uninstall's re-read is issued first and lands last. Without the
    // ordering fence the page would end up describing a ROM as gone that the
    // download has since put back.
    it("lets a later re-read of the same rom win over an earlier one that lands after it", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true, fs_size_bytes: 4096 }));
      subscribe(nextAppId);
      await flush();

      const afterUninstall = deferred<CachedGameDetail>();
      vi.mocked(cachedStore.getCachedGameDetail).mockReturnValueOnce(afterUninstall.promise);
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 42 } }));
        await Promise.resolve();
      });

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true, fs_size_bytes: 8192 }));
      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", downloadComplete(42));
        await Promise.resolve();
      });
      await flush();
      // The newest load is never the stale one — a deliberate re-read of the same
      // rom lands rather than being refused by its own fence.
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 42, installed: true, fsSizeBytes: 8192 });

      afterUninstall.resolve(found({ installed: false, fs_size_bytes: null }));
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 42, installed: true, fsSizeBytes: 8192 });
    });

    // Same rom throughout, so the rom binding admits the earlier answer — the
    // load sequence is the only thing that can tell the two apart. The vehicle is
    // the BIOS read rather than the core read because the core read is shared
    // with the info panel's load (`api/sharedReads.ts`): a second load of the
    // same rom joins the first read instead of issuing one that can answer
    // differently, so two distinct answers cannot be staged through it.
    it("drops a background answer read under a load a later load overtook", async () => {
      const overtakenBios = deferred<BiosStatusResult>();
      vi.mocked(backend.getBiosStatus).mockReturnValueOnce(overtakenBios.promise);
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: false, stale_fields: ["bios"] }));
      subscribe(nextAppId);
      await flush();
      expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledExactlyOnceWith(42);

      vi.mocked(backend.getBiosStatus).mockResolvedValue(biosAllPresent);
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true, stale_fields: ["bios"] }));
      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", downloadComplete(42));
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 42, biosStatus: "ok", biosLabel: "3/3" });

      overtakenBios.resolve(biosMissing);
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ biosStatus: "ok", biosLabel: "3/3" });
    });
  });

  // The mount load fires its background reads and returns; a switch landing
  // before one of them answers re-keys the entry without closing it, so the
  // generation the read was issued under is still current.
  describe("mount-load background refreshes across a version switch", () => {
    it("does not fold a core answer read for the rom the load resolved", async () => {
      const previousRomCore = deferred<CoreInfo>();
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(laterCoreInfo);
      vi.mocked(backend.getPlatformCoreInfo).mockReturnValueOnce(previousRomCore.promise);
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledExactlyOnceWith(42);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found(switchedDetail));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, activeCoreLabel: "Genesis Plus GX" });

      previousRomCore.resolve(coreInfo);
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, activeCoreLabel: "Genesis Plus GX" });
    });

    it("does not fold a BIOS answer read for the rom the load resolved", async () => {
      const previousRomBios = deferred<BiosStatusResult>();
      vi.mocked(backend.getBiosStatus).mockReturnValueOnce(previousRomBios.promise);
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ stale_fields: ["bios"] }));
      subscribe(nextAppId);
      await flush();
      expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledExactlyOnceWith(42);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found(switchedDetail));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, biosStatus: "ok", biosLabel: "3/3" });

      previousRomBios.resolve(biosMissing);
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, biosStatus: "ok", biosLabel: "3/3" });
    });

    it("does not fold an achievement count read for the rom the load resolved", async () => {
      const previousRomProgress = deferred<AchievementProgressResult>();
      vi.mocked(backend.getAchievementProgress).mockReturnValueOnce(previousRomProgress.promise);
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({ ra_id: 7, stale_fields: ["achievements"], achievement_summary: achievementSummary(7, 70) }),
      );
      subscribe(nextAppId);
      await flush();
      expect(vi.mocked(backend.getAchievementProgress)).toHaveBeenCalledExactlyOnceWith(42);
      expect(getGameDetail(nextAppId)).toMatchObject({ achievementEarned: 7, achievementTotal: 70 });

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({ rom_id: 43, ra_id: 9, achievement_summary: achievementSummary(3, 30) }),
      );
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, achievementEarned: 3, achievementTotal: 30 });

      previousRomProgress.resolve({ success: true, earned: 12, total: 70, earned_achievements: [] });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, achievementEarned: 3, achievementTotal: 30 });
    });

    // #1345: a switched-to detail with no achievement summary keeps the shown
    // count rather than degrading it to 0. That carry-over is the identity
    // write's doing, and the identity write is deliberately NOT rom-bound — this
    // pins that the binding above did not turn the carry-over into a reset.
    it("still keeps the last-known achievement counts when the switched-to detail carries no summary", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({ ra_id: 7, achievement_summary: achievementSummary(7, 70) }),
      );
      subscribe(nextAppId);
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ achievementEarned: 7, achievementTotal: 70 });

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ rom_id: 43 }));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, achievementEarned: 7, achievementTotal: 70 });
    });
  });

  describe("install-state notifications", () => {
    it("re-derives from a fresh cached detail on download_complete for this ROM", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: false }));
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true }));

      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", downloadComplete(42));
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(nextAppId);
      expect(getGameDetail(nextAppId).installed).toBe(true);
    });

    it("re-derives on romm_rom_uninstalled for this ROM", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true }));
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: false }));

      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 42 } }));
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId).installed).toBe(false);
    });

    it("re-derives on rom_adopted for this ROM", async () => {
      // An adoption writes an install record without a download, so no
      // download_complete fires — but `installed` changed just the same.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: false }));
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: true }));

      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "rom_adopted", rom_id: 42 } }));
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(nextAppId);
      expect(getGameDetail(nextAppId).installed).toBe(true);
    });

    it("ignores install events for another ROM", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: false }));
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockClear();

      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", downloadComplete(999));
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 999 } }));
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", { detail: { type: "rom_adopted", rom_id: 999 } }),
        );
        await Promise.resolve();
      });

      expect(vi.mocked(cachedStore.getCachedGameDetail)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId).installed).toBe(false);
    });
  });

  describe("explicit refreshes", () => {
    beforeEach(() => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
    });

    it("noteSaveSyncDisplay records a display the caller already knows to be true", async () => {
      subscribe(nextAppId);
      await flush();

      noteSaveSyncDisplay(nextAppId, 42, { status: "synced", label: "Just now", last_sync_check_at: null });

      expect(getGameDetail(nextAppId)).toMatchObject({ saveSyncStatus: "synced", saveSyncLabel: "Just now" });
    });

    it("noteSaveSyncDisplay does not record a display for the rom the entry has since left", async () => {
      subscribe(nextAppId);
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 42 });

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ rom_id: 43 }));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      noteSaveSyncDisplay(nextAppId, 42, { status: "synced", label: "Just now", last_sync_check_at: null });
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, saveSyncStatus: null, saveSyncLabel: "" });

      // The refusal is about whose display it is, not about the note being
      // inert: the same display for the rom the entry now holds still lands.
      noteSaveSyncDisplay(nextAppId, 43, { status: "synced", label: "Just now", last_sync_check_at: null });
      expect(getGameDetail(nextAppId)).toMatchObject({ saveSyncStatus: "synced", saveSyncLabel: "Just now" });
    });

    it("refreshBiosStatus adopts a refreshed level", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: { platform_slug: "snes", server_count: 3, local_count: 3, all_downloaded: true },
        bios_level: "ok",
        bios_label: "3/3",
      });

      await refreshBiosStatus(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({ biosStatus: "ok", biosLabel: "3/3" });
    });

    it("refreshBiosStatus leaves the shown level alone when the read fails", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 1, all_downloaded: false },
          bios_level: "partial",
          bios_label: "1/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("offline"));

      await refreshBiosStatus(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({ biosStatus: "partial", biosLabel: "1/3" });
    });

    it("refreshBiosStatus does not fold a level read for the rom the entry has since left", async () => {
      subscribe(nextAppId);
      await flush();
      const previousRom = deferred<BiosStatusResult>();
      vi.mocked(backend.getBiosStatus).mockReturnValueOnce(previousRom.promise);
      const inFlight = refreshBiosStatus(nextAppId);
      expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledExactlyOnceWith(42);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found(switchedDetail));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, biosStatus: "ok", biosLabel: "3/3" });

      previousRom.resolve(biosMissing);
      await inFlight;
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, biosStatus: "ok", biosLabel: "3/3" });
    });

    it("refreshCoreAndBios does not fold core or BIOS answers for the rom the entry has since left", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      const previousRomCore = deferred<CoreInfo>();
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(laterCoreInfo);
      vi.mocked(backend.getPlatformCoreInfo).mockReturnValueOnce(previousRomCore.promise);
      vi.mocked(backend.getBiosStatus).mockResolvedValue(biosMissing);
      const inFlight = refreshCoreAndBios(nextAppId);
      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledExactlyOnceWith(42);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found(switchedDetail));
      await act(async () => {
        dispatchVersionSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();
      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, activeCoreLabel: "Genesis Plus GX" });

      previousRomCore.resolve(coreInfo);
      await inFlight;
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({
        romId: 43,
        activeCoreLabel: "Genesis Plus GX",
        biosStatus: "ok",
        biosLabel: "3/3",
      });
      // The cache drop is not part of the fold: it only forces the next read to
      // go to the backend, so it runs even when the fold was refused.
      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(nextAppId);
    });

    it("refreshCoreAndBios re-derives both and drops the cached detail", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: { platform_slug: "snes", server_count: 1, local_count: 0, all_downloaded: false },
        bios_level: "missing",
        bios_label: "0/1",
      });

      await refreshCoreAndBios(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({
        activeCoreLabel: "Snes9x",
        biosNeeded: true,
        biosStatus: "missing",
      });
      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(nextAppId);
    });

    it("refreshCoreAndBios clears the BIOS need when the refreshed read reports none", async () => {
      // The core just changed, so the new core may genuinely need no BIOS — an
      // ANSWER saying so still takes the requirement off the row (#1690).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({ bios_status: null, bios_level: null, bios_label: null });

      await refreshCoreAndBios(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({
        activeCoreLabel: "Snes9x",
        biosNeeded: false,
        biosStatus: null,
        biosLabel: "",
      });
    });

    it("refreshCoreAndBios keeps the shown BIOS need when the refreshed read fails (#1693)", async () => {
      // The gear-menu core switch: the core read is local and succeeds, the BIOS
      // read goes over HTTP and fails. Clearing here launched the game without
      // its required BIOS and said nothing.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("offline"));

      await refreshCoreAndBios(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({
        // The core answer still lands — only the BIOS half is missing.
        activeCoreLabel: "Snes9x",
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
    });

    it("refreshCoreAndBios keeps the shown BIOS need when the read carries no answer (#1693)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        found({
          bios_status: { platform_slug: "snes", server_count: 3, local_count: 0, all_downloaded: false },
          bios_level: "missing",
          bios_label: "0/3",
        }),
      );
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: null,
        bios_level: null,
        bios_label: null,
        bios_status_unknown: true,
      });

      await refreshCoreAndBios(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({
        activeCoreLabel: "Snes9x",
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
    });

    it("does nothing for an appId nobody is subscribed to", async () => {
      noteSaveSyncDisplay(nextAppId, 42, { status: "none", label: "No saves", last_sync_check_at: null });
      await refreshBiosStatus(nextAppId);
      await refreshCoreAndBios(nextAppId);

      expect(vi.mocked(backend.getBiosStatus)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getPlatformCoreInfo)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId).saveSyncLabel).toBe("");
    });
  });
});
