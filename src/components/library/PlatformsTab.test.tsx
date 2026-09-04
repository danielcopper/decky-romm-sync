// The Library page's Platforms tab, driven through the page it lives on: what
// the list shows and what pressing a row does, and everything the focused
// platform's detail offers. `PlatformsTab`, `PlatformDetail` and
// `usePlatformsPage` only exist together — the tab renders what the hook holds
// and the detail acts through it — so they are exercised as one here rather
// than against a hand-built state object, which would pin the seam instead of
// the behaviour. The frame around the two tabs, and the Collections tab, are in
// `src/components/LibraryPage.test.tsx`.
//
// CATCH-REJECTION ASSERTION RULE: every catch with a state side effect is
// asserted through what the user then sees — the reverted toggle, the surfaced
// status line — never merely by the rejecting call having been made.
//
// Focus selects, so a row is selected by firing focusin on it, which is what
// Steam's navigation does; the @decky/ui stub in `src/test-setup.ts` forwards
// `onFocus` on a Focusable for exactly that reason.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, act, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { showContextMenu, showModal } from "@decky/ui";
import { LibraryPage } from "../LibraryPage";
import * as backend from "../../api/backend";
import { removeShortcut, setLaunchOptionsConfirmed } from "../../utils/steamShortcuts";
import { clearPlatformCollection } from "../../utils/collections";
import { setSyncProgress } from "../../utils/syncProgress";
import { biosColorForLevel } from "../../utils/biosColor";
import type { FirmwarePlatformExt, PlatformSyncSetting, SystemCoreInfo } from "../../types";

vi.mock("../../utils/scrollHelpers", () => ({ scrollToTop: vi.fn(), scrollElementToTop: vi.fn() }));
vi.mock("../../utils/steamShortcuts", () => ({
  removeShortcut: vi.fn(),
  setLaunchOptionsConfirmed: vi.fn(),
  getAllNonSteamShortcutAppIds: vi.fn(),
  getLiveRomMShortcutAppIds: vi.fn(),
}));
vi.mock("../../utils/collections", () => ({
  clearPlatformCollection: vi.fn(),
  clearAllRomMCollections: vi.fn(),
}));

const MGBA = {
  label: "mGBA",
  kind: "libretro" as const,
  core_so: "mgba_libretro",
  is_default: true,
  bakeable: true,
  reason: null,
};
const VBA = {
  label: "VBA Next",
  kind: "libretro" as const,
  core_so: "vba_next_libretro",
  is_default: false,
  bakeable: true,
  reason: null,
};

function coreInfo(overrides: Partial<SystemCoreInfo> = {}): SystemCoreInfo {
  return {
    emulators: [MGBA, VBA],
    emulator_data_available: true,
    active_core_label: "mGBA",
    ...overrides,
  };
}

function platform(overrides: Partial<PlatformSyncSetting> = {}): PlatformSyncSetting {
  return { id: 1, name: "Game Boy Advance", slug: "gba", rom_count: 12, sync_enabled: true, ...overrides };
}

function firmwareFile(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    file_name: "gba_bios.bin",
    size: 16384,
    md5: "",
    local_path: "/bios/gba_bios.bin",
    downloaded: false,
    description: "GBA BIOS",
    wanted: "needed" as const,
    required_by_active: true,
    on_server: true,
    ...overrides,
  };
}

function firmwarePlatform(overrides: Partial<FirmwarePlatformExt> = {}): FirmwarePlatformExt {
  return {
    platform_slug: "gba",
    files: [firmwareFile()],
    has_games: true,
    bios_level: "missing",
    required_count: 1,
    required_downloaded: 0,
    required_withheld: 0,
    server_count: 1,
    local_count: 0,
    deletable_count: 0,
    ...overrides,
  };
}

const flushAsync = () =>
  act(async () => {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  });

/** Fire the focus event Steam's navigation fires when a row takes focus.
 *  A list row is the one Focusable that wraps a single toggle — the panes and
 *  the list itself are Focusables too, and each of them holds every toggle. */
async function focusRow(container: HTMLElement, name: string): Promise<void> {
  const row = [...container.querySelectorAll<HTMLElement>('[data-testid="focusable"]')].find(
    (el) => el.textContent.includes(name) && el.querySelectorAll('[data-testid="toggle-input"]').length === 1,
  );
  if (!row) throw new Error(`no list row for ${name}`);
  await act(async () => {
    fireEvent.focusIn(row);
    for (let i = 0; i < 6; i++) await Promise.resolve();
  });
}

function buttonByText(container: HTMLElement, text: string): HTMLButtonElement | undefined {
  return [...container.querySelectorAll("button")].find((b) => b.textContent === text);
}

/** The pane's own On-disk palette, spelled out here so a silent change to one of
 *  the four states the device pass asked for fails rather than passes. */
const GREEN = "#5ba32b";
const RED = "#d94126";
const PALE_GREEN = "#8fc46b";
const GREY = "#8f98a0";
const AMBER = "#d4a72c";

/** The BIOS table's On-disk marks, in row order: the glyph and the colour it is
 *  drawn in. The glyph carries the verdict and the colour carries the need, so a
 *  test that read only one of them would pass on half the encoding. */
