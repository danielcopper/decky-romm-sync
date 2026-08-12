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
import { render, waitFor, act, within } from "@testing-library/react";
import { toaster } from "@decky/api";
import { showContextMenu, Navigation } from "@decky/ui";
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

// The real in-memory connection store is used so the button's live offline
// subscription is exercised (#1345); each test resets it to "connected" in
// beforeEach so the default path renders "play"/"download".

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

// Already-running guard deps (#1148 round 2). Default: no live session and nothing
// running, so the guard is inert and the existing Play-funnel tests run unchanged.
// CustomPlayButton is the only in-graph importer of either module, so a full mock
// exposing just the guard's two functions is safe.
vi.mock("../utils/sessionManager", () => ({
  isSessionActive: vi.fn(() => false),
}));
vi.mock("../utils/runningApps", () => ({
  isAppRunning: vi.fn(() => false),
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
// Stop-Game confirm — spy so the confirm-then-call ordering is observable
// without rendering the modal (same shape as the launch-gate modals above).
vi.mock("../components/StopGameModal", () => ({
  showStopGameModal: vi.fn(),
}));
// Adopt/replace/cancel dialog — spied so the button's routing off a
// `target_occupied` refusal is observable without rendering the modal.
vi.mock("../components/AdoptExistingModal", () => ({
  showAdoptExistingModal: vi.fn(),
}));

import { getCachedGameDetail } from "../utils/cachedGameDetailStore";
import { setRommConnectionState, reportServerReachable, getRommConnectionState } from "../utils/connectionState";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { markLaunchSkipped, consumeLaunchSkip } from "../utils/launchGate";
import { getMigrationState } from "../utils/migrationStore";
import { isSessionActive } from "../utils/sessionManager";
import { isAppRunning } from "../utils/runningApps";
import { showOfflineDriftModal } from "../components/OfflineDriftModal";
import { showFallbackLaunchModal } from "../components/FallbackLaunchModal";
import { handleConflicts } from "../components/SyncConflictModal";
import { showStopGameModal } from "../components/StopGameModal";
import { showAdoptExistingModal } from "../components/AdoptExistingModal";
import { mountPruneLeasePlugin, releaseAllPruneLeases } from "../utils/pruneLease";
import { resetBoundVanished, setBoundVanished } from "../utils/vanishedBinding";
import type { SyncConflict, SaveStatus } from "../types";

function mockCachedDetail(overrides: Partial<CachedGameDetail> = {}): void {
  vi.mocked(getCachedGameDetail).mockResolvedValue({
    found: true,
    rom_id: 42,
    rom_name: "Test ROM",
    installed: true,
    ...overrides,
  });
}

// Reset the shared connection store before every test (module-level state that
// persists across tests) so the default render path is "connected" (#1345).
beforeEach(() => {
  setRommConnectionState("connected");
  resetBoundVanished();
});

describe("CustomPlayButton — vanished bound ROM (#1570 F20)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.startDownload).mockReset();
  });

  const downloadButton = (container: HTMLElement): HTMLButtonElement =>
    container.querySelector<HTMLButtonElement>("button.romm-btn-download")!;

  it("disables Download once RomM confirms the bound ROM is gone", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const { findByText, container } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");
    expect(downloadButton(container).disabled).toBe(false);

    act(() => setBoundVanished(100, true));

    // The download can only ever come back not_found, so stop offering it.
    expect(downloadButton(container).disabled).toBe(true);
  });

  it("starts no download when a vanished button is activated anyway", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const { findByText, container } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");
    act(() => setBoundVanished(100, true));

    downloadButton(container).click();
    await act(async () => Promise.resolve());

    expect(backend.startDownload).not.toHaveBeenCalled();
  });

  it("keeps Download enabled when the server could not be reached", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const { findByText, container } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");

    // A failed query reports bound_vanished:false — uncertainty must never
    // disable the download. Fail-open is the whole feature's rule.
    act(() => setBoundVanished(100, false));

    expect(downloadButton(container).disabled).toBe(false);
  });

  it("does not disable a different game's Download", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const { findByText, container } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");

    act(() => setBoundVanished(999, true));

    expect(downloadButton(container).disabled).toBe(false);
  });
});

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

  it("a refused resume is said out loud rather than doing nothing", async () => {
    // The backend can turn a resume down — content appeared at the game's
    // location while it sat paused. Swallowing that leaves the user pressing a
    // button with no effect and no explanation.
    vi.mocked(backend.resumeDownload).mockResolvedValue({
      success: false,
      reason: "target_occupied",
      message: "A folder named 'Game 1' is already in place",
      existing: { name: "Game 1", path: "/roms/psx/Game 1", is_dir: true, size_bytes: 2048, modified_at: 0 },
      incoming: { name: "Game 1", size_bytes: 2048 },
      sizes_match: true,
      adoptable: true,
    });
    const { findByLabelText } = await renderActive(42, { status: "paused", resumable: true });
    const menu = openMenu(await findByLabelText("Download actions"));
    const resumeItem = await menu.findByText("Resume");

    await act(async () => {
      resumeItem.click();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Something else is at this game's location now — cancel the download and start again",
    });
  });

  it("a resume refused for any other reason surfaces the backend's own message", async () => {
    vi.mocked(backend.resumeDownload).mockResolvedValue({
      success: false,
      message: "Another version is now active",
    });
    const { findByLabelText } = await renderActive(42, { status: "paused", resumable: true });
    const menu = openMenu(await findByLabelText("Download actions"));
    const resumeItem = await menu.findByText("Resume");

    await act(async () => {
      resumeItem.click();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Another version is now active",
    });
  });

  it("a thrown resume is surfaced rather than swallowed", async () => {
    vi.mocked(backend.resumeDownload).mockRejectedValue(new Error("bridge down"));
    const { findByLabelText } = await renderActive(42, { status: "paused", resumable: true });
    const menu = openMenu(await findByLabelText("Download actions"));
    const resumeItem = await menu.findByText("Resume");

    await act(async () => {
      resumeItem.click();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Couldn't resume the download — is RomM server running?",
    });
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

  it("rehydrates a plain downloading (non-paused) download on mount from the queue (#145 / #1126)", async () => {
    // Sibling of the paused-rehydration case above: a plain "downloading" entry
    // in the queue at mount must rehydrate the button straight into its active
    // shape — NOT a fresh "Download" button whose click would restart from byte
    // 0 (the #145 regression, closed by #1126). No Download click and no live
    // progress frame — the active state is recovered purely from getDownloadQueue.
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.getDownloadQueue).mockResolvedValue({
      downloads: [
        {
          rom_id: 42,
          rom_name: "Test ROM",
          platform_name: "PSX",
          file_name: "test.chd",
          status: "downloading",
          progress: 0.3,
          bytes_downloaded: 300,
          total_bytes: 1000,
          resumable: true,
        },
      ],
    });

    const { findByLabelText, queryByText } = render(<CustomPlayButton appId={100} />);

    // Rehydrated into the active-download shape (the resumable actions dropdown),
    // reachable without ever falling back to a fresh "Download" button.
    const dropdownBtn = await findByLabelText("Download actions");
    expect(queryByText("Download")).toBeNull();

    // The rehydrated state drives the real controls — Pause hits the exact rom_id.
    const menu = openMenu(dropdownBtn);
    const pauseItem = await menu.findByText("Pause");
    await act(async () => {
      pauseItem.click();
      await Promise.resolve();
    });
    expect(backend.pauseDownload).toHaveBeenCalledWith(42);
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

describe("CustomPlayButton — offline gating reacts live to the store (#1345)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
  });

  it("Download re-enables when the store flips back to connected — no remount", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    setRommConnectionState("offline");
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const label = await findByText("Download");
    const btn = label.closest("button");
    // Offline at mount → the Download button is disabled.
    expect(btn).toBeDisabled();

    // The server comes back (any successful probe / call feeds the store). The
    // button must re-enable on the SAME mount — the device symptom was it staying
    // blocked until the page was re-entered.
    act(() => {
      reportServerReachable(true);
    });
    expect(btn).not.toBeDisabled();
  });

  it("Download disables live when the store flips to offline — no remount", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    setRommConnectionState("connected");
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const label = await findByText("Download");
    const btn = label.closest("button");
    expect(btn).not.toBeDisabled();

    act(() => {
      reportServerReachable(false);
    });
    expect(btn).toBeDisabled();
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

// #1148 round 2: the Play button is the sibling of the launch interceptor's
// already-running guard in `handlePlay`. Since #1313 the button renders Resume
// (not Play) whenever running is detected at/after mount, so this guard is now
// the BACKSTOP for the render→click RACE: the button shows Play (nothing running
// at mount), the session starts WITHOUT a session event reaching this button, and
// a Play press then must still skip the pre-launch sync (which would upload the
// save mid-session and manufacture an exit conflict) and just bring the game to
// front. cached rom_id=42, appId=100.
describe("CustomPlayButton — already-running guard (#1148 round 2)", () => {
  beforeEach(() => {
    // This file has no global mock-clear, so backend callable call history leaks
    // across describes (an earlier Play test already invoked isSaveTrackingConfigured
    // / debugLog). Clear it so the "never touched" guard assertions are meaningful.
    vi.clearAllMocks();
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    // Guard defaults: no live session, nothing running (overridden per test).
    vi.mocked(isSessionActive).mockReturnValue(false);
    vi.mocked(isAppRunning).mockReturnValue(false);
    // Gate predecessors so the NORMAL path can reach preLaunchSync when the guard
    // is inert; the guard tests assert these are never touched.
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 0,
      errors: [],
      conflicts: [],
    });
    vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })),
      allApps: [],
    });
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
    });
  });

  it("backstop: skips the gate/sync when the live session appears between render and click", async () => {
    // Nothing running at mount → the button renders Play (no Resume overlay).
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    // The session starts after render, before the click — the render→click race
    // the handlePlay guard exists to catch.
    vi.mocked(isSessionActive).mockReturnValue(true);
    await act(async () => {
      playBtn.click();
    });
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));

    // The gate/sync funnel never ran — neither its first op nor the sync itself.
    expect(vi.mocked(backend.isSaveTrackingConfigured)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    // The info log fired (non-vacuous — proves the guard branch, not just the sink).
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("already running — skipping pre-launch sync"),
    );
  });

  it("backstop: skips the gate/sync when a running-app source reports the appId at click", async () => {
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    // Running-app source flips true after render (post-mount race).
    vi.mocked(isAppRunning).mockReturnValue(true);
    await act(async () => {
      playBtn.click();
    });
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));

    expect(vi.mocked(isAppRunning)).toHaveBeenCalledWith(100);
    expect(vi.mocked(backend.isSaveTrackingConfigured)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("already running — skipping pre-launch sync"),
    );
  });

  it("runs the normal pre-launch funnel when nothing is running", async () => {
    // Defaults: no live session, isAppRunning false → guard inert.
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
    });
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));

    // Guard inert → the gate ran the pre-launch sync before launching, and no
    // already-running log was emitted.
    expect(vi.mocked(backend.preLaunchSync)).toHaveBeenCalledWith(42);
    expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("already running"));
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

