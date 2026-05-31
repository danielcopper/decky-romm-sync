// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (rolled-back toggle state, surfaced biosError /
// biosStatus string, debugLog spy, etc.). Asserting only that the rejecting
// call was invoked is vacuous — the rejection happens after the call returns
// so the test would pass with or without the .catch.
//
// LibraryPage catch sites (all asserted below):
//   - handleToggle catch → rollback setSyncPlatforms (sync_enabled flips back)
//   - handleSetAll catch → restore previous platforms snapshot
//   - handleCollectionToggle catch → rollback setCollections
//   - handleSetAllCollections catch → restore previous collections snapshot
//   - platform-groups inline catch → setPlatformGroups(!value) rollback
//   - refreshBios try/catch → setBiosError(`Failed to fetch firmware status: ${e}`)
//   - handleDownloadAll catch → setBiosStatus(`Download failed: ${e}`)
//   - handleDownloadRequired catch → setBiosStatus(`Download failed: ${e}`)
//   - getCollections/getSettings .catch → setCollectionsError(true)
//
// MUTATION CHECKS (by inspection — auto-mode classifier likely blocks on
// React state internals, so confidence is recorded here):
//   1. Removing the rollback inside handleToggle's catch would break the
//      "platform toggle rejection reverts checked state" test — the captured
//      ToggleField checked prop would remain at the optimistic value.
//   2. Removing setBiosError(result.message ...) from refreshBios's failure
//      branch would break the "biosError surfaces result.message" test —
//      the Error field's description would no longer render the message.
//   3. Removing the `!collectionsLoaded.current` guard would break the
//      "switching back to collections tab does not refetch" test — getCollections
//      would be called twice instead of once.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { LibraryPage } from "./LibraryPage";
import * as backend from "../api/backend";
import type { PlatformSyncSetting, CollectionSyncSetting, FirmwarePlatformExt, PluginSettings } from "../types";

// scrollToTop is a no-op in happy-dom; mock for cleanliness.
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// DropdownItem in the global @decky/ui stub is a passthrough <select> with no
// rgOptions / onChange capture. LibraryPage's BIOS tab uses DropdownItem for
// per-platform core selection; we need to drive its onChange callback to test
// the setSystemCore flow. Re-mock @decky/ui locally to expose DropdownItem
// props on a shared captured-array — every other component mirrors the global
// stub so the rest of the tree behaves identically.
interface CapturedDropdown {
  label?: string;
  rgOptions?: Array<{ data: unknown; label: string }>;
  selectedOption?: unknown;
  onChange?: (option: { data: string; label: string }) => void | Promise<void>;
}
const capturedDropdowns: CapturedDropdown[] = [];

vi.mock("@decky/ui", async () => {
  const { createElement: ce } = await import("react");
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const passthrough = (tag: string) => (p: AnyProps) => ce(tag, {}, p.children as never);
  return {
    PanelSection: (p: AnyProps & { title?: unknown }) =>
      ce(
        "section",
        { "data-testid": "panel-section", "data-title": typeof p.title === "string" ? p.title : undefined },
        typeof p.title === "string" ? ce("h2", { "data-testid": "panel-title" }, p.title) : null,
        p.children as never,
      ),
    PanelSectionRow: passthrough("div"),
    ButtonItem: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { onClick, disabled }, children as never),
    Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
      ce(
        "div",
        { "data-testid": "field" },
        ce("span", { "data-testid": "field-label" }, p.label as never),
        ce("span", { "data-testid": "field-desc" }, p.description as never),
      ),
    Focusable: passthrough("div"),
    DialogButton: ({ children, onClick }: AnyProps & { onClick?: () => void }) =>
      ce("button", { onClick }, children as never),
    DropdownItem: (p: CapturedDropdown) => {
      capturedDropdowns.push(p);
      return ce(
        "div",
        { "data-testid": "dropdown" },
        ce("span", { "data-testid": "dropdown-label" }, p.label as never),
      );
    },
    ToggleField: (
      p: AnyProps & {
        checked?: boolean;
        onChange?: (v: boolean) => void;
        label?: unknown;
        description?: unknown;
      },
    ) =>
      ce(
        "div",
        {
          "data-testid": "toggle",
          "data-label": typeof p.label === "string" ? p.label : undefined,
          "data-description": typeof p.description === "string" ? p.description : undefined,
        },
        ce("input", {
          type: "checkbox",
          "data-testid": "toggle-input",
          checked: p.checked ?? false,
          onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
        }),
        typeof p.label === "string" ? p.label : null,
      ),
    Spinner: () => ce("div", { "data-testid": "spinner" }),
  };
});

