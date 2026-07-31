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
import { act, waitFor } from "@testing-library/react";
import { toaster } from "@decky/api";
import { emitDeckyEvent, deckyEventListenerCount } from "./test-utils/decky-api-mock";
import {
  getSettingsResetNotice,
  getAllPlaytime,
  getAppIdRomIdMap,
  getInstalledRelaunchOptions,
  invalidateCachedGameDetail,
  getMetadataCachePage,
  releasePruneConflictLease,
  waitForPruneRelease,
} from "./api/backend";
import { getSettingsResetState, setSettingsResetState } from "./utils/settingsResetStore";
import { getDownloadState, setDownloads } from "./utils/downloadStore";
import { getSyncProgress, setSyncProgress } from "./utils/syncProgress";
import { estimateApplySeconds } from "./utils/syncEstimate";
import { resetEta, weightedCoarseFraction } from "./utils/syncEta";
import { recordSyncCreated, resetSyncDelta, getSyncDelta } from "./utils/syncDeltaStore";
import { resetSyncCancel } from "./utils/syncManager";
import { beginPrunePreview, beginPruneRun, getPruneState, resetPruneState } from "./utils/pruneStore";
import { mountPruneLeasePlugin, releaseAllPruneLeases } from "./utils/pruneLease";
import type {
  DownloadCompleteEvent,
  DownloadProgressEvent,
  SyncPlanData,
  SyncProgress,
  SyncStaleData,
  RomMetadata,
} from "./types";

vi.mock("./patches/gameDetailPatch", () => ({
  registerGameDetailPatch: vi.fn(),
  unregisterGameDetailPatch: vi.fn(),
  registerRomMAppId: vi.fn(),
  unregisterRomMAppId: vi.fn(),
}));
vi.mock("./patches/metadataPatches", () => ({
  registerMetadataPatches: vi.fn(),
  unregisterMetadataPatches: vi.fn(),
  applyAllPlaytime: vi.fn().mockResolvedValue(undefined),
  applyAllMetadata: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./utils/launchInterceptor", () => ({
  registerLaunchInterceptor: vi.fn(),
  unregisterLaunchInterceptor: vi.fn(),
}));
vi.mock("./utils/sessionManager", () => ({
  initSessionManager: vi.fn().mockResolvedValue(undefined),
  destroySessionManager: vi.fn(),
}));

const handlePruneAction = vi.fn().mockResolvedValue(undefined);
vi.mock("./utils/pruneActions", () => ({
  handlePruneAction: (...args: unknown[]) => handlePruneAction(...args),
  cancelPruneActions: vi.fn(),
}));
const publishCommittedVersionSwitch = vi.fn().mockResolvedValue(undefined);
vi.mock("./utils/versionSwitchApplication", () => ({
  publishCommittedVersionSwitch: (...args: unknown[]) => publishCommittedVersionSwitch(...args),
}));
vi.mock("./utils/syncManager", () => ({
  initUnitSyncManager: vi.fn(() => () => {}),
  resetSyncCancel: vi.fn(),
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
    invalidateCachedGameDetail: vi.fn(),
    logError: (...args: unknown[]) => logError(...args),
    logInfo: vi.fn(),
  };
});

import { applyAllPlaytime, registerMetadataPatches, applyAllMetadata } from "./patches/metadataPatches";
import { registerRomMAppId, unregisterRomMAppId } from "./patches/gameDetailPatch";
import definePluginResult from "./index";

// `definePlugin` is stubbed in test-setup to return its factory unchanged, so
// the default export IS the factory. Calling it registers the listeners and
// returns the plugin descriptor (with onDismount).
const pluginFactory = definePluginResult as unknown as () => { onDismount: () => void };

function flush(): Promise<void> {
  return new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
  // The metadata cache is paged at init; default to a single empty page so
  // loadAppIdsAndMetadata terminates and reaches initDone in every test. Cases
  // that assert init behaviour rely on this resolving (the raw callable stub
  // resolves undefined, which would throw on `page.total`).
  vi.mocked(getMetadataCachePage).mockResolvedValue({ items: {}, total: 0 });
  // The sync-progress store is a real module — reset it so an etaSeconds set by
  // one test's sync_plan doesn't leak into the next.
  setSyncProgress({ running: false, stage: "", current: 0, total: 0, message: "" });
  resetPruneState();
  handlePruneAction.mockClear();
  publishCommittedVersionSwitch.mockClear();
  vi.mocked(waitForPruneRelease).mockReset().mockResolvedValue({
    success: true,
    message: "Cleanup claim is released.",
  });
  vi.mocked(releasePruneConflictLease).mockReset().mockResolvedValue({ success: true, message: "released" });
  vi.mocked(invalidateCachedGameDetail).mockClear();
  // The global afterEach's vi.unstubAllGlobals wipes the Steam ambient globals
  // after the file's first test; several sync_complete paths read SteamClient /
  // appStore, so default them to no-ops here.
  vi.stubGlobal("SteamClient", { Apps: {} });
  vi.stubGlobal("appStore", { GetAppOverviewByAppID: () => null, allApps: [] });
});

