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
import { emitDeckyEvent } from "../test-utils/decky-api-mock";
import type { DownloadCompleteEvent } from "../types";
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
  MenuItem: (p: { onClick?: () => void; children?: ReactNode; disabled?: boolean }) =>
    createElement(
      "div",
      { role: "menuitem", onClick: p.onClick, "aria-disabled": p.disabled ? "true" : undefined },
      p.children,
    ),
  showContextMenu: (menu: ReactNode) => {
    captured.menu = menu;
  },
}));

// invalidateCachedGameDetail is re-exported from the store — mock it there.
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn(),
  invalidateCachedGameDetail: vi.fn(),
}));

// setLaunchOptionsConfirmed writes the switched version's launch command onto the
// Steam shortcut (#1298) — mock so the test asserts the call without SteamClient.
vi.mock("../utils/steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn().mockResolvedValue(true),
}));

// The unsynced-saves confirm has its own test (UnsyncedSavesSwitchModal.test.tsx);
// mock it here so the picker's soft-block branch is driven by a chosen outcome.
vi.mock("./UnsyncedSavesSwitchModal", () => ({
  showUnsyncedSavesModal: vi.fn(),
}));

import { invalidateCachedGameDetail } from "../utils/cachedGameDetailStore";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { showUnsyncedSavesModal } from "./UnsyncedSavesSwitchModal";

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
        switchable: true,
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
        switchable: true,
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
        switchable: true,
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
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
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
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
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

describe("VersionPicker — non-switchable rows (#1359)", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    vi.mocked(toaster.toast).mockReset();
  });

  // A group whose second version is a RomM sibling from a DIFFERENT local group
  // (switchable:false) alongside the switchable active version.
  function listWithNonSwitchable(): VersionList {
    return multiVersionList({
      versions: [
        {
          rom_id: 1,
          name: "Tomb Raider II",
          label: "Tomb Raider II",
          regions: ["USA"],
          languages: ["En"],
          revision: "",
          tags: [],
          synced: true,
          installed: true,
          active: true,
          is_default: true,
          switchable: true,
        },
        {
          rom_id: 5,
          name: "Lara Croft",
          label: "Lara Croft (USA)",
          regions: [],
          languages: [],
          revision: "",
          tags: [],
          synced: false,
          installed: false,
          active: false,
          is_default: false,
          switchable: false,
        },
      ],
    });
  }

  it("renders the non-switchable row disabled with the hint (still listed)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithNonSwitchable());

    const { menu } = await renderAndOpen();

    const items = within(menu.container).getAllByRole("menuitem");
    const laraRow = items.find((i) => i.textContent.includes("Lara Croft"));
    // The cross-group version is STILL listed so the user sees it exists…
    expect(laraRow).toBeTruthy();
    // …but the row is disabled and explains why.
    expect(laraRow?.getAttribute("aria-disabled")).toBe("true");
    expect(laraRow?.textContent).toContain("conflicting metadata match in RomM");
    // The switchable active row is not disabled and carries no hint.
    const trRow = items.find((i) => i.textContent.includes("Tomb Raider II"));
    expect(trRow?.getAttribute("aria-disabled")).toBeNull();
    expect(trRow?.textContent).not.toContain("conflicting metadata match");
  });

  it("clicking a non-switchable row fires no switch and no toast", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithNonSwitchable());

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Lara Croft (USA)");

    // The guard makes the click a no-op — the dead-end rejection toast never fires.
    expect(backend.switchVersion).not.toHaveBeenCalled();
    expect(toaster.toast).not.toHaveBeenCalled();
  });
});

