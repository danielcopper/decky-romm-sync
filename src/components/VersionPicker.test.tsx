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
import { getRommConnectionState, setRommConnectionState } from "../utils/connectionState";
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
  DialogButton: (p: {
    onClick?: (e: unknown) => void;
    children?: ReactNode;
    className?: string;
    "aria-label"?: string;
  }) =>
    createElement(
      "button",
      {
        "data-testid": "version-btn",
        onClick: p.onClick,
        className: p.className,
        "aria-label": p["aria-label"],
      },
      p.children,
    ),
  Menu: (p: { children?: ReactNode }) => createElement("div", { "data-testid": "version-menu" }, p.children),
  MenuItem: (p: { onClick?: () => void; children?: ReactNode; disabled?: boolean; tone?: string }) =>
    createElement(
      "div",
      {
        role: "menuitem",
        onClick: p.onClick,
        "aria-disabled": p.disabled ? "true" : undefined,
        "data-tone": p.tone,
      },
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
vi.mock("./RemovedGamesCleanup", () => ({
  openRemovedGamesCleanupModal: vi.fn(),
}));

import { invalidateCachedGameDetail } from "../utils/cachedGameDetailStore";
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";
import { showUnsyncedSavesModal } from "./UnsyncedSavesSwitchModal";
import { openRemovedGamesCleanupModal } from "./RemovedGamesCleanup";

const APP_ID = 100;

function multiVersionList(overrides: Partial<VersionList> = {}): VersionList {
  return {
    multi_version: true,
    server_query_failed: false,
    bound_vanished: false,
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
        vanished: false,
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
        vanished: false,
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
        vanished: false,
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
    vi.mocked(backend.getVersionList).mockResolvedValue({ multi_version: false, bound_vanished: false });

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
    vi.mocked(openRemovedGamesCleanupModal).mockReset().mockResolvedValue(true);
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
          vanished: false,
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
          vanished: false,
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

describe("VersionPicker — vanished retained rows (#1570)", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    vi.mocked(toaster.toast).mockReset();
    // Reset per test: the rows here assert both that cleanup opens and that it
    // does NOT, so a call leaking in from a sibling test would pass either way.
    vi.mocked(openRemovedGamesCleanupModal).mockReset().mockResolvedValue(true);
  });

  function listWithBoundVanished(): VersionList {
    const versions = (multiVersionList().versions ?? []).slice(0, 2).map((v) => ({ ...v }));
    versions[0] = { ...versions[0]!, active: true, installed: true, is_default: false, vanished: true };
    versions[1] = { ...versions[1]!, active: false, is_default: true, vanished: false };
    return multiVersionList({ bound_vanished: true, versions });
  }

  it("keeps a vanished active row visible, dimmed, labelled, marked, and non-switchable", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithBoundVanished());

    const { menu } = await renderAndOpen();

    const items = within(menu.container).getAllByRole("menuitem");
    const vanishedRow = items.find((i) => i.textContent.includes("Game (USA)"));
    expect(vanishedRow).toBeTruthy();
    expect(vanishedRow?.textContent).toContain("No longer available on RomM");
    expect(vanishedRow?.textContent).toContain("Downloaded");
    expect(vanishedRow?.textContent).toContain("✓");
    expect((vanishedRow?.firstElementChild as HTMLElement | null)?.style.opacity).toBe("0.55");
    // The row now carries the cleanup action, so "non-switchable" has to be
    // asserted as behaviour: activating it can never rebind the shortcut to a
    // version RomM no longer serves.
    expect(within(vanishedRow!).getByLabelText("Remove local data")).toBeTruthy();
    await clickRow(menu.container, "Game (USA)");
    expect(backend.switchVersion).not.toHaveBeenCalled();
    expect(openRemovedGamesCleanupModal).toHaveBeenCalledWith(1);
    // The live recovery target remains selectable.
    const liveRow = items.find((i) => i.textContent.includes("Game (Japan)"));
    expect(liveRow?.getAttribute("aria-disabled")).toBeNull();
  });

  it("carries the cleanup affordance on the vanished row itself, marked destructive", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithBoundVanished());

    const { menu } = await renderAndOpen();

    const items = within(menu.container).getAllByRole("menuitem");
    const vanishedRow = items.find((i) => i.textContent.includes("Game (USA)"))!;
    // A free-standing entry below the row belonged to no version visually; the
    // trash sits ON the row, and the row is the single gamepad-focusable unit.
    expect(within(vanishedRow).getByLabelText("Remove local data")).toBeTruthy();
    expect(vanishedRow.getAttribute("data-tone")).toBe("destructive");
    expect(vanishedRow.getAttribute("aria-disabled")).toBeNull();
    // The live row offers no removal and stays a plain switch target.
    const liveRow = items.find((i) => i.textContent.includes("Game (Japan)"))!;
    expect(within(liveRow).queryByLabelText("Remove local data")).toBeNull();
    expect(liveRow.getAttribute("data-tone")).toBeNull();
  });

  it("leaves the trash colour to CSS and opts only the menu row into the focused flip", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithBoundVanished());

    const { menu } = await renderAndOpen();
    const rowTrash = within(menu.container).getByLabelText("Remove local data");

    // An inline colour would survive Steam's repaint of the focused destructive
    // row and leave a red icon on a red row, so the element must carry none.
    expect(rowTrash.style.color).toBe("");
    expect(rowTrash.getAttribute("fill")).toBe("currentColor");
    expect(rowTrash.classList.contains("romm-vanished-trash")).toBe(true);
    expect(rowTrash.classList.contains("romm-vanished-trash-row")).toBe(true);
  });

  it("keeps the singleton binding's trash out of the focused flip", async () => {
    const bound = multiVersionList().versions![0]!;
    vi.mocked(backend.getVersionList).mockResolvedValue({
      multi_version: false,
      server_query_failed: false,
      bound_vanished: true,
      bound_version: { ...bound, vanished: true },
    });

    const picker = render(<VersionPicker appId={APP_ID} />);
    await picker.findByRole("button", { name: "Remove local data" });
    const trash = picker.container.querySelector<SVGElement>("svg.romm-vanished-trash")!;

    // This one sits in a .romm-disc-btn whose focus background stays dark —
    // the black-on-focus rule would swap one invisible icon for another.
    expect(trash).toBeTruthy();
    expect(trash.classList.contains("romm-vanished-trash-row")).toBe(false);
    expect(trash.style.color).toBe("");
  });

  it("offers no cleanup on a vanished row whose local data was never synced", async () => {
    const list = listWithBoundVanished();
    list.versions = (list.versions ?? []).map((v) => (v.rom_id === 1 ? { ...v, synced: false } : v));
    vi.mocked(backend.getVersionList).mockResolvedValue(list);

    const { menu } = await renderAndOpen();
    const vanishedRow = within(menu.container)
      .getAllByRole("menuitem")
      .find((i) => i.textContent.includes("Game (USA)"))!;

    // Nothing local to remove, so the row stays a dead end rather than offering
    // a destructive action that would find nothing.
    expect(within(vanishedRow).queryByLabelText("Remove local data")).toBeNull();
    expect(vanishedRow.getAttribute("aria-disabled")).toBe("true");

    await clickRow(menu.container, "Game (USA)");
    expect(openRemovedGamesCleanupModal).not.toHaveBeenCalled();
    expect(backend.switchVersion).not.toHaveBeenCalled();
  });

  it("never switches to a vanished row, inactive or not", async () => {
    const list = listWithBoundVanished();
    list.bound_vanished = false;
    list.versions = (list.versions ?? []).map((v) =>
      v.rom_id === 1 ? { ...v, active: false } : { ...v, active: true },
    );
    vi.mocked(backend.getVersionList).mockResolvedValue(list);

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (USA)");

    // Activating the row opens the cleanup confirmation — it can never rebind
    // the shortcut to a version RomM no longer serves.
    expect(backend.switchVersion).not.toHaveBeenCalled();
    expect(toaster.toast).not.toHaveBeenCalled();
    expect(openRemovedGamesCleanupModal).toHaveBeenCalledWith(1);
  });

  it("allows recovery by selecting a live alternative to the vanished binding", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithBoundVanished());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: false,
      reason: "bound_elsewhere",
      message: "test stop",
    });

    const { menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");

    expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 2, false);
  });

  it("offers local cleanup only for a synced vanished row and scopes it to that ROM", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithBoundVanished());

    const { menu } = await renderAndOpen();
    const cleanup = within(menu.container).getByLabelText("Remove local data");
    await act(async () => {
      fireEvent.click(cleanup.closest('[role="menuitem"]')!);
      await Promise.resolve();
    });

    expect(openRemovedGamesCleanupModal).toHaveBeenCalledWith(1);
    // One affordance for one vanished version — never a second, free-standing row.
    expect(within(menu.container).getAllByLabelText("Remove local data")).toHaveLength(1);
    expect(menu.container.textContent).not.toContain("Remove local data...");
  });

  it("offers local cleanup for a synced singleton vanished binding", async () => {
    const bound = multiVersionList().versions![0]!;
    vi.mocked(backend.getVersionList).mockResolvedValue({
      multi_version: false,
      server_query_failed: false,
      bound_vanished: true,
      bound_version: { ...bound, vanished: true },
    });
    vi.mocked(openRemovedGamesCleanupModal).mockResolvedValue(true);
    const picker = render(<VersionPicker appId={APP_ID} />);
    const cleanup = await picker.findByRole("button", { name: "Remove local data" });

    fireEvent.click(cleanup);
    await waitFor(() => expect(openRemovedGamesCleanupModal).toHaveBeenCalledWith(1));
  });

  it("explains WHY a singleton vanished binding only offers cleanup", async () => {
    const bound = multiVersionList().versions![0]!;
    vi.mocked(backend.getVersionList).mockResolvedValue({
      multi_version: false,
      server_query_failed: false,
      bound_vanished: true,
      bound_version: { ...bound, vanished: true },
    });

    const picker = render(<VersionPicker appId={APP_ID} />);
    await picker.findByRole("button", { name: "Remove local data" });

    // Without the label the lone destructive action has no stated cause.
    expect(picker.container.textContent).toContain("No longer available on RomM");
  });

  it("renders nothing for a singleton whose binding is still live", async () => {
    const bound = multiVersionList().versions![0]!;
    vi.mocked(backend.getVersionList).mockResolvedValue({
      multi_version: false,
      server_query_failed: false,
      bound_vanished: false,
      bound_version: bound,
    });

    const { container } = render(<VersionPicker appId={APP_ID} />);

    await waitFor(() => expect(vi.mocked(backend.getVersionList)).toHaveBeenCalledWith(APP_ID));
    expect(container.textContent).not.toContain("No longer available on RomM");
    expect(container.querySelector('[aria-label="Remove local data"]')).toBeNull();
  });

  it("surfaces an inline cleanup-preview failure", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(listWithBoundVanished());
    vi.mocked(openRemovedGamesCleanupModal).mockRejectedValue(new Error("offline"));
    const log = vi.spyOn(backend, "logError").mockImplementation(() => {});

    const { menu } = await renderAndOpen();
    await act(async () => {
      fireEvent.click(within(menu.container).getByLabelText("Remove local data").closest('[role="menuitem"]')!);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(log).toHaveBeenCalledWith(expect.stringContaining("offline"));
    expect(toaster.toast).toHaveBeenCalledWith({ title: "RomM Sync", body: "Could not prepare local cleanup." });
  });
});