describe("index.tsx — persistent prune listeners", () => {
  it("handles tokenized Steam actions at the plugin root and unregisters on dismount", async () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-1");
    const action = {
      run_id: "run-1",
      preview_id: "preview-1",
      action_token: "token-1",
      action: "remove_shortcut" as const,
      app_id: 9001,
    };
    expect(deckyEventListenerCount("prune_action_required")).toBe(1);

    await act(async () => {
      emitDeckyEvent("prune_action_required", action);
      await Promise.resolve();
    });

    expect(handlePruneAction).toHaveBeenCalledWith(action);
    plugin.onDismount();
    expect(deckyEventListenerCount("prune_action_required")).toBe(0);
    expect(deckyEventListenerCount("prune_progress")).toBe(0);
    expect(deckyEventListenerCount("prune_complete")).toBe(0);
  });

  it("stores progress and completion, invalidates affected details, and emits a refresh", async () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-1");
    const changed = vi.fn();
    globalThis.addEventListener("romm_data_changed", changed);

    act(() => {
      emitDeckyEvent("prune_progress", {
        run_id: "run-1",
        preview_id: "preview-1",
        current: 1,
        total: 2,
        stage: "checking",
        rom_ids: [7],
        name: "Removed Game",
      });
      emitDeckyEvent("prune_complete", {
        success: true,
        partial: false,
        run_id: "run-1",
        preview_id: "preview-1",
        removed_rom_ids: [7],
        affected_app_ids: [9001],
        removed_app_ids: [9001],
        results: [{ group_id: "group-1", rom_ids: [7], status: "removed", message: "Removed." }],
      });
    });

    expect(getPruneState().progress).toBeNull();
    expect(getPruneState().complete?.removed_rom_ids).toEqual([7]);
    expect(invalidateCachedGameDetail).toHaveBeenCalledWith(9001);
    expect(unregisterRomMAppId).toHaveBeenCalledWith(9001);
    expect(changed).toHaveBeenCalledTimes(1);
    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Removed 1 local entry." });

    globalThis.removeEventListener("romm_data_changed", changed);
    plugin.onDismount();
  });

  it("a foreign or duplicate terminal frame has no root side effects", () => {
    const plugin = pluginFactory();
    const changed = vi.fn();
    globalThis.addEventListener("romm_data_changed", changed);
    vi.mocked(unregisterRomMAppId).mockClear();
    beginPrunePreview("preview-current");
    beginPruneRun("current", "preview-current");
    const frame = {
      success: true,
      partial: false,
      run_id: "old",
      preview_id: "preview-old",
      chunk_index: 0,
      final: true,
      removed_rom_ids: [7],
      affected_app_ids: [9001],
      removed_app_ids: [9001],
      results: [{ group_id: "group-1", rom_ids: [7], status: "removed" as const, message: "Removed." }],
    };

    act(() => {
      emitDeckyEvent("prune_complete", frame);
      emitDeckyEvent("prune_complete", frame);
    });

    expect(getPruneState().runId).toBe("current");
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
    expect(unregisterRomMAppId).not.toHaveBeenCalled();
    expect(changed).not.toHaveBeenCalled();
    globalThis.removeEventListener("romm_data_changed", changed);
    plugin.onDismount();
  });

  it("surfaces a zero-row committed partial instead of reporting that nothing changed", () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-partial");

    act(() => {
      emitDeckyEvent("prune_complete", {
        success: false,
        partial: true,
        run_id: "run-partial",
        preview_id: "preview-partial",
        removed_count: 0,
        problem_count: 1,
        removed_rom_ids: [],
        affected_app_ids: [9001],
        removed_app_ids: [9001],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7],
            status: "partial",
            committed_action: "remove_shortcut",
            message: "Steam removed the shortcut, but local cleanup was retained.",
          },
        ],
      });
    });

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Shortcut removal committed; local cleanup incomplete.",
      subtext: "Steam removed the shortcut, but local cleanup was retained.",
    });
    plugin.onDismount();
  });

  it("hands back a continuation lease the terminal frame gave it nothing to do with", async () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-nothing");

    await act(async () => {
      emitDeckyEvent("prune_complete", {
        success: true,
        partial: false,
        run_id: "run-nothing",
        preview_id: "preview-nothing",
        // The lease is attached by the backend emit path; this run committed no
        // repoint, so publishPruneSwitches is never called for it.
        publication_required: true,
        prune_lease_token: "orphan-lease",
        removed_rom_ids: [7],
        affected_app_ids: [],
        results: [{ group_id: "group-1", rom_ids: [7], status: "removed", message: "Removed." }],
      });
      await Promise.resolve();
    });

    // Without this the lease pins the admission gate for its full 300s TTL and
    // every conflicting callable — including the next cleanup — is refused.
    await waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("orphan-lease"));
    plugin.onDismount();
  });

  it("publishes a known committed partial repoint after terminal completion", async () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-repoint");
    let release: ((value: { success: true; message: string }) => void) | undefined;
    vi.mocked(waitForPruneRelease).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );

    act(() => {
      emitDeckyEvent("prune_complete", {
        success: false,
        partial: true,
        run_id: "run-repoint-partial",
        preview_id: "preview-repoint",
        publication_required: true,
        prune_lease_token: "publication-lease",
        removed_rom_ids: [],
        affected_app_ids: [9001],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7, 8],
            status: "partial",
            committed_action: "repoint_shortcut",
            app_id: 9001,
            target_rom_id: 8,
            message: "The shortcut changed; source data was retained.",
          },
        ],
      });
    });
    await flush();

    expect(publishCommittedVersionSwitch).not.toHaveBeenCalled();
    expect(releasePruneConflictLease).not.toHaveBeenCalledWith("publication-lease");
    release?.({ success: true, message: "released" });
    await flush();
    expect(publishCommittedVersionSwitch).toHaveBeenCalledWith(9001, 8, undefined, expect.any(AbortSignal));
    expect(releasePruneConflictLease).toHaveBeenCalledWith("publication-lease");
    plugin.onDismount();
  });

  it("does not publish an ambiguous repoint outcome", async () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-ambiguous");

    act(() => {
      emitDeckyEvent("prune_complete", {
        success: false,
        partial: true,
        run_id: "run-repoint-ambiguous",
        preview_id: "preview-ambiguous",
        removed_rom_ids: [],
        affected_app_ids: [9001],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7, 8],
            status: "partial",
            committed_action: "repoint_shortcut",
            action_ambiguous: true,
            app_id: 9001,
            target_rom_id: 8,
            message: "The repoint outcome is unknown.",
          },
        ],
      });
    });
    await flush();

    expect(publishCommittedVersionSwitch).not.toHaveBeenCalled();
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Shortcut repoint outcome is uncertain; source data was retained.",
      subtext: "The repoint outcome is unknown.",
    });
    plugin.onDismount();
  });

  it("fails closed when a committed repoint terminal frame has no publication lease", async () => {
    const plugin = pluginFactory();
    beginPrunePreview("preview-missing-publication-lease");

    act(() => {
      emitDeckyEvent("prune_complete", {
        success: true,
        partial: false,
        run_id: "run-missing-publication-lease",
        preview_id: "preview-missing-publication-lease",
        publication_required: true,
        removed_rom_ids: [7],
        affected_app_ids: [9001],
        results: [
          {
            group_id: "group-1",
            rom_ids: [7, 8],
            status: "repointed",
            committed_action: "repoint_shortcut",
            app_id: 9001,
            target_rom_id: 8,
            message: "Repointed.",
          },
        ],
      });
    });
    await flush();

    expect(waitForPruneRelease).not.toHaveBeenCalledWith("run-missing-publication-lease");
    expect(publishCommittedVersionSwitch).not.toHaveBeenCalled();
    expect(logError).toHaveBeenCalledWith(
      "Cleanup publication was skipped because its continuation lease was missing.",
    );
    plugin.onDismount();
  });
});

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

