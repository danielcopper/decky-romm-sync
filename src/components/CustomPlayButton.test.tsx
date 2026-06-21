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

import { getCachedGameDetail } from "../utils/cachedGameDetailStore";

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
