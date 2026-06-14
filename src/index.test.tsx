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
import { getSettingsResetNotice } from "./api/backend";
import { getSettingsResetState, setSettingsResetState } from "./utils/settingsResetStore";
import type { DownloadCompleteEvent, SyncStaleData } from "./types";

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