describe("VersionPicker — liveness connection signals (#1570)", () => {
  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    setRommConnectionState("checking");
  });

  afterEach(() => setRommConnectionState("checking"));

  it("does not feed a bound-id 404 into the global connection store", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(
      multiVersionList({ bound_vanished: true, server_query_failed: false }),
    );

    render(<VersionPicker appId={APP_ID} />);
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledWith(APP_ID));

    expect(getRommConnectionState()).toBe("checking");
  });

  it("manufactures no verdict from a singleton vanished binding", async () => {
    const bound = multiVersionList().versions![0]!;
    vi.mocked(backend.getVersionList).mockResolvedValue({
      multi_version: false,
      server_query_failed: false,
      bound_vanished: true,
      bound_version: { ...bound, vanished: true },
    });

    const picker = render(<VersionPicker appId={APP_ID} />);
    await picker.findByRole("button", { name: "Remove local data" });

    // The 404 is an entity verdict; the server plainly answered, so neither
    // "connected" nor "offline" may be inferred from it.
    expect(getRommConnectionState()).toBe("checking");
  });

  it("preserves the explicit server-unreachable feed", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(
      multiVersionList({ bound_vanished: false, server_query_failed: true }),
    );

    render(<VersionPicker appId={APP_ID} />);
    await waitFor(() => expect(getRommConnectionState()).toBe("offline"));
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

  it("unmount aborts a delayed post-switch cover and releases only after its fetch settles", async () => {
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: "cmd",
      app_id: APP_ID,
      prune_lease_token: "version-lease",
    });
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    const { r, menu } = await renderAndOpen();
    await waitFor(() => expect(backend.fetchCoverBase64).toHaveBeenCalledTimes(3));
    let resolveCover!: (value: { base64: string }) => void;
    vi.mocked(backend.fetchCoverBase64)
      .mockReset()
      .mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveCover = resolve;
          }),
      );

    fireEvent.click(within(menu.container).getByText("Game (Japan)"));
    await waitFor(() => expect(backend.fetchCoverBase64).toHaveBeenCalledWith(2));
    r.unmount();
    await Promise.resolve();
    expect(backend.releasePruneConflictLease).not.toHaveBeenCalledWith("version-lease");
    resolveCover({ base64: "LATE" });
    await act(async () => {
      for (let i = 0; i < 5; i++) await Promise.resolve();
    });
    await waitFor(() => expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("version-lease"));

    expect(setArt).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
  });

  it("releases a switch token that arrives after unmount without writing Steam", async () => {
    vi.mocked(backend.fetchCoverBase64).mockResolvedValue({ base64: null });
    let resolveSwitch!: (value: {
      success: true;
      rom_id: number;
      target_installed: true;
      launch_options: string;
      app_id: number;
      prune_lease_token: string;
    }) => void;
    vi.mocked(backend.switchVersion).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSwitch = resolve;
        }),
    );
    vi.mocked(backend.releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
    const { r, menu } = await renderAndOpen();
    vi.mocked(backend.fetchCoverBase64).mockClear();

    fireEvent.click(within(menu.container).getByText("Game (Japan)"));
    await waitFor(() => expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 2, false));
    r.unmount();
    resolveSwitch({
      success: true,
      rom_id: 2,
      target_installed: true,
      launch_options: "cmd",
      app_id: APP_ID,
      prune_lease_token: "late-version-lease",
    });

    await waitFor(() => expect(backend.releasePruneConflictLease).toHaveBeenCalledWith("late-version-lease"));
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(backend.fetchCoverBase64).not.toHaveBeenCalledWith(2);
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
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

  it("stays silent when the in-flight switch rejects only because the page unmounted", async () => {
    const logWarnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
    try {
      vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
      let rejectSwitch!: (error: unknown) => void;
      vi.mocked(backend.switchVersion).mockImplementation(
        () =>
          new Promise((_resolve, reject) => {
            rejectSwitch = reject;
          }),
      );

      const { r, menu } = await renderAndOpen();
      fireEvent.click(within(menu.container).getByText("Game (Japan)"));
      await waitFor(() => expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 2, false));
      r.unmount();
      vi.mocked(toaster.toast).mockClear();

      await act(async () => {
        rejectSwitch(new Error("teardown cancelled the callable"));
        for (let i = 0; i < 6; i++) await Promise.resolve();
      });

      // The picker is gone: a "Could not switch version" toast would blame the
      // user's next screen for work that either committed or never ran.
      expect(toaster.toast).not.toHaveBeenCalled();
      expect(logWarnSpy).toHaveBeenCalledWith(expect.stringContaining("version switch continuation was cancelled"));
      expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
    } finally {
      logWarnSpy.mockRestore();
    }
  });
});