describe("VersionPicker — per-version covers (#1346)", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset().mockResolvedValue(multiVersionList());
    vi.mocked(backend.fetchCoverBase64).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset().mockResolvedValue(true);
    vi.mocked(invalidateCachedGameDetail).mockReset();
  });

  /** Render, wait for every row's cover fetch to settle, then open the menu. */
  async function renderWaitCoversAndOpen() {
    const r = render(<VersionPicker appId={APP_ID} />);
    await r.findByTestId("version-btn");
    // Covers load lazily on the list-load effect — one fetch per row (3 rows).
    await waitFor(() => expect(vi.mocked(backend.fetchCoverBase64)).toHaveBeenCalledTimes(3));
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
    });
    return render(<>{captured.menu}</>);
  }

  it("lazily fetches a cover for every version — synced AND not-synced", async () => {
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });

    const r = render(<VersionPicker appId={APP_ID} />);
    await r.findByTestId("version-btn");

    await waitFor(() => {
      expect(vi.mocked(backend.fetchCoverBase64)).toHaveBeenCalledWith(1);
      expect(vi.mocked(backend.fetchCoverBase64)).toHaveBeenCalledWith(2);
      // The Europe row is server-only (synced:false) — it is fetched too, via the
      // cache-first callable, so each version shows its own art (#1346).
      expect(vi.mocked(backend.fetchCoverBase64)).toHaveBeenCalledWith(3);
    });
  });

  it("renders each version's own distinct cover as an <img>", async () => {
    vi.mocked(backend.fetchCoverBase64).mockImplementation(async (romId: number) => ({
      base64: romId === 1 ? "AAAA" : romId === 2 ? "BBBB" : null,
    }));

    const menu = await renderWaitCoversAndOpen();

    const srcs = Array.from(menu.container.querySelectorAll("img")).map((i) => i.getAttribute("src"));
    expect(srcs).toContain("data:image/png;base64,AAAA");
    expect(srcs).toContain("data:image/png;base64,BBBB");
    // The third (null) row shows no <img> — only the two covers rendered.
    expect(menu.container.querySelectorAll("img")).toHaveLength(2);
  });

  it("falls back to the disc icon when a cover fetch returns null", async () => {
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });

    const menu = await renderWaitCoversAndOpen();

    // No covers rendered; every row shows the FaCompactDisc fallback (an <svg>).
    expect(menu.container.querySelectorAll("img")).toHaveLength(0);
    expect(menu.container.querySelectorAll("svg").length).toBeGreaterThanOrEqual(3);
  });

  it("publishes the newly active version's cover onto the Steam shortcut after a switch", async () => {
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: false,
      launch_options: "",
      app_id: APP_ID,
    });
    vi.mocked(backend.fetchCoverBase64).mockImplementation(async (romId: number) => ({
      base64: romId === 2 ? "JPCOVER" : null,
    }));

    const r = render(<VersionPicker appId={APP_ID} />);
    await r.findByTestId("version-btn");
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
    });
    const menu = render(<>{captured.menu}</>);
    await clickRow(menu.container, "Game (Japan)");

    // The Japan version's cover (rom_id 2) is applied to the group's shortcut as
    // the portrait grid (assetType 0) once the switch commits.
    expect(setArt).toHaveBeenCalledWith(APP_ID, "JPCOVER", "png", 0);
  });

  it("degrades silently when the post-switch cover fetch returns null (no SetCustomArtworkForApp)", async () => {
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: false,
      launch_options: "",
      app_id: APP_ID,
    });
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });

    const r = render(<VersionPicker appId={APP_ID} />);
    await r.findByTestId("version-btn");
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
    });
    const menu = render(<>{captured.menu}</>);
    await clickRow(menu.container, "Game (Japan)");

    // No cover obtainable → the old art is left in place, the switch still completes.
    expect(setArt).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
  });

  it("still completes the switch when the post-switch cover fetch REJECTS (non-vacuous catch)", async () => {
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });
    const logWarnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
    try {
      vi.mocked(backend.switchVersion).mockResolvedValue({
        success: true,
        rom_id: 2,
        target_installed: false,
        launch_options: "",
        app_id: APP_ID,
      });
      vi.mocked(backend.fetchCoverBase64).mockRejectedValue(new Error("offline"));

      const r = render(<VersionPicker appId={APP_ID} />);
      await r.findByTestId("version-btn");
      await act(async () => {
        fireEvent.click(r.getByTestId("version-btn"));
      });
      const menu = render(<>{captured.menu}</>);
      await clickRow(menu.container, "Game (Japan)");

      // The rejection is caught + warned; nothing is applied, and the switch still
      // commits (invalidate ran) — the rejection never propagates out of the switch.
      expect(setArt).not.toHaveBeenCalled();
      expect(logWarnSpy).toHaveBeenCalledWith(expect.stringContaining("cover apply after switch failed"));
      expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
    } finally {
      logWarnSpy.mockRestore();
    }
  });

  it("still completes the switch when SetCustomArtworkForApp THROWS (non-vacuous catch)", async () => {
    const setArt = vi.fn().mockRejectedValue(new Error("steam down"));
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });
    const logWarnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
    try {
      vi.mocked(backend.switchVersion).mockResolvedValue({
        success: true,
        rom_id: 2,
        target_installed: false,
        launch_options: "",
        app_id: APP_ID,
      });
      vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: "JP" });

      const r = render(<VersionPicker appId={APP_ID} />);
      await r.findByTestId("version-btn");
      await act(async () => {
        fireEvent.click(r.getByTestId("version-btn"));
      });
      const menu = render(<>{captured.menu}</>);
      await clickRow(menu.container, "Game (Japan)");

      // The apply was attempted with valid data, its rejection is caught + warned,
      // and the switch still commits without rethrowing.
      expect(setArt).toHaveBeenCalledWith(APP_ID, "JP", "png", 0);
      expect(logWarnSpy).toHaveBeenCalledWith(expect.stringContaining("cover apply after switch failed"));
      expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
    } finally {
      logWarnSpy.mockRestore();
    }
  });
});

