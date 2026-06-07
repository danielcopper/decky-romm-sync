// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (rolled-back toggle state, surfaced error string, etc.).
// Asserting only that the rejecting call was invoked is vacuous — the rejection
// happens after the call returns so the test would pass with or without the
// .catch.
//
// LibraryPage catch sites (all asserted below):
//   - handleToggle catch → rollback setSyncPlatforms (sync_enabled flips back)
//   - handleSetAll catch → restore previous platforms snapshot
//   - handleCollectionToggle catch → rollback setCollections
//   - handleSetAllCollections catch → restore previous collections snapshot
//   - platform-groups inline catch → setPlatformGroups(!value) rollback
//   - getCollections/getSettings .catch → setCollectionsError(true)
//
// The System view (per-platform core + BIOS state) lives in SystemPage, a
// top-level QAM page — its tests are in SystemPage.test.tsx, not here.
//
// MUTATION CHECKS (by inspection — auto-mode classifier likely blocks on
// React state internals, so confidence is recorded here):
//   1. Removing the rollback inside handleToggle's catch would break the
//      "platform toggle rejection reverts checked state" test — the captured
//      ToggleField checked prop would remain at the optimistic value.
//   2. Removing the `!collectionsLoaded.current` guard would break the
//      "switching back to collections tab does not refetch" test — getCollections
//      would be called twice instead of once.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { LibraryPage } from "./LibraryPage";
import * as backend from "../api/backend";
import type { PlatformSyncSetting, CollectionSyncSetting, PluginSettings } from "../types";

// scrollToTop is a no-op in happy-dom; mock for cleanliness.
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// Re-mock @decky/ui locally so the component tree renders with inspectable
// stubs (ToggleField checked-prop, Field label/description, DialogButton click).
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

describe("LibraryPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
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
  });

  // ------------------------------------------------------------------
  // A. Initial render + tab switching (lazy loading)
  // ------------------------------------------------------------------
  describe("initial render + tab switching", () => {
    it("mounts with the platforms tab active and calls getPlatforms once", async () => {
      render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(backend.getPlatforms)).toHaveBeenCalledTimes(1);
      // collections lazy data not yet fetched
      expect(vi.mocked(backend.getCollections)).not.toHaveBeenCalled();
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
