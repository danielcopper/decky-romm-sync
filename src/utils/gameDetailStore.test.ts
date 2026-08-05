import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import * as backend from "../api/backend";
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
import type { DownloadCompleteEvent, SaveStatus } from "../types";

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

const downloadComplete = (romId: number): DownloadCompleteEvent => ({
  rom_id: romId,
  rom_name: "Test ROM",
  platform_name: "SNES",
  file_path: "/roms/test.sfc",
  app_id: null,
  launch_options: "",
});

const coreInfo = {
  active_core: "snes9x.so",
  active_core_label: "Snes9x",
  platform_core_label: null,
  has_game_override: false,
  emulator_data_available: true,
  emulators: [
    {
      label: "Snes9x",
      kind: "libretro" as const,
      core_so: "snes9x.so",
      is_default: true,
      bakeable: true,
      reason: null,
    },
  ],
};

describe("gameDetailStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    nextAppId = ++appIdSeq;
    installDomEventListenerSpy();

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
        noteSaveSyncDisplay(nextAppId, { status: "none", label: "No saves", last_sync_check_at: null });
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

    it("surfaces a failed read through debugLog instead of leaving it unhandled", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("handler-boom"));
      vi.mocked(backend.debugLog).mockClear();

      await act(async () => {
        dispatchCoreChanged();
        await Promise.resolve();
      });
      await flush();

      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
    });
  });

  describe("version_switched notifications", () => {
    const dispatchSwitch = (appId: number) =>
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "version_switched", app_id: appId, rom_id: 43 } }),
      );

    it("re-derives the entry for this appId", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ rom_id: 43, rom_name: "Other version" }));

      await act(async () => {
        dispatchSwitch(nextAppId);
        await Promise.resolve();
      });
      await flush();

      expect(getGameDetail(nextAppId)).toMatchObject({ romId: 43, romName: "Other version" });
    });

    it("ignores a switch for another appId", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found());
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockClear();

      await act(async () => {
        dispatchSwitch(nextAppId + 1);
        await Promise.resolve();
      });

      expect(vi.mocked(cachedStore.getCachedGameDetail)).not.toHaveBeenCalled();
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

    it("ignores install events for another ROM", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(found({ installed: false }));
      subscribe(nextAppId);
      await flush();
      vi.mocked(cachedStore.getCachedGameDetail).mockClear();

      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", downloadComplete(999));
        globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 999 } }));
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

      noteSaveSyncDisplay(nextAppId, { status: "synced", label: "Just now", last_sync_check_at: null });

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

    it("refreshCoreAndBios clears the BIOS need when the refreshed read fails", async () => {
      subscribe(nextAppId);
      await flush();
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("offline"));

      await refreshCoreAndBios(nextAppId);

      expect(getGameDetail(nextAppId)).toMatchObject({ activeCoreLabel: "Snes9x", biosNeeded: false });
    });

    it("does nothing for an appId nobody is subscribed to", async () => {
      noteSaveSyncDisplay(nextAppId, { status: "none", label: "No saves", last_sync_check_at: null });
      await refreshBiosStatus(nextAppId);
      await refreshCoreAndBios(nextAppId);

      expect(vi.mocked(backend.getBiosStatus)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getPlatformCoreInfo)).not.toHaveBeenCalled();
      expect(getGameDetail(nextAppId).saveSyncLabel).toBe("");
    });
  });
});