/** Capture the events dispatched on `romm_data_changed` while `fn` runs. */
async function captureDataChanged(fn: () => Promise<void>): Promise<CustomEvent[]> {
  const dispatched: CustomEvent[] = [];
  const listener = (e: Event) => dispatched.push(e as CustomEvent);
  globalThis.addEventListener("romm_data_changed", listener);
  await fn();
  globalThis.removeEventListener("romm_data_changed", listener);
  return dispatched;
}

/** Click a captured-menu row and flush the async switch chain. */
async function clickRow(menuContainer: HTMLElement, label: string): Promise<void> {
  await act(async () => {
    fireEvent.click(within(menuContainer).getByText(label));
    // Flush the switch → modal → sync → retry → cover-apply microtask chain.
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

describe("VersionPicker — switching", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    vi.mocked(invalidateCachedGameDetail).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset().mockResolvedValue(true);
    vi.mocked(toaster.toast).mockReset();
  });

  it("switch to a downloaded target confirms its launch command onto the shortcut", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    const command = 'flatpak run net.retrodeck.retrodeck "/roms/game.iso"';
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: command,
      app_id: APP_ID,
    });

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 2, false);
    // The launch command is confirm-written onto the shortcut BEFORE the cache
    // invalidate + broadcast.
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, command);
    expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
    const versionEvt = dispatched.find((e) => e.detail?.type === "version_switched");
    expect(versionEvt?.detail).toEqual({ type: "version_switched", app_id: APP_ID, rom_id: 2 });
  });

  it("switch to an uninstalled target blanks the shortcut's launch command", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: false,
      launch_options: "",
      app_id: APP_ID,
    });

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    // Blank is applied on purpose so the shortcut never keeps the old command.
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, "");
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(true);
  });

  it("still invalidates + broadcasts (and warns) when the launch-options confirm resolves false", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: "cmd",
      app_id: APP_ID,
    });
    // The write didn't confirm — the backend rebind is already committed, so the
    // switch must still complete; only the shortcut command is left stale.
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(false);

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Switched — re-switch if launch fails" });
    expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(true);
  });

  it("still invalidates + broadcasts when the launch-options confirm THROWS (non-vacuous catch)", async () => {
    const logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    try {
      vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
      vi.mocked(backend.switchVersion).mockResolvedValue({
        success: true,
        rom_id: 2,
        target_installed: true,
        launch_options: "cmd",
        app_id: APP_ID,
      });
      vi.mocked(setLaunchOptionsConfirmed).mockRejectedValue(new Error("steam down"));

      const dispatched = await captureDataChanged(async () => {
        const { menu } = await renderAndOpen();
        await clickRow(menu.container, "Game (Japan)");
      });

      // A throw is treated like a failed confirm: warn + still complete the switch.
      expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("launch-options confirm threw"));
      expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Switched — re-switch if launch fails" });
      expect(invalidateCachedGameDetail).toHaveBeenCalledWith(APP_ID);
      expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(true);
    } finally {
      logErrorSpy.mockRestore();
    }
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

  it("toasts a short body with the backend detail in subtext on a plain failure (non-vacuous)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: false,
      reason: "bound_elsewhere",
      message: "That version is bound to another shortcut.",
    });

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    // Short body (Steam truncates it to one line), backend detail in subtext (#1359).
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Could not switch version",
      subtext: "That version is bound to another shortcut.",
    });
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
  });

  it("toasts a fallback on a switchVersion rejection (non-vacuous catch)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockRejectedValue(new Error("network down"));

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Could not switch version" });
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
  });
});