// Flush mount-time + chained promise resolutions.
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

function defaultSettings(): PluginSettings {
  return {
    romm_url: "",
    has_token: true,
    steam_input_mode: "default",
    sgdb_api_key_masked: "",
    log_level: "warn",
    romm_allow_insecure_ssl: false,
  };
}

function makePlatform(overrides: Partial<PlatformSyncSetting> = {}): PlatformSyncSetting {
  return {
    id: 1,
    name: "Genesis",
    slug: "genesis",
    rom_count: 10,
    sync_enabled: false,
    ...overrides,
  };
}

function makeCollection(overrides: Partial<CollectionSyncSetting> = {}): CollectionSyncSetting {
  return {
    id: "c1",
    name: "Favs",
    rom_count: 5,
    sync_enabled: false,
    kind: "user",
    is_favorite: true,
    ...overrides,
  };
}

function makeBiosPlatform(overrides: Partial<FirmwarePlatformExt> = {}): FirmwarePlatformExt {
  return {
    platform_slug: "snes",
    files: [],
    has_games: true,
    ...overrides,
  };
}

describe("LibraryPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    capturedDropdowns.length = 0;
    // Default callable behavior — tests override per case.
    vi.mocked(backend.getPlatforms).mockResolvedValue({
      success: true,
      platforms: [],
    });
    vi.mocked(backend.savePlatformSync).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.setAllPlatformsSync).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.getCollections).mockResolvedValue({
      success: true,
      collections: [],
    });
    vi.mocked(backend.saveCollectionSync).mockResolvedValue({ success: true });
    vi.mocked(backend.setAllCollectionsSync).mockResolvedValue({ success: true });
    vi.mocked(backend.saveCollectionPlatformGroups).mockResolvedValue({
      success: true,
    });
    vi.mocked(backend.getSettings).mockResolvedValue(defaultSettings());
    vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
      success: true,
      platforms: [],
    });
    vi.mocked(backend.downloadAllFirmware).mockResolvedValue({ success: true });
    vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
      success: true,
    });
    vi.mocked(backend.setSystemCore).mockResolvedValue({ success: true });
  });

  // ------------------------------------------------------------------
  // A. Initial render + tab switching (lazy loading)
  // ------------------------------------------------------------------
  describe("initial render + tab switching", () => {
    it("mounts with the platforms tab active and calls getPlatforms once", async () => {
      render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(backend.getPlatforms)).toHaveBeenCalledTimes(1);
      // collections + bios lazy data not yet fetched
      expect(vi.mocked(backend.getCollections)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getFirmwareStatus)).not.toHaveBeenCalled();
    });

    it("clicking the Collections tab lazy-loads getCollections + getSettings", async () => {
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getCollections)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.getSettings)).toHaveBeenCalledTimes(1);
    });

    it("clicking the BIOS tab lazy-loads getFirmwareStatus", async () => {
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
    });

    it("switching back to Collections does NOT refetch (collectionsLoaded guard)", async () => {
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getCollections)).toHaveBeenCalledTimes(1);
      await act(async () => {
        fireEvent.click(getByText("Platforms"));
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
      });
      // Still 1 — the ref guard prevents a re-fetch.
      expect(vi.mocked(backend.getCollections)).toHaveBeenCalledTimes(1);
    });

    it("switching back to BIOS does NOT refetch (biosLoaded guard)", async () => {
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
      await act(async () => {
        fireEvent.click(getByText("Platforms"));
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
    });
  });

  // ------------------------------------------------------------------
  // B. Platforms tab — mount (getPlatforms)
  // ------------------------------------------------------------------
  describe("platforms tab — mount", () => {
    it("renders a ToggleField per platform when getPlatforms succeeds", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 1, name: "Genesis" }), makePlatform({ id: 2, name: "SNES" })],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Genesis");
      expect(container.textContent).toContain("SNES");
    });

    it("surfaces a 'Failed to load platforms' button when getPlatforms returns success=false", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: false,
        platforms: [],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Failed to load platforms");
    });

    it("surfaces 'Failed to load platforms' when getPlatforms throws (catch sets syncError=true)", async () => {
      vi.mocked(backend.getPlatforms).mockRejectedValue(new Error("net"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Failed to load platforms");
    });

    it("clicking the failure-state button invokes onBack", async () => {
      const onBack = vi.fn();
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: false,
        platforms: [],
      });
      const { getByText } = render(<LibraryPage onBack={onBack} />);
      await flushAsync();
      fireEvent.click(getByText("Failed to load platforms"));
      // onBack is also wired to the top-level "Back" button — only one ButtonItem
      // surfaces the failure label, but counting once is what we want.
      expect(onBack).toHaveBeenCalledTimes(1);
    });

    it("removes the Spinner once getPlatforms resolves (finally setSyncLoading(false))", async () => {
      const { queryByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      // Initial render — getPlatforms not yet resolved
      expect(queryByTestId("spinner")).not.toBeNull();
      await flushAsync();
      expect(queryByTestId("spinner")).toBeNull();
    });
  });

  // ------------------------------------------------------------------
  // C. Platforms tab — handleToggle (optimistic + rollback)
  // ------------------------------------------------------------------
  describe("platforms tab — handleToggle", () => {
    it("optimistically flips sync_enabled and calls savePlatformSync", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 7, name: "Genesis", sync_enabled: false })],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      const toggleInputs = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]');
      // Only one platform → one toggle for that platform
      const platformToggle = toggleInputs[0]!;
      expect(platformToggle.checked).toBe(false);

      await act(async () => {
        fireEvent.click(platformToggle);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.savePlatformSync)).toHaveBeenCalledWith(7, true);
      const afterClick = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]')[0]!;
      expect(afterClick.checked).toBe(true);
    });

    it("reverts sync_enabled when savePlatformSync rejects", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 7, sync_enabled: false })],
      });
      vi.mocked(backend.savePlatformSync).mockRejectedValue(new Error("nope"));
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      const toggleInput = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]')[0]!;

      await act(async () => {
        fireEvent.click(toggleInput);
        // Allow optimistic update, the awaited rejected promise, and the
        // rollback setState to flush.
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });

      // CATCH-REJECTION assert: rolled back to false
      const reverted = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]')[0]!;
      expect(reverted.checked).toBe(false);
    });
  });

  // ------------------------------------------------------------------
  // D. Platforms tab — handleSetAll (optimistic + rollback)
  // ------------------------------------------------------------------
  describe("platforms tab — handleSetAll", () => {
    it("enables all platforms optimistically and calls setAllPlatformsSync(true)", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 1, sync_enabled: false }), makePlatform({ id: 2, sync_enabled: false })],
      });
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
      });

      expect(vi.mocked(backend.setAllPlatformsSync)).toHaveBeenCalledWith(true);
      const inputs = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]');
      expect(inputs[0]?.checked).toBe(true);
      expect(inputs[1]?.checked).toBe(true);
    });

    it("disables all platforms optimistically and calls setAllPlatformsSync(false)", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 1, sync_enabled: true }), makePlatform({ id: 2, sync_enabled: true })],
      });
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Disable All"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setAllPlatformsSync)).toHaveBeenCalledWith(false);
      const inputs = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]');
      expect(inputs[0]?.checked).toBe(false);
      expect(inputs[1]?.checked).toBe(false);
    });

    it("restores the previous snapshot when setAllPlatformsSync rejects", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 1, sync_enabled: true }), makePlatform({ id: 2, sync_enabled: false })],
      });
      vi.mocked(backend.setAllPlatformsSync).mockRejectedValue(new Error("x"));
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: snapshot restored
      const inputs = container.querySelectorAll<HTMLInputElement>('[data-testid="toggle-input"]');
      expect(inputs[0]?.checked).toBe(true);
      expect(inputs[1]?.checked).toBe(false);
    });
  });

  // ------------------------------------------------------------------
  // E. Collections tab — mount (lazy load)
  // ------------------------------------------------------------------
  describe("collections tab — mount", () => {
    it("populates collections + platformGroups from Promise.all on success", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "MyColl", kind: "user", is_favorite: false })],
      });
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        collection_create_platform_groups: true,
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("MyColl");
      // platformGroups true → the renamed toggle is checked.
      const platformGroupsToggle = container.querySelector<HTMLInputElement>(
        '[data-label="Show collection games in platform groups"] input',
      );
      expect(platformGroupsToggle?.checked).toBe(true);
    });

    it("falsy collection_create_platform_groups maps to checked=false", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "c1" })],
      });
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        collection_create_platform_groups: false,
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const platformGroupsToggle = container.querySelector<HTMLInputElement>(
        '[data-label="Show collection games in platform groups"] input',
      );
      expect(platformGroupsToggle?.checked).toBe(false);
    });

    it("surfaces an error when getCollections returns success=false", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: false,
        collections: [],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to load collections");
    });

    it("surfaces an error when getCollections throws (catch sets collectionsError=true)", async () => {
      vi.mocked(backend.getCollections).mockRejectedValue(new Error("boom"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to load collections");
    });

    it("renders the empty-state Field when the collections list is empty", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("No collections found");
    });
  });

  // ------------------------------------------------------------------
  // F. Collections tab — handleCollectionToggle (optimistic + rollback)
  // ------------------------------------------------------------------
  describe("collections tab — handleCollectionToggle", () => {
    it("optimistically toggles a collection and calls saveCollectionSync with kind", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "abc", name: "MyColl", sync_enabled: false, kind: "user", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Find the collection toggle by label (default sub-tab "my" → user collection visible).
      const collectionToggle = container.querySelector<HTMLInputElement>('[data-label="MyColl"] input')!;
      expect(collectionToggle.checked).toBe(false);
      await act(async () => {
        fireEvent.click(collectionToggle);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.saveCollectionSync)).toHaveBeenCalledWith("abc", "user", true);
      const after = container.querySelector<HTMLInputElement>('[data-label="MyColl"] input')!;
      expect(after.checked).toBe(true);
    });

    it("passes kind='smart' for smart collections", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "sc1", name: "Filter A", sync_enabled: false, kind: "smart", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Switch to Smart sub-tab
      await act(async () => {
        fireEvent.click(getByText("Smart"));
        await Promise.resolve();
      });
      const toggle = container.querySelector<HTMLInputElement>('[data-label="Filter A"] input')!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.saveCollectionSync)).toHaveBeenCalledWith("sc1", "smart", true);
    });

    it("reverts on saveCollectionSync rejection", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "abc", name: "MyColl", kind: "user", is_favorite: false, sync_enabled: false }),
        ],
      });
      vi.mocked(backend.saveCollectionSync).mockRejectedValue(new Error("nope"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const toggle = container.querySelector<HTMLInputElement>('[data-label="MyColl"] input')!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      const reverted = container.querySelector<HTMLInputElement>('[data-label="MyColl"] input')!;
      // CATCH-REJECTION assert: rolled back to original false
      expect(reverted.checked).toBe(false);
    });
  });

  // ------------------------------------------------------------------
  // G. Collections tab — Enable/Disable All + platform-groups toggle
  // ------------------------------------------------------------------
  describe("collections tab — set-all + platform groups", () => {
    it("calls setAllCollectionsSync(true, 'my') on Enable All in default My sub-tab", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "c1", kind: "user", is_favorite: false, sync_enabled: false })],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setAllCollectionsSync)).toHaveBeenCalledWith(true, "my");
    });

    it("calls setAllCollectionsSync(true, 'smart') when Smart sub-tab is active", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "s1", name: "S1", sync_enabled: false, kind: "smart", is_favorite: false })],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Smart"));
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setAllCollectionsSync)).toHaveBeenCalledWith(true, "smart");
    });

    it("restores the previous collections snapshot on setAllCollectionsSync rejection", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "a", name: "A", kind: "user", is_favorite: false, sync_enabled: true }),
          makeCollection({ id: "b", name: "B", kind: "user", is_favorite: false, sync_enabled: false }),
        ],
      });
      vi.mocked(backend.setAllCollectionsSync).mockRejectedValue(new Error("boom"));
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Disable All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: restored a=true, b=false
      const a = container.querySelector<HTMLInputElement>('[data-label="A"] input');
      const b = container.querySelector<HTMLInputElement>('[data-label="B"] input');
      expect(a?.checked).toBe(true);
      expect(b?.checked).toBe(false);
    });

    it("toggling 'Show collection games in platform groups' calls saveCollectionPlatformGroups", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "c1" })],
      });
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const toggle = container.querySelector<HTMLInputElement>(
        '[data-label="Show collection games in platform groups"] input',
      )!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.saveCollectionPlatformGroups)).toHaveBeenCalledWith(true);
    });

    it("reverts platformGroups state when saveCollectionPlatformGroups rejects", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "c1" })],
      });
      vi.mocked(backend.saveCollectionPlatformGroups).mockRejectedValue(new Error("x"));
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const toggle = container.querySelector<HTMLInputElement>(
        '[data-label="Show collection games in platform groups"] input',
      )!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: rolled back to false
      const after = container.querySelector<HTMLInputElement>(
        '[data-label="Show collection games in platform groups"] input',
      )!;
      expect(after.checked).toBe(false);
    });
  });

  // ------------------------------------------------------------------
  // H. Collections tab — sub-tabs (my / smart / franchise) + section headers
  // ------------------------------------------------------------------
  describe("collections tab — sub-tabs", () => {
    it("renders 3 sub-tab buttons with plain labels (no inline counts)", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "f1", name: "F1", kind: "user", is_favorite: true }),
          makeCollection({ id: "u1", name: "U1", kind: "user", is_favorite: false }),
          makeCollection({ id: "u2", name: "U2", kind: "user", is_favorite: false }),
          makeCollection({ id: "s1", name: "S1", kind: "smart", is_favorite: false }),
          makeCollection({ id: "fr1", name: "Fr1", kind: "franchise", is_favorite: false }),
          makeCollection({ id: "fr2", name: "Fr2", kind: "franchise", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Plain sub-tab labels — no inline counts.
      expect(getByText("My")).not.toBeNull();
      expect(getByText("Smart")).not.toBeNull();
      expect(getByText("Franchise")).not.toBeNull();
      // No "Favorites" sub-tab button (now a top-level toggle).
      expect(container.textContent).not.toContain("Favorites (");
    });

    it("defaults to the My sub-tab and shows only non-favorite user collections", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "f1", name: "FavOne", kind: "user", is_favorite: true }),
          makeCollection({ id: "u1", name: "UserOne", kind: "user", is_favorite: false }),
          makeCollection({ id: "s1", name: "SmartOne", kind: "smart", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // UserOne renders in My; SmartOne does not. FavOne renders only via the
      // top-level Sync RomM favorites toggle, not in the visible-list area.
      expect(container.querySelector('[data-label="UserOne"]')).not.toBeNull();
      expect(container.querySelector('[data-label="SmartOne"]')).toBeNull();
    });

    it("switching sub-tab filters the visible collection set", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "f1", name: "FavOne", kind: "user", is_favorite: true }),
          makeCollection({ id: "u1", name: "UserOne", kind: "user", is_favorite: false }),
          makeCollection({ id: "s1", name: "SmartOne", kind: "smart", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Default = My → UserOne visible, SmartOne hidden.
      expect(container.querySelector('[data-label="UserOne"]')).not.toBeNull();
      expect(container.querySelector('[data-label="SmartOne"]')).toBeNull();

      // Switch to Smart.
      await act(async () => {
        fireEvent.click(getByText("Smart"));
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="SmartOne"]')).not.toBeNull();
      expect(container.querySelector('[data-label="UserOne"]')).toBeNull();
    });

    it("renders the section header with the visible-count for the active sub-tab", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "u1", name: "U1", kind: "user", is_favorite: false }),
          makeCollection({ id: "u2", name: "U2", kind: "user", is_favorite: false }),
          makeCollection({ id: "s1", name: "S1", kind: "smart", is_favorite: false }),
          makeCollection({ id: "fr1", name: "Fr1", kind: "franchise", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Default My sub-tab → header reflects 2 visible.
      expect(container.textContent).toContain("MY COLLECTIONS (2)");

      await act(async () => {
        fireEvent.click(getByText("Smart"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("SMART COLLECTIONS (1)");

      await act(async () => {
        fireEvent.click(getByText("Franchise"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("FRANCHISE (1)");
    });

    it("renders a 'No <sub-tab> collections' empty state when the bucket is empty", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          // Only smart collections — my/franchise buckets are empty.
          makeCollection({ id: "s1", name: "S1", kind: "smart", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Default My sub-tab → no my collections present.
      expect(container.textContent).toContain("No my collections");
    });

    it("sub-tab resets to My each time the Collections tab is opened", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "u1", name: "UserOne", kind: "user", is_favorite: false }),
          makeCollection({ id: "s1", name: "SmartOne", kind: "smart", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Switch to Smart sub-tab.
      await act(async () => {
        fireEvent.click(getByText("Smart"));
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="SmartOne"]')).not.toBeNull();

      // Leave the Collections tab and come back; sub-tab should reset to My.
      await act(async () => {
        fireEvent.click(getByText("Platforms"));
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="UserOne"]')).not.toBeNull();
      expect(container.querySelector('[data-label="SmartOne"]')).toBeNull();
    });
  });

  // ------------------------------------------------------------------
  // H2. Collections tab — favorites top-level toggle
  // ------------------------------------------------------------------
  describe("collections tab — favorites toggle", () => {
    it("renders the Sync RomM favorites toggle with the singular description for 1 game", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "f1",
            name: "Faves",
            kind: "user",
            is_favorite: true,
            rom_count: 1,
            sync_enabled: false,
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const favToggle = container.querySelector<HTMLElement>('[data-label="Sync RomM favorites"]');
      expect(favToggle).not.toBeNull();
      expect(favToggle?.getAttribute("data-description")).toBe("Includes 1 favorited game");
    });

    it("renders the plural description for N>1 favorited games", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "f1", name: "Faves", kind: "user", is_favorite: true, rom_count: 7 })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const favToggle = container.querySelector<HTMLElement>('[data-label="Sync RomM favorites"]');
      expect(favToggle?.getAttribute("data-description")).toBe("Includes 7 favorited games");
    });

    it("renders the plural description for 0 favorited games", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "f1", name: "Faves", kind: "user", is_favorite: true, rom_count: 0 })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const favToggle = container.querySelector<HTMLElement>('[data-label="Sync RomM favorites"]');
      expect(favToggle?.getAttribute("data-description")).toBe("Includes 0 favorited games");
    });

    it("clicking the favorites toggle calls saveCollectionSync with the favorites id and kind=user", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "favid",
            name: "Faves",
            kind: "user",
            is_favorite: true,
            rom_count: 5,
            sync_enabled: false,
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const toggle = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input')!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.saveCollectionSync)).toHaveBeenCalledWith("favid", "user", true);
    });

    it("reverts the favorites toggle on saveCollectionSync rejection", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "favid",
            name: "Faves",
            kind: "user",
            is_favorite: true,
            rom_count: 5,
            sync_enabled: false,
          }),
        ],
      });
      vi.mocked(backend.saveCollectionSync).mockRejectedValue(new Error("nope"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const toggle = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input')!;
      await act(async () => {
        fireEvent.click(toggle);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      const reverted = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input')!;
      // CATCH-REJECTION assert: rolled back to original false
      expect(reverted.checked).toBe(false);
    });

    it("omits the favorites toggle when no favorites collection exists", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "U1", kind: "user", is_favorite: false })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="Sync RomM favorites"]')).toBeNull();
    });

    it("falls back to listing favorites in the My sub-tab when more than one exists (with console warning)", async () => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      try {
        vi.mocked(backend.getCollections).mockResolvedValue({
          success: true,
          collections: [
            makeCollection({ id: "f1", name: "FavA", kind: "user", is_favorite: true }),
            makeCollection({ id: "f2", name: "FavB", kind: "user", is_favorite: true }),
            makeCollection({ id: "u1", name: "UserOne", kind: "user", is_favorite: false }),
          ],
        });
        const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          fireEvent.click(getByText("Collections"));
          await Promise.resolve();
          await Promise.resolve();
        });
        // Toggle hidden — single-toggle UI can't represent multiple favorites.
        expect(container.querySelector('[data-label="Sync RomM favorites"]')).toBeNull();
        // Both favorites surface in My (alongside the regular user collection).
        expect(container.querySelector('[data-label="FavA"]')).not.toBeNull();
        expect(container.querySelector('[data-label="FavB"]')).not.toBeNull();
        expect(container.querySelector('[data-label="UserOne"]')).not.toBeNull();
        expect(warn).toHaveBeenCalledTimes(1);
      } finally {
        warn.mockRestore();
      }
    });
  });

  // ------------------------------------------------------------------
  // I. BIOS tab — refreshBios
  // ------------------------------------------------------------------
  describe("bios tab — refreshBios", () => {
    it("renders platforms and sets serverOffline on success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        server_offline: false,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "snes.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: true,
                description: "BIOS",
                hash_valid: true,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("snes");
    });

    it("renders the server-offline banner when getFirmwareStatus reports server_offline", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        server_offline: true,
        platforms: [],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Server offline");
    });

    it("surfaces result.message when getFirmwareStatus returns success=false with a message", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: false,
        message: "Server is sad",
        platforms: [],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION (failure branch): biosError = result.message
      expect(container.textContent).toContain("Server is sad");
    });

    it("falls back to 'Failed to fetch firmware status' when result.message is absent", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: false,
        platforms: [],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to fetch firmware status");
    });

    it("sets biosError='Failed to fetch firmware status: <e>' when getFirmwareStatus throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockRejectedValue(new Error("network"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: rendered with the interpolated Error
      expect(container.textContent).toContain("Failed to fetch firmware status: Error: network");
    });

    it("renders the no-firmware empty state when platforms list is empty", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("No firmware files found");
    });
  });

  // ------------------------------------------------------------------
  // J. BIOS tab — handleDownloadAll
  // ------------------------------------------------------------------
  describe("bios tab — handleDownloadAll", () => {
    function biosPlatformWithMissingOptional(): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: "snes",
        files: [
          {
            id: 1,
            file_name: "boot.rom",
            size: 100,
            md5: "x",
            downloaded: false,
            required: false,
            description: "Optional",
            hash_valid: null,
            classification: "optional",
          },
        ],
      });
    }

    it("calls downloadAllFirmware(slug) and then refreshes BIOS on success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        downloaded: 1,
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.downloadAllFirmware)).toHaveBeenCalledWith("snes");
      // refreshBios called once on tab activation + once after download
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
    });

    it("surfaces result.message when the download succeeds", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        message: "All good",
        downloaded: 1,
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("All good");
    });

    it("surfaces 'Download failed' when result.success=false with no message", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: false,
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Download failed");
    });

    it("sets biosStatus='Download failed: <e>' when downloadAllFirmware throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingOptional()],
      });
      vi.mocked(backend.downloadAllFirmware).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: status string rendered
      expect(container.textContent).toContain("Download failed: Error: io");
    });
  });

  // ------------------------------------------------------------------
  // K. BIOS tab — handleDownloadRequired
  // ------------------------------------------------------------------
  describe("bios tab — handleDownloadRequired", () => {
    function biosPlatformWithMissingRequired(): FirmwarePlatformExt {
      return makeBiosPlatform({
        platform_slug: "snes",
        files: [
          {
            id: 1,
            file_name: "bios.rom",
            size: 100,
            md5: "x",
            downloaded: false,
            required: true,
            description: "Required BIOS",
            hash_valid: null,
            classification: "required",
          },
        ],
      });
    }

    it("calls downloadRequiredFirmware(slug) and refreshes BIOS on success", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingRequired()],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: true,
        downloaded: 1,
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download Required"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.downloadRequiredFirmware)).toHaveBeenCalledWith("snes");
      expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(2);
    });

    it("surfaces 'Download failed: <e>' when downloadRequiredFirmware throws", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingRequired()],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download Required"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: status string rendered
      expect(container.textContent).toContain("Download failed: Error: io");
    });

    it("surfaces 'Download failed' fallback when result.success=false with no message", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [biosPlatformWithMissingRequired()],
      });
      vi.mocked(backend.downloadRequiredFirmware).mockResolvedValue({
        success: false,
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Download Required"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Download failed");
    });
  });

  // ------------------------------------------------------------------
  // L. BIOS tab — expand/collapse + hashIndicator + unknown summary
  // ------------------------------------------------------------------
  describe("bios tab — expand/collapse and file rendering", () => {
    it("expands files on Show Files click and collapses on the same button", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "ok.bin",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "OK File",
                hash_valid: true,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Initially collapsed — file name not rendered
      expect(container.textContent).not.toContain("OK File");
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("OK File");
      // Now collapse
      await act(async () => {
        fireEvent.click(getByText("Hide Files"));
        await Promise.resolve();
      });
      expect(container.textContent).not.toContain("OK File");
    });

    it("renders hashIndicator ' ✓' for downloaded files with hash_valid=true", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "good.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Good",
                hash_valid: true,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("good.rom ✓");
    });

    it("renders hashIndicator ' ⚠' for downloaded files with hash_valid=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "bad.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Bad",
                hash_valid: false,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("bad.rom ⚠");
    });

    it("renders hashIndicator ' —' for downloaded files with hash_valid=null", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "unk.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Unk",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("unk.rom —");
    });

    it("renders a missing required file (red dot branch) when expanded", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "missing-req.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: true,
                description: "ReqMissing",
                hash_valid: null,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      // Missing required → "Missing" suffix and red dot branch
      expect(container.textContent).toContain("missing-req.rom — Missing");
    });

    it("renders a missing optional file (gray dot branch) when expanded", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "missing-opt.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "OptMissing",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      // Missing optional → "Missing" suffix and gray dot branch
      expect(container.textContent).toContain("missing-opt.rom — Missing");
    });

    it("renders the unrecognized-file footer when unknown files are present", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "mystery.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "?",
                hash_valid: null,
                classification: "unknown",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Show Files (1)"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("1 file(s) not recognized");
    });
  });

  // ------------------------------------------------------------------
  // M. BIOS tab — getBiosSummary indirect coverage via rendering
  // ------------------------------------------------------------------
  describe("bios tab — summary text", () => {
    it("shows 'X / Y required' + 'All required ready' when all required are done and no optional missing", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "req.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: true,
                description: "Req",
                hash_valid: true,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("1 / 1 required");
      expect(container.textContent).toContain("All required ready");
    });

    it("shows 'N optional missing' when all required are done but optional is missing", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "req.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: true,
                description: "Req",
                hash_valid: true,
                classification: "required",
              },
              {
                id: 2,
                file_name: "opt.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "Opt",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("All required ready (1 optional missing)");
    });

    it("shows 'N required missing — games may not launch' when required is incomplete", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "req.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: true,
                description: "Req",
                hash_valid: null,
                classification: "required",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("1 required missing");
    });

    it("falls back to 'X / Y files' summary when there are no required files", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "opt.rom",
                size: 100,
                md5: "x",
                downloaded: true,
                required: false,
                description: "Opt",
                hash_valid: true,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("1 / 1 files");
      expect(container.textContent).toContain("All downloaded");
    });

    it("shows 'N missing' suffix when not all files are downloaded and no required files exist", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [
              {
                id: 1,
                file_name: "opt.rom",
                size: 100,
                md5: "x",
                downloaded: false,
                required: false,
                description: "Opt",
                hash_valid: null,
                classification: "optional",
              },
            ],
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("0 / 1 files");
      expect(container.textContent).toContain("1 missing");
    });
  });

  // ------------------------------------------------------------------
  // N. BIOS tab — setSystemCore (core dropdown)
  // ------------------------------------------------------------------
  describe("bios tab — setSystemCore", () => {
    it("does NOT render the dropdown when available_cores has <=1 entry", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [{ core_so: "snes9x.so", label: "snes9x", is_default: true }],
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(capturedDropdowns.length).toBe(0);
    });

    it("renders the core dropdown when available_cores has >1 entries", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(capturedDropdowns.length).toBe(1);
      const dropdown = capturedDropdowns[0]!;
      expect(dropdown.label).toBe("Active Core");
      expect(dropdown.rgOptions?.map((o) => o.data)).toEqual(["snes9x", "mesen-s"]);
      expect(dropdown.rgOptions?.[0]?.label).toBe("snes9x (default)");
      expect(dropdown.selectedOption).toBe("snes9x");
    });

    it("calls setSystemCore with empty label when default core is selected and dispatches romm_data_changed", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "mesen-s",
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await capturedDropdowns[0]?.onChange?.({
            data: "snes9x",
            label: "snes9x (default)",
          });
        });
        // Selecting the default core → label is "" sent to setSystemCore
        expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("snes", "");
        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({
          type: "core_changed",
          platform_slug: "snes",
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("calls setSystemCore with the explicit non-default label", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        await capturedDropdowns[0]?.onChange?.({
          data: "mesen-s",
          label: "mesen-s",
        });
      });
      expect(vi.mocked(backend.setSystemCore)).toHaveBeenCalledWith("snes", "mesen-s");
    });

    it("does NOT refresh BIOS or dispatch the event when setSystemCore returns success=false", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            active_core_label: "snes9x",
          }),
        ],
      });
      vi.mocked(backend.setSystemCore).mockResolvedValue({ success: false });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await capturedDropdowns[0]?.onChange?.({
            data: "mesen-s",
            label: "mesen-s",
          });
        });
        // refreshBios was called once on tab activation; not again on failure
        expect(vi.mocked(backend.getFirmwareStatus)).toHaveBeenCalledTimes(1);
        expect(listener).not.toHaveBeenCalled();
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("falls back to default core label when active_core_label is absent", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [
              { core_so: "snes9x.so", label: "snes9x", is_default: true },
              { core_so: "mesen-s.so", label: "mesen-s", is_default: false },
            ],
            // No active_core_label
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(capturedDropdowns[0]?.selectedOption).toBe("snes9x");
    });

    it("renders an inactive Core Field when active_core_label is set but only 1 available core exists", async () => {
      vi.mocked(backend.getFirmwareStatus).mockResolvedValue({
        success: true,
        platforms: [
          makeBiosPlatform({
            platform_slug: "snes",
            files: [],
            available_cores: [{ core_so: "snes9x.so", label: "snes9x", is_default: true }],
            active_core_label: "snes9x",
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("BIOS"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // No dropdown rendered, but the "Core" Field is.
      expect(capturedDropdowns.length).toBe(0);
      expect(container.textContent).toContain("snes9x");
    });
  });

  // ------------------------------------------------------------------
  // O. Back button
  // ------------------------------------------------------------------
  describe("back button", () => {
    it("invokes onBack when the Back button is clicked", async () => {
      const onBack = vi.fn();
      const { getByText } = render(<LibraryPage onBack={onBack} />);
      await flushAsync();
      fireEvent.click(getByText("Back"));
      expect(onBack).toHaveBeenCalledTimes(1);
    });
  });
});