describe("CustomPlayButton — uninstall is visible and single-shot (#1664)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    vi.mocked(showContextMenu).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
    vi.mocked(backend.removeRom).mockReset();
  });

  /** Open the play-state menu once and return a click-the-Uninstall-item function. */
  async function openUninstallMenu(container: HTMLElement): Promise<() => Promise<void>> {
    const chevron = container.querySelector(".romm-btn-dropdown") as HTMLElement | null;
    if (!chevron) throw new Error("dropdown chevron not rendered");
    act(() => {
      chevron.click();
    });
    const calls = vi.mocked(showContextMenu).mock.calls;
    const menu = calls[calls.length - 1]![0] as ReactElement;
    const { findByText } = render(menu);
    const uninstallItem = await findByText("Uninstall");
    return async () => {
      await act(async () => {
        uninstallItem.click();
        await Promise.resolve();
      });
    };
  }

  /** A removeRom that stays in flight until the test releases it. */
  function pendingRemoveRom(): (result?: backend.BackendResult) => Promise<void> {
    let release: (value: backend.BackendResult) => void = () => {};
    vi.mocked(backend.removeRom).mockReturnValue(
      new Promise<backend.BackendResult>((resolve) => {
        release = resolve;
      }),
    );
    return async (result = { success: true, message: "" }) => {
      await act(async () => {
        release(result);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
    };
  }

  it("shows the removal is running before the backend answers", async () => {
    const finish = pendingRemoveRom();
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    const pressUninstall = await openUninstallMenu(container);
    await pressUninstall();

    // The pending state is set before the await, so a removal that takes minutes
    // is not indistinguishable from a dead button.
    expect(await findByText("Uninstalling...")).toBeTruthy();
    await finish();
  });

  it("counts files removed as the backend reports them", async () => {
    const finish = pendingRemoveRom();
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    const pressUninstall = await openUninstallMenu(container);
    await pressUninstall();

    act(() => {
      emitDeckyEvent("uninstall_progress", { rom_id: 42, files_removed: 128, files_total: 331 });
    });

    expect(await findByText("Uninstalling 128/331")).toBeTruthy();
    await finish();
  });

  it("ignores a progress frame for another ROM", async () => {
    const finish = pendingRemoveRom();
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    const pressUninstall = await openUninstallMenu(container);
    await pressUninstall();

    act(() => {
      emitDeckyEvent("uninstall_progress", { rom_id: 7, files_removed: 128, files_total: 331 });
    });

    // Still the plain label — a frame for another ROM must not move this counter.
    expect(await findByText("Uninstalling...")).toBeTruthy();
    await finish();
  });

  it("no-ops a second press while the first removal is still running", async () => {
    const finish = pendingRemoveRom();
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    const pressUninstall = await openUninstallMenu(container);

    await pressUninstall();
    await pressUninstall();
    await pressUninstall();

    expect(vi.mocked(backend.removeRom)).toHaveBeenCalledTimes(1);
    await finish();
  });

  it("accepts a fresh press once the first removal has finished", async () => {
    vi.mocked(backend.removeRom).mockResolvedValue({ success: false, message: "boom" });
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    const pressUninstall = await openUninstallMenu(container);

    await pressUninstall();
    await findByText("Play");
    await pressUninstall();

    expect(vi.mocked(backend.removeRom)).toHaveBeenCalledTimes(2);
  });

  it("returns to the pre-uninstall button when the backend refuses", async () => {
    vi.mocked(backend.removeRom).mockResolvedValue({
      success: false,
      reason: "in_progress",
      message: "This ROM is already being uninstalled",
    });
    mockCachedDetail({ rom_id: 42, installed: true });
    const { container, findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    const pressUninstall = await openUninstallMenu(container);

    await pressUninstall();

    await findByText("Play");
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "This ROM is already being uninstalled",
    });
  });

  it("drops its progress listener on unmount", async () => {
    mockCachedDetail({ rom_id: 42, installed: true });
    const { findByText, unmount } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    expect(deckyEventListenerCount("uninstall_progress")).toBe(1);

    unmount();

    expect(deckyEventListenerCount("uninstall_progress")).toBe(0);
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

  it("proceeds with the downloaded toast and no fallback confirm on a clean pre-launch download", async () => {
    mockCachedDetail();
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 1,
      uploaded: 0,
      downloaded: 1,
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
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: "Saves downloaded from RomM" }),
    );
  });

  it("shows the uploaded toast when a pre-launch sync only pushed saves (row-9 upload)", async () => {
    mockCachedDetail();
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 1,
      uploaded: 1,
      downloaded: 0,
      errors: [],
      conflicts: [],
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
    });

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalled());
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Saves uploaded to RomM" }));
  });

  it("names both directions when a pre-launch sync moved saves both ways", async () => {
    mockCachedDetail();
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 3,
      uploaded: 1,
      downloaded: 2,
      errors: [],
      conflicts: [],
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
    });

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalled());
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: "Saves synced with RomM (1 up, 2 down)" }),
    );
  });

  it("fires no toast when a clean pre-launch sync transferred nothing", async () => {
    mockCachedDetail();
    vi.mocked(toaster.toast).mockReset();
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 0,
      uploaded: 0,
      downloaded: 0,
      errors: [],
      conflicts: [],
    });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");
    await act(async () => {
      playBtn.click();
    });

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalled());
    expect(vi.mocked(toaster.toast)).not.toHaveBeenCalled();
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
});