describe("VersionPicker — unsynced-saves soft-block", () => {
  const block = {
    success: false as const,
    reason: "unsynced_saves" as const,
    message: "Unsynced saves on the current version.",
    server_reachable: true,
    unsynced_rom_id: 1,
    unsynced_version_name: "Game (USA)",
  };
  const successResult = {
    success: true as const,
    rom_id: 2,
    target_installed: false,
    launch_options: "",
    app_id: APP_ID,
  };

  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset().mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.syncRomSaves).mockReset();
    vi.mocked(backend.refreshSaveStatus).mockReset().mockResolvedValue({ success: true });
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    vi.mocked(invalidateCachedGameDetail).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset().mockResolvedValue(true);
    vi.mocked(showUnsyncedSavesModal).mockReset();
    vi.mocked(toaster.toast).mockReset();
  });

  it("opens the modal with the stranded version's name + reachability", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValue(block);
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("cancel");

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    expect(showUnsyncedSavesModal).toHaveBeenCalledWith({
      versionName: "Game (USA)",
      serverReachable: true,
    });
  });

  it("'Switch anyway' forces the switch with allow_stranded=true and applies it", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce(successResult);
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("switch_anyway");

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(backend.switchVersion).toHaveBeenNthCalledWith(1, APP_ID, 2, false);
    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, true);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, "");
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(true);
  });

  it("toasts a short body + backend subtext when the forced 'Switch anyway' fails", async () => {
    // Block → "Switch anyway" → the forced switch itself fails: the toast must use
    // the short body with the backend detail in subtext, same as the plain path (#1359).
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce({
      success: false,
      reason: "bound_elsewhere",
      message: "That version is bound to another shortcut.",
    });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("switch_anyway");

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, true);
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Could not switch version",
      subtext: "That version is bound to another shortcut.",
    });
    // The failed force never applies the switch.
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);
  });

  it("'Sync now & switch' syncs the stranded version then retries the switch", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce(successResult);
    vi.mocked(backend.syncRomSaves).mockResolvedValue({ success: true, message: "", synced: 1 });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(backend.syncRomSaves).toHaveBeenCalledWith(1);
    // Second switch is the post-sync retry (allow_stranded still false).
    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, false);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, "");
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(true);
  });

  it("aborts with a toast + save-status refresh when the pre-switch sync fails", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValue(block);
    vi.mocked(backend.syncRomSaves).mockResolvedValue({ success: false, message: "boom", synced: 0 });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Couldn't sync saves — try again" });
    // The stranded version's status is refreshed so the conflict UI can surface.
    expect(backend.refreshSaveStatus).toHaveBeenCalledWith(1);
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);
    expect(backend.switchVersion).toHaveBeenCalledTimes(1); // no retry
  });

  it("aborts when the sync surfaces conflicts", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValue(block);
    vi.mocked(backend.syncRomSaves).mockResolvedValue({
      success: true,
      message: "",
      synced: 0,
      conflicts: [{ filename: "save.srm" } as never],
    });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Resolve save conflicts first" });
    expect(backend.refreshSaveStatus).toHaveBeenCalledWith(1);
    expect(backend.switchVersion).toHaveBeenCalledTimes(1);
  });

  it("shows a distinct toast when the post-sync retry re-blocks on unsynced saves", async () => {
    // Sync succeeds cleanly, but the retry still reports drift (partial upload /
    // race) — the message must say the saves are still unsynced, not the generic
    // "couldn't switch".
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce(block);
    vi.mocked(backend.syncRomSaves).mockResolvedValue({ success: true, message: "", synced: 1 });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Saves still unsynced — try again" });
    expect(backend.refreshSaveStatus).toHaveBeenCalledWith(1);
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);
  });

  it("logs a warning when the post-abort save-status refresh rejects (non-vacuous catch)", async () => {
    const logWarnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
    try {
      vi.mocked(backend.switchVersion).mockResolvedValue(block);
      vi.mocked(backend.syncRomSaves).mockResolvedValue({ success: false, message: "boom", synced: 0 });
      vi.mocked(backend.refreshSaveStatus).mockRejectedValue(new Error("offline"));
      vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");

      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");

      // The abort toast still fires, and the rejected refresh is warned (not swallowed silently).
      expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Couldn't sync saves — try again" });
      expect(logWarnSpy).toHaveBeenCalledWith(expect.stringContaining("post-abort save-status refresh failed"));
    } finally {
      logWarnSpy.mockRestore();
    }
  });

  it("cancel leaves the binding untouched", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValue(block);
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("cancel");

    const dispatched = await captureDataChanged(async () => {
      const { menu } = await renderAndOpen();
      await clickRow(menu.container, "Game (Japan)");
    });

    expect(backend.switchVersion).toHaveBeenCalledTimes(1); // only the initial probe
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);
    expect(toaster.toast).not.toHaveBeenCalled();
  });

  it("offline block: 'Switch anyway' still forces the switch (T5)", async () => {
    vi.mocked(backend.switchVersion)
      .mockResolvedValueOnce({ ...block, server_reachable: false })
      .mockResolvedValueOnce(successResult);
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("switch_anyway");

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    expect(showUnsyncedSavesModal).toHaveBeenCalledWith({ versionName: "Game (USA)", serverReachable: false });
    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, true);
  });
});

