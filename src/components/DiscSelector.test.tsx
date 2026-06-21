/**
 * DiscSelector tests — driven through the `emitDeckyEvent` event-bus harness.
 *
 * The global `@decky/ui` stub renders `Dropdown` as a bare passthrough that
 * drops `rgOptions` / `selectedOption` / `onChange` / `renderButtonValue`, so
 * this file locally re-mocks `@decky/ui` to CAPTURE those props (Vitest's
 * per-file mock hoisting wins over the global stub). That lets the tests assert
 * the option set + the active badge label and drive the `onChange` callback
 * exactly as a real Dropdown selection would.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, waitFor, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { toaster } from "@decky/api";
import { DiscSelector } from "./DiscSelector";
import { emitDeckyEvent, deckyEventListenerCount } from "../test-utils/decky-api-mock";
import * as backend from "../api/backend";
import type { CachedGameDetail, DiscSelection } from "../api/backend";
import type { DownloadCompleteEvent } from "../types";

// --- Local @decky/ui mock: capture the Dropdown props ------------------------
interface DropdownOption {
  data: unknown;
  label: ReactNode;
}
interface DropdownProps {
  rgOptions?: DropdownOption[];
  selectedOption?: unknown;
  onChange?: (option: DropdownOption) => void;
  renderButtonValue?: (element: ReactNode) => ReactNode;
  menuLabel?: string;
}
const captured: { dropdowns: DropdownProps[] } = { dropdowns: [] };

/** The most recently rendered Dropdown's props (target is ES2020 — no `.at`). */
function lastDropdown(): DropdownProps {
  return captured.dropdowns[captured.dropdowns.length - 1]!;
}

vi.mock("@decky/ui", () => ({
  Dropdown: (p: DropdownProps) => {
    captured.dropdowns.push(p);
    // Render the badge face so the active label is queryable in the DOM.
    return createElement("div", { "data-testid": "disc-dropdown" }, p.renderButtonValue?.(null) as ReactNode);
  },
}));

// Cached-detail store: synchronous resolve so init settles in one tick.
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn<(appId: number) => Promise<CachedGameDetail>>(),
  invalidateCachedGameDetail: vi.fn(),
}));

// setLaunchOptionsConfirmed lives in steamShortcuts — mock it so a successful
// pick can be asserted without touching SteamClient.
vi.mock("../utils/steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn<(appId: number, value: string) => Promise<boolean>>().mockResolvedValue(true),
}));

import { getCachedGameDetail } from "../utils/cachedGameDetailStore";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";

function mockCachedDetail(overrides: Partial<CachedGameDetail> = {}): void {
  vi.mocked(getCachedGameDetail).mockResolvedValue({
    found: true,
    rom_id: 42,
    rom_name: "Final Fantasy VII",
    installed: true,
    ...overrides,
  });
}

// A representative multi-disc, m3u-default selection: 3 discs, no pin (follows
// the m3u playlist).
const m3uSelection: DiscSelection = {
  multi_disc: true,
  discs: [
    { filename: "ff7 (Disc 1).cue", label: "Disc 1", index: 1 },
    { filename: "ff7 (Disc 2).cue", label: "Disc 2", index: 2 },
    { filename: "ff7 (Disc 3).cue", label: "Disc 3", index: 3 },
  ],
  selected: null,
  default: { kind: "m3u", label: "All discs (m3u)", filename: "ff7.m3u" },
};

// A no-m3u multi-disc selection: disc 1 is the default (no separate follow
// entry), already pinned to disc 2.
const discDefaultSelection: DiscSelection = {
  multi_disc: true,
  discs: [
    { filename: "game (Disc 1).chd", label: "Disc 1", index: 1 },
    { filename: "game (Disc 2).chd", label: "Disc 2", index: 2 },
  ],
  selected: "game (Disc 2).chd",
  default: { kind: "disc", label: "Disc 1", filename: "game (Disc 1).chd" },
};