describe("index.tsx — download_progress cancelled eviction (#149 downloads-round)", () => {
  it("drops the entry from the store when a cancelled frame arrives", async () => {
    const plugin = pluginFactory();
    setDownloads([
      {
        rom_id: 42,
        rom_name: "Paused",
        platform_name: "N64",
        file_name: "game.z64",
        status: "paused",
        progress: 0.5,
        bytes_downloaded: 500,
        total_bytes: 1000,
        resumable: true,
      },
    ]);

    act(() => {
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", {
        rom_id: 42,
        rom_name: "Paused",
        platform_name: "N64",
        file_name: "game.z64",
        status: "cancelled",
        progress: 0.5,
        bytes_downloaded: 500,
        total_bytes: 1000,
        resumable: true,
      });
    });

    // Explicit discard → no residue in the store (which MainPage's count + the
    // DownloadQueue view both read).
    expect(getDownloadState().some((d) => d.rom_id === 42)).toBe(false);
    plugin.onDismount();
  });

  it("updates in place (does not drop) for a non-cancelled frame", async () => {
    const plugin = pluginFactory();
    setDownloads([]);

    act(() => {
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", {
        rom_id: 7,
        rom_name: "Live",
        platform_name: "N64",
        file_name: "g.z64",
        status: "downloading",
        progress: 0.2,
        bytes_downloaded: 200,
        total_bytes: 1000,
        resumable: false,
      });
    });

    expect(getDownloadState().find((d) => d.rom_id === 7)?.status).toBe("downloading");
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

  it("chunk-paces a large stale removal (25 back-to-back, 50ms breather) and records the delta up front (#977)", async () => {
    const plugin = pluginFactory();
    await flush();
    removeShortcut.mockClear();
    resetSyncDelta();

    // 26 stale shortcuts = one full 25-item chunk + a remainder, so exactly one
    // 50ms breather must fall between the two chunks.
    const remove = Array.from({ length: 26 }, (_, i) => ({ rom_id: i + 1, app_id: 1000 + i }));

    vi.useFakeTimers();
    try {
      act(() => {
        emitDeckyEvent<[SyncStaleData]>("sync_stale", { remove });
      });
      await act(async () => {
        for (let i = 0; i < 40; i++) await Promise.resolve();
      });
      // First 25-item chunk removed back-to-back; the 26th is gated behind the breather.
      expect(removeShortcut).toHaveBeenCalledTimes(25);
      // The removed-delta for ALL 26 is recorded up front, so a sync_complete that
      // interleaves during the paced breather reads the true count, not a partial one.
      expect(getSyncDelta().removed).toBe(26);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      expect(removeShortcut).toHaveBeenCalledTimes(26);
    } finally {
      vi.useRealTimers();
    }
    plugin.onDismount();
  });

  it("holds its own event lease through a paced tail when sync_complete never arrives", async () => {
    const plugin = pluginFactory();
    await flush();
    vi.mocked(releasePruneConflictLease).mockClear();
    const remove = Array.from({ length: 26 }, (_, i) => ({ rom_id: i + 1, app_id: 2000 + i }));

    vi.useFakeTimers();
    try {
      act(() => {
        emitDeckyEvent<[SyncStaleData]>("sync_stale", { remove, prune_lease_token: "standalone-stale-lease" });
      });
      await act(async () => {
        for (let i = 0; i < 40; i++) await Promise.resolve();
      });
      expect(removeShortcut).toHaveBeenCalledTimes(25);
      expect(releasePruneConflictLease).not.toHaveBeenCalledWith("standalone-stale-lease");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      expect(removeShortcut).toHaveBeenCalledTimes(26);
      await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("standalone-stale-lease"));
    } finally {
      vi.useRealTimers();
      plugin.onDismount();
    }
  });

  it("catches a rejecting stale tail so it never wedges the later sync_complete continuation", async () => {
    const plugin = pluginFactory();
    await flush();
    vi.mocked(releasePruneConflictLease).mockClear();
    removeShortcut.mockClear();
    logError.mockClear();
    createOrUpdateCollections.mockClear();

    try {
      // A tombstoned plugin generation refuses the tail's continuation outright,
      // so the stored promise REJECTS — the shape L20 is about.
      await releaseAllPruneLeases();
      act(() => {
        emitDeckyEvent<[SyncStaleData]>("sync_stale", {
          remove: [{ rom_id: 1, app_id: 3000 }],
          prune_lease_token: "rejecting-stale-lease",
        });
      });
      await flush();

      // Post-catch state: the failure is surfaced where the tail is STORED, so the
      // stored promise is settled (nothing waits on an unhandled rejection), no
      // Steam write happened, and the refused token is released anyway.
      expect(logError).toHaveBeenCalledWith(expect.stringContaining("stale shortcut removal failed"));
      expect(removeShortcut).not.toHaveBeenCalled();
      await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("rejecting-stale-lease"));

      mountPruneLeasePlugin();
      // The completion continuation awaits that same tail and still runs its
      // sibling reconciles to the end instead of being aborted by it.
      act(() => {
        emitDeckyEvent<[SyncCompleteAfterStaleFailure]>("sync_complete", {
          platform_app_ids: { gba: [3000] },
          total_games: 1,
          prune_lease_token: "completion-after-failed-tail",
        });
      });
      await flush();

      expect(createOrUpdateCollections).toHaveBeenCalled();
      await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("completion-after-failed-tail"));
    } finally {
      mountPruneLeasePlugin();
      plugin.onDismount();
    }
  });
});

