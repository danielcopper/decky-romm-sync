/**
 * Reference test for the `src/test-utils/decky-api-mock.ts` event-bus harness.
 *
 * Exercises CustomPlayButton's per-button `download_failed` listener — the
 * exact case #654 was opened to unblock. The test:
 *
 * 1. Mocks `getCachedGameDetail` so the button reaches `state === "play"`.
 * 2. Dispatches a `download_failed` event matching the button's `romId` via
 *    `emitDeckyEvent` from the harness.
 * 3. Asserts the button transitioned back to "Download" — the visible
 *    side-effect of `handleButtonDownloadFailure(...) -> reset()`.
 *
 * Future component tests that consume `@decky/api` events should follow this
 * shape. The bus is reset between tests by `src/test-setup.ts`.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, waitFor, act } from "@testing-library/react";
import { toaster } from "@decky/api";
import { showContextMenu } from "@decky/ui";
import type { ReactElement } from "react";
import { CustomPlayButton } from "./CustomPlayButton";
import { emitDeckyEvent, deckyEventListenerCount } from "../test-utils/decky-api-mock";
import * as backend from "../api/backend";
import type { CachedGameDetail } from "../api/backend";
import type { DownloadFailedEvent, DownloadProgressEvent } from "../types";

// Stub the cached-detail store: synchronous Promise.resolve so the initial
// useEffect settles within a single waitFor tick. The default test-setup
// `callable()` stub would otherwise leave the button stuck in "loading".
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn<(appId: number) => Promise<CachedGameDetail>>(),
  invalidateCachedGameDetail: vi.fn(),
}));

// Connection state defaults to "connected" — the offline branch is exercised
// elsewhere; here we want the simplest path into the "play"/"download" render.
vi.mock("../utils/connectionState", () => ({
  getRommConnectionState: () => "connected",
}));

// Uninstall resets the shortcut's launch_options via setLaunchOptionsConfirmed
// (#1051) — mock it so the test asserts the call without touching SteamClient.
vi.mock("../utils/steamShortcuts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../utils/steamShortcuts")>()),
  setLaunchOptionsConfirmed: vi.fn().mockResolvedValue(true),
}));

// Keep the real launch gate + skip-set (so handlePlay's full funnel runs and the
// skip-FIRST C1 behavior is exercised end-to-end); only SPY on markLaunchSkipped
// so its call order vs RunGame is observable.
vi.mock("../utils/launchGate", async (importActual) => {
  const actual = await importActual<typeof import("../utils/launchGate")>();
  return { ...actual, markLaunchSkipped: vi.fn(actual.markLaunchSkipped) };
});

// Migration store is a real module defaulting to { pending: false }; mock it so
// the migration-block verdict test can flip `pending` true.
vi.mock("../utils/migrationStore", () => ({
  getMigrationState: vi.fn(() => ({ pending: false })),
}));

// Shared launch-gate modals — spy so the Play button's verdict switch is
// observable without rendering each modal (mirrors the watcher's test shape).
vi.mock("../components/OfflineDriftModal", () => ({
  showOfflineDriftModal: vi.fn(),
}));
vi.mock("../components/FallbackLaunchModal", () => ({
  showFallbackLaunchModal: vi.fn(),
}));
vi.mock("../components/SyncConflictModal", () => ({
  handleConflicts: vi.fn(),
}));

import { getCachedGameDetail } from "../utils/cachedGameDetailStore";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { markLaunchSkipped, consumeLaunchSkip } from "../utils/launchGate";
import { getMigrationState } from "../utils/migrationStore";
import { showOfflineDriftModal } from "../components/OfflineDriftModal";
import { showFallbackLaunchModal } from "../components/FallbackLaunchModal";
import { handleConflicts } from "../components/SyncConflictModal";
import type { SyncConflict } from "../types";

function mockCachedDetail(overrides: Partial<CachedGameDetail> = {}): void {
  vi.mocked(getCachedGameDetail).mockResolvedValue({
    found: true,
    rom_id: 42,
    rom_name: "Test ROM",
    installed: true,
    ...overrides,
  });
}

describe("CustomPlayButton — download_failed listener", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
  });

  it("registers a download_failed listener on mount", async () => {
    mockCachedDetail();
    expect(deckyEventListenerCount("download_failed")).toBe(0);

    render(<CustomPlayButton appId={100} />);

    await waitFor(() => {
      expect(deckyEventListenerCount("download_failed")).toBe(1);
    });
  });

  it("transitions back to Download when a matching download_failed event arrives", async () => {
    mockCachedDetail({ rom_id: 42, installed: true });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);

    // Initial state lands on "play" once getCachedGameDetail resolves.
    await findByText("Play");

    // Dispatch the Decky-loader event the listener subscribes to. The
    // listener calls setState — wrap in act() so the resulting render flushes.
    act(() => {
      const event: DownloadFailedEvent = {
        rom_id: 42,
        rom_name: "Test ROM",
        platform_name: "PSX",
        error_message: "disk full",
      };
      emitDeckyEvent<[DownloadFailedEvent]>("download_failed", event);
    });

    // Reset path: setState("download"), so the Download label appears and
    // the Play label is gone.
    await findByText("Download");
    expect(queryByText("Play")).toBeNull();
  });

  it("ignores download_failed for a different rom_id", async () => {
    mockCachedDetail({ rom_id: 42, installed: true });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    act(() => {
      emitDeckyEvent<[DownloadFailedEvent]>("download_failed", {
        rom_id: 999, // mismatched — listener no-ops
        rom_name: "Other",
        platform_name: "PSX",
        error_message: "boom",
      });
    });

    // Button stays in "play" state — Play label persists, Download absent.
    expect(await findByText("Play")).toBeInTheDocument();
    expect(queryByText("Download")).toBeNull();
  });

  it("removes the download_failed listener on unmount", async () => {
    mockCachedDetail();
    const { unmount } = render(<CustomPlayButton appId={100} />);

    await waitFor(() => {
      expect(deckyEventListenerCount("download_failed")).toBe(1);
    });

    unmount();
    expect(deckyEventListenerCount("download_failed")).toBe(0);
  });
});

describe("CustomPlayButton — download_progress cancelled listener (#1017)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
  });

  it("transitions out of its downloading state when a matching cancelled frame arrives", async () => {
    // The cancel terminal frame the backend now emits (#1017) — the button's
    // download_progress listener resets to "download" on status "cancelled",
    // exactly as it does for "failed".
    mockCachedDetail({ rom_id: 42, installed: true });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    act(() => {
      const event: DownloadProgressEvent = {
        rom_id: 42,
        rom_name: "Test ROM",
        platform_name: "PSX",
        file_name: "test.chd",
        status: "cancelled",
        progress: 0.3,
        bytes_downloaded: 300,
        total_bytes: 1000,
      };
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", event);
    });

    // Post-state: the Download label is shown and Play is gone — the visible
    // side-effect of setState("download") on the cancelled frame.
    await findByText("Download");
    expect(queryByText("Play")).toBeNull();
  });

  it("ignores a cancelled frame for a different rom_id", async () => {
    mockCachedDetail({ rom_id: 42, installed: true });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    act(() => {
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", {
        rom_id: 999, // mismatched — listener no-ops
        rom_name: "Other",
        platform_name: "PSX",
        file_name: "other.chd",
        status: "cancelled",
        progress: 0,
        bytes_downloaded: 0,
        total_bytes: 0,
      });
    });

    // Button stays in "play" — the cancelled frame for another ROM is ignored.
    expect(await findByText("Play")).toBeInTheDocument();
    expect(queryByText("Download")).toBeNull();
  });
});

describe("CustomPlayButton — cancel X on active download (#1049)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    // startDownload resolves success so handleDownload leaves actionPending
    // true (it only resets actionPending on !success). A subsequent
    // download_progress "downloading" frame then sets dlProgress, making
    // `downloading` truthy and rendering the cancel X.
    vi.mocked(backend.startDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.cancelDownload).mockResolvedValue({ success: true, message: "" });
  });

  // Drive the button into its active-download render: cache says not installed
  // (→ "download" state), click Download (→ actionPending), then a matching
  // "downloading" progress frame sets dlProgress (→ downloading truthy).
  async function renderDownloading(romId = 42) {
    mockCachedDetail({ rom_id: romId, installed: false });
    const utils = render(<CustomPlayButton appId={100} />);
    const downloadBtn = await utils.findByText("Download");

    await act(async () => {
      downloadBtn.click();
      // Drain handleDownload (startDownload resolve → actionPending stays true).
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      const event: DownloadProgressEvent = {
        rom_id: romId,
        rom_name: "Test ROM",
        platform_name: "PSX",
        file_name: "test.chd",
        status: "downloading",
        progress: 0.3,
        bytes_downloaded: 300,
        total_bytes: 1000,
      };
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", event);
    });

    return utils;
  }

  it("renders the cancel X while a download is actively running", async () => {
    const { findByLabelText } = await renderDownloading(42);
    // The icon-only cancel button is identified by its aria-label/title.
    expect(await findByLabelText("Cancel download")).toBeInTheDocument();
  });

  it("does NOT render the cancel X in the idle Download state", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const { findByText, queryByLabelText } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");
    // No download in flight → no cancel control.
    expect(queryByLabelText("Cancel download")).toBeNull();
  });

  it("clicking the cancel X calls cancelDownload with the rom_id", async () => {
    const { findByLabelText } = await renderDownloading(42);
    const cancelX = await findByLabelText("Cancel download");

    await act(async () => {
      cancelX.click();
      // Let the detached cancelDownload().catch chain settle.
      await Promise.resolve();
    });

    // Non-vacuous: assert the exact rom_id was passed.
    expect(backend.cancelDownload).toHaveBeenCalledWith(42);
  });

  it("swallows a cancelDownload rejection without crashing the button", async () => {
    vi.mocked(backend.cancelDownload).mockRejectedValue(new Error("nope"));
    const { findByLabelText } = await renderDownloading(42);
    const cancelX = await findByLabelText("Cancel download");

    await act(async () => {
      cancelX.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Post-catch state: the X is still rendered (button did not crash); the
    // backend cancellation frame, not this catch, is what tears the row down.
    expect(backend.cancelDownload).toHaveBeenCalledWith(42);
    expect(await findByLabelText("Cancel download")).toBeInTheDocument();
  });
});

describe("CustomPlayButton — pause/resume on active download (#1124)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(showContextMenu).mockReset();
    vi.mocked(backend.startDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.cancelDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.pauseDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.resumeDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.getDownloadQueue).mockResolvedValue({ downloads: [] });
  });

  // Drive the button into an active-download render. `resumable` and `status`
  // come straight off the emitted progress frame, so a single frame puts the
  // button into the downloading+resumable, downloading+not-resumable, or paused
  // shape under test.
  async function renderActive(
    romId: number,
    frame: { status: string; resumable?: boolean },
  ): Promise<ReturnType<typeof render>> {
    mockCachedDetail({ rom_id: romId, installed: false });
    const utils = render(<CustomPlayButton appId={100} />);
    const downloadBtn = await utils.findByText("Download");

    await act(async () => {
      downloadBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      const event: DownloadProgressEvent = {
        rom_id: romId,
        rom_name: "Test ROM",
        platform_name: "PSX",
        file_name: "test.chd",
        status: frame.status,
        progress: 0.3,
        bytes_downloaded: 300,
        total_bytes: 1000,
        ...(frame.resumable === undefined ? {} : { resumable: frame.resumable }),
      };
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", event);
    });

    return utils;
  }

  // Open the download-actions dropdown and pull the <Menu> element off the
  // showContextMenu spy, then render it so its MenuItem buttons are clickable.
  function openMenu(button: HTMLElement): ReturnType<typeof render> {
    act(() => {
      button.click();
    });
    expect(showContextMenu).toHaveBeenCalled();
    const calls = vi.mocked(showContextMenu).mock.calls;
    const menu = calls[calls.length - 1]![0] as ReactElement;
    return render(menu);
  }

  it("downloading + resumable renders the actions dropdown (not a bare cancel X) and Pause calls pauseDownload", async () => {
    const { findByLabelText } = await renderActive(42, { status: "downloading", resumable: true });
    const dropdownBtn = await findByLabelText("Download actions");

    const { findByText } = openMenu(dropdownBtn);
    const pauseItem = await findByText("Pause");

    await act(async () => {
      pauseItem.click();
      await Promise.resolve();
    });

    // Non-vacuous: the exact rom_id was paused.
    expect(backend.pauseDownload).toHaveBeenCalledWith(42);
  });

  it("downloading + NOT resumable renders the bare cancel X and no actions dropdown", async () => {
    const { findByLabelText, queryByLabelText } = await renderActive(42, {
      status: "downloading",
      resumable: false,
    });
    expect(await findByLabelText("Cancel download")).toBeInTheDocument();
    expect(queryByLabelText("Download actions")).toBeNull();
  });

  it("a paused frame shows the paused button + a Resume action that calls resumeDownload", async () => {
    const { findByText, findByLabelText } = await renderActive(42, { status: "paused", resumable: true });

    // The main button surfaces the frozen "Paused" indication.
    expect(await findByText("Paused")).toBeInTheDocument();

    const dropdownBtn = await findByLabelText("Download actions");
    const menu = openMenu(dropdownBtn);
    const resumeItem = await menu.findByText("Resume");

    await act(async () => {
      resumeItem.click();
      await Promise.resolve();
    });

    // Non-vacuous: the exact rom_id was resumed.
    expect(backend.resumeDownload).toHaveBeenCalledWith(42);
  });

  it("Cancel from the resumable dropdown still cancels the download", async () => {
    const { findByLabelText } = await renderActive(42, { status: "downloading", resumable: true });
    const dropdownBtn = await findByLabelText("Download actions");

    const { findByText } = openMenu(dropdownBtn);
    const cancelItem = await findByText("Cancel");

    await act(async () => {
      cancelItem.click();
      await Promise.resolve();
    });

    expect(backend.cancelDownload).toHaveBeenCalledWith(42);
  });

  it("rehydrates a paused download on mount from the queue (survives leaving + returning)", async () => {
    // No Download click and no live progress frame — the paused state is
    // recovered purely from getDownloadQueue at mount (#1124 M1). Without this,
    // a remounted button shows a plain "Download" whose click would restart
    // from byte 0, discarding the paused partial.
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.getDownloadQueue).mockResolvedValue({
      downloads: [
        {
          rom_id: 42,
          rom_name: "Test ROM",
          platform_name: "PSX",
          file_name: "test.chd",
          status: "paused",
          progress: 0.3,
          bytes_downloaded: 300,
          total_bytes: 1000,
          resumable: true,
        },
      ],
    });

    const { findByText, findByLabelText } = render(<CustomPlayButton appId={100} />);

    // Rehydrated straight into the paused shape — Resume is reachable without
    // ever showing a fresh "Download" button.
    expect(await findByText("Paused")).toBeInTheDocument();
    const dropdownBtn = await findByLabelText("Download actions");
    const menu = openMenu(dropdownBtn);
    const resumeItem = await menu.findByText("Resume");

    await act(async () => {
      resumeItem.click();
      await Promise.resolve();
    });

    expect(backend.resumeDownload).toHaveBeenCalledWith(42);
  });
});

describe("CustomPlayButton — extraction phase on a multi-file download", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.startDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.cancelDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.getDownloadQueue).mockResolvedValue({ downloads: [] });
  });

  // Drive the button into its active-download render, then optionally hand it a
  // follow-up frame (an extracting frame, here). Mirrors renderDownloading/
  // renderActive above but parameterised on the second frame.
  async function renderWithFrames(frames: DownloadProgressEvent[]): Promise<ReturnType<typeof render>> {
    mockCachedDetail({ rom_id: 42, installed: false });
    const utils = render(<CustomPlayButton appId={100} />);
    const downloadBtn = await utils.findByText("Download");

    await act(async () => {
      downloadBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    for (const frame of frames) {
      act(() => {
        emitDeckyEvent<[DownloadProgressEvent]>("download_progress", frame);
      });
    }

    return utils;
  }

  const downloadingFrame: DownloadProgressEvent = {
    rom_id: 42,
    rom_name: "Test ROM",
    platform_name: "PSX",
    file_name: "game.zip",
    status: "downloading",
    progress: 1,
    bytes_downloaded: 1000,
    total_bytes: 1000,
    resumable: false,
  };

  const extractingFrame: DownloadProgressEvent = {
    rom_id: 42,
    rom_name: "Test ROM",
    platform_name: "PSX",
    file_name: "game.zip",
    status: "extracting",
    progress: 0.42,
    bytes_downloaded: 4200,
    total_bytes: 10000,
    resumable: false,
  };

  it("shows 'Extracting… N%' (N from extracted bytes/total) once an extracting frame arrives", async () => {
    const { findByText } = await renderWithFrames([downloadingFrame, extractingFrame]);
    // 4200 / 10000 = 42% — the label reads the uncompressed-byte fraction.
    expect(await findByText("Extracting… 42%")).toBeInTheDocument();
  });

  it("removes the cancel control during extraction and surfaces a disabled throbber instead", async () => {
    const { findByLabelText, queryByLabelText } = await renderWithFrames([downloadingFrame, extractingFrame]);

    // The disabled throbber is the only right-side action while extracting.
    const throbber = await findByLabelText("Extracting");
    expect(throbber).toBeInTheDocument();
    expect(throbber).toBeDisabled();

    // No cancel X and no pause/resume dropdown during extraction.
    expect(queryByLabelText("Cancel download")).toBeNull();
    expect(queryByLabelText("Download actions")).toBeNull();
  });

  it("keeps the (neutral-restyled) cancel X in the normal downloading phase", async () => {
    // The normal downloading phase still carries the cancel X — now restyled to
    // the Steam-native translucent-white look. The @decky/ui mock drops inline
    // `style`, so the colour itself isn't observable here; assert the control
    // renders and is enabled (cancellable) — the contrast with the extracting
    // phase's disabled throbber is what the restyle test pair guards.
    const { findByLabelText, queryByLabelText } = await renderWithFrames([downloadingFrame]);
    const cancelX = await findByLabelText("Cancel download");

    expect(cancelX).toBeInTheDocument();
    expect(cancelX).toHaveClass("romm-btn-cancel");
    expect(cancelX).not.toBeDisabled();
    // While downloading (not extracting) there's no throbber action.
    expect(queryByLabelText("Extracting")).toBeNull();
  });
});

describe("CustomPlayButton — pre-launch savefiles_in_content_dir benign skip (#239)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    // Gate predecessors of runPreLaunchSync: tracking configured + no core change
    // so handlePlay reaches preLaunchSync and then the launch dispatch.
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    // Fresh reachability probe is online → the gate runs the online pre-launch
    // sync branch (not the offline drift check).
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    // RunGame is the launch sink — assert it fires on the benign-skip path.
    vi.stubGlobal("SteamClient", {
      Apps: { RunGame: vi.fn() },
    });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })),
      allApps: [],
    });
  });

  it("treats the benign skip as a no-op: no error toast AND the game still launches", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
    });
    // Backend benign-skip blocked shape: success:false but reason is the
    // content-dir slug, synced 0, no errors, no conflicts.
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      reason: "savefiles_in_content_dir",
      message: "Save sync is unavailable: RetroArch is set to write saves to the content directory.",
      synced: 0,
      errors: [],
      conflicts: [],
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");

    await act(async () => {
      playBtn.click();
      // Drain the handlePlay gate chain (tracking → core → preLaunchSync → launch).
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Launch proceeded — RunGame fired with the resolved gameId.
    expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100);
    // No error / fallback toast surfaced for the benign skip.
    expect(vi.mocked(toaster.toast)).not.toHaveBeenCalled();
    // No fallback-launch confirm modal was opened (would mean we treated it as failure).
    expect(vi.mocked(backend.preLaunchSync)).toHaveBeenCalledWith(42);
  });
});

describe("CustomPlayButton — uninstall resets launch_options (#1051)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    vi.mocked(showContextMenu).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
    vi.mocked(backend.removeRom).mockResolvedValue({ success: true, message: "" });
  });

  // Open the play-state "RomM Actions" menu (the chevron next to Play) and click
  // its Uninstall item — mirrors the download-actions menu-driving pattern above.
  async function clickUninstall(container: HTMLElement): Promise<void> {
    const chevron = container.querySelector(".romm-btn-dropdown") as HTMLElement | null;
    if (!chevron) throw new Error("dropdown chevron not rendered");
    act(() => {
      chevron.click();
    });
    const calls = vi.mocked(showContextMenu).mock.calls;
    const menu = calls[calls.length - 1]![0] as ReactElement;
    const { findByText } = render(menu);
    const uninstallItem = await findByText("Uninstall");
    await act(async () => {
      uninstallItem.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("clears the shortcut launch command to the uninstalled placeholder on a successful uninstall", async () => {
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    await clickUninstall(container);

    // Reset to "" for the shortcut's appId so a raced-past not_installed can't
    // exec a stale command into the deleted path (#1051).
    expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(100, "");
    expect(vi.mocked(backend.removeRom)).toHaveBeenCalledWith(42);
  });

  it("does not reset launch_options when the uninstall fails", async () => {
    vi.mocked(backend.removeRom).mockResolvedValue({ success: false, message: "boom" });
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    await clickUninstall(container);

    // The reset lives in the success branch — a failed uninstall leaves the
    // command untouched (the shortcut is still installed).
    expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
  });
});

describe("CustomPlayButton — pre-launch failure shapes without an errors array (#1050)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    vi.mocked(showFallbackLaunchModal).mockReset();
    vi.mocked(backend.preLaunchSync).mockReset();
    // Gate predecessors so handlePlay reaches runPreLaunchSync.
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    // Online probe → the online pre-launch sync branch.
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })),
      allApps: [],
    });
  });

  // success:false failures that carry NO errors array — the shapes the gate maps
  // to `sync_failed` → the shared fallback confirm.
  const FAILURE_SHAPES = [
    {
      reason: "device_not_registered",
      message: "Device is not registered with RomM. Open the Saves tab to set it up.",
    },
    { reason: "save_sort_changed", message: "RetroArch save sorting changed — migrate saves in Settings first" },
    {
      reason: "blocked_by_migration",
      message: "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
    },
  ];

  it.each(FAILURE_SHAPES)(
    "surfaces the shared fallback-launch confirm with the backend message on $reason instead of proceeding silently",
    async ({ reason, message }) => {
      mockCachedDetail();
      vi.mocked(backend.preLaunchSync).mockResolvedValue({
        success: false,
        reason,
        message,
        synced: 0,
        errors: [],
        conflicts: [],
      });
      // User cancels the fallback.
      vi.mocked(showFallbackLaunchModal).mockResolvedValue(false);

      const { findByText } = render(<CustomPlayButton appId={100} />);
      const playBtn = await findByText("Play");
      await act(async () => {
        playBtn.click();
        await Promise.resolve();
        await Promise.resolve();
      });

      // The shared fallback confirm opened (not a silent launch) carrying the
      // backend's specific message (e.g. save_sort_changed's "migrate saves in
      // Settings first" — previously never shown).
      await waitFor(() => expect(vi.mocked(showFallbackLaunchModal)).toHaveBeenCalledWith(message));
      // Cancelling the fallback must NOT launch.
      expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    },
  );

  it("launches with local saves when the user confirms the shared fallback on a no-errors failure", async () => {
    mockCachedDetail();
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      reason: "device_not_registered",
      message: "Device is not registered with RomM.",
      synced: 0,
      errors: [],
      conflicts: [],
    });
    // User confirms "Launch Anyway".
    vi.mocked(showFallbackLaunchModal).mockResolvedValue(true);

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
  });

  it("stays in the conflict state and toasts the message when resolve-conflict sync fails without conflicts", async () => {
    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    // Drive the button into the conflict state via the backend push (DOM event).
    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: 42, has_conflict: true } }),
      );
    });
    const resolveBtn = await findByText("Resolve Conflict");

    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      reason: "device_not_registered",
      message: "Device is not registered with RomM.",
      synced: 0,
      errors: [],
      conflicts: [],
    });

    await act(async () => {
      resolveBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Device is not registered") }),
    );
    // Still in the conflict state — not dropped to "play".
    await findByText("Resolve Conflict");
  });

  it("proceeds with the synced toast and no fallback confirm on a clean pre-launch sync", async () => {
    mockCachedDetail();
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 1,
      errors: [],
      conflicts: [],
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
    });

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalled());
    expect(vi.mocked(showFallbackLaunchModal)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100);
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Saves synced with RomM" }));
  });

  it("surfaces the shared fallback (empty message → generic copy) when pre-launch sync throws", async () => {
    mockCachedDetail();
    vi.mocked(backend.preLaunchSync).mockRejectedValue(new Error("network down"));
    vi.mocked(showFallbackLaunchModal).mockResolvedValue(false);

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // A throw/timeout maps to sync_failed with an empty message — the modal
    // itself supplies the generic "Couldn't sync saves" copy from "".
    await waitFor(() => expect(vi.mocked(showFallbackLaunchModal)).toHaveBeenCalledWith(""));
    // Cancelled → no launch.
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("tolerates a minimal failure shape with no reason or errors and launches on fallback confirm", async () => {
    mockCachedDetail();
    // No reason / errors / synced — exercises the `reason ?? ""` and
    // `errors?.join() ?? ""` fallbacks in the failure-debug log.
    vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: false, message: "Save sync unavailable" });
    vi.mocked(showFallbackLaunchModal).mockResolvedValue(true);

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(vi.mocked(showFallbackLaunchModal)).toHaveBeenCalledWith("Save sync unavailable"));
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalled());
  });

  it("returns to the Play button when resolve-conflict sync succeeds", async () => {
    mockCachedDetail();
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: 42, has_conflict: true } }),
      );
    });
    const resolveBtn = await findByText("Resolve Conflict");

    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 0,
      errors: [],
      conflicts: [],
    });

    await act(async () => {
      resolveBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    await findByText("Play");
    expect(queryByText("Resolve Conflict")).toBeNull();
  });

  it("shows the generic resolve toast when the failed resolve carries no message", async () => {
    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: 42, has_conflict: true } }),
      );
    });
    const resolveBtn = await findByText("Resolve Conflict");

    // Empty message → the `|| "Couldn't resolve conflict…"` fallback body.
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      message: "",
      synced: 0,
      errors: [],
      conflicts: [],
    });

    await act(async () => {
      resolveBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Couldn't resolve conflict") }),
    );
    await findByText("Resolve Conflict");
  });
});

// ---------------------------------------------------------------------------
// Shared launch-gate funnel (ADR-0015) driven through the Play button. The
// gate (runLaunchGate) is REAL; only the leaf backend probes + shared modal
// helpers are stubbed, so these assert the Play button's verdict→UI mapping.
// ---------------------------------------------------------------------------
describe("CustomPlayButton — shared launch gate (ADR-0015)", () => {
  const conflict = (overrides: Partial<SyncConflict> = {}): SyncConflict => ({
    type: "sync_conflict",
    rom_id: 42,
    filename: "save.srm",
    server_save_id: 7,
    server_updated_at: "2026-01-01T00:00:00Z",
    server_size: 1024,
    local_path: "/local/save.srm",
    local_hash: "abc",
    local_mtime: "2026-01-01T00:00:00Z",
    local_size: 1024,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  });

  beforeEach(() => {
    vi.clearAllMocks();
    // Drain any skip-set leak from a prior test's launch (the real skip-set is
    // module-level state) so a mark in one test never silently affects the next.
    consumeLaunchSkip(100);

    // Gate predecessors default to "pass": no migration, tracking configured,
    // no core change. Each test overrides the branch it exercises.
    vi.mocked(getMigrationState).mockReturnValue({ pending: false });
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: true, message: "", synced: 0, conflicts: [] });

    vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })),
      allApps: [],
    });
  });

  async function clickPlay(): Promise<void> {
    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      // Drain the gate chain (migration → tracking → core → reachability →
      // sync/drift → verdict → modal → dispatch).
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("online allow → marks the launch skipped BEFORE RunGame and launches (C1 double-gate fix)", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    // Clean sync → allow.
    vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: true, message: "", synced: 0, conflicts: [] });

    await clickPlay();

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
    // C1: markLaunchSkipped(appId) fired, and it fired BEFORE RunGame — so the
    // global watcher skips this launch instead of cancel-then-re-gating it.
    expect(vi.mocked(markLaunchSkipped)).toHaveBeenCalledWith(100);
    const markOrder = vi.mocked(markLaunchSkipped).mock.invocationCallOrder[0]!;
    const runOrder = vi.mocked(SteamClient.Apps.RunGame).mock.invocationCallOrder[0]!;
    expect(markOrder).toBeLessThan(runOrder);
    // The skip-set actually carries appId 100 (the real markLaunchSkipped ran).
    expect(consumeLaunchSkip(100)).toBe(true);
  });

  it("offline + local drift → OfflineDriftModal; start_anyway launches", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 42 });
    vi.mocked(showOfflineDriftModal).mockResolvedValue("start_anyway");

    await clickPlay();

    // The offline-drift modal was the funnel's verdict (NOT the old stale-flag
    // confirmOfflineLaunch), and the pre-launch sync never ran (offline branch).
    expect(vi.mocked(showOfflineDriftModal)).toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
    expect(vi.mocked(markLaunchSkipped)).toHaveBeenCalledWith(100);
  });

  it("offline + local drift → OfflineDriftModal; cancel does NOT launch", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 42 });
    vi.mocked(showOfflineDriftModal).mockResolvedValue("cancel");

    await clickPlay();

    expect(vi.mocked(showOfflineDriftModal)).toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    expect(vi.mocked(markLaunchSkipped)).not.toHaveBeenCalled();
  });

  it("offline + drift → retry → gate re-probes, now online → launches via online path", async () => {
    // First gate pass: offline + drift → offline modal. User picks "retry".
    // Second gate pass: probe now returns online → clean sync → allow → launch.
    vi.mocked(backend.probeReachability).mockResolvedValueOnce({ online: false }).mockResolvedValue({ online: true });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 42 });
    vi.mocked(showOfflineDriftModal).mockResolvedValueOnce("retry");
    vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: true, message: "", synced: 0, conflicts: [] });

    await clickPlay();

    // The modal asked, the user retried, and the gate RE-RAN: a second
    // reachability probe fired (the re-probe), the now-online branch ran the
    // pre-launch sync, and the launch dispatched. Non-vacuous: assert the ops
    // were invoked AGAIN on retry, not just once.
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
    expect(vi.mocked(backend.probeReachability).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(vi.mocked(backend.preLaunchSync)).toHaveBeenCalled();
    expect(vi.mocked(markLaunchSkipped)).toHaveBeenCalledWith(100);
  });

  it("offline + drift → retry → still offline+drift → re-shows modal; cancel bails (no launch)", async () => {
    // Both gate passes are offline + drift. User retries once, then cancels.
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 42 });
    vi.mocked(showOfflineDriftModal).mockResolvedValueOnce("retry").mockResolvedValueOnce("cancel");

    await clickPlay();

    // The modal was shown TWICE (initial + after the retry re-ran the gate) and
    // the gate re-probed; the final "cancel" bails without launching.
    await waitFor(() => expect(vi.mocked(showOfflineDriftModal)).toHaveBeenCalledTimes(2));
    expect(vi.mocked(backend.probeReachability).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    expect(vi.mocked(markLaunchSkipped)).not.toHaveBeenCalled();
  });

  it("offline + drift → retry → now online conflict → conflict modal (online path)", async () => {
    // Retry flips online and the online sync surfaces a conflict → the conflict
    // modal runs, proving retry routes to the FULL online path, not just allow.
    vi.mocked(backend.probeReachability).mockResolvedValueOnce({ online: false }).mockResolvedValue({ online: true });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 42 });
    vi.mocked(showOfflineDriftModal).mockResolvedValueOnce("retry");
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      message: "conflict",
      synced: 0,
      conflicts: [conflict()],
    });
    vi.mocked(handleConflicts).mockResolvedValue("cancel");

    await clickPlay();

    await waitFor(() => expect(vi.mocked(handleConflicts)).toHaveBeenCalledWith([conflict()]));
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("offline + NO local drift → launches silently (no modal)", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: false, rom_id: 42 });

    await clickPlay();

    // Nothing to lose → silent allow: no drift modal, no fallback, just launch.
    expect(vi.mocked(showOfflineDriftModal)).not.toHaveBeenCalled();
    expect(vi.mocked(showFallbackLaunchModal)).not.toHaveBeenCalled();
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
  });

  it("online conflict → shared handleConflicts; resolved → romm_data_changed + launch", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      message: "conflict",
      synced: 0,
      conflicts: [conflict()],
    });
    vi.mocked(handleConflicts).mockResolvedValue("resolved");

    const dataChanged = vi.fn();
    globalThis.addEventListener("romm_data_changed", dataChanged);

    await clickPlay();

    // The SHARED handleConflicts (from SyncConflictModal) handled the conflicts,
    // not a Play-button-local duplicate.
    expect(vi.mocked(handleConflicts)).toHaveBeenCalledWith([conflict()]);
    // Resolved → sibling refresh dispatched, then launch.
    expect(dataChanged).toHaveBeenCalled();
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));

    globalThis.removeEventListener("romm_data_changed", dataChanged);
  });

  it("online conflict → cancelled → stays in conflict state, no launch", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      message: "conflict",
      synced: 0,
      conflicts: [conflict()],
    });
    vi.mocked(handleConflicts).mockResolvedValue("cancel");

    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(vi.mocked(handleConflicts)).toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    // Cancelling a conflict drops the button into its "conflict" (Resolve) state.
    await findByText("Resolve Conflict");
  });

  it("a verdict modal helper that THROWS → resets to play (never frozen in syncing)", async () => {
    // runPreLaunchSync flips the button to "syncing", the gate returns a
    // conflict verdict, then the shared handleConflicts REJECTS at the framework
    // level. Without the outer try/catch the button would stay stuck in
    // "syncing"; with it, handlePlay recovers to "play".
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: false,
      message: "conflict",
      synced: 0,
      conflicts: [conflict()],
    });
    vi.mocked(handleConflicts).mockRejectedValue(new Error("modal blew up"));

    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Recovered: the Play label is back (NOT stuck on "Syncing saves...") and no
    // launch happened.
    expect(await findByText("Play")).toBeInTheDocument();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("migration pending → blocks: no sync, no modal, no launch", async () => {
    vi.mocked(getMigrationState).mockReturnValue({ pending: true });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });

    await clickPlay();

    // The migration block short-circuits the funnel before any network step.
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    expect(vi.mocked(markLaunchSkipped)).not.toHaveBeenCalled();
  });

  it("tracking-setup abort → silent bail back to play, no launch", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    // Unconfigured tracking whose setup needs a user choice (server has saves)
    // → the page-aware ensureTrackingConfigured returns "abort" (routes to the
    // saves tab) → gate `abort`. The funnel never reaches reachability/sync.
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: false, active_slot: "default" });
    vi.mocked(backend.getSaveSetupInfo).mockResolvedValue({
      recommended_action: "needs_user_choice",
      default_slot: "default",
      server_slots: [{ slot: "default" }],
    } as unknown as Awaited<ReturnType<typeof backend.getSaveSetupInfo>>);

    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Abort → bailed silently to "play" (Play button back), no launch, and the
    // funnel short-circuited before the reachability probe.
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();
    await findByText("Play");
  });

  it("reachability probe rejects → treated as offline; with drift → OfflineDriftModal", async () => {
    vi.mocked(backend.probeReachability).mockRejectedValue(new Error("net"));
    vi.mocked(backend.checkLocalDrift).mockResolvedValue({ drifted: true, rom_id: 42 });
    vi.mocked(showOfflineDriftModal).mockResolvedValue("cancel");

    await clickPlay();

    // A thrown probe is treated as offline (the probe `.catch` arm), so the
    // funnel takes the drift branch — NOT a silent online allow.
    expect(vi.mocked(showOfflineDriftModal)).toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("local-drift check rejects → treated as not-drifted; launches silently", async () => {
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: false });
    vi.mocked(backend.checkLocalDrift).mockRejectedValue(new Error("net"));

    await clickPlay();

    // A thrown drift check resolves to not-drifted (the drift `.catch` arm), so
    // the offline branch silently allows rather than showing a false modal.
    expect(vi.mocked(showOfflineDriftModal)).not.toHaveBeenCalled();
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
  });

  it("unresolved romId → launches straight through with no gating", async () => {
    // Cached as installed but with no rom_id — the launch isn't ours to gate, so
    // handlePlay launches straight away (still marking the skip-set).
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_name: "Test ROM",
      installed: true,
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // No gate steps ran — no probe, no sync — just a direct (skip-marked) launch.
    expect(vi.mocked(backend.probeReachability)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(markLaunchSkipped)).toHaveBeenCalledWith(100);
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
  });
});

// ---------------------------------------------------------------------------
// F7 — settling into "play" fires a fire-and-forget background save-status
// refresh when save sync is enabled (the save_status_updated → romm_data_changed
// production trigger).
// ---------------------------------------------------------------------------
describe("CustomPlayButton — F7 background save-status refresh on settle-to-play", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(backend.refreshSaveStatus).mockResolvedValue({ success: true });
  });

  it("fires refreshSaveStatus(romId) once it settles into play with save_sync_enabled", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
      save_sync_enabled: true,
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    await waitFor(() => expect(vi.mocked(backend.refreshSaveStatus)).toHaveBeenCalledWith(42));
  });

  it("does NOT fire refreshSaveStatus when save sync is disabled", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
      save_sync_enabled: false,
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    // Give the init effect a tick to (not) fire.
    await act(async () => {
      await Promise.resolve();
    });

    expect(vi.mocked(backend.refreshSaveStatus)).not.toHaveBeenCalled();
  });

  it("does NOT fire refreshSaveStatus when the button settles into conflict (not play)", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
      save_sync_enabled: true,
      save_status: {
        files: [{ filename: "save.srm", status: "conflict" }],
        conflicts: [
          {
            type: "sync_conflict",
            rom_id: 42,
            filename: "save.srm",
            server_save_id: 7,
            server_updated_at: "2026-01-01T00:00:00Z",
            server_size: 1,
            local_path: null,
            local_hash: null,
            local_mtime: null,
            local_size: null,
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
      },
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Resolve Conflict");
    await act(async () => {
      await Promise.resolve();
    });

    // F7 is scoped to the play branch only — a conflict-on-load doesn't trigger it.
    expect(vi.mocked(backend.refreshSaveStatus)).not.toHaveBeenCalled();
  });

  it("swallows a refreshSaveStatus rejection without disturbing the play state", async () => {
    vi.mocked(backend.refreshSaveStatus).mockRejectedValue(new Error("offline"));
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
      save_sync_enabled: true,
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    // The catch is fire-and-forget — the button still shows Play (post-catch
    // state unchanged) and the rejected call was made.
    await findByText("Play");
    await waitFor(() => expect(vi.mocked(backend.refreshSaveStatus)).toHaveBeenCalledWith(42));
    expect(await findByText("Play")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// #1150 — the Play-button pre-launch relaunch re-confirm. dispatchLaunch pulls
// getRomRelaunchOptions and confirm-sets the shortcut's launch_options BEFORE
// RunGame, healing mid-session drift on the common launch path. Best-effort:
// a None item skips the set, and a rejection logs + still launches.
// ---------------------------------------------------------------------------
describe("CustomPlayButton — pre-launch relaunch re-confirm (#1150)", () => {
  const RELAUNCH_COMMAND = 'flatpak run net.retrodeck.retrodeck "/roms/gba/pokemon.gba"';

  beforeEach(() => {
    vi.clearAllMocks();
    consumeLaunchSkip(100);
    // Allow-path gate predecessors: online, tracking configured, no core change,
    // clean pre-launch sync → the gate returns "allow" → dispatchLaunch runs.
    vi.mocked(getMigrationState).mockReturnValue({ pending: false });
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({ success: true, message: "", synced: 0, conflicts: [] });
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);

    vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })),
      allApps: [],
    });
  });

  async function clickPlay(): Promise<void> {
    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
      // Drain the gate chain + the dispatchLaunch re-confirm awaits.
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("re-confirms launch_options (getRomRelaunchOptions → setLaunchOptionsConfirmed) BEFORE RunGame", async () => {
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({ app_id: 100, launch_options: RELAUNCH_COMMAND });

    await clickPlay();

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
    // The re-confirm pulled this ROM's resolved command and confirm-set it onto
    // the shortcut's appId with that exact command.
    expect(vi.mocked(backend.getRomRelaunchOptions)).toHaveBeenCalledWith(42);
    expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(100, RELAUNCH_COMMAND);
    // Order: getRomRelaunchOptions → setLaunchOptionsConfirmed → RunGame.
    const getOrder = vi.mocked(backend.getRomRelaunchOptions).mock.invocationCallOrder[0]!;
    const setOrder = vi.mocked(setLaunchOptionsConfirmed).mock.invocationCallOrder[0]!;
    const runOrder = vi.mocked(SteamClient.Apps.RunGame).mock.invocationCallOrder[0]!;
    expect(getOrder).toBeLessThan(setOrder);
    expect(setOrder).toBeLessThan(runOrder);
  });

  it("a null item skips setLaunchOptionsConfirmed but still launches", async () => {
    // No install/binding to re-confirm → backend returns null → the set is
    // skipped, but the launch must still proceed (nothing to heal, no block).
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue(null);

    await clickPlay();

    expect(vi.mocked(backend.getRomRelaunchOptions)).toHaveBeenCalledWith(42);
    expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
  });

  it("a rejected re-confirm logs the pre-launch message AND still launches (non-vacuous catch)", async () => {
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    vi.mocked(backend.getRomRelaunchOptions).mockRejectedValue(new Error("offline"));

    await clickPlay();

    // Post-catch state: the failure was logged with the #1150 message AND the
    // launch still fired (best-effort — a failed re-confirm is no worse than today).
    await waitFor(() =>
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("pre-launch relaunch re-confirm failed")),
    );
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));

    logSpy.mockRestore();
  });

  it("a hung getRomRelaunchOptions falls through to the launch after the timeout (button never trapped)", async () => {
    // The Decky callable bridge can hang forever on a wedged backend. The fetch
    // is bounded by a 3s Promise.race; on timeout the re-confirm is skipped and
    // the launch still fires — the button must not stay stuck on "Launching…".
    // RTL's findBy* deadlocks under fake timers, so render + settle to "Play"
    // under REAL timers, then switch to fake timers right before the click so the
    // 3s re-confirm timeout fires without a real wait (kept fast).
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    // Never resolves — simulates a wedged backend / hung bridge.
    vi.mocked(backend.getRomRelaunchOptions).mockReturnValue(new Promise<never>(() => {}));

    try {
      mockCachedDetail();
      const { findByText } = render(<CustomPlayButton appId={100} />);
      const playBtn = await findByText("Play");

      vi.useFakeTimers();
      await act(async () => {
        playBtn.click();
        // The gate chain up to dispatchLaunch is microtask-driven (no setTimeout);
        // advancing past 3000ms flushes those microtasks and fires the re-confirm
        // timeout that unblocks the hung fetch.
        await vi.advanceTimersByTimeAsync(3000);
      });

      // The hung fetch timed out → re-confirm skipped (no set), logged, and the
      // launch STILL fired. RunGame is the proof the button escaped "Launching…".
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("pre-launch relaunch re-confirm failed"));
      expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100);
    } finally {
      vi.useRealTimers();
      logSpy.mockRestore();
    }
  });
});
