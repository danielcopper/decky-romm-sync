/**
 * Exercises index.tsx's `download_complete` and `migration_relaunch_options`
 * listeners through the @decky/api event harness. The plugin factory registers
 * the listeners on the in-memory bus; tests dispatch events via emitDeckyEvent
 * and assert the launch-options confirm-poll fires for the payload's appId.
 *
 * The heavyweight registration side effects (game-detail patch, launch
 * interceptor, metadata patches, session manager) are mocked to no-ops so the
 * factory can run in happy-dom without touching Steam internals. steamShortcuts
 * is mocked so the confirm-poll is observable; logError is mocked so the
 * post-catch side effect (the surfaced error message) is observable.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { act } from "@testing-library/react";
import { toaster } from "@decky/api";
import { emitDeckyEvent, deckyEventListenerCount } from "./test-utils/decky-api-mock";
import { getSettingsResetNotice, getAllPlaytime, getAppIdRomIdMap } from "./api/backend";
import { getSettingsResetState, setSettingsResetState } from "./utils/settingsResetStore";
import { recordSyncCreated, resetSyncDelta } from "./utils/syncDeltaStore";
import type { DownloadCompleteEvent, SyncPlanData, SyncStaleData } from "./types";

vi.mock("./patches/gameDetailPatch", () => ({
  registerGameDetailPatch: vi.fn(),
  unregisterGameDetailPatch: vi.fn(),
  registerRomMAppId: vi.fn(),
}));
vi.mock("./patches/metadataPatches", () => ({
  registerMetadataPatches: vi.fn(),
  unregisterMetadataPatches: vi.fn(),
  applyAllPlaytime: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./utils/launchInterceptor", () => ({
  registerLaunchInterceptor: vi.fn(),
  unregisterLaunchInterceptor: vi.fn(),
}));
vi.mock("./utils/sessionManager", () => ({
  initSessionManager: vi.fn().mockResolvedValue(undefined),
  destroySessionManager: vi.fn(),
}));
vi.mock("./utils/syncManager", () => ({
  initUnitSyncManager: vi.fn(() => () => {}),
}));

// Observe the collection create/update + stale-cleanup calls fired by
// onSyncComplete. getHostname resolves a fixed hostname so the machine-scoped
// `RomM: <platform> (steamdeck)` suffix is deterministic.
const createOrUpdateCollections = vi.fn().mockResolvedValue(undefined);
const createOrUpdateRomMCollections = vi.fn().mockResolvedValue(undefined);
const clearPlatformCollection = vi.fn().mockResolvedValue(undefined);
vi.mock("./utils/collections", () => ({
  createOrUpdateCollections: (...args: unknown[]) => createOrUpdateCollections(...args),
  createOrUpdateRomMCollections: (...args: unknown[]) => createOrUpdateRomMCollections(...args),
  clearPlatformCollection: (...args: unknown[]) => clearPlatformCollection(...args),
  getHostname: vi.fn().mockResolvedValue("steamdeck"),
}));

// Observe the launch-options confirm-poll.
const setLaunchOptionsConfirmed = vi.fn().mockResolvedValue(true);
const removeShortcut = vi.fn();
vi.mock("./utils/steamShortcuts", () => ({
  removeShortcut: (...args: unknown[]) => removeShortcut(...args),
  setLaunchOptionsConfirmed: (...args: unknown[]) => setLaunchOptionsConfirmed(...args),
}));

// Observe the surfaced error message (post-catch side effect).
const logError = vi.fn();
vi.mock("./api/backend", async () => {
  const actual = await vi.importActual<typeof import("./api/backend")>("./api/backend");
  return {
    ...actual,
    logError: (...args: unknown[]) => logError(...args),
    logInfo: vi.fn(),
  };
});

import { applyAllPlaytime } from "./patches/metadataPatches";
import definePluginResult from "./index";

// `definePlugin` is stubbed in test-setup to return its factory unchanged, so
// the default export IS the factory. Calling it registers the listeners and
// returns the plugin descriptor (with onDismount).
const pluginFactory = definePluginResult as unknown as () => { onDismount: () => void };

function flush(): Promise<void> {
  return new Promise((r) => setTimeout(r, 0));
}

describe("index.tsx — download_complete launch-options sync", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    logError.mockClear();
  });

  it("confirm-sets launch options for the payload appId on download_complete", async () => {
    const plugin = pluginFactory();

    const event: DownloadCompleteEvent = {
      rom_id: 42,
      rom_name: "Test ROM",
      platform_name: "PSX",
      file_path: "/games/test.bin",
      app_id: 5000,
      launch_options: 'flatpak run net.retrodeck.retrodeck "/games/test.bin"',
    };
    act(() => {
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", event);
    });
    await flush();

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(
      5000,
      'flatpak run net.retrodeck.retrodeck "/games/test.bin"',
    );
    plugin.onDismount();
  });

  it("no-ops gracefully when the downloaded rom has no bound appId (null)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", {
        rom_id: 999,
        rom_name: "Unsynced",
        platform_name: "PSX",
        file_path: "/games/u.bin",
        app_id: null,
        launch_options: 'flatpak run net.retrodeck.retrodeck "/games/u.bin"',
      });
    });
    await flush();

    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("surfaces a logError when setLaunchOptionsConfirmed rejects", async () => {
    setLaunchOptionsConfirmed.mockRejectedValue(new Error("set failed"));
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", {
        rom_id: 42,
        rom_name: "Test ROM",
        platform_name: "PSX",
        file_path: "/games/test.bin",
        app_id: 5000,
        launch_options: 'flatpak run net.retrodeck.retrodeck "/games/test.bin"',
      });
    });
    await flush();

    expect(logError).toHaveBeenCalledWith(
      expect.stringContaining("download_complete: failed to set launch options for rom 42"),
    );
    plugin.onDismount();
  });
});

describe("index.tsx — sync_stale listener", () => {
  beforeEach(() => {
    removeShortcut.mockClear();
    logError.mockClear();
  });

  it("removes each stale shortcut by the payload app_id (no rom_id→app_id re-resolve)", async () => {
    // No getExistingRomMShortcuts is even imported — proving the orphan race is
    // gone: removal happens via the payload app_id the backend captured before
    // unbinding, so an empty backend map can't strand the shortcut.
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncStaleData]>("sync_stale", {
        remove: [
          { rom_id: 99, app_id: 9900 },
          { rom_id: 77, app_id: 7700 },
        ],
      });
    });
    await flush();

    expect(removeShortcut).toHaveBeenCalledWith(9900);
    expect(removeShortcut).toHaveBeenCalledWith(7700);
    expect(removeShortcut).toHaveBeenCalledTimes(2);
    plugin.onDismount();
  });

  it("ignores an empty remove array", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncStaleData]>("sync_stale", { remove: [] });
    });
    await flush();

    expect(removeShortcut).not.toHaveBeenCalled();
    plugin.onDismount();
  });
});

describe("index.tsx — migration_relaunch_options listener", () => {
  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    logError.mockClear();
  });

  it("confirm-sets launch options for each migrated item", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[{ items: { app_id: number; launch_options: string }[] }]>("migration_relaunch_options", {
        items: [
          { app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck "/new/a.bin"' },
          { app_id: 200, launch_options: 'flatpak run net.retrodeck.retrodeck "/new/b.bin"' },
        ],
      });
    });
    await flush();

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, 'flatpak run net.retrodeck.retrodeck "/new/a.bin"');
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(200, 'flatpak run net.retrodeck.retrodeck "/new/b.bin"');
    plugin.onDismount();
  });

  it("ignores an empty items array", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[{ items: { app_id: number; launch_options: string }[] }]>("migration_relaunch_options", {
        items: [],
      });
    });
    await flush();

    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("surfaces a logError when setLaunchOptionsConfirmed rejects for an item", async () => {
    setLaunchOptionsConfirmed.mockRejectedValue(new Error("set failed"));
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[{ items: { app_id: number; launch_options: string }[] }]>("migration_relaunch_options", {
        items: [{ app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck "/new/a.bin"' }],
      });
    });
    await flush();

    expect(logError).toHaveBeenCalledWith(
      expect.stringContaining("migration_relaunch_options: failed to set launch options for appId 100"),
    );
    plugin.onDismount();
  });

  it("removes the migration_relaunch_options listener on unmount", () => {
    const plugin = pluginFactory();
    expect(deckyEventListenerCount("migration_relaunch_options")).toBe(1);

    plugin.onDismount();
    expect(deckyEventListenerCount("migration_relaunch_options")).toBe(0);
  });
});

describe("index.tsx — corrupt-settings reset notice", () => {
  beforeEach(() => {
    vi.mocked(toaster.toast).mockClear();
    logError.mockClear();
    vi.mocked(getSettingsResetNotice).mockReset();
    // Reset the module store so a prior test's pending state doesn't leak.
    setSettingsResetState({ pending: false, backedUpTo: null });
  });

  it("populates the store and fires NO toast when the boot notice reports a reset", async () => {
    vi.mocked(getSettingsResetNotice).mockResolvedValue({
      pending: true,
      backed_up_to: "settings.json.corrupt-1781697600",
    });
    const plugin = pluginFactory();
    await flush();

    // Persistent banner store is populated — surfaced by the QAM banner +
    // game-detail card, not a toast.
    expect(getSettingsResetState()).toEqual({
      pending: true,
      backedUpTo: "settings.json.corrupt-1781697600",
    });
    expect(toaster.toast).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("leaves the store not-pending and fires no toast when the boot notice reports no reset", async () => {
    vi.mocked(getSettingsResetNotice).mockResolvedValue({ pending: false, backed_up_to: null });
    const plugin = pluginFactory();
    await flush();

    expect(getSettingsResetState()).toEqual({ pending: false, backedUpTo: null });
    expect(toaster.toast).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("surfaces a logError when the reset-notice check rejects", async () => {
    vi.mocked(getSettingsResetNotice).mockRejectedValue(new Error("boom"));
    const plugin = pluginFactory();
    await flush();

    expect(logError).toHaveBeenCalledWith(expect.stringContaining("Failed to check settings reset notice"));
    expect(toaster.toast).not.toHaveBeenCalled();
    plugin.onDismount();
  });
});

describe("index.tsx — sync_complete stale-collection cleanup (#1040)", () => {
  // A SNES platform collection and a [Faves] RomM smart-collection, both
  // machine-scoped to "steamdeck" (the getHostname mock). Delete is a vi.fn so
  // the smart-collection delete is observable; the platform collection is
  // removed via the mocked clearPlatformCollection, so only its presence in
  // userCollections matters for the stale filter.
  function seedCollections(): {
    snes: { Delete: ReturnType<typeof vi.fn> };
    faves: { Delete: ReturnType<typeof vi.fn> };
  } {
    const snes = { id: "snes-id", displayName: "RomM: Super Nintendo (steamdeck)", Delete: vi.fn() };
    const faves = { id: "faves-id", displayName: "RomM: [Faves] (steamdeck)", Delete: vi.fn() };
    vi.stubGlobal("collectionStore", { userCollections: [snes, faves] });
    return { snes, faves };
  }

  type SyncCompletePayload = {
    platform_app_ids: Record<string, number[]>;
    romm_collection_app_ids?: Record<string, number[]>;
    total_games: number;
    cancelled?: boolean;
  };

  function emitSyncComplete(payload: SyncCompletePayload): void {
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", payload);
    });
  }

  beforeEach(() => {
    createOrUpdateCollections.mockClear();
    createOrUpdateRomMCollections.mockClear();
    clearPlatformCollection.mockClear();
    vi.mocked(applyAllPlaytime).mockClear();
    vi.mocked(applyAllPlaytime).mockResolvedValue(undefined);
    vi.mocked(toaster.toast).mockClear();
    logError.mockClear();
    // Give the playtime re-apply detach a well-shaped payload so it reaches
    // applyAllPlaytime instead of throwing on a destructure of undefined.
    vi.mocked(getAllPlaytime).mockResolvedValue({ playtime: {} });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({});
  });

  it("runs the stale cleanup on a completed (non-cancelled) sync", async () => {
    const { faves } = seedCollections();
    const plugin = pluginFactory();

    // Only "Nintendo 64" is active — SNES and [Faves] are stale and removed.
    emitSyncComplete({ platform_app_ids: { "Nintendo 64": [1] }, total_games: 1 });
    await flush();

    expect(clearPlatformCollection).toHaveBeenCalledWith("Super Nintendo");
    expect(faves.Delete).toHaveBeenCalledTimes(1);
    plugin.onDismount();
  });

  it("skips the stale cleanup on a cancelled sync with a partial map (regression)", async () => {
    const { snes, faves } = seedCollections();
    const plugin = pluginFactory();

    // Cancel reached only "Nintendo 64"; SNES + [Faves] must SURVIVE.
    emitSyncComplete({ platform_app_ids: { "Nintendo 64": [1] }, total_games: 1, cancelled: true });
    await flush();

    expect(clearPlatformCollection).not.toHaveBeenCalled();
    expect(snes.Delete).not.toHaveBeenCalled();
    expect(faves.Delete).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("skips the stale cleanup on an early cancel with an empty map (full-wipe case)", async () => {
    const { snes, faves } = seedCollections();
    const plugin = pluginFactory();

    // Cancel fired before unit 1 — the map is empty. Treating it as the active
    // set would wipe EVERY RomM collection; nothing must be deleted.
    emitSyncComplete({ platform_app_ids: {}, total_games: 0, cancelled: true });
    await flush();

    expect(clearPlatformCollection).not.toHaveBeenCalled();
    expect(snes.Delete).not.toHaveBeenCalled();
    expect(faves.Delete).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("still fires the cancelled toast and re-applies playtime on a cancelled sync", async () => {
    seedCollections();
    const plugin = pluginFactory();
    // The factory's own init runs one initial playtime apply; clear it so the
    // assertion counts only the apply triggered by sync_complete.
    await flush();
    vi.mocked(applyAllPlaytime).mockClear();
    vi.mocked(toaster.toast).mockClear();

    emitSyncComplete({ platform_app_ids: { "Nintendo 64": [1] }, total_games: 1, cancelled: true });
    await flush();

    expect(toaster.toast).toHaveBeenCalledWith(expect.objectContaining({ body: expect.stringContaining("cancelled") }));
    expect(applyAllPlaytime).toHaveBeenCalledTimes(1);
    plugin.onDismount();
  });

  it("still creates/updates the reached platforms' collections on a cancelled sync", async () => {
    seedCollections();
    const plugin = pluginFactory();

    emitSyncComplete({ platform_app_ids: { "Nintendo 64": [1] }, total_games: 1, cancelled: true });
    await flush();

    // The additive create/update path is NOT gated on cancel — the platforms
    // that DID complete still get their collections.
    expect(createOrUpdateCollections).toHaveBeenCalledWith({ "Nintendo 64": [1] });
    plugin.onDismount();
  });
});

describe("index.tsx — sync_complete toast shows the true delta (#744)", () => {
  // total_games is intentionally MISLEADING in these payloads (the bug): the
  // toast must ignore it and report the real created/removed delta tracked by
  // syncDeltaStore. created is seeded via recordSyncCreated (the mocked
  // syncManager would do this on the create path); removed flows through the
  // real sync_stale listener.
  type SyncCompletePayload = {
    platform_app_ids: Record<string, number[]>;
    romm_collection_app_ids?: Record<string, number[]>;
    total_games: number;
    cancelled?: boolean;
  };

  function lastToastBody(): string | undefined {
    const calls = vi.mocked(toaster.toast).mock.calls;
    if (calls.length === 0) return undefined;
    const last = calls[calls.length - 1]![0] as { body?: string };
    return last.body;
  }

  beforeEach(() => {
    vi.mocked(toaster.toast).mockClear();
    logError.mockClear();
    vi.mocked(applyAllPlaytime).mockResolvedValue(undefined);
    vi.mocked(getAllPlaytime).mockResolvedValue({ playtime: {} });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({});
    // No RomM collections so the stale-cleanup detach is a no-op for these tests.
    vi.stubGlobal("collectionStore", { userCollections: [] });
    resetSyncDelta();
  });

  it("reports 'X added, Y removed' when both are non-zero (ignores total_games)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { units: [], total_units: 2, total_roms: 2 });
    });
    // Two distinct shortcuts created this run (what the syncManager create path records).
    recordSyncCreated(100);
    recordSyncCreated(200);
    // One shortcut removed via the real sync_stale listener.
    act(() => {
      emitDeckyEvent<[SyncStaleData]>("sync_stale", { remove: [{ rom_id: 7, app_id: 700 }] });
    });

    // total_games=53 is the misleading total — the toast must NOT use it.
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 53 });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync complete — 2 added, 1 removed.");
    plugin.onDismount();
  });

  it("omits the zero part — only removals → 'Sync complete — N removed.'", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { units: [], total_units: 1, total_roms: 0 });
    });
    act(() => {
      emitDeckyEvent<[SyncStaleData]>("sync_stale", {
        remove: [
          { rom_id: 7, app_id: 700 },
          { rom_id: 8, app_id: 800 },
        ],
      });
    });
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 53 });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync complete — 2 removed.");
    plugin.onDismount();
  });

  it("reports 'Library up to date.' when nothing changed (the #744 repro)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { units: [], total_units: 1, total_roms: 53 });
    });
    // No creates, no removes — but total_games=53 (the old toast wrongly said
    // "53 games added"). The fixed toast must say the library is up to date.
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 53 });
    });
    await flush();

    expect(lastToastBody()).toBe("Library up to date.");
    plugin.onDismount();
  });

  it("dedups a shortcut created in two units (platform + collection) — counted once", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { units: [], total_units: 2, total_roms: 1 });
    });
    // Same appId surfaces in its platform unit and a collection unit; the Set
    // in the store collapses it to one "added".
    recordSyncCreated(100);
    recordSyncCreated(100);
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 1 });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync complete — 1 added.");
    plugin.onDismount();
  });

  it("on cancel with partial work → 'Sync cancelled — … so far.'", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { units: [], total_units: 3, total_roms: 10 });
    });
    recordSyncCreated(100);
    recordSyncCreated(200);
    recordSyncCreated(300);
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 53,
        cancelled: true,
      });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync cancelled — 3 added so far.");
    plugin.onDismount();
  });

  it("on cancel before any work → 'Sync cancelled.' (no delta)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { units: [], total_units: 3, total_roms: 10 });
    });
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 53,
        cancelled: true,
      });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync cancelled.");
    plugin.onDismount();
  });
});
