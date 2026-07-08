/**
 * VersionPicker tests — capture-the-menu pattern (mirrors DiscSelector.test.tsx).
 *
 * The component fetches the version list via `getVersionList`, renders a "Version"
 * row with a trigger button, and opens the version list via `showContextMenu`.
 * This file locally re-mocks `@decky/ui` to render the trigger and CAPTURE the
 * menu element, then renders it to assert markers + drive a switch exactly as a
 * real menu click would.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, waitFor, act, fireEvent, within } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { toaster } from "@decky/api";
import { VersionPicker } from "./VersionPicker";
import * as backend from "../api/backend";
import type { VersionList } from "../api/backend";
import {
  installDomEventListenerSpy,
  uninstallDomEventListenerSpy,
  domListenerCount,
} from "../test-utils/dom-event-listener-spy";

// --- Local @decky/ui mock: render the trigger, capture the context menu ------
const captured: { menu: ReactNode } = { menu: null };

vi.mock("@decky/ui", () => ({
  DialogButton: (p: { onClick?: (e: unknown) => void; children?: ReactNode; className?: string }) =>
    createElement("button", { "data-testid": "version-btn", onClick: p.onClick, className: p.className }, p.children),
  Menu: (p: { children?: ReactNode }) => createElement("div", { "data-testid": "version-menu" }, p.children),
  MenuItem: (p: { onClick?: () => void; children?: ReactNode }) =>
    createElement("div", { role: "menuitem", onClick: p.onClick }, p.children),
  showContextMenu: (menu: ReactNode) => {
    captured.menu = menu;
  },
}));

// invalidateCachedGameDetail is re-exported from the store — mock it there.
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn(),
  invalidateCachedGameDetail: vi.fn(),
}));

import { invalidateCachedGameDetail } from "../utils/cachedGameDetailStore";

const APP_ID = 100;

function multiVersionList(overrides: Partial<VersionList> = {}): VersionList {
  return {
    multi_version: true,
    server_query_failed: false,
    versions: [
      {
        rom_id: 1,
        name: "Game (USA)",
        label: "Game (USA)",
        regions: ["USA"],
        languages: ["En"],
        revision: "",
        tags: [],
        synced: true,
        installed: true,
        active: true,
        is_default: false,
      },
      {
        rom_id: 2,
        name: "Game (Japan)",
        label: "Game (Japan)",
        regions: ["Japan"],
        languages: ["Ja"],
        revision: "",
        tags: [],
        synced: true,
        installed: false,
        active: false,
        is_default: true,
      },
      {
        rom_id: 3,
        name: "Game (Europe)",
        label: "Game (Europe)",
        regions: ["Europe"],
        languages: ["En"],
        revision: "",
        tags: [],
        synced: false,
        installed: false,
        active: false,
        is_default: false,
      },
    ],
    ...overrides,
  };
}

/** Render, wait for the trigger, click it, and render the captured menu. */
async function renderAndOpen(appId = APP_ID) {
  const r = render(<VersionPicker appId={appId} />);
  await r.findByTestId("version-btn");
  await act(async () => {
    fireEvent.click(r.getByTestId("version-btn"));
  });
  const menu = render(<>{captured.menu}</>);
  return { r, menu };
}

describe("VersionPicker — render gate", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
  });

  it("renders nothing for a single-version group (multi_version:false)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue({ multi_version: false });

    const { container } = render(<VersionPicker appId={APP_ID} />);

    await waitFor(() => expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledWith(APP_ID));
    expect(container.querySelector('[data-testid="version-btn"]')).toBeNull();
    expect(container.textContent).not.toContain("Version");
  });

  it("renders a compact icon trigger (no verbose label) for a multi-version group", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { findByTestId } = render(<VersionPicker appId={APP_ID} />);

    const btn = await findByTestId("version-btn");
    // Icon-only trigger — the active version's verbose label is NOT on the trigger
    // itself (it lives in the anchored menu + the GAME INFO tab), so the play row
    // stays compact next to the disc picker.
    expect(btn.textContent).not.toContain("Game (USA)");
  });
});