type SyncCompleteAfterStaleFailure = {
  platform_app_ids: Record<string, number[]>;
  total_games: number;
  prune_lease_token?: string;
};

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

describe("index.tsx — startup launch-options reconcile (#1043)", () => {
  const relaunchOptions = (items: { app_id: number; launch_options: string }[]) => ({
    success: true as const,
    items,
    prune_lease_token: items.length > 0 ? "installed-lease" : null,
  });

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    logError.mockClear();
    // Make the init detach reach initDone=true so the reconcile fires.
    vi.mocked(getAllPlaytime).mockResolvedValue({ playtime: {} });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({});
    // Settle the sibling reset-notice detach so its .catch doesn't muddy logError.
    vi.mocked(getSettingsResetNotice).mockResolvedValue({ pending: false, backed_up_to: null });
    vi.mocked(getInstalledRelaunchOptions).mockReset();
  });

  it("confirm-sets launch options for each reconciled item after init", async () => {
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue(
      relaunchOptions([
        { app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/a.bin"' },
        { app_id: 200, launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/b.bin"' },
      ]),
    );
    const plugin = pluginFactory();
    await flush();

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, 'flatpak run net.retrodeck.retrodeck "/roms/a.bin"');
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(200, 'flatpak run net.retrodeck.retrodeck "/roms/b.bin"');
    expect(logError).not.toHaveBeenCalledWith(expect.stringContaining("startup_reconcile"));
    plugin.onDismount();
  });

  it("never confirm-sets when there is nothing installed to reconcile", async () => {
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue(relaunchOptions([]));
    const plugin = pluginFactory();
    await flush();

    expect(getInstalledRelaunchOptions).toHaveBeenCalled();
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    plugin.onDismount();
  });

  it("surfaces a startup_reconcile-prefixed logError when a confirm returns false", async () => {
    setLaunchOptionsConfirmed.mockResolvedValue(false);
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue(
      relaunchOptions([{ app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/a.bin"' }]),
    );
    const plugin = pluginFactory();
    await flush();

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, 'flatpak run net.retrodeck.retrodeck "/roms/a.bin"');
    expect(logError).toHaveBeenCalledWith("startup_reconcile: failed to confirm launch options for appId 100");
    plugin.onDismount();
  });

  it("surfaces a startup_reconcile-prefixed logError when the pull callable rejects", async () => {
    vi.mocked(getInstalledRelaunchOptions).mockRejectedValue(new Error("pull failed"));
    const plugin = pluginFactory();
    await flush();

    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(logError).toHaveBeenCalledWith(
      expect.stringContaining("startup_reconcile: failed to reconcile launch options"),
    );
    plugin.onDismount();
  });
});

describe("index.tsx — sync_complete launch-options reconcile (#1151)", () => {
  type SyncCompletePayload = {
    platform_app_ids: Record<string, number[]>;
    romm_collection_app_ids?: Record<string, number[]>;
    total_games: number;
    cancelled?: boolean;
    prune_lease_token?: string;
  };

  const relaunchOptions = (items: { app_id: number; launch_options: string }[]) => ({
    success: true as const,
    items,
    prune_lease_token: items.length > 0 ? "installed-lease" : null,
  });

  beforeEach(() => {
    setLaunchOptionsConfirmed.mockClear();
    setLaunchOptionsConfirmed.mockResolvedValue(true);
    logError.mockClear();
    vi.mocked(getAllPlaytime).mockResolvedValue({ playtime: {} });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({});
    vi.mocked(getSettingsResetNotice).mockResolvedValue({ pending: false, backed_up_to: null });
    // The startup reconcile fires on factory init; default it to an empty set
    // so each test isolates the sync_complete-triggered reconcile below.
    vi.mocked(getInstalledRelaunchOptions).mockReset();
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue(relaunchOptions([]));
  });

  it("re-confirms launch options for every installed+bound ROM after a sync", async () => {
    const plugin = pluginFactory();
    await flush(); // settle the startup reconcile (empty set)
    setLaunchOptionsConfirmed.mockClear();
    vi.mocked(getInstalledRelaunchOptions).mockClear();
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue(
      relaunchOptions([{ app_id: 100, launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/a.bin"' }]),
    );

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 0,
        cancelled: false,
      });
    });
    await flush();

    expect(getInstalledRelaunchOptions).toHaveBeenCalled();
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, 'flatpak run net.retrodeck.retrodeck "/roms/a.bin"');
    expect(logError).not.toHaveBeenCalledWith(expect.stringContaining("sync_reconcile"));
    plugin.onDismount();
  });

  it("reconciles even when the sync was cancelled", async () => {
    const plugin = pluginFactory();
    await flush();
    setLaunchOptionsConfirmed.mockClear();
    vi.mocked(getInstalledRelaunchOptions).mockClear();
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue(
      relaunchOptions([{ app_id: 200, launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/b.bin"' }]),
    );

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 0,
        cancelled: true,
      });
    });
    await flush();

    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(200, 'flatpak run net.retrodeck.retrodeck "/roms/b.bin"');
    plugin.onDismount();
  });

  it("surfaces a sync_reconcile-prefixed logError when the pull callable rejects", async () => {
    const plugin = pluginFactory();
    await flush();
    setLaunchOptionsConfirmed.mockClear();
    logError.mockClear();
    vi.mocked(getInstalledRelaunchOptions).mockReset();
    vi.mocked(getInstalledRelaunchOptions).mockRejectedValue(new Error("pull failed"));

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 0,
        cancelled: false,
      });
    });
    await flush();

    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(logError).toHaveBeenCalledWith(
      expect.stringContaining("sync_reconcile: failed to reconcile launch options"),
    );
    plugin.onDismount();
  });

  it("holds the sync event lease until collection and sibling Steam continuations settle", async () => {
    let finishCollections: (() => void) | undefined;
    createOrUpdateCollections.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishCollections = resolve;
        }),
    );
    const plugin = pluginFactory();
    await flush();
    vi.mocked(releasePruneConflictLease).mockClear();

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: { SNES: [100] },
        total_games: 1,
        prune_lease_token: "sync-complete-lease",
      });
    });
    await vi.waitFor(() =>
      expect(createOrUpdateCollections).toHaveBeenCalledWith({ SNES: [100] }, undefined, expect.any(AbortSignal)),
    );
    expect(releasePruneConflictLease).not.toHaveBeenCalledWith("sync-complete-lease");

    finishCollections?.();
    await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("sync-complete-lease"));
    plugin.onDismount();
  });

  it("plugin dismount defers sync lease release until a started collection save settles", async () => {
    let finishCollections: (() => void) | undefined;
    createOrUpdateCollections.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          finishCollections = resolve;
        }),
    );
    const plugin = pluginFactory();
    await flush();
    vi.mocked(releasePruneConflictLease).mockClear();
    createOrUpdateRomMCollections.mockClear();

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: { SNES: [100] },
        romm_collection_app_ids: { Favorites: [100] },
        total_games: 1,
        prune_lease_token: "dismount-sync-lease",
      });
    });
    await vi.waitFor(() => expect(createOrUpdateCollections).toHaveBeenCalled());

    plugin.onDismount();
    await Promise.resolve();
    expect(releasePruneConflictLease).not.toHaveBeenCalledWith("dismount-sync-lease");

    finishCollections?.();
    await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("dismount-sync-lease"));
    expect(createOrUpdateRomMCollections).not.toHaveBeenCalled();
  });

  it("plugin dismount releases an installed-reconcile token that arrives afterward", async () => {
    const plugin = pluginFactory();
    await flush();
    setLaunchOptionsConfirmed.mockClear();
    vi.mocked(getInstalledRelaunchOptions).mockReset();
    let resolveReconcile!: (value: ReturnType<typeof relaunchOptions>) => void;
    vi.mocked(getInstalledRelaunchOptions).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveReconcile = resolve;
        }),
    );

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 0,
        prune_lease_token: "outer-sync-lease",
      });
    });
    await waitFor(() => expect(getInstalledRelaunchOptions).toHaveBeenCalled());
    plugin.onDismount();
    resolveReconcile({
      success: true,
      items: [{ app_id: 100, launch_options: "cmd" }],
      prune_lease_token: "late-installed-lease",
    });

    await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("late-installed-lease"));
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
  });

  it("holds the sync event lease until the paced sync_stale tail settles", async () => {
    const plugin = pluginFactory();
    await flush();
    vi.mocked(releasePruneConflictLease).mockClear();
    removeShortcut.mockClear();
    const remove = Array.from({ length: 26 }, (_, index) => ({ rom_id: index + 1, app_id: 1000 + index }));

    vi.useFakeTimers();
    try {
      act(() => {
        emitDeckyEvent<[SyncStaleData]>("sync_stale", { remove, prune_lease_token: "stale-event-lease" });
      });
      await act(async () => {
        for (let index = 0; index < 40; index++) await Promise.resolve();
      });
      expect(removeShortcut).toHaveBeenCalledTimes(25);
      expect(releasePruneConflictLease).not.toHaveBeenCalledWith("stale-event-lease");

      act(() => {
        emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
          platform_app_ids: {},
          total_games: 0,
          prune_lease_token: "stale-tail-lease",
        });
      });
      await act(async () => {
        for (let index = 0; index < 20; index++) await Promise.resolve();
      });
      expect(releasePruneConflictLease).not.toHaveBeenCalledWith("stale-tail-lease");

      await act(async () => {
        await vi.advanceTimersByTimeAsync(50);
      });
      expect(removeShortcut).toHaveBeenCalledTimes(26);
      await vi.waitFor(() => expect(releasePruneConflictLease).toHaveBeenCalledWith("stale-tail-lease"));
      expect(releasePruneConflictLease).toHaveBeenCalledWith("stale-event-lease");
      expect(
        vi.mocked(releasePruneConflictLease).mock.calls.filter(([token]) => token === "stale-event-lease"),
      ).toHaveLength(1);
    } finally {
      vi.useRealTimers();
      plugin.onDismount();
    }
  });
});