describe("DiscSelector — render gate", () => {
  beforeEach(() => {
    captured.dropdowns = [];
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.getDiscSelection).mockReset();
  });

  it("renders nothing for a single-disc (multi_disc:false) ROM", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue({ multi_disc: false });

    const { container } = render(<DiscSelector appId={100} />);

    await waitFor(() => {
      expect(vi.mocked(backend.getDiscSelection)).toHaveBeenCalledWith(42);
    });
    expect(container.querySelector('[data-testid="disc-dropdown"]')).toBeNull();
    expect(captured.dropdowns).toHaveLength(0);
  });

  it("renders nothing when the ROM is not found in the cache", async () => {
    vi.mocked(getCachedGameDetail).mockResolvedValue({ found: false });

    const { container } = render(<DiscSelector appId={100} />);

    // No rom_id resolved → getDiscSelection never called, nothing rendered.
    await Promise.resolve();
    expect(vi.mocked(backend.getDiscSelection)).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="disc-dropdown"]')).toBeNull();
  });

  it("renders nothing when the ROM is not installed", async () => {
    mockCachedDetail({ installed: false });

    const { container } = render(<DiscSelector appId={100} />);

    await Promise.resolve();
    await Promise.resolve();
    expect(vi.mocked(backend.getDiscSelection)).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="disc-dropdown"]')).toBeNull();
  });
});

describe("DiscSelector — multi-disc rendering", () => {
  beforeEach(() => {
    captured.dropdowns = [];
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.getDiscSelection).mockReset();
  });

  it("renders the dropdown with an m3u-default option followed by each disc", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);

    const { findByTestId } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    const props = lastDropdown();
    // First option follows the m3u default, then one per disc.
    expect(props.rgOptions?.map((o) => o.data)).toEqual([
      null,
      "ff7 (Disc 1).cue",
      "ff7 (Disc 2).cue",
      "ff7 (Disc 3).cue",
    ]);
    expect(props.rgOptions?.map((o) => o.label)).toEqual(["All discs (m3u)", "Disc 1", "Disc 2", "Disc 3"]);
    // No pin → selectedOption follows the m3u default (null).
    expect(props.selectedOption).toBeNull();
    expect(props.menuLabel).toBe("Disc");
  });

  it("renders disc-only options (disc 1 default) when there is no m3u and reflects the active pin", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(discDefaultSelection);

    const { findByText } = render(<DiscSelector appId={100} />);
    // Active badge label = pinned disc 2 (rendered in the face).
    await findByText("Disc 2");

    const props = lastDropdown();
    // No null "follow default" entry — the discs ARE the options.
    expect(props.rgOptions?.map((o) => o.data)).toEqual(["game (Disc 1).chd", "game (Disc 2).chd"]);
    expect(props.selectedOption).toBe("game (Disc 2).chd");
  });

  it("shows the m3u default label in the badge face when no disc is pinned", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);

    const { findByText } = render(<DiscSelector appId={100} />);
    expect(await findByText("All discs (m3u)")).toBeInTheDocument();
  });
});

describe("DiscSelector — selecting a disc", () => {
  beforeEach(() => {
    captured.dropdowns = [];
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.getDiscSelection).mockReset();
    vi.mocked(backend.selectDisc).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockClear();
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
    vi.mocked(toaster.toast).mockReset();
  });

  it("calls selectDisc then setLaunchOptionsConfirmed with the re-baked launch_options", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);
    vi.mocked(backend.selectDisc).mockResolvedValue({
      success: true,
      launch_options: "flatpak run net.retrodeck.retrodeck '/roms/ff7 (Disc 2).cue'",
      selected: "ff7 (Disc 2).cue",
    });

    const { findByTestId } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    await act(async () => {
      lastDropdown().onChange?.({ data: "ff7 (Disc 2).cue", label: "Disc 2" });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(backend.selectDisc).toHaveBeenCalledWith(42, "ff7 (Disc 2).cue");
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(
      100,
      "flatpak run net.retrodeck.retrodeck '/roms/ff7 (Disc 2).cue'",
    );
  });

  it("selecting the m3u default (data:null) clears the pin via selectDisc(rid, null)", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue({ ...m3uSelection, selected: "ff7 (Disc 2).cue" });
    vi.mocked(backend.selectDisc).mockResolvedValue({
      success: true,
      launch_options: "flatpak run net.retrodeck.retrodeck '/roms/ff7.m3u'",
      selected: null,
    });

    const { findByTestId } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    await act(async () => {
      lastDropdown().onChange?.({ data: null, label: "All discs (m3u)" });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(backend.selectDisc).toHaveBeenCalledWith(42, null);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(100, "flatpak run net.retrodeck.retrodeck '/roms/ff7.m3u'");
  });

  it("updates the active badge label after a successful pick", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);
    vi.mocked(backend.selectDisc).mockResolvedValue({
      success: true,
      launch_options: "cmd '/roms/ff7 (Disc 3).cue'",
      selected: "ff7 (Disc 3).cue",
    });

    const { findByTestId, findByText } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    await act(async () => {
      lastDropdown().onChange?.({ data: "ff7 (Disc 3).cue", label: "Disc 3" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // The badge face now reflects the pinned disc.
    expect(await findByText("Disc 3")).toBeInTheDocument();
  });

  it("toasts the failure message and does NOT confirm-set launch options when selectDisc fails", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);
    vi.mocked(backend.selectDisc).mockResolvedValue({
      success: false,
      reason: "not_found",
      message: "Disc not found in the install directory",
    });

    const { findByTestId } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    await act(async () => {
      lastDropdown().onChange?.({ data: "ghost.cue", label: "Ghost" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // Non-vacuous: the exact backend message is toasted, and no shortcut write.
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Disc not found in the install directory",
    });
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
  });

  it("toasts a fallback on a selectDisc rejection (non-vacuous catch)", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);
    vi.mocked(backend.selectDisc).mockRejectedValue(new Error("network down"));

    const { findByTestId } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    await act(async () => {
      lastDropdown().onChange?.({ data: "ff7 (Disc 2).cue", label: "Disc 2" });
      await Promise.resolve();
      await Promise.resolve();
    });

    // Observable catch effect: a fallback toast, and no confirm-set.
    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Failed to select disc" });
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
  });
});