// ---------------------------------------------------------------------------
// #1276 — Resolve Conflict READS the already-known conflict via getSaveStatus
// (a read) instead of re-running the act-capable preLaunchSync (which could
// upload/download OTHER files as a side effect). An empty/absent conflicts list
// means the conflict was resolved elsewhere → back to play; a server-query
// failure or a throw keeps the button in conflict (never silently proceeds).
// ---------------------------------------------------------------------------
describe("CustomPlayButton — resolve conflict reads the known conflict (#1276)", () => {
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

  const saveStatus = (overrides: Partial<SaveStatus> = {}): SaveStatus => ({
    rom_id: 42,
    files: [],
    playtime: {
      total_seconds: 0,
      session_count: 0,
      last_session_start: null,
      last_session_duration_sec: null,
      last_played: null,
    },
    device_id: "dev-1",
    last_sync_check_at: null,
    ...overrides,
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    vi.mocked(handleConflicts).mockReset();
  });

  // Render the button and drive it into the conflict ("Resolve Conflict") state
  // via the backend push (a romm_data_changed DOM event carrying has_conflict).
  async function renderInConflict(): Promise<ReturnType<typeof render>> {
    mockCachedDetail();
    const utils = render(<CustomPlayButton appId={100} />);
    await utils.findByText("Play");
    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: 42, has_conflict: true } }),
      );
    });
    await utils.findByText("Resolve Conflict");
    return utils;
  }

  async function clickResolve(utils: ReturnType<typeof render>): Promise<void> {
    const resolveBtn = await utils.findByText("Resolve Conflict");
    await act(async () => {
      resolveBtn.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("reads the conflict via getSaveStatus (not the act-capable preLaunchSync) and hands it to the modal", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ conflicts: [conflict()] }));
    vi.mocked(handleConflicts).mockResolvedValue("resolved");

    const utils = await renderInConflict();
    await clickResolve(utils);

    // The resolve path READ the status and did NOT re-run preLaunchSync, so no
    // other file in the ROM could be uploaded/downloaded as a side effect.
    expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(42);
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(handleConflicts)).toHaveBeenCalledWith([conflict()]);
  });

  it("resolving the conflict dispatches romm_data_changed and returns to play", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ conflicts: [conflict()] }));
    vi.mocked(handleConflicts).mockResolvedValue("resolved");
    const dataChanged = vi.fn();
    globalThis.addEventListener("romm_data_changed", dataChanged);

    const utils = await renderInConflict();
    // Drop the has_conflict push that drove us into conflict; only the resolve
    // dispatch should register from here on.
    dataChanged.mockClear();
    await clickResolve(utils);

    expect(vi.mocked(handleConflicts)).toHaveBeenCalledWith([conflict()]);
    // Resolved → sibling-refresh dispatched, button back to Play.
    expect(dataChanged).toHaveBeenCalled();
    await utils.findByText("Play");
    expect(utils.queryByText("Resolve Conflict")).toBeNull();

    globalThis.removeEventListener("romm_data_changed", dataChanged);
  });

  it("cancelling the conflict modal keeps the button in the conflict state", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ conflicts: [conflict()] }));
    vi.mocked(handleConflicts).mockResolvedValue("cancel");
    const dataChanged = vi.fn();
    globalThis.addEventListener("romm_data_changed", dataChanged);

    const utils = await renderInConflict();
    dataChanged.mockClear();
    await clickResolve(utils);

    expect(vi.mocked(handleConflicts)).toHaveBeenCalledWith([conflict()]);
    // Cancel → still in conflict (Resolve button present), no drop-to-play
    // sibling-refresh dispatched.
    expect(dataChanged).not.toHaveBeenCalled();
    await utils.findByText("Resolve Conflict");

    globalThis.removeEventListener("romm_data_changed", dataChanged);
  });

  it("an empty conflicts list (resolved elsewhere) returns to play WITHOUT opening the modal", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus({ conflicts: [] }));

    const utils = await renderInConflict();
    await clickResolve(utils);

    // No conflict left → the resolution modal never opened, so no
    // resolveSyncConflict act was issued; the button settles back to Play.
    expect(vi.mocked(handleConflicts)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.resolveSyncConflict)).not.toHaveBeenCalled();
    await utils.findByText("Play");
    expect(utils.queryByText("Resolve Conflict")).toBeNull();
  });

  it("an absent conflicts field is treated as resolved → back to play", async () => {
    // getSaveStatus with no conflicts key at all (conflicts is optional).
    vi.mocked(backend.getSaveStatus).mockResolvedValue(saveStatus());

    const utils = await renderInConflict();
    await clickResolve(utils);

    expect(vi.mocked(handleConflicts)).not.toHaveBeenCalled();
    await utils.findByText("Play");
  });

  it("server_query_failed keeps the button in conflict and toasts (does NOT silently proceed)", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue(
      saveStatus({ server_query_failed: true, server_query_reason: "server_unreachable", conflicts: [] }),
    );
    const dataChanged = vi.fn();
    globalThis.addEventListener("romm_data_changed", dataChanged);

    const utils = await renderInConflict();
    dataChanged.mockClear();
    await clickResolve(utils);

    // A connectivity blip must not masquerade as "resolved": stay in conflict,
    // toast, never open the modal, never dispatch a sibling-refresh.
    expect(vi.mocked(handleConflicts)).not.toHaveBeenCalled();
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Couldn't reach server") }),
    );
    expect(dataChanged).not.toHaveBeenCalled();
    await utils.findByText("Resolve Conflict");

    globalThis.removeEventListener("romm_data_changed", dataChanged);
  });

  it("server_query_failed with an unreachable reason DOES drive the store offline", async () => {
    setRommConnectionState("connected");
    vi.mocked(backend.getSaveStatus).mockResolvedValue(
      saveStatus({ server_query_failed: true, server_query_reason: "server_unreachable", conflicts: [] }),
    );

    const utils = await renderInConflict();
    await clickResolve(utils);

    // Post-catch state: the global store actually flipped, and the copy still
    // names the connection — that IS the cause on this branch.
    expect(getRommConnectionState()).toBe("offline");
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Couldn't reach server") }),
    );
    await utils.findByText("Resolve Conflict");
  });

  it("server_query_failed with a not_found reason leaves the store ALONE and does not blame the connection", async () => {
    setRommConnectionState("connected");
    vi.mocked(backend.getSaveStatus).mockResolvedValue(
      saveStatus({ server_query_failed: true, server_query_reason: "not_found", conflicts: [] }),
    );

    const utils = await renderInConflict();
    await clickResolve(utils);

    // The #1570 defect: a definitive 404 is the server ANSWERING, so feeding
    // the global store off the bare flag blacked out the whole UI. The toast
    // must not assert reachability either, nor claim the saves are gone — the
    // 404 can be the device registration rather than the ROM.
    expect(getRommConnectionState()).toBe("connected");
    const toastCalls = vi.mocked(toaster.toast).mock.calls;
    const body = toastCalls[toastCalls.length - 1]?.[0].body ?? "";
    expect(body).not.toMatch(/couldn't reach|unreachable|not reachable|offline/i);
    expect(body).not.toMatch(/no saves|has no save|without saves/i);
    // Still refuses to claim the conflict was resolved.
    expect(vi.mocked(handleConflicts)).not.toHaveBeenCalled();
    await utils.findByText("Resolve Conflict");
  });

  it("a thrown getSaveStatus keeps the button in conflict and toasts", async () => {
    vi.mocked(backend.getSaveStatus).mockRejectedValue(new Error("network down"));

    const utils = await renderInConflict();
    await clickResolve(utils);

    // Post-catch state: toast surfaced and the button stayed on Resolve Conflict.
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Couldn't reach server") }),
    );
    await utils.findByText("Resolve Conflict");
  });

  it("a prune-active getSaveStatus failure keeps the visible conflict", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValue({
      success: false,
      reason: "prune_active",
      message: "Cleanup is active.",
    });

    const utils = await renderInConflict();
    await act(async () => {
      (await utils.findByText("Resolve Conflict")).click();
    });

    expect(vi.mocked(handleConflicts)).not.toHaveBeenCalled();
    expect(toaster.toast).toHaveBeenCalledWith({ title: "Tender", body: "Cleanup is active." });
    await utils.findByText("Resolve Conflict");
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

  it("no launch target → toast explaining the files were kept, no launch (#1652)", async () => {
    // A PS3 title downloaded as a .pkg installer: on disk, recorded, but nothing
    // RPCS3 can boot. The gate blocks before any save work and the press must
    // say so — a silent bail would read as a dead button.
    vi.mocked(backend.getInstalledRom).mockResolvedValue({
      rom_id: 42,
      file_name: "Puppeteer.pkg",
      file_path: "/roms/ps3/Puppeteer/Puppeteer.pkg",
      system: "ps3",
      platform_slug: "ps3",
      installed_at: "2026-01-01T00:00:00Z",
      launchable: false,
    });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });

    await clickPlay();

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "Tender",
      body: "This download has no file the emulator can launch. The files are on disk — see the game page.",
    });
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    // Blocked before the save-sync work — no upload for a session that never starts.
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
  });

  it("a launchable install passes the launch-target step and launches", async () => {
    vi.mocked(backend.getInstalledRom).mockResolvedValue({
      rom_id: 42,
      file_name: "game.chd",
      file_path: "/roms/psx/game.chd",
      system: "psx",
      platform_slug: "psx",
      installed_at: "2026-01-01T00:00:00Z",
      launchable: true,
    });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });

    await clickPlay();

    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
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
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue({
      success: true,
      app_id: 100,
      launch_options: RELAUNCH_COMMAND,
      prune_lease_token: "launch-lease",
    });

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

    // Post-catch state: the failure was logged with the shared-helper message
    // (carrying the "CustomPlayButton" context) AND the launch still fired
    // (best-effort — a failed re-confirm is no worse than today).
    await waitFor(() =>
      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("CustomPlayButton: launch_options re-confirm failed"),
      ),
    );
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));

    logSpy.mockRestore();
  });

  it("a hung getRomRelaunchOptions aborts launch after the timeout and restores Play", async () => {
    // The Decky callable bridge can hang forever on a wedged backend. The fetch
    // is bounded by a 3s Promise.race; on timeout the launch is aborted and the
    // button must not stay stuck on "Launching…".
    // RTL's findBy* deadlocks under fake timers, so render + settle to "Play"
    // under REAL timers, then switch to fake timers right before the click so the
    // 3s re-confirm timeout fires without a real wait (kept fast).
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    // Never resolves — simulates a wedged backend / hung bridge.
    vi.mocked(backend.getRomRelaunchOptions).mockReturnValue(new Promise<never>(() => {}));

    try {
      mockCachedDetail();
      const { findByText, getByText } = render(<CustomPlayButton appId={100} />);
      const playBtn = await findByText("Play");

      vi.useFakeTimers();
      await act(async () => {
        playBtn.click();
        // Let the gate chain schedule the re-confirm timeout before advancing it.
        for (let index = 0; index < 12; index++) await Promise.resolve();
      });
      expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });

      // The hung fetch timed out → no set and no launch. Returning to Play proves
      // the current component is not trapped in its optimistic launching state.
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("CustomPlayButton: launch_options re-confirm timed out"),
      );
      expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
      expect(getByText("Play")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
      logSpy.mockRestore();
    }
  });

  it("plugin teardown while relaunch options are pending releases the late token and never calls RunGame", async () => {
    let resolveFetch!: (value: Awaited<ReturnType<typeof backend.getRomRelaunchOptions>>) => void;
    vi.mocked(backend.getRomRelaunchOptions).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });

    try {
      await clickPlay();
      await waitFor(() => expect(backend.getRomRelaunchOptions).toHaveBeenCalledWith(42));
      await releaseAllPruneLeases();
      resolveFetch({
        success: true,
        app_id: 100,
        launch_options: RELAUNCH_COMMAND,
        prune_lease_token: "late-plugin-launch-lease",
      });
      await act(async () => {
        for (let index = 0; index < 8; index++) await Promise.resolve();
      });

      expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("late-plugin-launch-lease");
      expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
      expect(SteamClient.Apps.RunGame).not.toHaveBeenCalled();
    } finally {
      mountPruneLeasePlugin();
    }
  });
});