describe("index.tsx — sync_complete registers RomM appIds (#1205)", () => {
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
    vi.mocked(registerRomMAppId).mockClear();
    logError.mockClear();
    vi.mocked(getAllPlaytime).mockResolvedValue({ playtime: {} });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({});
    vi.mocked(getSettingsResetNotice).mockResolvedValue({ pending: false, backed_up_to: null });
    vi.mocked(getInstalledRelaunchOptions).mockReset();
    vi.mocked(getInstalledRelaunchOptions).mockResolvedValue({
      success: true,
      items: [],
      prune_lease_token: null,
    });
    // Empty collectionStore so the detached stale-cleanup is a no-op here.
    vi.stubGlobal("collectionStore", { userCollections: [] });
  });

  it("registers every platform and RomM-collection appId from the payload", async () => {
    const plugin = pluginFactory();
    await flush(); // settle startup detaches (they call registerRomMAppId with the empty appIdMap)
    vi.mocked(registerRomMAppId).mockClear();

    emitSyncComplete({
      platform_app_ids: { "Nintendo 64": [100, 101], PSX: [200] },
      romm_collection_app_ids: { "[Faves]": [300], "[RPGs]": [200, 400] },
      total_games: 5,
    });
    await flush();

    // Every appId across BOTH maps is registered (200 spans a platform and a
    // collection — idempotent, still fine).
    for (const appId of [100, 101, 200, 300, 400]) {
      expect(registerRomMAppId).toHaveBeenCalledWith(appId);
    }
    plugin.onDismount();
  });

  it("registers RomM-collection appIds even when platform_app_ids is empty (collection-only sync)", async () => {
    // The #1205 core repro: a collection-only sync never populates
    // platform_app_ids, so its new shortcuts land only in romm_collection_app_ids.
    // The old platform-only loop left them unregistered until a Steam restart.
    const plugin = pluginFactory();
    await flush();
    vi.mocked(registerRomMAppId).mockClear();

    emitSyncComplete({
      platform_app_ids: {},
      romm_collection_app_ids: { "[Faves]": [777, 888] },
      total_games: 2,
    });
    await flush();

    expect(registerRomMAppId).toHaveBeenCalledWith(777);
    expect(registerRomMAppId).toHaveBeenCalledWith(888);
    plugin.onDismount();
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

    expect(clearPlatformCollection).toHaveBeenCalledWith("Super Nintendo", expect.any(AbortSignal));
    expect(faves.Delete).toHaveBeenCalledTimes(1);
    plugin.onDismount();
  });

  it("keeps a case-variant ACTIVE RomM collection (does not delete it) (#1569)", async () => {
    const { snes, faves } = seedCollections();
    const plugin = pluginFactory();

    // The live collection is "[Faves]"; the active map keys it as "faves" (the
    // reporter's folded-first-seen casing). Case-insensitive identity → it is
    // ACTIVE and must survive. SNES has no active platform → still stale.
    emitSyncComplete({
      platform_app_ids: { "Nintendo 64": [1] },
      romm_collection_app_ids: { faves: [1] },
      total_games: 1,
    });
    await flush();

    expect(faves.Delete).not.toHaveBeenCalled();
    // Non-vacuous: the stale SNES platform IS still cleaned, so cleanup ran.
    expect(clearPlatformCollection).toHaveBeenCalledWith("Super Nintendo", expect.any(AbortSignal));
    expect(snes.Delete).not.toHaveBeenCalled(); // platform delete routes via clearPlatformCollection
    plugin.onDismount();
  });

  it("keeps a case-variant ACTIVE platform collection (does not clear it) (#1569)", async () => {
    const { faves } = seedCollections();
    const plugin = pluginFactory();

    // Live "RomM: Super Nintendo (steamdeck)"; active map keys it "super nintendo".
    // Case-insensitive → ACTIVE, must not be cleared. [Faves] has no active RomM
    // entry → stale and removed (non-vacuous: cleanup ran).
    emitSyncComplete({
      platform_app_ids: { "super nintendo": [1] },
      total_games: 1,
    });
    await flush();

    expect(clearPlatformCollection).not.toHaveBeenCalled();
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
    expect(createOrUpdateCollections).toHaveBeenCalledWith({ "Nintendo 64": [1] }, undefined, expect.any(AbortSignal));
    plugin.onDismount();
  });
});