describe("VersionPicker — event refresh", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
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

  it("re-fetches on download_complete for a group member so the Downloaded badge is never stale", async () => {
    // A download changes the install picture WITHOUT a version switch (#1345):
    // the pre-fix picker kept the superseded list until the next switch.
    const stale = multiVersionList();
    const fresh = multiVersionList();
    fresh.versions![1] = { ...fresh.versions![1]!, installed: true };
    vi.mocked(backend.getVersionList).mockResolvedValueOnce(stale).mockResolvedValueOnce(fresh);

    const { findByTestId, getByTestId } = render(<VersionPicker appId={APP_ID} />);
    await findByTestId("version-btn");
    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);

    await act(async () => {
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", { rom_id: 2 } as DownloadCompleteEvent);
      await Promise.resolve();
    });
    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(2);

    // Non-vacuous: the reloaded list is what the menu renders — the freshly
    // downloaded Japan row now carries the Downloaded badge.
    await act(async () => {
      fireEvent.click(getByTestId("version-btn"));
    });
    const menu = render(<>{captured.menu}</>);
    const items = within(menu.container).getAllByRole("menuitem");
    expect(items[1]?.textContent).toContain("Game (Japan)");
    expect(items[1]?.textContent).toContain("Downloaded");
  });

  it("ignores a download_complete for a rom outside the group", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { findByTestId } = render(<VersionPicker appId={APP_ID} />);
    await findByTestId("version-btn");
    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);

    await act(async () => {
      emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", { rom_id: 999 } as DownloadCompleteEvent);
      await Promise.resolve();
    });

    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);
  });

  it("re-fetches when a group member is uninstalled", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());

    const { findByTestId } = render(<VersionPicker appId={APP_ID} />);
    await findByTestId("version-btn");
    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(1);

    await act(async () => {
      globalThis.dispatchEvent(new CustomEvent("romm_rom_uninstalled", { detail: { rom_id: 1 } }));
      await Promise.resolve();
    });

    expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledTimes(2);
  });
});