function diskMarks(container: HTMLElement): { glyph: string; color: string }[] {
  return [...container.querySelectorAll<HTMLElement>("span[title]")]
    .filter((el) => /^[✓✗?]$/.test(el.textContent.trim().charAt(0)))
    .map((el) => ({ glyph: el.textContent.trim().charAt(0), color: el.style.color }));
}

/** The BIOS table's Contents cells, by their exact text.
 *  Exact rather than a substring of the pane: "unknown" is also a word in the
 *  readiness summary above the table, so a substring match would pass on a cell
 *  that says nothing of the kind. */
function contentsCells(container: HTMLElement): string[] {
  const cells = [...container.querySelectorAll("span")].map((s) => s.textContent);
  return cells.filter((text) => /^(—|an image|\d+ images?|no image|unknown)$/.test(text));
}

function lastModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

/** Accept the ConfirmModal the last showModal call carried. */
async function confirmLastModal(): Promise<void> {
  const props = lastModalProps<{ onOK?: () => void }>();
  await act(async () => {
    props?.onOK?.();
    for (let i = 0; i < 8; i++) await Promise.resolve();
  });
}

/** Render the Menu the last showContextMenu call carried, and press one entry. */
async function pickFromCoreMenu(label: string): Promise<void> {
  const calls = vi.mocked(showContextMenu).mock.calls;
  const menu = calls[calls.length - 1]?.[0] as ReactElement | undefined;
  if (!menu) throw new Error("no context menu was shown");
  const { container } = render(menu);
  const entry = [...container.querySelectorAll("button")].find((b) => b.textContent.startsWith(label));
  if (!entry) throw new Error(`no menu entry starting with ${label}`);
  await act(async () => {
    fireEvent.click(entry);
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
}

describe("Library › Platforms", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(backend.getPlatforms).mockResolvedValue({ success: true, platforms: [platform()] });
    vi.mocked(backend.getFirmwareStatus).mockResolvedValue({ success: true, platforms: [firmwarePlatform()] });
    vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({ platforms: [{ slug: "gba", name: "GBA", count: 9 }] });
    vi.mocked(backend.getSystemCoreInfo).mockResolvedValue(coreInfo());
    vi.mocked(backend.countPlatformSaves).mockResolvedValue({ count: 3 });
    vi.mocked(backend.savePlatformSync).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.setAllPlatformsSync).mockResolvedValue({ success: true, message: "" });
    vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: [] });
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
    vi.mocked(clearPlatformCollection).mockResolvedValue(undefined);
  });

  afterEach(() => {
    act(() => {
      setSyncProgress({ running: false, stage: "", current: 0, total: 0, message: "", runId: "" });
    });
  });

  // ------------------------------------------------------------------
  // The list
  // ------------------------------------------------------------------
  describe("the list", () => {
    const threePlatforms = [
      platform({ id: 1, name: "Nintendo 64", slug: "n64", sync_enabled: false }),
      platform({ id: 2, name: "Game Boy Advance", slug: "gba", sync_enabled: true }),
      platform({ id: 3, name: "Dreamcast", slug: "dc", sync_enabled: true }),
    ];

    it("groups Synced above Available, alphabetical inside each", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({ success: true, platforms: threePlatforms });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const text = container.textContent;
      expect(text.indexOf("SYNCED (2)")).toBeLessThan(text.indexOf("Dreamcast"));
      expect(text.indexOf("Dreamcast")).toBeLessThan(text.indexOf("Game Boy Advance"));
      expect(text.indexOf("Game Boy Advance")).toBeLessThan(text.indexOf("AVAILABLE (1)"));
      expect(text.indexOf("AVAILABLE (1)")).toBeLessThan(text.indexOf("Nintendo 64"));
    });

    it("keeps the order frozen when a row is toggled off", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({ success: true, platforms: threePlatforms });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const toggles = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]');
      await act(async () => {
        fireEvent.click(toggles[0]!);
        await Promise.resolve();
      });

      // Dreamcast is now off and still stands first, under Synced.
      const text = container.textContent;
      expect(text.indexOf("Dreamcast")).toBeLessThan(text.indexOf("AVAILABLE (1)"));
      expect(toggles[0]!.checked).toBe(false);
    });

    it("states the BIOS requirement as a number and an em dash where none is wanted", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [
          platform({ id: 1, slug: "gba", name: "Game Boy Advance" }),
          platform({ id: 2, slug: "n64", name: "Nintendo 64" }),
        ],
      });
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({ required_count: 5, required_downloaded: 3, bios_level: "partial" }),
          firmwarePlatform({ platform_slug: "n64", required_count: 0, bios_level: "ok", files: [] }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("3 / 5");
      // Nothing required → no ratio, and the dot is still there because the
      // firmware read did speak for the platform.
      expect(container.querySelector('[data-testid="bios-dot-n64"]')).toBeTruthy();
      expect(container.textContent).toContain("—");
    });

    it("draws the row's ratio in the dot's colour, and states the header's number", async () => {
      // The point of the ratio in the row is scanning the list without walking
      // it, which needs the colour as well as the number — a red 0 / 2 has to be
      // findable at a glance. It is the same number the detail's header carries.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [firmwarePlatform({ required_count: 2, required_downloaded: 0, bios_level: "missing" })],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const ratio = [...container.querySelectorAll<HTMLElement>("span")].find((el) => el.textContent === "0 / 2");
      expect(ratio).toBeTruthy();
      expect(ratio!.style.color).toBe(biosColorForLevel("missing"));
      expect(container.textContent).toContain("BIOS 0 / 2");
    });

    it("keeps the dot grey rather than dropping it where there is no level", async () => {
      // Drawn on every row: a dot that comes and goes shifts each name beside
      // it, and the list is meant to be scanned down its left edge. Grey is the
      // colour for "no level", which is what the shared mapping answers.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({ success: true, platforms: [] });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const dot = container.querySelector<HTMLElement>('[data-testid="bios-dot-gba"]');
      expect(dot).not.toBeNull();
      expect(dot!.style.backgroundColor).toBe(biosColorForLevel(null));
    });

    it("toggles a platform optimistically through its own id", async () => {
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const toggle = container.querySelector<HTMLInputElement>('[data-testid="toggle-input"]')!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.savePlatformSync)).toHaveBeenCalledWith(1, false);
      expect(toggle.checked).toBe(false);
    });

    it("puts the toggle back when the write is refused", async () => {
      vi.mocked(backend.savePlatformSync).mockRejectedValue(new Error("offline"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const toggle = container.querySelector<HTMLInputElement>('[data-testid="toggle-input"]')!;
      await act(async () => {
        fireEvent.click(toggle);
        for (let i = 0; i < 4; i++) await Promise.resolve();
      });

      expect(toggle.checked).toBe(true);
    });

    it("enables and disables every platform from above the groups", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({ success: true, platforms: threePlatforms });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByText(container, "Disable all")!);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setAllPlatformsSync)).toHaveBeenCalledWith(false);
      for (const t of container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]')) {
        expect(t.checked).toBe(false);
      }
    });

    it("restores the previous set when Enable all is refused", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({ success: true, platforms: threePlatforms });
      vi.mocked(backend.setAllPlatformsSync).mockRejectedValue(new Error("offline"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByText(container, "Enable all")!);
        for (let i = 0; i < 4; i++) await Promise.resolve();
      });

      const checked = [...container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]')].map(
        (t) => t.checked,
      );
      // Dreamcast, GBA on; Nintendo 64 off — exactly the pre-press set.
      expect(checked).toEqual([true, true, false]);
    });

    it("says so when the platform list itself cannot be read", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({ success: false, platforms: [] });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read your platforms");
    });

    it("says so when the platform read throws", async () => {
      vi.mocked(backend.getPlatforms).mockRejectedValue(new Error("net"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read your platforms");
    });
  });

  // ------------------------------------------------------------------
  // Selection and the per-platform core read
  // ------------------------------------------------------------------
  describe("the core read", () => {
    it("reads the core of the platform that opens selected, once", async () => {
      render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(vi.mocked(backend.getSystemCoreInfo)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.getSystemCoreInfo)).toHaveBeenCalledWith("gba");
    });

    it("reads a platform's core once however often it is selected", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [
          platform({ id: 1, slug: "gba", name: "Game Boy Advance" }),
          platform({ id: 2, slug: "n64", name: "Nintendo 64" }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await focusRow(container, "Nintendo 64");
      await focusRow(container, "Game Boy Advance");
      await focusRow(container, "Nintendo 64");

      expect(vi.mocked(backend.getSystemCoreInfo).mock.calls.map((c) => c[0])).toEqual(["gba", "n64"]);
    });

    it("leaves an action's result on the platform it was about", async () => {
      // Walking the list while a download's line is up must not carry that line
      // onto another platform's pane, nor lose it on the way back.
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [
          platform({ id: 1, slug: "gba", name: "Game Boy Advance" }),
          platform({ id: 2, slug: "n64", name: "Nintendo 64" }),
        ],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: true,
        message: "Downloaded 1 required firmware files",
        downloaded: 1,
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Download required (1)")!);
        for (let i = 0; i < 8; i++) await Promise.resolve();
      });
      expect(within(container).getByTestId("status-bios").textContent).toBe("Downloaded 1 required firmware files");

      await focusRow(container, "Nintendo 64");
      expect(within(container).queryByTestId("status-bios")).toBeNull();

      await focusRow(container, "Game Boy Advance");
      expect(within(container).getByTestId("status-bios").textContent).toBe("Downloaded 1 required firmware files");
    });

    it("says which platform is working while another pane's buttons are disabled", async () => {
      // Every platform's actions disable while one runs, and the line that would
      // explain the wait is bound to the platform acting — so the pane the
      // reader walks to has to say why its own buttons are dead.
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [
          platform({ id: 1, slug: "gba", name: "Game Boy Advance" }),
          platform({ id: 2, slug: "n64", name: "Nintendo 64" }),
        ],
      });
      // The other pane needs buttons of its own to be shown disabled, so both
      // platforms carry a firmware entry and a shortcut count.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [firmwarePlatform(), firmwarePlatform({ platform_slug: "n64" })],
      });
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [
          { slug: "gba", name: "GBA", count: 9 },
          { slug: "n64", name: "N64", count: 4 },
        ],
      });
      let finish: (v: { success: boolean; message: string; downloaded: number }) => void = () => {};
      vi.mocked(backend.downloadRequiredFirmware).mockReturnValue(
        new Promise((resolve) => {
          finish = resolve;
        }),
      );
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Download required (1)")!);
        for (let i = 0; i < 4; i++) await Promise.resolve();
      });

      await focusRow(container, "Nintendo 64");
      expect(container.textContent).toContain("Working on Game Boy Advance");
      expect(buttonByText(container, "Download required (1)")?.disabled).toBe(true);

      await act(async () => {
        finish({ success: true, message: "Downloaded 1 required firmware files", downloaded: 1 });
        for (let i = 0; i < 8; i++) await Promise.resolve();
      });
      expect(container.textContent).not.toContain("Working on Game Boy Advance");
    });

    it("says the read failed rather than reading forever", async () => {
      vi.mocked(backend.getSystemCoreInfo).mockRejectedValue(new Error("net"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read the emulators for this platform");
    });
  });

  // ------------------------------------------------------------------
  // The detail's header and its core section
  // ------------------------------------------------------------------
  describe("the detail", () => {
    it("carries the counts and the core on one header line", async () => {
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("12 on RomM · 9 in Steam · mGBA");
    });

    it("asks for a sync before offering a core when nothing of the platform is in Steam", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({ platforms: [] });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Sync this platform first");
      expect(buttonByText(container, "Change core ›")).toBeUndefined();
    });

    it("offers nothing to switch when the platform has one emulator", async () => {
      vi.mocked(backend.getSystemCoreInfo).mockResolvedValue(coreInfo({ emulators: [MGBA] }));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("offers one emulator");
      expect(buttonByText(container, "Change core ›")).toBeUndefined();
    });

    it("says RetroDECK was not found rather than showing an empty picker", async () => {
      vi.mocked(backend.getSystemCoreInfo).mockResolvedValue(
        coreInfo({ emulators: [], emulator_data_available: false, active_core_label: null }),
      );
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("RetroDECK was not found");
    });

    it("says the shortcut count failed instead of stating three things that are not true", async () => {
      // Read as zero, a failed count withdraws the core picker, empties the
      // header and disables the removal — none of which was established.
      vi.mocked(backend.getRegistryPlatforms).mockRejectedValue(new Error("net"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read how many of these games are in Steam");
      expect(container.textContent).not.toContain("in Steam ·");
      expect(container.textContent).not.toContain("Sync this platform first");
      expect(buttonByText(container, "Change core ›")).toBeTruthy();
      expect(buttonByText(container, "Remove shortcuts")).not.toBeDisabled();
    });

    it("tells a failed BIOS read apart from a platform the overview cannot speak for", async () => {
      vi.mocked(backend.getFirmwareStatus).mockRejectedValue(new Error("net"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read the BIOS state");
      expect(container.textContent).not.toContain("Nothing is known about this platform");
    });

    it("reads a platform the overview simply has no entry for as an answer", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({ success: true, platforms: [] });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Nothing is known about this platform");
      expect(container.textContent).not.toContain("Could not read the BIOS state");
    });

    it("surfaces a BIOS read that answered a failure rather than dropping it", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: false,
        platforms: [],
        message: "backend said no",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read the BIOS state");
    });

    it("says a failed re-read on the pane that still shows the old rows", async () => {
      // A failed refresh does not clear the map, so this pane keeps its
      // pre-change rows. That is where a reader needs telling — the notice used
      // to appear only where there were no rows to be wrong about.
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: true,
        message: "Downloaded 1 required firmware files",
        downloaded: 1,
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("gba_bios.bin");

      vi.mocked(backend.getFirmwareStatus).mockRejectedValue(new Error("net"));
      await act(async () => {
        fireEvent.click(buttonByText(container, "Download required (1)")!);
        for (let i = 0; i < 10; i++) await Promise.resolve();
      });

      // The rows are still there, and now so is the warning that they are stale.
      expect(container.textContent).toContain("gba_bios.bin");
      expect(container.textContent).toContain("what is below may be out of date");
    });

    it("keeps the removal group and disables what there is nothing to delete", async () => {
      // Never hidden: a platform whose shortcuts are gone but whose saves remain
      // must still offer the button that reaches them, and this is the only page
      // that offers it.
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({ platforms: [] });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(buttonByText(container, "Remove 0 shortcuts")).toBeDisabled();
      expect(buttonByText(container, "Delete 3 save files")).not.toBeDisabled();
    });

    it("spins while the count is unread and disables on a real zero", async () => {
      // Three states, three appearances: unread claims nothing (disabled, a
      // spinner), zero states an emptiness the read established (disabled, the
      // number), and a failure says so rather than showing either.
      let answer: (v: { count: number }) => void = () => {};
      vi.mocked(backend.countPlatformSaves).mockReturnValue(
        new Promise((resolve) => {
          answer = resolve;
        }),
      );
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      const pending = buttonByText(container, "Delete save files");
      expect(pending).toBeDisabled();
      expect(pending!.querySelector('[data-testid="spinner"]')).toBeTruthy();

      await act(async () => {
        answer({ count: 0 });
        for (let i = 0; i < 6; i++) await Promise.resolve();
      });
      expect(buttonByText(container, "Delete 0 save files")).toBeDisabled();
      expect(container.querySelector('[data-testid="spinner"]')).toBeNull();
    });

    it("says the saves count could not be read instead of spinning forever", async () => {
      vi.mocked(backend.countPlatformSaves).mockRejectedValue(new Error("db"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("Could not read how many save files this platform holds");
      expect(container.querySelector('[data-testid="spinner"]')).toBeNull();
      // Not a zero either: the button is pressable, because a failed count is
      // not evidence that there is nothing to delete.
      expect(buttonByText(container, "Delete save files")).not.toBeDisabled();
    });
  });

  // ------------------------------------------------------------------
  // Changing the core
  // ------------------------------------------------------------------
  describe("changing the core", () => {
    beforeEach(() => {
      vi.mocked(backend.setSystemCore).mockResolvedValue({ success: true, rebake_items: [] });
    });

    async function openCoreMenu(container: HTMLElement): Promise<void> {
      await act(async () => {
        fireEvent.click(buttonByText(container, "Change core ›")!);
        await Promise.resolve();
      });
    }

    it("pins the picked emulator", async () => {
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await openCoreMenu(container);
      await pickFromCoreMenu("VBA Next");

      expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("gba", "VBA Next");
    });

    it("clears the override when the default-marked emulator is picked", async () => {
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await openCoreMenu(container);
      await pickFromCoreMenu("mGBA");

      expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("gba", "");
    });

    it("re-bakes every affected shortcut, re-reads the core and the BIOS state, and tells the game page", async () => {
      vi.mocked(backend.setSystemCore).mockResolvedValue({
        success: true,
        rebake_items: [{ app_id: 7, launch_options: "flatpak run … -e vba_next_libretro" }],
      });
      vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
      const events: CustomEvent[] = [];
      const listener = ((e: Event) => events.push(e as CustomEvent)) as EventListener;
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const { container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        const firmwareReadsBefore = vi.mocked(backend.getFirmwareStatus).mock.calls.length;
        await openCoreMenu(container);
        await pickFromCoreMenu("VBA Next");
        await flushAsync();

        expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(7, "flatpak run … -e vba_next_libretro");
        expect(vi.mocked(backend.getFirmwareStatus).mock.calls.length).toBeGreaterThan(firmwareReadsBefore);
        expect(vi.mocked(backend.getSystemCoreInfo)).toHaveBeenCalledTimes(2);
        expect(events.map((e) => e.detail.type)).toContain("core_changed");
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("shows a refused switch instead of leaving the old label standing (#1016)", async () => {
      vi.mocked(backend.setSystemCore).mockResolvedValue({
        success: false,
        message: "RetroDECK is not installed",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await openCoreMenu(container);
      await pickFromCoreMenu("VBA Next");

      expect(within(container).getByTestId("status-core").textContent).toBe("RetroDECK is not installed");
      // The label the header carries is still the core that is actually active.
      expect(container.textContent).toContain("· mGBA");
    });

    it("shows a switch that threw", async () => {
      vi.mocked(backend.setSystemCore).mockRejectedValue(new Error("bridge closed"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await openCoreMenu(container);
      await pickFromCoreMenu("VBA Next");

      expect(within(container).getByTestId("status-core").textContent).toBe("Could not change the core");
    });
  });

  // ------------------------------------------------------------------
  // The BIOS table and its buttons
  // ------------------------------------------------------------------
  describe("the BIOS files", () => {
    it("renders the three columns, and a file row's Contents is the em dash (#1803)", async () => {
      // Nothing ASKED about a plain file's contents — the machine-wide reading
      // is deliberately unverified — so the em dash stands for the absence of a
      // question, never for a question answered "nothing".
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      // "File", not "Wanted": the wanted word left the row when colour took over,
      // so a column header naming it would label nothing.
      expect(container.textContent).toContain("File");
      expect(container.textContent).toContain("On disk");
      expect(container.textContent).toContain("Contents");
      expect(container.textContent).toContain("gba_bios.bin");
      expect(contentsCells(container)).toContain("—");
    });

    it("marks the four need-and-verdict states apart, and says so in a legend", async () => {
      // The glyph is the VERDICT, the colour is the NEED — two facts, two
      // channels, so neither has to be read off the other. `satisfied` is the
      // verdict, never presence.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [
              firmwareFile({ file_name: "a.bin", required_by_active: true, downloaded: true, satisfied: true }),
              firmwareFile({ file_name: "b.bin", required_by_active: true, downloaded: false, satisfied: false }),
              firmwareFile({
                file_name: "c.bin",
                wanted: "optional",
                required_by_active: false,
                downloaded: true,
                satisfied: true,
              }),
              firmwareFile({
                file_name: "d.bin",
                wanted: "optional",
                required_by_active: false,
                downloaded: false,
                satisfied: false,
              }),
            ],
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(diskMarks(container)).toEqual([
        { glyph: "✓", color: GREEN },
        { glyph: "✗", color: RED },
        { glyph: "✓", color: PALE_GREEN },
        { glyph: "✗", color: GREY },
      ]);
      for (const line of ["required, here", "required, missing", "here, not required", "missing, not required"]) {
        expect(container.textContent).toContain(line);
      }
    });

    it("marks a row nothing could judge apart from one shown to be missing", async () => {
      // Amber, not red: calling it missing would claim an absence nothing
      // established, and the row's own verdict is what the register forbids
      // guessing at.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [
              firmwareFile({ file_name: "folder", declared_kind: "directory", downloaded: true, satisfied: null }),
              firmwareFile({ file_name: "e.bin", wanted: "unknown", required_by_active: false, downloaded: false }),
            ],
            bios_level: "unknown",
            required_withheld: 1,
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(diskMarks(container)).toEqual([
        { glyph: "?", color: AMBER },
        { glyph: "?", color: AMBER },
      ]);
      expect(container.textContent).toContain("could not be checked");
      expect(container.textContent).not.toContain("required, missing");
    });

    it("says a file name once", async () => {
      // RomM answers an entry with no description of its own by repeating the
      // file name, and the row used to print both.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({ files: [firmwareFile({ file_name: "scph7003.bin", description: "scph7003.bin" })] }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent.split("scph7003.bin").length - 1).toBe(1);
    });

    it("counts a satisfied folder's images in Contents and lists them under the row", async () => {
      const images = ["Japan    v1.00  ROM1", "USA      v2.20  ROM1"];
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [
              firmwareFile({
                file_name: "bios",
                declared_kind: "directory",
                downloaded: true,
                satisfied: true,
                images,
              }),
            ],
            required_downloaded: 1,
            bios_level: "ok",
            local_count: 1,
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(contentsCells(container)).toContain("2 images");
      // Verbatim, padding and all — that alignment is what makes a line
      // matchable against the emulator's own picker.
      for (const image of images) expect(container.textContent).toContain(image);
    });

    it("says a folder holds no image where the read established that", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [
              firmwareFile({ file_name: "bios", declared_kind: "directory", downloaded: true, satisfied: false }),
            ],
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(contentsCells(container)).toContain("no image");
    });

    it("says a folder's contents are unknown where nothing could establish them", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [firmwareFile({ file_name: "bios", declared_kind: "directory", downloaded: true, satisfied: null })],
            bios_level: "unknown",
            required_withheld: 1,
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(contentsCells(container)).toContain("unknown");
    });

    it("offers a Download on a missing row the library holds (#164)", async () => {
      vi.mocked(backend.downloadPlatformFirmwareFile).mockResolvedValue({
        success: true,
        message: "Downloaded gba_bios.bin",
        downloaded: 1,
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByText(container, "Download")!);
        for (let i = 0; i < 8; i++) await Promise.resolve();
      });

      expect(vi.mocked(backend.downloadPlatformFirmwareFile)).toHaveBeenCalledWith("gba", "gba_bios.bin");
      expect(within(container).getByTestId("status-bios").textContent).toBe("Downloaded gba_bios.bin");
    });

    it("offers no Download for a file already here, nor for one the library does not hold", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [
              firmwareFile({ file_name: "here.bin", downloaded: true }),
              firmwareFile({ file_name: "elsewhere.bin", on_server: false, id: null }),
            ],
            required_downloaded: 1,
            local_count: 1,
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(buttonByText(container, "Download")).toBeUndefined();
    });

    it("offers no Download for a folder declaration, absent or not", async () => {
      // The emulator LISTS that name, so there is no file to fetch into it —
      // what would satisfy the row is a BIOS image inside the folder, which is a
      // row of its own. Absent is the case a presence check would let through.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            files: [
              firmwareFile({ file_name: "bios", declared_kind: "directory", downloaded: false, satisfied: false }),
            ],
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("bios");
      expect(buttonByText(container, "Download")).toBeUndefined();
      expect(buttonByText(container, "Download all")).toBeUndefined();
    });

    it("withdraws every download while RomM is unreachable", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        server_offline: true,
        platforms: [firmwarePlatform()],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(buttonByText(container, "Download")).toBeUndefined();
      expect(buttonByText(container, "Download required (1)")).toBeUndefined();
      expect(buttonByText(container, "Download all")).toBeUndefined();
    });

    it("fetches the required files and announces the change", async () => {
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: true,
        message: "Downloaded 1 required firmware files",
        downloaded: 1,
      });
      const events: CustomEvent[] = [];
      const listener = ((e: Event) => events.push(e as CustomEvent)) as EventListener;
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const { container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          fireEvent.click(buttonByText(container, "Download required (1)")!);
          for (let i = 0; i < 8; i++) await Promise.resolve();
        });

        expect(vi.mocked(backend.downloadRequiredFirmware)).toHaveBeenCalledWith("gba");
        expect(events.map((e) => e.detail.type)).toContain("bios");
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("stays silent towards the game page when a run moved no file", async () => {
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        message: "Downloaded 0 firmware files",
        downloaded: 0,
      });
      const events: CustomEvent[] = [];
      const listener = ((e: Event) => events.push(e as CustomEvent)) as EventListener;
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const { container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          fireEvent.click(buttonByText(container, "Download all")!);
          for (let i = 0; i < 8; i++) await Promise.resolve();
        });

        expect(events).toHaveLength(0);
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("surfaces a download that threw", async () => {
      vi.mocked(backend.downloadRequiredFirmware).mockRejectedValue(new Error("io"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Download required (1)")!);
        for (let i = 0; i < 8; i++) await Promise.resolve();
      });

      expect(within(container).getByTestId("status-bios").textContent).toContain("Download failed");
    });

    it("offers no delete when no download record is still on disk", async () => {
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(buttonByText(container, "Delete BIOS (0)")).toBeUndefined();
    });

    it("confirms before deleting, then deletes and announces the change", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [firmwarePlatform({ deletable_count: 2 })],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: true,
        deleted_count: 2,
        message: "Deleted 2 BIOS files",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByText(container, "Delete BIOS (2)")!);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.deletePlatformBios)).not.toHaveBeenCalled();
      expect(lastModalProps<{ strTitle?: string }>()?.strTitle).toBe("Delete BIOS files for Game Boy Advance?");

      await confirmLastModal();
      expect(vi.mocked(backend.deletePlatformBios)).toHaveBeenCalledWith("gba");
      expect(within(container).getByTestId("status-bios").textContent).toBe("Deleted 2 BIOS files");
    });

    it("surfaces a BIOS delete that threw", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [firmwarePlatform({ deletable_count: 2 })],
      });
      vi.mocked(backend.deletePlatformBios).mockRejectedValue(new Error("io"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Delete BIOS (2)")!);
        await Promise.resolve();
      });
      await confirmLastModal();

      expect(within(container).getByTestId("status-bios").textContent).toContain("Failed to delete BIOS files");
    });

    it("announces nothing when the BIOS delete refuses", async () => {
      // The event fans out to every open game page and each match pays a live
      // check_platform_bios for it, so a delete that moved no file must stay
      // silent rather than send one nothing can act on.
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [firmwarePlatform({ deletable_count: 2 })],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: false,
        deleted_count: 0,
        message: "Nothing to delete",
      });
      const events: CustomEvent[] = [];
      const listener = ((e: Event) => events.push(e as CustomEvent)) as EventListener;
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const { container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          fireEvent.click(buttonByText(container, "Delete BIOS (2)")!);
          await Promise.resolve();
        });
        await confirmLastModal();

        expect(within(container).getByTestId("status-bios").textContent).toBe("Nothing to delete");
        expect(events).toHaveLength(0);
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("withdraws the downloads and says why when nothing could be established", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          firmwarePlatform({
            bios_level: "unknown",
            required_withheld: 0,
            required_count: 0,
            files: [firmwareFile({ wanted: "unknown", required_by_active: false })],
          }),
        ],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("BIOS requirement unknown");
      expect(container.textContent).toContain("BIOS management is not supported for this system yet");
      expect(buttonByText(container, "Download")).toBeUndefined();
      expect(buttonByText(container, "Download all")).toBeUndefined();
    });

    it("keeps the downloads when only the readiness verdict is withheld", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [firmwarePlatform({ bios_level: "unknown", required_withheld: 1 })],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(container.textContent).toContain("BIOS readiness unknown");
      expect(container.textContent).toContain("A required file could not be judged");
      expect(buttonByText(container, "Download required (1)")).toBeTruthy();
    });
  });

  // ------------------------------------------------------------------
  // Removing a platform's shortcuts and saves
  // ------------------------------------------------------------------
  describe("removing", () => {
    beforeEach(() => {
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [11, 12],
        rom_ids: [1, 2],
        platform_name: "Game Boy Advance",
      });
      vi.mocked(backend.reportRemovalResults).mockResolvedValue({ success: true, message: "" });
      vi.mocked(removeShortcut).mockResolvedValue(undefined);
    });

    it("confirms, then removes the shortcuts, clears the collection and reports back", async () => {
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.removePlatformShortcuts)).not.toHaveBeenCalled();

      await confirmLastModal();
      await flushAsync();

      expect(vi.mocked(backend.removePlatformShortcuts)).toHaveBeenCalledWith("gba");
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(2);
      expect(vi.mocked(clearPlatformCollection)).toHaveBeenCalledWith("Game Boy Advance", expect.any(AbortSignal));
      expect(vi.mocked(backend.reportRemovalResults)).toHaveBeenCalledWith([1, 2], null);
    });

    it("surfaces a shortcut removal that threw", async () => {
      vi.mocked(backend.removePlatformShortcuts).mockRejectedValue(new Error("net"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      await confirmLastModal();

      expect(within(container).getByTestId("status-remove").textContent).toBe("Failed to remove shortcuts");
    });

    it("clears the collection under the name the backend answered with", async () => {
      // The backend knows the Steam collection's real name; the row's name is
      // RomM's and can differ, so it is only the fallback — which is why the two
      // names here are deliberately not the same string.
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [11, 12],
        rom_ids: [1, 2],
        platform_name: "Nintendo - Game Boy Advance",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      await confirmLastModal();
      await flushAsync();

      expect(vi.mocked(clearPlatformCollection)).toHaveBeenCalledWith(
        "Nintendo - Game Boy Advance",
        expect.any(AbortSignal),
      );
    });

    it("falls back to the row's name when the backend names no collection", async () => {
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [11],
        rom_ids: [1],
        platform_name: "",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      await confirmLastModal();
      await flushAsync();

      expect(vi.mocked(clearPlatformCollection)).toHaveBeenCalledWith("Game Boy Advance", expect.any(AbortSignal));
    });

    it("reports nothing back when the removal freed no rows and holds no lease", async () => {
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [11],
        rom_ids: [],
        platform_name: "Game Boy Advance",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      await confirmLastModal();
      await flushAsync();

      expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
      expect(within(container).getByTestId("status-remove").textContent).toBe("Removed 1 Game Boy Advance game");
    });

    it("keeps the removal progress on the platform it is removing", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [
          platform({ id: 1, slug: "gba", name: "Game Boy Advance" }),
          platform({ id: 2, slug: "n64", name: "Nintendo 64" }),
        ],
      });
      // `removeShortcut` is synchronous, so the removals cannot be paused; the
      // collection clear that follows them can, and the progress line is still
      // up at that point — it is cleared in the continuation's `finally`.
      let releaseClear: () => void = () => {};
      vi.mocked(clearPlatformCollection).mockImplementation(
        () =>
          new Promise<void>((resolve) => {
            releaseClear = resolve;
          }),
      );
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      await confirmLastModal();
      await flushAsync();

      expect(container.textContent).toContain("Removing 2 of 2…");

      await focusRow(container, "Nintendo 64");
      expect(container.textContent).not.toContain("Removing 2 of 2…");

      await focusRow(container, "Game Boy Advance");
      expect(container.textContent).toContain("Removing 2 of 2…");

      await act(async () => {
        releaseClear();
        for (let i = 0; i < 10; i++) await Promise.resolve();
      });
    });

    it("surfaces the sync_active refusal and removes nothing", async () => {
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: false,
        reason: "sync_active",
        message: "A library sync is in progress — wait for it to finish.",
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Remove 9 shortcuts")!);
        await Promise.resolve();
      });
      await confirmLastModal();

      expect(within(container).getByTestId("status-remove").textContent).toContain("A library sync is in progress");
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();
      expect(vi.mocked(clearPlatformCollection)).not.toHaveBeenCalled();
    });

    it("disables the shortcut removal with its reason while a sync runs", async () => {
      act(() => {
        setSyncProgress({ running: true, stage: "applying", current: 1, total: 5, message: "", runId: "run-1" });
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      expect(buttonByText(container, "Remove 9 shortcuts")).toBeDisabled();
      expect(container.textContent).toContain("Unavailable while a library sync is running.");
      // The save deletion is not sync-gated.
      expect(buttonByText(container, "Delete 3 save files")).not.toBeDisabled();
    });

    it("confirms before deleting saves, then deletes them and tells the save surfaces", async () => {
      vi.mocked(backend.deletePlatformSaves).mockResolvedValue({
        success: true,
        deleted_count: 3,
        message: "Deleted 3 save files",
      });
      const events: CustomEvent[] = [];
      const listener = ((e: Event) => events.push(e as CustomEvent)) as EventListener;
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const { container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          fireEvent.click(buttonByText(container, "Delete 3 save files")!);
          await Promise.resolve();
        });
        expect(vi.mocked(backend.deletePlatformSaves)).not.toHaveBeenCalled();
        expect(lastModalProps<{ strTitle?: string }>()?.strTitle).toBe("Delete all save files for Game Boy Advance?");

        await confirmLastModal();
        expect(vi.mocked(backend.deletePlatformSaves)).toHaveBeenCalledWith("gba");
        expect(within(container).getByTestId("status-remove").textContent).toBe("Deleted 3 save files");
        expect(events.map((e) => e.detail.type)).toContain("save_sync");
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("re-reads the saves count after deleting, so the button stops offering them", async () => {
      vi.mocked(backend.deletePlatformSaves).mockResolvedValue({
        success: true,
        deleted_count: 3,
        message: "Deleted 3 save files",
      });
      vi.mocked(backend.countPlatformSaves).mockResolvedValueOnce({ count: 3 }).mockResolvedValue({ count: 0 });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByText(container, "Delete 3 save files")!);
        await Promise.resolve();
      });
      await confirmLastModal();
      await flushAsync();

      expect(buttonByText(container, "Delete 0 save files")).toBeDisabled();
    });

    it("surfaces a save deletion that threw", async () => {
      vi.mocked(backend.deletePlatformSaves).mockRejectedValue(new Error("io"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByText(container, "Delete 3 save files")!);
        await Promise.resolve();
      });
      await confirmLastModal();

      expect(within(container).getByTestId("status-remove").textContent).toBe("Failed to delete saves");
    });
  });
});