describe("index.tsx — sync_complete re-applies overview metadata (#1207)", () => {
  type SyncCompletePayload = {
    platform_app_ids: Record<string, number[]>;
    romm_collection_app_ids?: Record<string, number[]>;
    total_games: number;
    cancelled?: boolean;
  };

  function meta(summary: string): RomMetadata {
    return {
      summary,
      genres: [],
      companies: [],
      first_release_date: null,
      average_rating: null,
      game_modes: [],
      player_count: "",
      cached_at: 0,
    };
  }

  beforeEach(() => {
    vi.mocked(registerMetadataPatches).mockClear();
    vi.mocked(applyAllMetadata).mockClear();
    vi.mocked(applyAllMetadata).mockResolvedValue(undefined);
    vi.mocked(applyAllPlaytime).mockResolvedValue(undefined);
    vi.mocked(getAllPlaytime).mockResolvedValue({ playtime: {} });
    // Init's own metadata fetch resolves empty; each test sets distinct fresh
    // data AFTER init so the sync_complete re-fetch is provably re-fetched.
    vi.mocked(getMetadataCachePage).mockResolvedValue({ items: {}, total: 0 });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({});
  });

  it("re-fetches the paged cache + map and re-applies on a normal completion", async () => {
    const plugin = pluginFactory();
    await flush(); // init done — registerMetadataPatches called once with the empty init cache
    vi.mocked(registerMetadataPatches).mockClear();
    vi.mocked(applyAllMetadata).mockClear();

    // The re-fetch after sync must see FRESH data, not the init-time empty cache.
    vi.mocked(getMetadataCachePage).mockResolvedValue({ items: { "100": meta("Fresh") }, total: 1 });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({ "100": 55 });

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 1 });
    });
    await flush();

    // registerMetadataPatches received the RE-FETCHED cache + map (distinct page content).
    expect(registerMetadataPatches).toHaveBeenCalledTimes(1);
    const [cacheArg, mapArg] = vi.mocked(registerMetadataPatches).mock.calls[0]!;
    expect((cacheArg as Record<string, RomMetadata>)["100"]!.summary).toBe("Fresh");
    expect(mapArg).toEqual({ "100": 55 });
    // …and the readiness-gated overview pass re-ran.
    expect(applyAllMetadata).toHaveBeenCalledTimes(1);
    plugin.onDismount();
  });

  it("re-applies overview metadata on a CANCELLED sync too (partial units are still fresh)", async () => {
    const plugin = pluginFactory();
    await flush();
    vi.mocked(registerMetadataPatches).mockClear();
    vi.mocked(applyAllMetadata).mockClear();
    vi.mocked(getMetadataCachePage).mockResolvedValue({ items: { "7": meta("Partial") }, total: 1 });
    vi.mocked(getAppIdRomIdMap).mockResolvedValue({ "7": 9 });

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 3, cancelled: true });
    });
    await flush();

    expect(registerMetadataPatches).toHaveBeenCalledTimes(1);
    expect(applyAllMetadata).toHaveBeenCalledTimes(1);
    plugin.onDismount();
  });

  it("logs and leaves the other blocks intact when the metadata re-fetch fails", async () => {
    const plugin = pluginFactory();
    await flush();
    vi.mocked(applyAllMetadata).mockClear();
    vi.mocked(applyAllPlaytime).mockClear();
    logError.mockClear();
    // The paged re-fetch throws; the detached block's own catch logs it.
    vi.mocked(getMetadataCachePage).mockRejectedValue(new Error("boom"));

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 1 });
    });
    await flush();

    expect(logError).toHaveBeenCalledWith(expect.stringContaining("Failed to re-apply metadata after sync"));
    expect(applyAllMetadata).not.toHaveBeenCalled();
    // Non-vacuous: the playtime re-apply is a separate detached block and still ran.
    expect(applyAllPlaytime).toHaveBeenCalled();
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
    interrupted?: boolean;
    interrupt_reason?: string;
    restart_recommended?: boolean;
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

  it("sync_plan resets the per-run cancel flag (#1198)", async () => {
    const plugin = pluginFactory();
    vi.mocked(resetSyncCancel).mockClear();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-xyz", units: [], total_units: 1, total_roms: 1 });
    });

    // The listener clears the per-run cancel flag once per run, before any unit
    // — reliable even on a skip-only run where no per-unit handler fires. Run
    // identity for a Cancel click now comes from the sync_progress store (#1202).
    expect(vi.mocked(resetSyncCancel)).toHaveBeenCalled();
    plugin.onDismount();
  });

  it("drives terminal teardown from sync_complete even if no stage:done frame follows", async () => {
    // The on-device hang: the apply left the store on the optimistic "applying"
    // frame, sync_complete arrived, but the separate backend stage:"done"
    // sync_progress frame never did — so the QAM stayed stuck on "Applying".
    // sync_complete alone must flip the store to a terminal stage.
    const plugin = pluginFactory();
    setSyncProgress({ running: true, stage: "applying", message: "Applying changes..." });

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 42 });
    });
    await flush();

    expect(getSyncProgress().running).toBe(false);
    expect(getSyncProgress().stage).toBe("done");
    plugin.onDismount();
  });

  it("flips the store to a cancelled stage when sync_complete is cancelled", async () => {
    const plugin = pluginFactory();
    setSyncProgress({ running: true, stage: "applying", message: "Applying changes..." });

    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 5, cancelled: true });
    });
    await flush();

    expect(getSyncProgress().running).toBe(false);
    expect(getSyncProgress().stage).toBe("cancelled");
    plugin.onDismount();
  });

  it("reports 'X added, Y removed' when both are non-zero (ignores total_games)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 2, total_roms: 2 });
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
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 1, total_roms: 0 });
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
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 1, total_roms: 53 });
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
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 2, total_roms: 1 });
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
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 3, total_roms: 10 });
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
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 3, total_roms: 10 });
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

  it("on a heartbeat-timeout interrupt with partial work → 'Sync interrupted — … so far.' (#1384)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 3, total_roms: 10 });
    });
    recordSyncCreated(100);
    recordSyncCreated(200);
    recordSyncCreated(300);
    // An interrupted run rides the cancelled finalize — the backend sets BOTH
    // flags. The additive `interrupted` must win the wording: the run died
    // externally (frontend crash/reload), the user never pressed Cancel.
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 53,
        cancelled: true,
        interrupted: true,
      });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync interrupted — 3 added so far.");
    plugin.onDismount();
  });

  it("on a heartbeat-timeout interrupt before any work → 'Sync interrupted.' (no delta, #1384)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 3, total_roms: 10 });
    });
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 53,
        cancelled: true,
        interrupted: true,
      });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync interrupted.");
    plugin.onDismount();
  });

  it("on a session-budget pause → shows the pause guidance verbatim with the delta (#1383)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 3, total_roms: 10 });
    });
    recordSyncCreated(100);
    recordSyncCreated(200);
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 53,
        cancelled: true,
        interrupt_reason:
          "Sync paused: Steam's memory is nearly full. Restart Steam when convenient, then Resume Sync.",
      });
    });
    await flush();

    // The distinct reason wins over the generic "Sync cancelled — …" wording, and
    // the reason's trailing period is stripped so the parenthetical reads cleanly.
    expect(lastToastBody()).toBe(
      "Sync paused: Steam's memory is nearly full. Restart Steam when convenient, then Resume Sync (2 added so far).",
    );
    // The pause toast gets a longer duration so the guidance isn't truncated away
    // before it is read (#1383).
    const toastCalls = vi.mocked(toaster.toast).mock.calls;
    const lastToast = toastCalls[toastCalls.length - 1]![0] as { duration?: number };
    expect(lastToast.duration).toBe(15000);
    plugin.onDismount();
  });

  it("a non-pause completion toast carries no custom duration (default lifetime)", async () => {
    const plugin = pluginFactory();
    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 1, total_roms: 1 });
    });
    recordSyncCreated(100);
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", { platform_app_ids: {}, total_games: 1 });
    });
    await flush();
    const toastCalls = vi.mocked(toaster.toast).mock.calls;
    const lastToast = toastCalls[toastCalls.length - 1]![0] as { duration?: number };
    expect(lastToast.duration).toBeUndefined();
    plugin.onDismount();
  });

  it("on a session-budget pause with no delta → shows just the reason (#1383)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 3, total_roms: 10 });
    });
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 53,
        cancelled: true,
        interrupt_reason:
          "Sync paused: Steam's memory is nearly full. Restart Steam when convenient, then Resume Sync.",
      });
    });
    await flush();

    expect(lastToastBody()).toBe(
      "Sync paused: Steam's memory is nearly full. Restart Steam when convenient, then Resume Sync.",
    );
    plugin.onDismount();
  });

  it("on a clean run with restart_recommended → appends the restart nudge (#1383)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-1", units: [], total_units: 1, total_roms: 1 });
    });
    recordSyncCreated(100);
    act(() => {
      emitDeckyEvent<[SyncCompletePayload]>("sync_complete", {
        platform_app_ids: {},
        total_games: 1,
        restart_recommended: true,
      });
    });
    await flush();

    expect(lastToastBody()).toBe("Sync complete — 1 added. Steam restart recommended before further large operations.");
    plugin.onDismount();
  });
});