// ---------------------------------------------------------------------------
// #1313 — the state-aware Resume button. When the game is already running the
// button renders "Resume" (top precedence over install/conflict/download) and
// brings the live session to the foreground via SteamUIStore navigation
// (SetRunningApp + NavigateToRunningApp) — Steam's own gamescope "Resume Game"
// path, NOT the pre-launch sync funnel and NOT the desktop-only RaiseWindowForGame
// (which silently no-ops in Game Mode). Detection is reactive: seeded at mount from
// isSessionActive(romId)/isAppRunning and flipped live by the romm_session_changed
// DOM event. The #1148/#1308 already-running guards stay intact as backstops.
// ---------------------------------------------------------------------------
describe("CustomPlayButton — state-aware Resume (#1313)", () => {
  let setRunningApp: ReturnType<typeof vi.fn>;
  let navigateToRunningApp: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
    // Detection defaults: not running (overridden per test).
    vi.mocked(isSessionActive).mockReturnValue(false);
    vi.mocked(isAppRunning).mockReturnValue(false);
    // Gate predecessors so the self-heal fall-through can reach the full funnel.
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 0,
      errors: [],
      conflicts: [],
    });
    // dispatchLaunch re-confirms launch_options first; keep that fetch fast (a
    // later describe leaves it hanging and clearAllMocks doesn't reset the impl).
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue(null);
    // SteamUIStore is the foreground path: SetRunningApp + NavigateToRunningApp.
    // Fresh spies each test so call assertions are clean; RunGame is the
    // launch/self-heal sink, appStore resolves a stable gameId.
    setRunningApp = vi.fn();
    navigateToRunningApp = vi.fn();
    vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });
    vi.stubGlobal("SteamUIStore", { SetRunningApp: setRunningApp, NavigateToRunningApp: navigateToRunningApp });
    vi.stubGlobal("appStore", {
      GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })),
      allApps: [],
    });
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
    });
  });

  it("renders Resume (not Play) and foregrounds via SteamUIStore when this rom is the live session — no gate/sync/RunGame", async () => {
    vi.mocked(isSessionActive).mockReturnValue(true);

    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);

    // Overlay wins: Resume shows, Play does not.
    await findByText("Resume");
    expect(queryByText("Play")).toBeNull();

    const resumeBtn = await findByText("Resume");
    await act(async () => {
      resumeBtn.click();
    });

    // Foreground the live session via Steam's own resume path (select + navigate)…
    await waitFor(() => expect(setRunningApp).toHaveBeenCalledWith(100));
    expect(navigateToRunningApp).toHaveBeenCalled();
    // …and NEVER the launch funnel: no gate op, no sync, no RunGame, no route fallback.
    expect(vi.mocked(backend.isSaveTrackingConfigured)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
    expect(vi.mocked(Navigation.Navigate)).not.toHaveBeenCalled();
  });

  it("renders Resume and foregrounds when a running-app source reports the appId — no gate/sync/RunGame", async () => {
    vi.mocked(isSessionActive).mockReturnValue(false);
    vi.mocked(isAppRunning).mockReturnValue(true);

    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Resume");
    expect(queryByText("Play")).toBeNull();

    const resumeBtn = await findByText("Resume");
    await act(async () => {
      resumeBtn.click();
    });

    // Seeded via isAppRunning(appId) — proves the running-app detection branch.
    expect(vi.mocked(isAppRunning)).toHaveBeenCalledWith(100);
    await waitFor(() => expect(setRunningApp).toHaveBeenCalledWith(100));
    expect(navigateToRunningApp).toHaveBeenCalled();
    expect(vi.mocked(backend.isSaveTrackingConfigured)).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("renders Play (overlay inert) when nothing is running at mount", async () => {
    // Defaults: no live session, isAppRunning false.
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    expect(queryByText("Resume")).toBeNull();
  });

  it("flips to Resume when a session-start event for this rom arrives", async () => {
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: true, appId: 100, romId: 42 } }),
      );
    });

    await findByText("Resume");
    expect(queryByText("Play")).toBeNull();
  });

  it("flips back to Play when the session-stop event for this rom arrives", async () => {
    vi.mocked(isSessionActive).mockReturnValue(true);

    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Resume");

    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: false, appId: 100, romId: 42 } }),
      );
    });

    await findByText("Play");
    expect(queryByText("Resume")).toBeNull();
  });

  it("resets a stuck Launching state when the session for this rom ends without a remount", async () => {
    // Desktop windowed BPM never remounts the page after a game exits, so the
    // session-end event is the only reset path out of "Launching..." there
    // (Game Mode remounts and re-inits instead).
    mockCachedDetail();
    const { findByText } = render(<CustomPlayButton appId={100} />);
    const playBtn = await findByText("Play");

    // The click leaves the underlying state at "launching"; the running
    // overlay shows Resume on top of it while the session lives.
    await act(async () => {
      playBtn.click();
    });
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: true, appId: 100, romId: 42 } }),
      );
    });
    await findByText("Resume");

    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: false, appId: 100, romId: 42 } }),
      );
    });

    // Non-vacuous: pre-fix the overlay dropped and exposed the stuck
    // "Launching..." state — the button must fall back to Play instead.
    expect(await findByText("Play")).toBeInTheDocument();
  });

  it("ignores a session event for a different rom", async () => {
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: true, appId: 999, romId: 999 } }),
      );
    });

    // Mismatched romId — the overlay never flips.
    expect(await findByText("Play")).toBeInTheDocument();
    expect(queryByText("Resume")).toBeNull();
  });

  it("removes the exact romm_session_changed handler on unmount", async () => {
    // globalThis listeners aren't tracked by the decky harness, so spy the real
    // add/remove to prove the []-effect cleanup removed the SAME handler ref.
    const addSpy = vi.spyOn(globalThis, "addEventListener");
    const removeSpy = vi.spyOn(globalThis, "removeEventListener");

    const { unmount } = render(<CustomPlayButton appId={100} />);
    await waitFor(() => expect(vi.mocked(getCachedGameDetail)).toHaveBeenCalled());

    // Capture the exact handler the effect registered for romm_session_changed.
    const addCall = addSpy.mock.calls.find(([type]) => type === "romm_session_changed");
    expect(addCall).toBeDefined();
    const handler = addCall![1];

    unmount();

    // Non-vacuous: the cleanup removed romm_session_changed with that same ref.
    expect(removeSpy).toHaveBeenCalledWith("romm_session_changed", handler);

    addSpy.mockRestore();
    removeSpy.mockRestore();
  });

  it("self-heals a stale overlay: falls through to the launch funnel when nothing is actually running", async () => {
    // Seed Resume via the session-start EVENT while the live sources stay false
    // (isSessionActive false, isAppRunning false), so the liveness gate in
    // handleResumeGame sees nothing running and falls through to handlePlay — the
    // already-running guard there is inert, so the FULL funnel runs (self-heal).
    const { findByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: true, appId: 100, romId: 42 } }),
      );
    });
    const resumeBtn = await findByText("Resume");

    await act(async () => {
      resumeBtn.click();
    });

    // Nothing running → cleared overlay → normal funnel ran (pre-launch sync) → launched.
    await waitFor(() => expect(vi.mocked(backend.preLaunchSync)).toHaveBeenCalledWith(42));
    await waitFor(() => expect(vi.mocked(SteamClient.Apps.RunGame)).toHaveBeenCalledWith("gid-1", "", -1, 100));
    // The foreground path was NOT taken — no SteamUIStore selection, no route nav.
    expect(setRunningApp).not.toHaveBeenCalled();
    expect(navigateToRunningApp).not.toHaveBeenCalled();
    expect(vi.mocked(Navigation.Navigate)).not.toHaveBeenCalled();
  });

  it("falls back to Navigation.Navigate('/apprunning') when NavigateToRunningApp is missing (API drift)", async () => {
    vi.mocked(isSessionActive).mockReturnValue(true);
    // Older SteamUI: SetRunningApp present, NavigateToRunningApp absent.
    vi.stubGlobal("SteamUIStore", { SetRunningApp: setRunningApp });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const resumeBtn = await findByText("Resume");

    await act(async () => {
      resumeBtn.click();
    });

    // Selection still runs; foregrounding falls back to the direct route nav.
    await waitFor(() => expect(vi.mocked(Navigation.Navigate)).toHaveBeenCalledWith("/apprunning"));
    expect(setRunningApp).toHaveBeenCalledWith(100);
    // Still not a launch: no gate, no sync, no RunGame.
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("falls back to Navigation.Navigate('/apprunning') when SteamUIStore.SetRunningApp throws (present-but-broken store)", async () => {
    vi.mocked(isSessionActive).mockReturnValue(true);
    // Present store, but SetRunningApp is a throwing getter/method — the exact
    // present-but-broken failure class this whole PR was born from. Without the
    // guard the async fn rejects, detach() swallows it, and the route fallback is
    // skipped → user stranded (no foreground, no backstop).
    const throwingSet = vi.fn(() => {
      throw new Error("SetRunningApp exploded");
    });
    vi.stubGlobal("SteamUIStore", { SetRunningApp: throwingSet, NavigateToRunningApp: navigateToRunningApp });

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const resumeBtn = await findByText("Resume");

    await act(async () => {
      resumeBtn.click();
    });

    // The throw is swallowed and the last-resort route nav still foregrounds…
    await waitFor(() => expect(vi.mocked(Navigation.Navigate)).toHaveBeenCalledWith("/apprunning"));
    // …and the catch is non-vacuously observable: the debug fallback line was logged.
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("SteamUIStore threw, falling back to Navigate"),
    );
    // SetRunningApp was attempted (we entered the guarded block); the throw stopped
    // the navigate-to-running before it ran; and it's still not a launch.
    expect(throwingSet).toHaveBeenCalledWith(100);
    expect(navigateToRunningApp).not.toHaveBeenCalled();
    expect(vi.mocked(backend.preLaunchSync)).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });

  it("navigates directly when SteamUIStore is absent (extreme API drift)", async () => {
    vi.mocked(isSessionActive).mockReturnValue(true);
    // No SteamUIStore at all — the last-resort route nav still foregrounds.
    vi.stubGlobal("SteamUIStore", undefined);

    const { findByText } = render(<CustomPlayButton appId={100} />);
    const resumeBtn = await findByText("Resume");

    await act(async () => {
      resumeBtn.click();
    });

    await waitFor(() => expect(vi.mocked(Navigation.Navigate)).toHaveBeenCalledWith("/apprunning"));
    // SetRunningApp is unreachable (no store), and it's still not a launch.
    expect(setRunningApp).not.toHaveBeenCalled();
    expect(vi.mocked(SteamClient.Apps.RunGame)).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Stop Game — the running overlay's chevron action. Steam cannot terminate these
// games (the shortcut execs `flatpak run`, whose portal-started sandbox is not
// under Steam's reaper, so TerminateApp is a proven on-device no-op), so the
// kill is a backend callable. It is destructive and unconfirmable after the
// fact, hence the confirm modal in front of it.
// ---------------------------------------------------------------------------
describe("CustomPlayButton — Stop Game", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(showContextMenu).mockReset();
    vi.mocked(toaster.toast).mockReset();
    // Live session for appId 100 / rom 42 → the running overlay renders.
    vi.mocked(isSessionActive).mockReturnValue(true);
    vi.mocked(isAppRunning).mockReturnValue(true);
    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 42,
      rom_name: "Test ROM",
      installed: true,
    });
    // Default: the user confirms, the backend reports a clean stop.
    vi.mocked(showStopGameModal).mockResolvedValue(true);
    vi.mocked(backend.stopRunningGame).mockResolvedValue({ success: true, stopped: 2, force_killed: 0 });
    vi.stubGlobal("SteamUIStore", { SetRunningApp: vi.fn(), NavigateToRunningApp: vi.fn() });
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: vi.fn(() => ({ GetGameID: () => "gid-1" })), allApps: [] });
  });

  // Open the running-actions chevron and render the <Menu> the showContextMenu
  // spy captured, so its MenuItem buttons are clickable. Queries are scoped to
  // this menu's own container via `within`: several tests open the menu more
  // than once, and RTL's render-bound queries search all of document.body, so
  // an unscoped lookup would match every menu rendered so far.
  function openRunningMenu(button: HTMLElement): ReturnType<typeof within> {
    act(() => {
      button.click();
    });
    expect(showContextMenu).toHaveBeenCalled();
    const calls = vi.mocked(showContextMenu).mock.calls;
    const { container } = render(calls[calls.length - 1]![0] as ReactElement);
    return within(container);
  }

  async function renderRunningWithMenu(): Promise<{
    utils: ReturnType<typeof render>;
    menu: ReturnType<typeof within>;
  }> {
    const utils = render(<CustomPlayButton appId={100} />);
    await utils.findByText("Resume");
    const chevron = await utils.findByLabelText("Game actions");
    return { utils, menu: openRunningMenu(chevron) };
  }

  it("offers Stop Game in the running overlay's chevron menu", async () => {
    const { menu } = await renderRunningWithMenu();

    expect(await menu.findByText("Stop Game")).toBeInTheDocument();
  });

  it("confirms first, then calls the backend and clears the running overlay", async () => {
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(showStopGameModal).toHaveBeenCalledTimes(1);
    expect(backend.stopRunningGame).toHaveBeenCalledTimes(1);
    // Post-state: the overlay came down and the underlying Play button shows.
    expect(await utils.findByText("Play")).toBeInTheDocument();
    expect(utils.queryByText("Resume")).toBeNull();
  });

  it("passes the rom id so the backend stops THIS game's instance", async () => {
    // RetroDECK can have several live instances at once; without the rom id the
    // backend cannot tell them apart and ends all of them (#1619).
    const { menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(backend.stopRunningGame).toHaveBeenCalledWith(42);
  });

  it("does not call the backend before the rom id has resolved", async () => {
    // No rom id means no instance to identify, and "stop whichever is running"
    // is the very bug the argument exists to prevent.
    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_name: "Test ROM", installed: true });
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(backend.stopRunningGame).not.toHaveBeenCalled();
    // Nothing to confirm either — the destructive prompt is never raised for an
    // action that cannot be carried out.
    expect(showStopGameModal).not.toHaveBeenCalled();
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Couldn't stop the game — still loading its details",
    });
    // Nothing was stopped, so Resume must stay reachable.
    expect(await utils.findByText("Resume")).toBeInTheDocument();
  });

  it("keeps the overlay up when the backend matched no instance to this game", async () => {
    // `game_not_running`: RetroDECK is alive but nothing of it is running this
    // ROM, so the backend signalled nothing. The game may well still be running
    // (a sandbox path the match could not tie back), so — unlike `not_running` —
    // the overlay must NOT come down: Resume has to stay reachable.
    vi.mocked(backend.stopRunningGame).mockResolvedValue({
      success: false,
      reason: "game_not_running",
      message: "RetroDECK is running, but not this game — nothing was stopped.",
    });
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "RetroDECK is running, but not this game — nothing was stopped.",
    });
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("stop_running_game refused for appId=100 — reason=game_not_running"),
    );
    expect(await utils.findByText("Resume")).toBeInTheDocument();
    expect(utils.queryByText("Play")).toBeNull();
  });

  it("does NOT call the backend when the confirm is cancelled, and leaves the overlay up", async () => {
    vi.mocked(showStopGameModal).mockResolvedValue(false);
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(showStopGameModal).toHaveBeenCalledTimes(1);
    expect(backend.stopRunningGame).not.toHaveBeenCalled();
    // The game is untouched, so Resume must stay reachable.
    expect(await utils.findByText("Resume")).toBeInTheDocument();
    expect(utils.queryByText("Play")).toBeNull();
  });

  it("self-heals a stale overlay without confirming or calling the backend", async () => {
    // Overlay seeded by the session-start EVENT while the live sources say
    // nothing is running — the same stale-overlay case handleResumeGame heals.
    vi.mocked(isSessionActive).mockReturnValue(false);
    vi.mocked(isAppRunning).mockReturnValue(false);

    const utils = render(<CustomPlayButton appId={100} />);
    await utils.findByText("Play");
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: true, appId: 100, romId: 42 } }),
      );
    });
    await utils.findByText("Resume");

    const chevron = await utils.findByLabelText("Game actions");
    const menu = openRunningMenu(chevron);
    const stopItem = await menu.findByText("Stop Game");
    await act(async () => {
      stopItem.click();
      await Promise.resolve();
    });

    // Nothing to kill → no prompt, no callable, and the overlay is cleared.
    expect(showStopGameModal).not.toHaveBeenCalled();
    expect(backend.stopRunningGame).not.toHaveBeenCalled();
    expect(await utils.findByText("Play")).toBeInTheDocument();
    expect(utils.queryByText("Resume")).toBeNull();
  });

  it("clears the overlay when the backend reports nothing was running", async () => {
    // The backend found no live RetroDECK process — the stale-overlay case one
    // layer down. The game is not running, so the overlay must still come down.
    vi.mocked(backend.stopRunningGame).mockResolvedValue({
      success: false,
      reason: "not_running",
      message: "No running game was found to stop.",
    });
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await utils.findByText("Play")).toBeInTheDocument();
    // Not an error the user has to act on — no toast.
    expect(vi.mocked(toaster.toast)).not.toHaveBeenCalled();
  });

  it("toasts and keeps the overlay up when the backend reports a real failure", async () => {
    vi.mocked(backend.stopRunningGame).mockResolvedValue({
      success: false,
      reason: "permission_denied",
      message: "Couldn't signal the emulator",
    });
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Couldn't signal the emulator",
    });
    // The game may well still be running — Resume must stay reachable.
    expect(await utils.findByText("Resume")).toBeInTheDocument();
    expect(utils.queryByText("Play")).toBeNull();
  });

  it("catches a rejected stop call: toasts, logs, and leaves the overlay up", async () => {
    vi.mocked(backend.stopRunningGame).mockRejectedValue(new Error("bridge died"));
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Non-vacuous post-catch state: the fallback toast fired, the failure was
    // logged, and the overlay is deliberately NOT cleared (no verdict was
    // reached, so the game may still be running).
    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Couldn't stop the game",
    });
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("stop_running_game threw for appId=100"),
    );
    expect(await utils.findByText("Resume")).toBeInTheDocument();
  });

  it("disables Stop Game and reads Stopping... while the call is outstanding", async () => {
    // The backend ladder can take seconds with nothing visible changing (the
    // emulator is flushing its save). Park the call so the in-flight render is
    // observable, then release it.
    let release: (v: { success: boolean; stopped: number; force_killed: number }) => void = () => {};
    vi.mocked(backend.stopRunningGame).mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Reopening the chevron mid-flight shows the disabled, relabelled item.
    const chevron = await utils.findByLabelText("Game actions");
    const pendingMenu = openRunningMenu(chevron);
    const pendingItem = await pendingMenu.findByText("Stopping...");
    expect(pendingItem).toBeInTheDocument();
    expect(pendingMenu.queryByText("Stop Game")).toBeNull();
    expect(pendingItem.closest("button")).toBeDisabled();

    await act(async () => {
      release({ success: true, stopped: 1, force_killed: 0 });
      await Promise.resolve();
      await Promise.resolve();
    });

    // Post-state: the stop landed and the overlay came down.
    expect(await utils.findByText("Play")).toBeInTheDocument();
  });

  it("does not fire a second backend stop while one is in flight", async () => {
    // A second stop request to a flushing emulator destroys the save it is
    // writing, so the in-flight press must not reach the backend at all.
    let release: (v: { success: boolean; stopped: number; force_killed: number }) => void = () => {};
    vi.mocked(backend.stopRunningGame).mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
    });
    // Press the ORIGINAL (pre-flag) menu item again — the render-level disable
    // never saw this node, so only the in-flight guard can stop it.
    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Exactly one backend call, and the second press never re-confirmed either.
    expect(backend.stopRunningGame).toHaveBeenCalledTimes(1);
    expect(showStopGameModal).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("a stop is already in flight"));

    await act(async () => {
      release({ success: true, stopped: 1, force_killed: 0 });
      await Promise.resolve();
    });
    expect(await utils.findByText("Play")).toBeInTheDocument();
  });

  it("re-enables Stop Game after a failed stop so it can be retried", async () => {
    vi.mocked(backend.stopRunningGame).mockRejectedValue(new Error("bridge died"));
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Non-vacuous: the flag was released in the finally, so the item is back to
    // "Stop Game" and enabled — a leaked flag would strand it on "Stopping...".
    const chevron = await utils.findByLabelText("Game actions");
    const retryMenu = openRunningMenu(chevron);
    const retryItem = await retryMenu.findByText("Stop Game");
    expect(retryItem.closest("button")).not.toBeDisabled();
    expect(retryMenu.queryByText("Stopping...")).toBeNull();
  });

  it("does not strand Stopping... when the confirm is cancelled", async () => {
    vi.mocked(showStopGameModal).mockResolvedValue(false);
    const { utils, menu } = await renderRunningWithMenu();
    const stopItem = await menu.findByText("Stop Game");

    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // The claim is taken only after the confirm resolves true, so an abandoned
    // modal leaves the item untouched.
    const chevron = await utils.findByLabelText("Game actions");
    const reopened = openRunningMenu(chevron);
    expect(await reopened.findByText("Stop Game")).toBeInTheDocument();
    expect(reopened.queryByText("Stopping...")).toBeNull();
  });

  it("resets a stuck Launching state on a successful stop", async () => {
    // The session-start path sets isRunning but leaves state === "launching";
    // clearing only the overlay would expose a stale "Launching..." label.
    vi.mocked(isSessionActive).mockReturnValue(false);
    vi.mocked(isAppRunning).mockReturnValue(false);
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "default" });
    vi.mocked(backend.checkCoreChange).mockResolvedValue({ changed: false });
    vi.mocked(backend.probeReachability).mockResolvedValue({ online: true });
    vi.mocked(backend.preLaunchSync).mockResolvedValue({
      success: true,
      message: "",
      synced: 0,
      errors: [],
      conflicts: [],
    });
    vi.mocked(backend.getRomRelaunchOptions).mockResolvedValue(null);
    vi.stubGlobal("SteamClient", { Apps: { RunGame: vi.fn() } });

    const utils = render(<CustomPlayButton appId={100} />);
    const playBtn = await utils.findByText("Play");
    await act(async () => {
      playBtn.click();
    });
    // The launch left the state at "launching"; the session-start event raises
    // the overlay on top of it, and the live sources now report the session.
    vi.mocked(isSessionActive).mockReturnValue(true);
    vi.mocked(isAppRunning).mockReturnValue(true);
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_session_changed", { detail: { running: true, appId: 100, romId: 42 } }),
      );
    });
    await utils.findByText("Resume");

    const chevron = await utils.findByLabelText("Game actions");
    const menu = openRunningMenu(chevron);
    const stopItem = await menu.findByText("Stop Game");
    await act(async () => {
      stopItem.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    // Non-vacuous: without the launching reset the overlay drops to the stuck
    // "Launching..." label instead of Play.
    expect(await utils.findByText("Play")).toBeInTheDocument();
    expect(utils.queryByText("Launching...")).toBeNull();
  });
});