describe("DiscSelector — event-driven re-fetch + cleanup", () => {
  beforeEach(() => {
    captured.dropdowns = [];
    vi.mocked(getCachedGameDetail).mockReset();
    vi.mocked(backend.getDiscSelection).mockReset();
  });

  it("registers download_complete + romm_rom_uninstalled listeners on mount", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue({ multi_disc: false });

    render(<DiscSelector appId={100} />);

    await waitFor(() => {
      expect(deckyEventListenerCount("download_complete")).toBe(1);
    });
  });

  it("re-fetches getDiscSelection on a matching download_complete (newly multi-disc)", async () => {
    // First fetch: not multi-disc (the ROM wasn't installed as multi-disc yet).
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValueOnce({ multi_disc: false });

    const { findByTestId } = render(<DiscSelector appId={100} />);
    await waitFor(() => expect(vi.mocked(backend.getDiscSelection)).toHaveBeenCalledTimes(1));

    // Now a download_complete arrives and the re-fetch returns a multi-disc set.
    vi.mocked(backend.getDiscSelection).mockResolvedValueOnce(m3uSelection);

    await act(async () => {
      const event: DownloadCompleteEvent = {
        rom_id: 42,
        rom_name: "Final Fantasy VII",
        platform_name: "PSX",
        file_path: "/roms/ff7.m3u",
        app_id: 100,
        launch_options: "cmd",
      };
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", event);
      await Promise.resolve();
      await Promise.resolve();
    });

    // Re-fetched, and the dropdown now renders.
    expect(vi.mocked(backend.getDiscSelection)).toHaveBeenCalledTimes(2);
    expect(await findByTestId("disc-dropdown")).toBeInTheDocument();
  });

  it("ignores download_complete for a different rom_id", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue({ multi_disc: false });

    render(<DiscSelector appId={100} />);
    await waitFor(() => expect(vi.mocked(backend.getDiscSelection)).toHaveBeenCalledTimes(1));

    await act(async () => {
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", {
        rom_id: 999,
        rom_name: "Other",
        platform_name: "PSX",
        file_path: "/roms/other.m3u",
        app_id: 1,
        launch_options: "cmd",
      });
      await Promise.resolve();
    });

    // Mismatched rom_id → no re-fetch.
    expect(vi.mocked(backend.getDiscSelection)).toHaveBeenCalledTimes(1);
  });

  it("hides the dropdown when a matching romm_rom_uninstalled fires", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue(m3uSelection);

    const { findByTestId, container } = render(<DiscSelector appId={100} />);
    await findByTestId("disc-dropdown");

    await act(async () => {
      globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 42 } }));
      await Promise.resolve();
    });

    expect(container.querySelector('[data-testid="disc-dropdown"]')).toBeNull();
  });

  it("removes both listeners on unmount", async () => {
    mockCachedDetail();
    vi.mocked(backend.getDiscSelection).mockResolvedValue({ multi_disc: false });

    const { unmount } = render(<DiscSelector appId={100} />);
    await waitFor(() => expect(deckyEventListenerCount("download_complete")).toBe(1));

    unmount();
    expect(deckyEventListenerCount("download_complete")).toBe(0);
  });
});