describe("index.tsx — sync_plan seeds the applying-phase ETA (always-on estimate)", () => {
  it("writes the composition-priced seed (unbound rows as creates) into the sync progress store", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [{ type: "platform", id: 1, name: "N64", slug: "n64", rom_count: 120, bound_count: 0 }],
        total_units: 3,
        total_roms: 120,
      });
    });

    // Nothing bound yet, so every planned item is a create — the fresh-import
    // shape, priced exactly as the preview would price it.
    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(120, 0));
    plugin.onDismount();
  });

  it("prices already-bound rows as cheap updates, not as fresh creates (#1511)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [
          // A fully-mirrored platform re-syncing: every row already carries a
          // shortcut, so the run is all updates. Pricing it as creates is the
          // over-read #1511 was opened for; the seed must price it as updates.
          {
            type: "platform",
            id: 1,
            name: "N64",
            slug: "n64",
            rom_count: 1000,
            collapsed_count: 1000,
            bound_count: 1000,
          },
        ],
        total_units: 1,
        total_roms: 1000,
        total_estimated_items: 1000,
      });
    });

    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(0, 1000));
    resetEta();
    plugin.onDismount();
  });

  it("prices a Force Full Sync's sibling duplicates as nothing, not as phantom creates (#1517)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [
          // A Force Full Sync clears the completion stamps, so collapsed_count is
          // absent and the unit weighs its pre-collapse rom_count — sibling
          // duplicates included. Only new_shortcut_count knows those duplicates
          // are not new shortcuts; the bound-row subtraction would price 400 of
          // them as creates, each with a cover download it never performs.
          {
            type: "platform",
            id: 1,
            name: "N64",
            slug: "n64",
            rom_count: 1000,
            bound_count: 600,
            new_shortcut_count: 0,
          },
        ],
        total_units: 1,
        total_roms: 1000,
      });
    });

    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(0, 600));
    resetEta();
    plugin.onDismount();
  });

  it("preserves etaSeconds across a subsequent backend sync_progress frame", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [{ type: "platform", id: 1, name: "N64", slug: "n64", rom_count: 200, bound_count: 0 }],
        total_units: 1,
        total_roms: 200,
      });
    });
    // A backend frame carries no etaSeconds — the listener must not wipe it.
    act(() => {
      emitDeckyEvent<[SyncProgress]>("sync_progress", {
        running: true,
        stage: "applying",
        step: 1,
        totalSteps: 1,
        message: "N64: 1/200",
      });
    });

    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(200, 0));
    expect(getSyncProgress().stage).toBe("applying");
    plugin.onDismount();
  });

  it("does NOT clobber an etaSeconds already seeded by the preview path (handleApply)", async () => {
    const plugin = pluginFactory();

    // handleApply full-replaces the store with a tighter delta-based etaSeconds
    // before sync_plan arrives; the listener must leave that seed intact. Both
    // click paths full-replace at click time, so a present etaSeconds is always
    // this run's preview seed, never a stale prior-run value.
    const previewSeed = 321;
    setSyncProgress({ running: true, stage: "applying", message: "Applying changes...", etaSeconds: previewSeed });
    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", { run_id: "run-eta", units: [], total_units: 3, total_roms: 5400 });
    });

    // The crude estimateApplySeconds(5400, 0) bound must NOT overwrite the preview seed.
    expect(getSyncProgress().etaSeconds).toBe(previewSeed);
    plugin.onDismount();
  });

  it("still seeds the total_roms bound when no preview seed is present (skip-preview path)", async () => {
    const plugin = pluginFactory();

    // Skip-preview never sets an etaSeconds — the store has none at sync_plan
    // time, so the listener still supplies the upper bound. Regression guard for
    // the etaSeconds-undefined gate.
    expect(getSyncProgress().etaSeconds).toBeUndefined();
    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [{ type: "platform", id: 1, name: "N64", slug: "n64", rom_count: 80, bound_count: 0 }],
        total_units: 2,
        total_roms: 80,
      });
    });

    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(80, 0));
    plugin.onDismount();
  });

  it("excludes predicted-skip units from the seed (#1382 skip-aware)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [
          {
            type: "platform",
            id: 1,
            name: "N64",
            slug: "n64",
            rom_count: 115,
            collapsed_count: 115,
            bound_count: 115,
            predicted_skip: true,
          },
          { type: "platform", id: 2, name: "GBA", slug: "gba", rom_count: 5, bound_count: 0 },
        ],
        total_units: 3,
        total_roms: 120,
        total_estimated_items: 5,
      });
    });

    // An incremental re-sync prices only the predicted work, not the library.
    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(5, 0));
    resetEta();
    plugin.onDismount();
  });

  it("seeds the live estimator with skip-aware unit weights (predicted_skip → 0, collapsed over raw)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [
          // Predicted skip: weight 0 even though counts are known.
          {
            type: "platform",
            id: 1,
            name: "N64",
            slug: "n64",
            rom_count: 100,
            predicted_skip: true,
            collapsed_count: 60,
          },
          // Known collapsed count wins over the raw rom_count.
          {
            type: "platform",
            id: 2,
            name: "GBA",
            slug: "gba",
            rom_count: 100,
            predicted_skip: false,
            collapsed_count: 40,
          },
          // Never synced: raw rom_count fallback.
          { type: "platform", id: 3, name: "SNES", slug: "snes", rom_count: 30, predicted_skip: false },
        ],
        total_units: 3,
        total_roms: 230,
        total_estimated_items: 70,
      });
    });

    // Observable through the weighted coarse fraction over the seeded weights
    // [0, 40, 30]: units 1+2 done (0 + 40) plus half of SNES (15) → 55/70 of
    // the weight. The leading predicted-skip unit weighs 0, so it claims an
    // equal 1/3 index slice as its floor and the weighted share fills the band
    // above it (#1506): 1/3 + (2/3)·55/70.
    expect(weightedCoarseFraction(2, 0.5, 3)).toBeCloseTo(1 / 3 + (2 / 3) * (55 / 70), 10);
    resetEta();
    plugin.onDismount();
  });

  it("falls back to raw weights and total_roms when the estimate fields are absent (old backend)", async () => {
    const plugin = pluginFactory();

    act(() => {
      emitDeckyEvent<[SyncPlanData]>("sync_plan", {
        run_id: "run-eta",
        units: [
          { type: "platform", id: 1, name: "N64", slug: "n64", rom_count: 60 },
          { type: "platform", id: 2, name: "GBA", slug: "gba", rom_count: 20 },
        ],
        total_units: 2,
        total_roms: 80,
      });
    });

    expect(getSyncProgress().etaSeconds).toBeCloseTo(estimateApplySeconds(80, 0));
    // Raw rom_count weights: unit 1 done (60) plus half of unit 2 (10) → 70/80.
    expect(weightedCoarseFraction(1, 0.5, 2)).toBeCloseTo(70 / 80, 10);
    resetEta();
    plugin.onDismount();
  });
});