describe("VersionPicker — listener cleanup", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
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

describe("VersionPicker — in-flight switch guard (#1345 / E)", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    vi.mocked(invalidateCachedGameDetail).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset().mockResolvedValue(true);
    vi.mocked(showUnsyncedSavesModal).mockReset();
    vi.mocked(toaster.toast).mockReset();
  });

  it("disables the trigger (throbber) and blocks the menu while the post-switch reload is pending", async () => {
    // The switch succeeds but the version_switched reload stays pending — this is
    // exactly the stale-list window the guard protects.
    let resolveReload!: (v: VersionList) => void;
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList())
      .mockReturnValueOnce(new Promise<VersionList>((res) => (resolveReload = res)));
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: "",
      app_id: APP_ID,
    });

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    // In-flight: the chevron is replaced by the throbber, and a second trigger
    // click can't reopen the menu against the stale list.
    expect(r.container.querySelector(".romm-throbber")).not.toBeNull();
    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    expect(captured.menu).toBeNull();

    // Land the reload so the test leaves no dangling promise/guard.
    await act(async () => {
      resolveReload(multiVersionList());
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
  });

  it("re-enables the trigger once the post-switch list reload lands", async () => {
    let resolveReload!: (v: VersionList) => void;
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList())
      .mockReturnValueOnce(new Promise<VersionList>((res) => (resolveReload = res)));
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: "",
      app_id: APP_ID,
    });

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");
    expect(r.container.querySelector(".romm-throbber")).not.toBeNull();

    await act(async () => {
      resolveReload(multiVersionList());
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });

    // Guard cleared: throbber gone and the menu opens again.
    expect(r.container.querySelector(".romm-throbber")).toBeNull();
    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    expect(captured.menu).not.toBeNull();
  });

  it("re-enables the trigger after a rejected switch (guard never sticks)", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockRejectedValue(new Error("network down"));

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    // A rejected switch fires no reload, but the guard is still released.
    expect(r.container.querySelector(".romm-throbber")).toBeNull();
    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    expect(captured.menu).not.toBeNull();
  });

  it("re-enables the trigger after a cancelled unsynced-saves soft-block", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: false as const,
      reason: "unsynced_saves" as const,
      message: "",
      server_reachable: true,
      unsynced_rom_id: 1,
      unsynced_version_name: "Game (USA)",
    });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("cancel");

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    expect(r.container.querySelector(".romm-throbber")).toBeNull();
    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    expect(captured.menu).not.toBeNull();
  });

  it("a switch-back immediately after re-enable reaches the backend (swallowed-click regression)", async () => {
    // After A(1)→B(2), the reload returns a FRESH list where B is active and A is not.
    const switchedList = multiVersionList({
      versions: (multiVersionList().versions ?? []).map((v) => ({ ...v, active: v.rom_id === 2 })),
    });
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList()) // initial: USA (1) active
      .mockResolvedValue(switchedList); // reload + later: Japan (2) active
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: "",
      app_id: APP_ID,
    });

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)"); // switch 1→2; reload lands the fresh list
    await waitFor(() => expect(r.container.querySelector(".romm-throbber")).toBeNull());

    // Re-open against the FRESH list (USA now non-active) and switch BACK to USA.
    vi.mocked(backend.switchVersion).mockClear();
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 1,
      target_installed: true,
      launch_options: "",
      app_id: APP_ID,
    });
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    const menu2 = render(<>{captured.menu}</>);
    await clickRow(menu2.container, "Game (USA)");

    // Without the guard the stale list still marked USA active, and
    // `if (target.active) return` would have eaten this click. With the guard the
    // list is fresh (USA non-active) by re-enable time, so the switch reaches the backend.
    expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 1, false);
  });
});