describe("CustomPlayButton — version switch (#1298)", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    // Prior describes leave the running-overlay stubs live (Resume button) —
    // reset to "nothing running" so the button lands on Play/Download.
    vi.mocked(isSessionActive).mockReturnValue(false);
    vi.mocked(isAppRunning).mockReturnValue(false);
  });

  const dispatchVersionSwitched = (appId: number, romId: number) =>
    // Async act: the handler re-reads getCachedGameDetail (async) before setState,
    // so flush a microtask so the state settles inside act.
    act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", {
          detail: { type: "version_switched", app_id: appId, rom_id: romId },
        }),
      );
      await Promise.resolve();
    });

  it("flips Play → Download when the switched-to version is not installed", async () => {
    // Mount installed (Play), then the switch re-reads the cached detail and finds
    // the newly-bound version uninstalled.
    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 42, rom_name: "USA", installed: true });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 7, rom_name: "JPN", installed: false });
    await dispatchVersionSwitched(100, 7);

    await findByText("Download");
    expect(queryByText("Play")).toBeNull();
  });

  it("flips Download → Play when the switched-to version is installed", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 42, rom_name: "USA", installed: false });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");

    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 7, rom_name: "JPN", installed: true });
    await dispatchVersionSwitched(100, 7);

    await findByText("Play");
    expect(queryByText("Download")).toBeNull();
  });

  it("ignores a version_switched event for a different appId", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 42, rom_name: "USA", installed: true });
    const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
    await findByText("Play");

    // A switch on another game's appId must not re-read or re-derive this button.
    vi.mocked(getCachedGameDetail).mockClear();
    await dispatchVersionSwitched(999, 7);

    expect(await findByText("Play")).toBeInTheDocument();
    expect(queryByText("Download")).toBeNull();
    expect(vi.mocked(getCachedGameDetail)).not.toHaveBeenCalled();
  });

  it("logs an error and leaves the button unchanged when the rebound detail is not found", async () => {
    const logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    try {
      vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 42, rom_name: "USA", installed: true });
      const { findByText, queryByText } = render(<CustomPlayButton appId={100} />);
      await findByText("Play");

      // The rebound detail didn't resolve — warn (not silently drop) and keep the button.
      vi.mocked(getCachedGameDetail).mockResolvedValue({ found: false });
      await dispatchVersionSwitched(100, 7);

      expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("cached detail not found"));
      expect(await findByText("Play")).toBeInTheDocument();
      expect(queryByText("Download")).toBeNull();
    } finally {
      logErrorSpy.mockRestore();
    }
  });

  it("logs an error when the cached-detail re-read rejects (non-vacuous catch)", async () => {
    const logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    try {
      vi.mocked(getCachedGameDetail).mockResolvedValue({ found: true, rom_id: 42, rom_name: "USA", installed: true });
      const { findByText } = render(<CustomPlayButton appId={100} />);
      await findByText("Play");

      vi.mocked(getCachedGameDetail).mockRejectedValue(new Error("boom"));
      await dispatchVersionSwitched(100, 7);

      await waitFor(() =>
        expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("version_switched handler failed")),
      );
    } finally {
      logErrorSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// Rehydrated / active download button keeps its DARK remainder (fix/rehydrated-
// download-button-colors).
//
// ROOT CAUSE: the injected `.romm-btn-download:hover, .romm-btn-download.gpfocus`
// rule (styleInjector.ts) forces a bright-blue `!important` background. That blue
// is only correct for the IDLE Download button (whose baseBg is already blue). The
// SAME `.romm-btn-download` class is on the ACTIVE download button (downloading /
// paused / extracting), whose baseBg is a dark shade under a green progress fill —
// so a focused/hovered active button gets its dark unfilled remainder repainted
// bright Steam-blue, erasing the dark base (the pulse and fill are untouched). On
// remount the initial-focus grab (the 400ms `useEffect`) programmatically adds
// `.gpfocus` to the first button in the container — the active main button — so a
// REHYDRATED paused download reliably shows the blue remainder; the live path
// hides it only because Steam's gamepad focus has already moved to the pause /
// cancel action by the time the transfer is paused.
//
// FIX: scope the blue focus/hover highlight to an idle-only `romm-btn-download-idle`
// class, added to the button only when it is NOT in an active-download render. The
// active button keeps `romm-btn-download` (position/overflow for the fill) but is
// no longer a target of the blue rule, so its dark inline baseBg stands.
//
// Harness note: the @decky/ui mock drops the DialogButton inline `style` and
// `findSP` returns undefined (styleInjector injects nothing), so neither the
// dropped inline baseBg nor the injected blue is observable via computed style.
// The faithful mechanical proxy for "the blue rule can't reach this button" is the
// className it keys off: the idle button carries `romm-btn-download-idle`, the
// active button never does. The green fill and amber pulse ARE observable (a plain
// div and the Focusable's forwarded style) and are pinned alongside.
// ---------------------------------------------------------------------------
describe("CustomPlayButton — active-download button never takes the idle blue focus highlight", () => {
  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.startDownload).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.getDownloadQueue).mockResolvedValue({ downloads: [] });
  });

  // Rehydrate the button straight into an active-download render from the queue
  // (the #1124 path a page re-entry takes), then wait for it to settle. `status`
  // picks the paused vs still-running shape; the label proves the settle.
  async function renderRehydrated(status: "paused" | "downloading"): Promise<ReturnType<typeof render>> {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.getDownloadQueue).mockResolvedValue({
      downloads: [
        {
          rom_id: 42,
          rom_name: "Test ROM",
          platform_name: "PSP",
          file_name: "game.iso",
          status,
          progress: 0.3,
          bytes_downloaded: 300,
          total_bytes: 1000,
          resumable: true,
        },
      ],
    });
    const utils = render(<CustomPlayButton appId={100} />);
    await utils.findByText(status === "paused" ? "Paused" : "300 / 1000 B");
    return utils;
  }

  it("the idle Download button carries romm-btn-download-idle so it keeps the blue hover/focus highlight", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const { findByText, container } = render(<CustomPlayButton appId={100} />);
    await findByText("Download");

    // The idle button is the only intended target of the bright-blue focus/hover
    // rule (its baseBg is already blue). This assertion is what flips red→green
    // with the fix: the idle-only marker class did not exist before.
    const btn = container.querySelector(".romm-btn-download");
    expect(btn).not.toBeNull();
    expect(btn).toHaveClass("romm-btn-download-idle");
  });

  it("a rehydrated paused download keeps the dark-remainder shape with a green fill and amber pulse", async () => {
    const { container } = await renderRehydrated("paused");

    // Active button: structural class only, never the idle blue-focus target — so
    // a focus/hover cannot repaint its dark remainder bright blue (the device bug).
    const btn = container.querySelector(".romm-btn-download")!;
    expect(btn).toHaveClass("romm-btn-download");
    expect(btn).not.toHaveClass("romm-btn-download-idle");

    // Green progress fill at the frozen width (300/1000 = 30%), never the idle blue.
    const fill = container.querySelector(".romm-dl-fill") as HTMLElement;
    expect(fill.style.width).toBe("30%");
    expect(fill.getAttribute("style")).toContain("linear-gradient");
    expect(fill.getAttribute("style")).not.toContain("1a9fff");

    // Amber paused pulse on the active-group container (unaffected by the bug/fix).
    // The Focusable mock forwards `style` but drops `className`, so the container is
    // located by its data-testid rather than the romm-dl-active-group class.
    const group = container.querySelector('[data-testid="focusable"]') as HTMLElement;
    expect(group.style.getPropertyValue("--romm-pulse-color")).toContain("212,167,44");
  });

  it("a rehydrated running download keeps the dark-remainder shape with a green fill", async () => {
    const { container } = await renderRehydrated("downloading");

    const btn = container.querySelector(".romm-btn-download")!;
    expect(btn).toHaveClass("romm-btn-download");
    expect(btn).not.toHaveClass("romm-btn-download-idle");

    const fill = container.querySelector(".romm-dl-fill") as HTMLElement;
    expect(fill.style.width).toBe("30%");
    expect(fill.getAttribute("style")).toContain("linear-gradient");
    expect(fill.getAttribute("style")).not.toContain("1a9fff");
  });

  it("the live paused button renders the identical class/fill/pulse shape as the rehydrated one", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    const utils = render(<CustomPlayButton appId={100} />);
    const downloadBtn = await utils.findByText("Download");

    await act(async () => {
      downloadBtn.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    act(() => {
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", {
        rom_id: 42,
        rom_name: "Test ROM",
        platform_name: "PSP",
        file_name: "game.iso",
        status: "paused",
        progress: 0.3,
        bytes_downloaded: 300,
        total_bytes: 1000,
        resumable: true,
      });
    });
    await utils.findByText("Paused");

    const btn = utils.container.querySelector(".romm-btn-download")!;
    expect(btn).toHaveClass("romm-btn-download");
    expect(btn).not.toHaveClass("romm-btn-download-idle");
    const fill = utils.container.querySelector(".romm-dl-fill") as HTMLElement;
    expect(fill.style.width).toBe("30%");
    const group = utils.container.querySelector('[data-testid="focusable"]') as HTMLElement;
    expect(group.style.getPropertyValue("--romm-pulse-color")).toContain("212,167,44");
  });
});