describe("VersionPicker — menu markers", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
  });

  it("lists every version and marks active / default / downloaded / not-synced", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { menu } = await renderAndOpen();

    const items = within(menu.container).getAllByRole("menuitem");
    expect(items).toHaveLength(3);
    const text = (i: number): string => items[i]?.textContent ?? "";
    // Active row (USA) carries the check + Downloaded badge.
    expect(text(0)).toContain("Game (USA)");
    expect(text(0)).toContain("✓");
    expect(text(0)).toContain("Downloaded");
    // Default row (Japan) carries the Default badge, no check.
    expect(text(1)).toContain("Default");
    expect(text(1)).not.toContain("✓");
    // Server-only row (Europe) is marked not synced.
    expect(text(2)).toContain("not synced");
  });
});

describe("VersionPicker — switching", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
    vi.mocked(invalidateCachedGameDetail).mockReset();
    vi.mocked(toaster.toast).mockReset();
  });

  it("switches to a version: invalidates the cache and dispatches version_switched", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({ success: true, rom_id: 2, rom_name: "Game (Japan)" });

    const dispatched: CustomEvent[] = [];
    const listener = (e: Event) => dispatched.push(e as CustomEvent);
    globalThis.addEventListener("romm_data_changed", listener);

    const { menu } = await renderAndOpen();
    await act(async () => {
      fireEvent.click(within(menu.container).getByText("Game (Japan)"));
      await Promise.resolve();
      await Promise.resolve();
    });
    globalThis.removeEventListener("romm_data_changed", listener);

    expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 2);
    expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
    const versionEvt = dispatched.find((e) => e.detail?.type === "version_switched");
    expect(versionEvt?.detail).toEqual({ type: "version_switched", app_id: APP_ID, rom_id: 2 });
  });

  it("does nothing when the active version is re-selected", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { menu } = await renderAndOpen();
    await act(async () => {
      fireEvent.click(within(menu.container).getByText("Game (USA)")); // the active one
      await Promise.resolve();
    });

    expect(backend.switchVersion).not.toHaveBeenCalled();
  });

  it("toasts the backend message on a failed switch (non-vacuous)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: false,
      reason: "installed",
      message: "Uninstall the game to switch versions.",
    });

    const { menu } = await renderAndOpen();
    await act(async () => {
      fireEvent.click(within(menu.container).getByText("Game (Japan)"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Uninstall the game to switch versions.",
    });
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
  });

  it("toasts a fallback on a switchVersion rejection (non-vacuous catch)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockRejectedValue(new Error("network down"));

    const { menu } = await renderAndOpen();
    await act(async () => {
      fireEvent.click(within(menu.container).getByText("Game (Japan)"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Could not switch version" });
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
  });
});

describe("VersionPicker — event refresh", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
  });

  it("re-fetches the version list on a matching version_switched event", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { findByTestId } = render(<VersionPicker appId={APP_ID} />);
    await findByTestId("version-btn");
    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);

    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "version_switched", app_id: APP_ID, rom_id: 2 } }),
      );
      await Promise.resolve();
    });

    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(2);
  });

  it("ignores a version_switched event for a different appId", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { findByTestId } = render(<VersionPicker appId={APP_ID} />);
    await findByTestId("version-btn");
    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);

    await act(async () => {
      globalThis.dispatchEvent(
        new CustomEvent("romm_data_changed", { detail: { type: "version_switched", app_id: 999, rom_id: 2 } }),
      );
      await Promise.resolve();
    });

    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);
  });
});

describe("VersionPicker — listener cleanup", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
    installDomEventListenerSpy();
  });

  afterEach(() => {
    uninstallDomEventListenerSpy();
  });

  it("removes its romm_data_changed listener on unmount (no leak)", async () => {
    const before = domListenerCount("romm_data_changed");
    const { unmount, findByTestId } = render(<VersionPicker appId={APP_ID} />);
    await findByTestId("version-btn");
    // The picker's initial-load effect registers exactly one listener…
    expect(domListenerCount("romm_data_changed")).toBe(before + 1);
    unmount();
    // …and the effect cleanup removes it (dropping the removeEventListener in the
    // useEffect return makes this assertion fail).
    expect(domListenerCount("romm_data_changed")).toBe(before);
  });
});
