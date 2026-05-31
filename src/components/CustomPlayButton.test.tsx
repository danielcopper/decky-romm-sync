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
import { CustomPlayButton } from "./CustomPlayButton";
import { emitDeckyEvent, deckyEventListenerCount } from "../test-utils/decky-api-mock";
import type { CachedGameDetail } from "../api/backend";
import type { DownloadFailedEvent } from "../types";

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