describe("VersionPicker — switch target liveness (#1570)", () => {
  const vanishedFailure = {
    success: false as const,
    reason: "version_vanished" as const,
    message: "This version is no longer available on RomM.",
  };

  beforeEach(() => {
    captured.menu = null;
    vi.mocked(backend.getVersionList).mockReset();
    vi.mocked(backend.switchVersion).mockReset();
    vi.mocked(backend.fetchCoverBase64).mockReset().mockResolvedValue({ base64: null });
    vi.mocked(invalidateCachedGameDetail).mockReset();
    vi.mocked(setLaunchOptionsConfirmed).mockReset().mockResolvedValue(true);
    vi.mocked(toaster.toast).mockReset();
    setRommConnectionState("checking");
  });

  afterEach(() => setRommConnectionState("checking"));

  it("refuses the initial click, preserves detail, emits no success effects, and converges from a direct reload", async () => {
    const refreshed = multiVersionList({
      versions: (multiVersionList().versions ?? []).map((v) =>
        v.rom_id === 2 ? { ...v, vanished: true, is_default: false } : v,
      ),
    });
    vi.mocked(backend.getVersionList).mockResolvedValueOnce(multiVersionList()).mockResolvedValueOnce(refreshed);
    vi.mocked(backend.switchVersion).mockResolvedValue(vanishedFailure);
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });

    const { r, menu } = await renderAndOpen();
    vi.mocked(backend.fetchCoverBase64).mockClear();
    const dispatched = await captureDataChanged(() => clickRow(menu.container, "Game (Japan)"));

    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Could not switch version",
      subtext: vanishedFailure.message,
    });
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledTimes(2));
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(backend.fetchCoverBase64).not.toHaveBeenCalled();
    expect(setArt).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);

    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    const refreshedMenu = render(<>{captured.menu}</>);
    const target = within(refreshedMenu.container)
      .getAllByRole("menuitem")
      .find((item) => item.textContent.includes("Game (Japan)"));
    // The refused target converged out of the switchable set: it now states why
    // and offers only the cleanup, so a repeat click can never re-attempt it.
    expect(target?.textContent).toContain("No longer available on RomM");
    expect(within(target!).getByLabelText("Remove local data")).toBeTruthy();
    expect(target?.getAttribute("data-tone")).toBe("destructive");

    vi.mocked(backend.switchVersion).mockClear();
    await clickRow(refreshedMenu.container, "Game (Japan)");
    expect(backend.switchVersion).not.toHaveBeenCalled();
  });

  it("leaves connection state alone for version_vanished itself", async () => {
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList())
      .mockResolvedValueOnce(multiVersionList({ bound_vanished: true }));
    vi.mocked(backend.switchVersion).mockResolvedValue(vanishedFailure);

    const { menu } = await renderAndOpen();
    setRommConnectionState("checking");
    await clickRow(menu.container, "Game (Japan)");
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledTimes(2));

    expect(getRommConnectionState()).toBe("checking");
  });

  it("reports only an explicit server_unreachable initial refusal as offline", async () => {
    vi.mocked(backend.getVersionList).mockResolvedValue(multiVersionList());
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: false,
      reason: "server_unreachable",
      message: "Server unreachable",
    });

    const { menu } = await renderAndOpen();
    setRommConnectionState("checking");
    await clickRow(menu.container, "Game (Japan)");

    expect(getRommConnectionState()).toBe("offline");
    expect(backend.getVersionList).toHaveBeenCalledTimes(1);
  });

  it("does not force a successful fail-open switch to connected", async () => {
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList())
      .mockResolvedValueOnce(multiVersionList({ bound_vanished: true }));
    vi.mocked(backend.switchVersion).mockResolvedValue({
      success: true,
      rom_id: 2,
      target_installed: false,
      launch_options: "",
      app_id: APP_ID,
    });

    const { menu } = await renderAndOpen();
    setRommConnectionState("checking");
    await clickRow(menu.container, "Game (Japan)");
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledTimes(2));

    expect(getRommConnectionState()).toBe("checking");
  });

  it("ignores a late refusal reload after a newer successful-switch reload settles", async () => {
    let resolveRefusalReload!: (value: VersionList) => void;
    const refusalReload = new Promise<VersionList>((resolve) => {
      resolveRefusalReload = resolve;
    });
    const switchedList = multiVersionList({
      server_query_failed: true,
      versions: (multiVersionList().versions ?? []).map((v) => ({ ...v, active: v.rom_id === 3 })),
    });
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList())
      .mockReturnValueOnce(refusalReload)
      .mockResolvedValueOnce(switchedList);
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(vanishedFailure).mockResolvedValueOnce({
      success: true,
      rom_id: 3,
      target_installed: false,
      launch_options: "",
      app_id: APP_ID,
    });

    const { r, menu } = await renderAndOpen();
    setRommConnectionState("checking");
    await clickRow(menu.container, "Game (Japan)");
    expect(backend.getVersionList).toHaveBeenCalledTimes(2);

    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    const nextMenu = render(<>{captured.menu}</>);
    await clickRow(nextMenu.container, "Game (Europe)");
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(r.container.querySelector(".romm-throbber")).toBeNull());
    expect(getRommConnectionState()).toBe("offline");

    await act(async () => {
      resolveRefusalReload(multiVersionList());
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    expect(getRommConnectionState()).toBe("offline");

    vi.mocked(backend.switchVersion).mockClear().mockResolvedValue({
      success: false,
      reason: "bound_elsewhere",
      message: "test stop",
    });
    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    const settledMenu = render(<>{captured.menu}</>);
    await clickRow(settledMenu.container, "Game (USA)");

    // If the late refusal snapshot had overwritten the newer list, USA would be
    // marked active and handleSwitch would swallow this switch-back click.
    expect(backend.switchVersion).toHaveBeenCalledWith(APP_ID, 1, false);
  });

  it("ignores a refusal reload completion from an obsolete appId lifetime", async () => {
    const nextAppId = APP_ID + 1;
    let resolveOldReload!: (value: VersionList) => void;
    const oldReload = new Promise<VersionList>((resolve) => {
      resolveOldReload = resolve;
    });
    const nextAppList = multiVersionList({
      server_query_failed: true,
      versions: (multiVersionList().versions ?? []).map((v) => ({ ...v, active: v.rom_id === 3 })),
    });
    vi.mocked(backend.getVersionList)
      .mockResolvedValueOnce(multiVersionList())
      .mockReturnValueOnce(oldReload)
      .mockResolvedValueOnce(nextAppList);
    vi.mocked(backend.switchVersion).mockResolvedValue(vanishedFailure);

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");
    expect(backend.getVersionList).toHaveBeenCalledTimes(2);
    setRommConnectionState("checking");

    r.rerender(<VersionPicker appId={nextAppId} />);
    await waitFor(() => expect(backend.getVersionList).toHaveBeenNthCalledWith(3, nextAppId));
    await waitFor(() => expect(getRommConnectionState()).toBe("offline"));

    await act(async () => {
      resolveOldReload(multiVersionList());
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    expect(getRommConnectionState()).toBe("offline");

    vi.mocked(backend.switchVersion).mockClear().mockResolvedValue({
      success: false,
      reason: "bound_elsewhere",
      message: "test stop",
    });
    captured.menu = null;
    await act(async () => {
      fireEvent.click(r.getByTestId("version-btn"));
      await Promise.resolve();
    });
    const nextAppMenu = render(<>{captured.menu}</>);
    await clickRow(nextAppMenu.container, "Game (USA)");

    expect(backend.switchVersion).toHaveBeenCalledWith(nextAppId, 1, false);
  });

  it("ignores a refusal reload completion after unmount", async () => {
    let resolveReload!: (value: VersionList) => void;
    const reload = new Promise<VersionList>((resolve) => {
      resolveReload = resolve;
    });
    vi.mocked(backend.getVersionList).mockResolvedValueOnce(multiVersionList()).mockReturnValueOnce(reload);
    vi.mocked(backend.switchVersion).mockResolvedValue(vanishedFailure);

    const { r, menu } = await renderAndOpen();
    await clickRow(menu.container, "Game (Japan)");
    expect(backend.getVersionList).toHaveBeenCalledTimes(2);
    setRommConnectionState("checking");
    r.unmount();

    await act(async () => {
      resolveReload(multiVersionList({ server_query_failed: true }));
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });

    expect(getRommConnectionState()).toBe("checking");
  });

  it("keeps the refusal visible and releases the guard when its direct reload rejects", async () => {
    const logWarnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
    try {
      vi.mocked(backend.getVersionList)
        .mockResolvedValueOnce(multiVersionList())
        .mockRejectedValueOnce(new Error("refresh failed"));
      vi.mocked(backend.switchVersion).mockResolvedValue(vanishedFailure);

      const { r, menu } = await renderAndOpen();
      setRommConnectionState("checking");
      const dispatched = await captureDataChanged(() => clickRow(menu.container, "Game (Japan)"));

      expect(toaster.toast).toHaveBeenCalledWith({
        title: "RomM Sync",
        body: "Could not switch version",
        subtext: vanishedFailure.message,
      });
      expect(logWarnSpy).toHaveBeenCalledWith(expect.stringContaining("version-vanished list refresh failed"));
      expect(getRommConnectionState()).toBe("checking");
      expect(r.container.querySelector(".romm-throbber")).toBeNull();
      expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);

      captured.menu = null;
      await act(async () => {
        fireEvent.click(r.getByTestId("version-btn"));
        await Promise.resolve();
      });
      expect(captured.menu).not.toBeNull();
    } finally {
      logWarnSpy.mockRestore();
    }
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
    setRommConnectionState("checking");
  });

  afterEach(() => setRommConnectionState("checking"));

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

  it("unmount during save sync stops before the successor switch commits", async () => {
    let resolveSync!: (value: { success: true; message: string; synced: number }) => void;
    vi.mocked(backend.switchVersion).mockResolvedValue(block);
    vi.mocked(backend.syncRomSaves).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSync = resolve;
        }),
    );
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");
    const { r, menu } = await renderAndOpen();

    fireEvent.click(within(menu.container).getByText("Game (Japan)"));
    await waitFor(() => expect(backend.syncRomSaves).toHaveBeenCalledWith(1));
    r.unmount();
    resolveSync({ success: true, message: "", synced: 1 });
    await act(async () => {
      for (let index = 0; index < 8; index++) await Promise.resolve();
    });

    expect(backend.switchVersion).toHaveBeenCalledTimes(1);
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
  });

  it("unmount during the unsynced-save modal stops before Switch anyway commits", async () => {
    let resolveChoice!: (value: "switch_anyway") => void;
    vi.mocked(backend.switchVersion).mockResolvedValue(block);
    vi.mocked(showUnsyncedSavesModal).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveChoice = resolve;
        }),
    );
    const { r, menu } = await renderAndOpen();

    fireEvent.click(within(menu.container).getByText("Game (Japan)"));
    await waitFor(() => expect(showUnsyncedSavesModal).toHaveBeenCalled());
    r.unmount();
    resolveChoice("switch_anyway");
    await act(async () => {
      for (let index = 0; index < 8; index++) await Promise.resolve();
    });

    expect(backend.switchVersion).toHaveBeenCalledTimes(1);
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
  });

  it("a genuine remount admits its own switch while the old modal chain stays stale", async () => {
    let resolveOldChoice!: (value: "switch_anyway") => void;
    vi.mocked(backend.switchVersion)
      .mockResolvedValueOnce(block)
      .mockResolvedValueOnce(block)
      .mockResolvedValueOnce(successResult);
    vi.mocked(showUnsyncedSavesModal)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOldChoice = resolve;
          }),
      )
      .mockResolvedValueOnce("switch_anyway");

    const oldPicker = await renderAndOpen();
    fireEvent.click(within(oldPicker.menu.container).getByText("Game (Japan)"));
    await waitFor(() => expect(showUnsyncedSavesModal).toHaveBeenCalledTimes(1));
    oldPicker.r.unmount();

    const currentPicker = await renderAndOpen();
    await clickRow(currentPicker.menu.container, "Game (Japan)");
    resolveOldChoice("switch_anyway");
    await act(async () => {
      for (let index = 0; index < 8; index++) await Promise.resolve();
    });

    expect(backend.switchVersion).toHaveBeenCalledTimes(3);
    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, false);
    expect(backend.switchVersion).toHaveBeenNthCalledWith(3, APP_ID, 2, true);
    expect(setLaunchOptionsConfirmed).toHaveBeenCalledWith(APP_ID, "");
  });

  it("routes a version_vanished 'Switch anyway' retry through the common refusal path", async () => {
    const vanished = {
      success: false as const,
      reason: "version_vanished" as const,
      message: "This version is no longer available on RomM.",
    };
    vi.mocked(backend.getVersionList)
      .mockReset()
      .mockResolvedValueOnce(multiVersionList())
      .mockResolvedValueOnce(multiVersionList({ bound_vanished: true }));
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce(vanished);
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("switch_anyway");
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });

    const { menu } = await renderAndOpen();
    vi.mocked(backend.fetchCoverBase64).mockClear();
    setRommConnectionState("checking");
    const dispatched = await captureDataChanged(() => clickRow(menu.container, "Game (Japan)"));

    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, true);
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Could not switch version",
      subtext: vanished.message,
    });
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledTimes(2));
    expect(getRommConnectionState()).toBe("connected");
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(backend.fetchCoverBase64).not.toHaveBeenCalled();
    expect(setArt).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);
  });

  it("reports an explicit server_unreachable 'Switch anyway' retry as offline", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce({
      success: false,
      reason: "server_unreachable",
      message: "Server unreachable",
    });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("switch_anyway");

    const { menu } = await renderAndOpen();
    setRommConnectionState("checking");
    await clickRow(menu.container, "Game (Japan)");

    expect(getRommConnectionState()).toBe("offline");
    expect(backend.getVersionList).toHaveBeenCalledTimes(1);
  });

  it("routes a version_vanished post-save retry through the common refusal path", async () => {
    const vanished = {
      success: false as const,
      reason: "version_vanished" as const,
      message: "This version is no longer available on RomM.",
    };
    vi.mocked(backend.getVersionList)
      .mockReset()
      .mockResolvedValueOnce(multiVersionList())
      .mockResolvedValueOnce(multiVersionList({ bound_vanished: true }));
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce(vanished);
    vi.mocked(backend.syncRomSaves).mockResolvedValue({ success: true, message: "", synced: 1 });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");
    const setArt = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("SteamClient", { Apps: { SetCustomArtworkForApp: setArt } });

    const { menu } = await renderAndOpen();
    vi.mocked(backend.fetchCoverBase64).mockClear();
    setRommConnectionState("checking");
    const dispatched = await captureDataChanged(() => clickRow(menu.container, "Game (Japan)"));

    expect(backend.switchVersion).toHaveBeenNthCalledWith(2, APP_ID, 2, false);
    expect(toaster.toast).toHaveBeenCalledWith({
      title: "RomM Sync",
      body: "Could not switch version",
      subtext: vanished.message,
    });
    await waitFor(() => expect(backend.getVersionList).toHaveBeenCalledTimes(2));
    expect(getRommConnectionState()).toBe("connected");
    expect(setLaunchOptionsConfirmed).not.toHaveBeenCalled();
    expect(backend.fetchCoverBase64).not.toHaveBeenCalled();
    expect(setArt).not.toHaveBeenCalled();
    expect(invalidateCachedGameDetail).not.toHaveBeenCalled();
    expect(dispatched.some((e) => e.detail?.type === "version_switched")).toBe(false);
  });

  it("reports an explicit server_unreachable post-save retry as offline", async () => {
    vi.mocked(backend.switchVersion).mockResolvedValueOnce(block).mockResolvedValueOnce({
      success: false,
      reason: "server_unreachable",
      message: "Server unreachable",
    });
    vi.mocked(backend.syncRomSaves).mockResolvedValue({ success: true, message: "", synced: 1 });
    vi.mocked(showUnsyncedSavesModal).mockResolvedValue("sync_and_switch");

    const { menu } = await renderAndOpen();
    setRommConnectionState("checking");
    await clickRow(menu.container, "Game (Japan)");

    expect(getRommConnectionState()).toBe("offline");
    expect(backend.getVersionList).toHaveBeenCalledTimes(1);
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