describe("CustomPlayButton — content already on disk (#260)", () => {
  const OCCUPIED = {
    success: false as const,
    reason: "target_occupied" as const,
    message: "A file named 'game.z64' is already in place",
    existing: { name: "game.z64", path: "/roms/n64/game.z64", is_dir: false, size_bytes: 2048, modified_at: 0 },
    incoming: { name: "game.z64", size_bytes: 1024 },
    sizes_match: false,
    adoptable: true,
  };

  beforeEach(() => {
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.startDownload).mockReset();
    vi.mocked(backend.adoptExistingRom).mockReset();
    vi.mocked(showAdoptExistingModal).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockClear();
    vi.mocked(toaster.toast).mockClear();
  });

  it("labels the button 'Use Existing Files' when the cached stat found content", async () => {
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: true });
    const { findByText } = render(<CustomPlayButton appId={100} />);
    expect(await findByText("Use Existing Files")).toBeTruthy();
  });

  it("still says Download when nothing is in the way", async () => {
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: false });
    const { findByText } = render(<CustomPlayButton appId={100} />);
    expect(await findByText("Download")).toBeTruthy();
  });

  it("stops saying 'Use Existing Files' once the files are uninstalled", async () => {
    // The stat that set the flag ran at mount. An uninstall deletes exactly the
    // content it found, so the label has to go back — otherwise it names files
    // that are no longer there.
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: true });
    const utils = render(<CustomPlayButton appId={100} />);
    expect(await utils.findByText("Use Existing Files")).toBeTruthy();

    await act(async () => {
      globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 42 } }));
      await Promise.resolve();
    });

    expect(await utils.findByText("Download")).toBeTruthy();
  });

  it("stops saying 'Use Existing Files' when a transfer is cancelled", async () => {
    // A cancelled replace-download already removed a multi-file ROM's directory
    // at admission, so the stat behind the label is spent.
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: true });
    const utils = render(<CustomPlayButton appId={100} />);
    expect(await utils.findByText("Use Existing Files")).toBeTruthy();

    act(() => {
      emitDeckyEvent<[DownloadProgressEvent]>("download_progress", {
        rom_id: 42,
        rom_name: "Test ROM",
        platform_name: "PSX",
        file_name: "game.iso",
        status: "cancelled",
        progress: 0.4,
        bytes_downloaded: 400,
        total_bytes: 1000,
      });
    });

    expect(await utils.findByText("Download")).toBeTruthy();
  });

  it("stops saying 'Use Existing Files' when a download fails", async () => {
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: true });
    const utils = render(<CustomPlayButton appId={100} />);
    expect(await utils.findByText("Use Existing Files")).toBeTruthy();

    act(() => {
      emitDeckyEvent<[DownloadFailedEvent]>("download_failed", {
        rom_id: 42,
        rom_name: "Test ROM",
        platform_name: "PSX",
        error_message: "boom",
      });
    });

    expect(await utils.findByText("Download")).toBeTruthy();
  });

  it("a version switch takes the incoming ROM's occupancy, not the outgoing one's", async () => {
    // The outgoing version's answer says nothing about where the new one lives.
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: true });
    const utils = render(<CustomPlayButton appId={100} />);
    expect(await utils.findByText("Use Existing Files")).toBeTruthy();

    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 43,
      rom_name: "Other version",
      installed: false,
      target_path_occupied: false,
    });
    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "version_switched", app_id: 100, rom_id: 43 } }),
      );
      await Promise.resolve();
    });

    expect(await utils.findByText("Download")).toBeTruthy();
  });

  it("a version switch to an occupied ROM says so", async () => {
    mockCachedDetail({ rom_id: 42, installed: false, target_path_occupied: false });
    const utils = render(<CustomPlayButton appId={100} />);
    expect(await utils.findByText("Download")).toBeTruthy();

    vi.mocked(getCachedGameDetail).mockResolvedValue({
      found: true,
      rom_id: 43,
      rom_name: "Other version",
      installed: false,
      target_path_occupied: true,
    });
    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "version_switched", app_id: 100, rom_id: 43 } }),
      );
      await Promise.resolve();
    });

    expect(await utils.findByText("Use Existing Files")).toBeTruthy();
  });

  it("opens the dialog on a target_occupied refusal instead of toasting a failure", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload).mockResolvedValue(OCCUPIED);
    vi.mocked(showAdoptExistingModal).mockResolvedValue("cancel");
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(showAdoptExistingModal)).toHaveBeenCalledWith(42, OCCUPIED);
    expect(vi.mocked(toaster.toast)).not.toHaveBeenCalled();
    await utils.findByText("Use Existing Files");
  });

  it("replace re-runs the download with the replace flag set", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload)
      .mockResolvedValueOnce(OCCUPIED)
      .mockResolvedValueOnce({ success: true, message: "Download started" });
    vi.mocked(showAdoptExistingModal).mockResolvedValue("replace");
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(backend.startDownload).mock.calls).toEqual([
      [42, false],
      [42, true],
    ]);
  });

  it("cancel starts nothing", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload).mockResolvedValue(OCCUPIED);
    vi.mocked(showAdoptExistingModal).mockResolvedValue("cancel");
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(backend.startDownload)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(backend.adoptExistingRom)).not.toHaveBeenCalled();
  });

  it("adopt records the install and writes the launch command onto the shortcut", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload).mockResolvedValue(OCCUPIED);
    vi.mocked(showAdoptExistingModal).mockResolvedValue("adopt");
    vi.mocked(backend.adoptExistingRom).mockResolvedValue({
      success: true,
      message: "Using the files already on this device",
      file_path: "/roms/n64/game.z64",
      rom_dir: null,
      app_id: 100,
      launch_options: 'flatpak run … "/roms/n64/game.z64"',
      prune_lease_token: "adopt-token",
    });
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(backend.adoptExistingRom)).toHaveBeenCalledWith(42);
    expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(100, 'flatpak run … "/roms/n64/game.z64"');
    // The lease the backend issued for this Steam write is given back, not held
    // to its TTL — the bound branch is the only one that ever receives one.
    expect(vi.mocked(backend.releasePruneConflictLease)).toHaveBeenCalledWith("adopt-token");
    await utils.findByText("Play");
  });

  it("an unbound adopted ROM writes no launch options and holds no lease", async () => {
    // The backend guards acquisition — an unbound adopt is issued no token, so
    // there is nothing here to release. The token in this mock is the shape the
    // frontend must stay inert to if one ever arrives anyway: no Steam write,
    // and no lease taken out that nothing would give back.
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload).mockResolvedValue(OCCUPIED);
    vi.mocked(showAdoptExistingModal).mockResolvedValue("adopt");
    vi.mocked(backend.adoptExistingRom).mockResolvedValue({
      success: true,
      message: "ok",
      file_path: "/roms/n64/game.z64",
      rom_dir: null,
      app_id: null,
      launch_options: "",
      prune_lease_token: "stray-token",
    });
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    await utils.findByText("Play");
  });

  it("a refused adoption surfaces its message and leaves the button on the download state", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload).mockResolvedValue(OCCUPIED);
    vi.mocked(showAdoptExistingModal).mockResolvedValue("adopt");
    vi.mocked(backend.adoptExistingRom).mockResolvedValue({
      success: false,
      reason: "nothing_to_adopt",
      message: "The files are no longer there — nothing was adopted",
    });
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "The files are no longer there — nothing was adopted",
    });
    await utils.findByText("Use Existing Files");
  });

  it("a thrown adoption is surfaced rather than swallowed", async () => {
    mockCachedDetail({ rom_id: 42, installed: false });
    vi.mocked(backend.startDownload).mockResolvedValue(OCCUPIED);
    vi.mocked(showAdoptExistingModal).mockResolvedValue("adopt");
    vi.mocked(backend.adoptExistingRom).mockRejectedValue(new Error("bridge down"));
    const utils = render(<CustomPlayButton appId={100} />);
    const btn = await utils.findByText("Download");

    await act(async () => {
      btn.click();
    });

    expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith({
      title: "Tender",
      body: "Couldn't use the existing files — is RomM server running?",
    });
    await utils.findByText("Use Existing Files");
  });
});
