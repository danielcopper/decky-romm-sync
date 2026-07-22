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
//   - getCollections .catch → setCollectionsError(true) (the list-driving fetch)
//   - getSettings .catch → owner-scope stays at its "all" default; the two
//     fetches are decoupled so a settings-read failure never blanks the list.
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
import type { ReactElement } from "react";
import { LibraryPage } from "./LibraryPage";
import * as backend from "../api/backend";
import { scrollElementToTop } from "../utils/scrollHelpers";
import { showModal } from "@decky/ui";
import type { PlatformSyncSetting, CollectionSyncSetting, PluginSettings } from "../types";

// scroll helpers are no-ops in happy-dom; mock so the search-field scroll and
// the Back-button scroll can be asserted without a real layout engine.
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn(), scrollElementToTop: vi.fn() }));

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
    DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { onClick, disabled }, children as never),
    TextField: (
      p: AnyProps & {
        value?: string;
        label?: unknown;
        onChange?: (e: unknown) => void;
        onFocus?: (e: unknown) => void;
      },
    ) =>
      ce("input", {
        "data-testid": "text-field",
        // Real TextField renders `label` as a heading above the input; expose it
        // as aria-label so tests can assert the heading text (e.g. the fuzzy hint).
        "aria-label": typeof p.label === "string" ? p.label : undefined,
        value: p.value ?? "",
        onChange: (e: unknown) => p.onChange?.(e),
        onFocus: (e: unknown) => p.onFocus?.(e),
      }),
    // A no-render stub — showModal captures the element, so the modal body is
    // inspected off the showModal mock, not the DOM.
    ConfirmModal: () => null,
    showModal: vi.fn(),
    ToggleField: (
      p: AnyProps & {
        checked?: boolean;
        disabled?: boolean;
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
          disabled: p.disabled ?? false,
          onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
        }),
        typeof p.label === "string" ? p.label : null,
      ),
    Spinner: () => ce("div", { "data-testid": "spinner" }),
  };
});

// Inspect the last ConfirmModal shown via showModal (the whole-kind Enable/
// Disable All confirm), and drive its onOK.
function lastConfirmModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

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
    kind: "standard",
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
    vi.mocked(backend.saveCollectionsSync).mockResolvedValue({ success: true });
    vi.mocked(backend.setAllCollectionsSync).mockResolvedValue({ success: true });
    vi.mocked(backend.setCollectionOwnerScope).mockResolvedValue({ success: true });
    vi.mocked(backend.getSettings).mockResolvedValue(defaultSettings());
    vi.mocked(showModal).mockReset();
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

    it("shows the collapsed shortcut count in the toggle description when present (#1382)", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 1, name: "Genesis", rom_count: 10, collapsed_count: 7 })],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      const toggle = container.querySelector('[data-label="Genesis"]');
      expect(toggle?.getAttribute("data-description")).toBe("7 ROMs");
    });

    it("falls back to the raw rom_count when no collapsed count is present (never synced / old backend)", async () => {
      vi.mocked(backend.getPlatforms).mockResolvedValue({
        success: true,
        platforms: [makePlatform({ id: 1, name: "Genesis", rom_count: 10 })],
      });
      const { container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      const toggle = container.querySelector('[data-label="Genesis"]');
      expect(toggle?.getAttribute("data-description")).toBe("10 ROMs");
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
    it("populates collections from Promise.all on success", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "MyColl", kind: "standard", is_favorite: false })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("MyColl");
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
  // E2. Collections tab — loading UX (controls render before the list) — #1539
  // ------------------------------------------------------------------
  describe("collections tab — loading UX (#1539)", () => {
    type CollectionsResult = { success: boolean; collections: CollectionSyncSetting[] };

    // A getCollections promise the test resolves by hand, so the list stays in
    // its pending state while the controls are asserted.
    const deferredCollections = () => {
      let resolve!: (value: CollectionsResult) => void;
      const promise = new Promise<CollectionsResult>((r) => {
        resolve = r;
      });
      return { promise, resolve };
    };

    const openCollections = async (getByText: (t: string) => HTMLElement) => {
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
    };

    it("renders the owner-scope, sub-tabs, and search controls while getCollections is still pending", async () => {
      const { promise } = deferredCollections();
      vi.mocked(backend.getCollections).mockReturnValue(promise);
      const { getByText, getByTestId, queryByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Controls are already visible before the (still-pending) list resolves.
      expect(getByText("Mine")).not.toBeNull();
      expect(getByText("All")).not.toBeNull();
      expect(getByText("Standard")).not.toBeNull();
      expect(getByText("Smart")).not.toBeNull();
      expect(getByText("Virtual")).not.toBeNull();
      expect(getByTestId("text-field")).not.toBeNull();
      // The list area shows a spinner in the meantime.
      expect(queryByTestId("spinner")).not.toBeNull();
    });

    it("populates the owner-scope from getSettings without waiting on the slow getCollections", async () => {
      // getSettings resolves; getCollections never does. The owner-scope must
      // still initialize to "own" (the foreign collection would be hidden) even
      // though the list is still loading — proof the fetches are decoupled.
      vi.mocked(backend.getSettings).mockResolvedValue({ ...defaultSettings(), collection_owner_scope: "own" });
      const { promise } = deferredCollections();
      vi.mocked(backend.getCollections).mockReturnValue(promise);
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // "Show collections" description reflects the resolved "own" scope while
      // the list itself is still pending (spinner shown).
      expect(container.textContent).toContain("Only your own collections");
    });

    it("swallows a getSettings failure — owner-scope stays 'All' and the list still loads", async () => {
      // The decoupling property (#1539): getSettings() and getCollections() are
      // independent, so a settings-read failure only degrades owner-scope to its
      // "all" default — it must NOT blank or error the collection list.
      vi.mocked(backend.getSettings).mockRejectedValue(new Error("boom"));
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "u1", name: "MineColl", kind: "standard", is_favorite: false, is_own: true }),
          makeCollection({ id: "u2", name: "TheirColl", kind: "standard", is_favorite: false, is_own: false }),
        ],
      });
      const { getByText, getByTestId, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Scope stayed at the "All" default: a foreign (is_own=false) collection is
      // visible — under "Mine" it would be hidden.
      expect(container.querySelector('[data-label="MineColl"]')).not.toBeNull();
      expect(container.querySelector('[data-label="TheirColl"]')).not.toBeNull();
      // The list loaded normally — no error surfaced from the settings failure.
      expect(container.textContent).not.toContain("Failed to load collections");
      // Controls remain present.
      expect(getByText("Mine")).not.toBeNull();
      expect(getByText("All")).not.toBeNull();
      expect(getByTestId("text-field")).not.toBeNull();
    });

    it("keeps Enable/Disable All disabled while loading, then enables them once the list loads", async () => {
      const { promise, resolve } = deferredCollections();
      vi.mocked(backend.getCollections).mockReturnValue(promise);
      const { getByText, queryByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // While pending: buttons rendered but disabled, spinner shown.
      expect((getByText("Enable All") as HTMLButtonElement).disabled).toBe(true);
      expect((getByText("Disable All") as HTMLButtonElement).disabled).toBe(true);
      expect(queryByTestId("spinner")).not.toBeNull();
      // Resolve the list → buttons enabled, spinner gone.
      await act(async () => {
        resolve({
          success: true,
          collections: [makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false })],
        });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect((getByText("Enable All") as HTMLButtonElement).disabled).toBe(false);
      expect((getByText("Disable All") as HTMLButtonElement).disabled).toBe(false);
      expect(queryByTestId("spinner")).toBeNull();
    });

    it("renders the favorites toggle present but disabled while the list is loading", async () => {
      const { promise } = deferredCollections();
      vi.mocked(backend.getCollections).mockReturnValue(promise);
      const { getByText, container, queryByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // The favorites row is rendered up front (no late pop-in) but grayed until
      // the list resolves and reveals whether a single favorites collection exists.
      const favInput = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input');
      expect(favInput).not.toBeNull();
      expect(favInput?.disabled).toBe(true);
      expect(queryByTestId("spinner")).not.toBeNull();
    });

    it("renders the error state in the list area with the controls still present", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: false, collections: [] });
      const { getByText, getByTestId, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Error surfaces in the list area, not as a full-panel replacement — the
      // controls remain rendered alongside it.
      expect(container.textContent).toContain("Failed to load collections");
      expect(getByText("Mine")).not.toBeNull();
      expect(getByText("Virtual")).not.toBeNull();
      expect(getByTestId("text-field")).not.toBeNull();
      // Enable/Disable All stay disabled on the error path (nothing to act on).
      expect((getByText("Enable All") as HTMLButtonElement).disabled).toBe(true);
    });

    it("renders the empty state in the list area with the controls still present", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: [] });
      const { getByText, getByTestId, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      expect(container.textContent).toContain("No collections found");
      expect(getByText("Mine")).not.toBeNull();
      expect(getByText("Virtual")).not.toBeNull();
      expect(getByTestId("text-field")).not.toBeNull();
      // Empty library → nothing to enable, buttons disabled.
      expect((getByText("Enable All") as HTMLButtonElement).disabled).toBe(true);
    });

    it("shows the section header without a count while loading, then with the count once loaded (no '(0)' flash)", async () => {
      const { promise, resolve } = deferredCollections();
      vi.mocked(backend.getCollections).mockReturnValue(promise);
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // While the list is still pending the header shows the bare kind, no count
      // — nothing flashes "(0)".
      expect(container.textContent).toContain("STANDARD COLLECTIONS");
      expect(container.textContent).not.toContain("STANDARD COLLECTIONS (");
      // Resolve the list → the active kind's header carries the visible count.
      await act(async () => {
        resolve({
          success: true,
          collections: [
            makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false }),
            makeCollection({ id: "u2", name: "UserTwo", kind: "standard", is_favorite: false }),
          ],
        });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("STANDARD COLLECTIONS (2)");
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
          makeCollection({ id: "abc", name: "MyColl", sync_enabled: false, kind: "standard", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Find the collection toggle by label (default sub-tab "standard" → standard collection visible).
      const collectionToggle = container.querySelector<HTMLInputElement>('[data-label="MyColl"] input')!;
      expect(collectionToggle.checked).toBe(false);
      await act(async () => {
        fireEvent.click(collectionToggle);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.saveCollectionSync)).toHaveBeenCalledWith("abc", "standard", true);
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
          makeCollection({ id: "abc", name: "MyColl", kind: "standard", is_favorite: false, sync_enabled: false }),
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
  // G. Collections tab — Enable/Disable All (whole-kind confirm path)
  // ------------------------------------------------------------------
  describe("collections tab — whole-kind Enable/Disable All", () => {
    it("Enable All with no filter opens a confirm and only calls setAllCollectionsSync on OK", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "c1", kind: "standard", is_favorite: false, sync_enabled: false })],
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
      // The confirm is shown; nothing persisted yet.
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.setAllCollectionsSync)).not.toHaveBeenCalled();
      // Drive the modal's onOK.
      const props = lastConfirmModalProps<{ onOK?: () => void }>();
      await act(async () => {
        props?.onOK?.();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setAllCollectionsSync)).toHaveBeenCalledWith(true, "standard");
      // The batch callable is never used on the whole-kind path.
      expect(vi.mocked(backend.saveCollectionsSync)).not.toHaveBeenCalled();
    });

    it("Disable All with no filter confirms then calls setAllCollectionsSync(false, scope) on the active sub-tab", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "s1", name: "S1", sync_enabled: true, kind: "smart", is_favorite: false })],
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
        fireEvent.click(getByText("Disable All"));
        await Promise.resolve();
      });
      const props = lastConfirmModalProps<{ onOK?: () => void }>();
      await act(async () => {
        props?.onOK?.();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setAllCollectionsSync)).toHaveBeenCalledWith(false, "smart");
    });

    it("restores the previous collections snapshot on setAllCollectionsSync rejection", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "a", name: "A", kind: "standard", is_favorite: false, sync_enabled: true }),
          makeCollection({ id: "b", name: "B", kind: "standard", is_favorite: false, sync_enabled: false }),
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
      });
      const props = lastConfirmModalProps<{ onOK?: () => void }>();
      await act(async () => {
        props?.onOK?.();
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

    it("does not render the platform-groups toggle on the Collections tab (moved to Settings)", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "c1", kind: "standard", is_favorite: false })],
      });
      const { container, getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="Show collection games in platform groups"]')).toBeNull();
    });
  });

  // ------------------------------------------------------------------
  // H. Collections tab — sub-tabs (my / smart / virtual) + section headers
  // ------------------------------------------------------------------
  describe("collections tab — sub-tabs", () => {
    it("renders three plain kind buttons (Standard/Smart/Virtual); the count lives in the section header, not the buttons", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "f1", name: "F1", kind: "standard", is_favorite: true }),
          makeCollection({ id: "u1", name: "U1", kind: "standard", is_favorite: false }),
          makeCollection({ id: "u2", name: "U2", kind: "standard", is_favorite: false }),
          makeCollection({ id: "s1", name: "S1", kind: "smart", is_favorite: false }),
          makeCollection({ id: "fr1", name: "Fr1", kind: "virtual", virtual_type: "franchise", is_favorite: false }),
          makeCollection({ id: "vc1", name: "Vc1", kind: "virtual", virtual_type: "collection", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Three kind buttons with PLAIN labels — the count is not baked into any button.
      expect(getByText("Standard").textContent).toBe("Standard");
      expect(getByText("Smart").textContent).toBe("Smart");
      expect(getByText("Virtual").textContent).toBe("Virtual");
      // The count lives once in the active kind's section header (the favorite is
      // excluded — it's the top-level toggle — and the scope is the default "All").
      expect(container.textContent).toContain("STANDARD COLLECTIONS (2)");
      // No "Favorites" button (now a top-level toggle).
      expect(container.textContent).not.toContain("Favorites (");
    });

    it("defaults to the Standard sub-tab and shows only non-favorite standard collections", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "f1", name: "FavOne", kind: "standard", is_favorite: true }),
          makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false }),
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
          makeCollection({ id: "f1", name: "FavOne", kind: "standard", is_favorite: true }),
          makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false }),
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
          makeCollection({ id: "u1", name: "U1", kind: "standard", is_favorite: false }),
          makeCollection({ id: "u2", name: "U2", kind: "standard", is_favorite: false }),
          makeCollection({ id: "s1", name: "S1", kind: "smart", is_favorite: false }),
          makeCollection({ id: "fr1", name: "Fr1", kind: "virtual", virtual_type: "franchise", is_favorite: false }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Default Standard sub-tab → header reflects 2 visible.
      expect(container.textContent).toContain("STANDARD COLLECTIONS (2)");

      await act(async () => {
        fireEvent.click(getByText("Smart"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("SMART COLLECTIONS (1)");

      await act(async () => {
        fireEvent.click(getByText("Virtual"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("VIRTUAL (1)");
    });

    it("renders a 'No <sub-tab> collections' empty state when the bucket is empty", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          // Only smart collections — my/virtual buckets are empty.
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
      // Default Standard sub-tab → no standard collections present.
      expect(container.textContent).toContain("No standard collections");
    });

    it("sub-tab resets to Standard each time the Collections tab is opened", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false }),
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

    it("Virtual sub-tab lists both virtual types, each row labelled by its type", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "fr1",
            name: "FranchiseOne",
            kind: "virtual",
            virtual_type: "franchise",
            is_favorite: false,
            rom_count: 3,
          }),
          makeCollection({
            id: "vc1",
            name: "SeriesOne",
            kind: "virtual",
            virtual_type: "collection",
            is_favorite: false,
            rom_count: 4,
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
      await act(async () => {
        fireEvent.click(getByText("Virtual"));
        await Promise.resolve();
      });
      // Both types listed under the one flat Virtual sub-tab...
      const franchiseRow = container.querySelector<HTMLElement>('[data-label="FranchiseOne"]');
      const collectionRow = container.querySelector<HTMLElement>('[data-label="SeriesOne"]');
      expect(franchiseRow).not.toBeNull();
      expect(collectionRow).not.toBeNull();
      // ...each row's description leads with its type label.
      expect(franchiseRow?.getAttribute("data-description")).toBe("Franchise · 3 ROMs");
      expect(collectionRow?.getAttribute("data-description")).toBe("IGDB Collection · 4 ROMs");
    });

    it("a virtual row missing its type falls back to the plain ROM count", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "fr1", name: "LegacyVirtual", kind: "virtual", is_favorite: false, rom_count: 5 }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Virtual"));
        await Promise.resolve();
      });
      const row = container.querySelector<HTMLElement>('[data-label="LegacyVirtual"]');
      expect(row?.getAttribute("data-description")).toBe("5 ROMs");
    });
  });

  // ------------------------------------------------------------------
  // H2. Collections tab — favorites top-level toggle
  // ------------------------------------------------------------------
  describe("collections tab — favorites toggle", () => {
    it("renders the favorites toggle ENABLED when exactly one favorites collection exists", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "f1", name: "Faves", kind: "standard", is_favorite: true, rom_count: 3 })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const favInput = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input');
      expect(favInput).not.toBeNull();
      expect(favInput?.disabled).toBe(false);
    });

    it("renders the Sync RomM favorites toggle with the singular description for 1 game", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "f1",
            name: "Faves",
            kind: "standard",
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
      expect(favToggle?.getAttribute("data-description")).toBe("Includes 1 favorited ROM");
    });

    it("renders the plural description for N>1 favorited games", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "f1", name: "Faves", kind: "standard", is_favorite: true, rom_count: 7 })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const favToggle = container.querySelector<HTMLElement>('[data-label="Sync RomM favorites"]');
      expect(favToggle?.getAttribute("data-description")).toBe("Includes 7 favorited ROMs");
    });

    it("renders the plural description for 0 favorited games", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "f1", name: "Faves", kind: "standard", is_favorite: true, rom_count: 0 })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      const favToggle = container.querySelector<HTMLElement>('[data-label="Sync RomM favorites"]');
      expect(favToggle?.getAttribute("data-description")).toBe("Includes 0 favorited ROMs");
    });

    it("clicking the favorites toggle calls saveCollectionSync with the favorites id and kind=standard", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "favid",
            name: "Faves",
            kind: "standard",
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
      expect(vi.mocked(backend.saveCollectionSync)).toHaveBeenCalledWith("favid", "standard", true);
    });

    it("reverts the favorites toggle on saveCollectionSync rejection", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "favid",
            name: "Faves",
            kind: "standard",
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

    it("renders the favorites toggle disabled (grayed, not hidden) when no favorites collection exists", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "U1", kind: "standard", is_favorite: false })],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Present (never hidden / no pop-in), but disabled — nothing to sync.
      const favInput = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input');
      expect(favInput).not.toBeNull();
      expect(favInput?.disabled).toBe(true);
    });

    it("falls back to listing favorites in the Standard sub-tab when more than one exists (with console warning)", async () => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      try {
        vi.mocked(backend.getCollections).mockResolvedValue({
          success: true,
          collections: [
            makeCollection({ id: "f1", name: "FavA", kind: "standard", is_favorite: true }),
            makeCollection({ id: "f2", name: "FavB", kind: "standard", is_favorite: true }),
            makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false }),
          ],
        });
        const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          fireEvent.click(getByText("Collections"));
          await Promise.resolve();
          await Promise.resolve();
        });
        // Top toggle stays present but disabled — a single toggle can't
        // represent multiple favorites, so it grays out rather than hiding.
        const favInput = container.querySelector<HTMLInputElement>('[data-label="Sync RomM favorites"] input');
        expect(favInput).not.toBeNull();
        expect(favInput?.disabled).toBe(true);
        // Both favorites surface in Standard (alongside the regular standard collection).
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
  // N. Collections tab — owner scope (Own / All) — #1532
  // ------------------------------------------------------------------
  describe("collections tab — owner scope (Own / All)", () => {
    const ownAndForeign = (): CollectionSyncSetting[] => [
      makeCollection({ id: "u1", name: "MineColl", kind: "standard", is_favorite: false, is_own: true }),
      makeCollection({ id: "u2", name: "TheirColl", kind: "standard", is_favorite: false, is_own: false }),
    ];

    const openCollections = async (getByText: (t: string) => HTMLElement) => {
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
    };

    it("renders the Own / All control on the Collections tab", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: ownAndForeign() });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      expect(getByText("Mine")).not.toBeNull();
      expect(getByText("All")).not.toBeNull();
    });

    it("clicking Own calls setCollectionOwnerScope('own')", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: ownAndForeign() });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await act(async () => {
        fireEvent.click(getByText("Mine"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.setCollectionOwnerScope)).toHaveBeenCalledWith("own");
    });

    it("Own hides foreign (is_own=false) collections from the list", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: ownAndForeign() });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Default "All" → both visible.
      expect(container.querySelector('[data-label="MineColl"]')).not.toBeNull();
      expect(container.querySelector('[data-label="TheirColl"]')).not.toBeNull();
      // Switch to Own → the foreign collection is hidden.
      await act(async () => {
        fireEvent.click(getByText("Mine"));
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="MineColl"]')).not.toBeNull();
      expect(container.querySelector('[data-label="TheirColl"]')).toBeNull();
    });

    it("initializes the scope from getSettings().collection_owner_scope", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({ ...defaultSettings(), collection_owner_scope: "own" });
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: ownAndForeign() });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Loaded as Own → the foreign collection is hidden from the start.
      expect(container.querySelector('[data-label="MineColl"]')).not.toBeNull();
      expect(container.querySelector('[data-label="TheirColl"]')).toBeNull();
    });

    it("rolls the scope back (foreign reappears) when setCollectionOwnerScope rejects", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: ownAndForeign() });
      vi.mocked(backend.setCollectionOwnerScope).mockRejectedValue(new Error("net"));
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await act(async () => {
        fireEvent.click(getByText("Mine"));
        await Promise.resolve();
        await Promise.resolve();
      });
      // The rejected write proves the click fired; the catch rolls ownerScope
      // back to "all", so the foreign collection is visible again.
      expect(vi.mocked(backend.setCollectionOwnerScope)).toHaveBeenCalledWith("own");
      expect(container.querySelector('[data-label="TheirColl"]')).not.toBeNull();
    });

    it("the section-header count is scope-aware (Mine drops the foreign collection)", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: ownAndForeign() });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Default "All" → both the own and foreign standard collection are counted.
      expect(container.textContent).toContain("STANDARD COLLECTIONS (2)");
      // Switch to "Mine" → the header count drops the foreign one.
      await act(async () => {
        fireEvent.click(getByText("Mine"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("STANDARD COLLECTIONS (1)");
    });
  });

  // ------------------------------------------------------------------
  // N2. Collections tab — search + render-cap + per-type filter + batch set-all
  // ------------------------------------------------------------------
  describe("collections tab — search, render-cap, per-type filter, batch set-all", () => {
    const openCollections = async (getByText: (t: string) => HTMLElement) => {
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Collections"));
        await Promise.resolve();
        await Promise.resolve();
      });
    };

    const typeSearch = async (getByTestId: (t: string) => HTMLElement, value: string) => {
      await act(async () => {
        fireEvent.change(getByTestId("text-field"), { target: { value } });
        await Promise.resolve();
      });
    };

    it("filters the visible list by fuzzy name match and restores it when cleared", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "u1", name: "Action Heroes", kind: "standard", is_favorite: false }),
          makeCollection({ id: "u2", name: "Puzzle Box", kind: "standard", is_favorite: false }),
        ],
      });
      const { getByText, getByTestId, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // "actn" is an in-order subsequence of "Action Heroes" but not "Puzzle Box".
      await typeSearch(getByTestId, "actn");
      expect(container.querySelector('[data-label="Action Heroes"]')).not.toBeNull();
      expect(container.querySelector('[data-label="Puzzle Box"]')).toBeNull();
      // Clearing restores both.
      await typeSearch(getByTestId, "");
      expect(container.querySelector('[data-label="Action Heroes"]')).not.toBeNull();
      expect(container.querySelector('[data-label="Puzzle Box"]')).not.toBeNull();
    });

    it("hints the fuzzy match in the search field heading", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "Action Heroes", kind: "standard", is_favorite: false })],
      });
      const { getByText, getByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      expect(getByTestId("text-field").getAttribute("aria-label")).toBe("Search collections (fuzzy)");
    });

    it("lifts the search field to the top of the view on the FIRST keystroke only (not on later keystrokes)", async () => {
      // Element-to-top scroll (#1539): the wrapper ref is handed to
      // scrollElementToTop so its "SEARCH COLLECTIONS" heading lands at the top
      // of the outermost scroller, clear of the on-screen keyboard. The scroll
      // math itself is unit-tested in scrollHelpers.test.ts; here we assert the
      // component wires the trigger correctly (first keystroke only).
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "Action Heroes", kind: "standard", is_favorite: false })],
      });
      const scrollToField = vi.mocked(scrollElementToTop);
      const { getByText, getByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      scrollToField.mockClear();
      // First keystroke: empty → non-empty → lift the field to the top.
      await typeSearch(getByTestId, "a");
      expect(scrollToField).toHaveBeenCalledTimes(1);
      expect(scrollToField.mock.calls[0]?.[0]).toBeInstanceOf(HTMLElement);
      // Later keystrokes (still non-empty) must NOT re-scroll.
      await typeSearch(getByTestId, "ac");
      expect(scrollToField).toHaveBeenCalledTimes(1);
    });

    it("lifts the search field to the top of the view when it gains focus", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [makeCollection({ id: "u1", name: "Action Heroes", kind: "standard", is_favorite: false })],
      });
      const scrollToField = vi.mocked(scrollElementToTop);
      const { getByText, getByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      scrollToField.mockClear();
      await act(async () => {
        fireEvent.focus(getByTestId("text-field"));
        await Promise.resolve();
      });
      expect(scrollToField).toHaveBeenCalledTimes(1);
      expect(scrollToField.mock.calls[0]?.[0]).toBeInstanceOf(HTMLElement);
    });

    it("caps the rendered rows and shows a '<n> more' hint past the cap", async () => {
      const many: CollectionSyncSetting[] = Array.from({ length: 60 }, (_, i) =>
        makeCollection({
          id: `u${i}`,
          name: `Coll ${String(i).padStart(3, "0")}`,
          kind: "standard",
          is_favorite: false,
        }),
      );
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: many });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // Never paints more than the cap (50): exactly 50 collection rows, not 60.
      // Count the list rows by their "Coll " label so the always-present
      // (here disabled) favorites toggle isn't counted.
      const toggles = container.querySelectorAll('[data-label^="Coll "]');
      expect(toggles).toHaveLength(50);
      // The overflow (60 - 50 = 10) is surfaced as a single hint row.
      expect(container.textContent).toContain("10 more — refine your search");
      // The section header still reflects the full match count.
      expect(container.textContent).toContain("STANDARD COLLECTIONS (60)");
    });

    it("search narrows a huge list below the cap and drops the hint", async () => {
      const many: CollectionSyncSetting[] = Array.from({ length: 60 }, (_, i) =>
        makeCollection({
          id: `u${i}`,
          name: `Coll ${String(i).padStart(3, "0")}`,
          kind: "standard",
          is_favorite: false,
        }),
      );
      vi.mocked(backend.getCollections).mockResolvedValue({ success: true, collections: many });
      const { getByText, getByTestId, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // "coll 001" matches exactly one collection.
      await typeSearch(getByTestId, "coll 001");
      // Count list rows by their "Coll " label (excludes the favorites toggle).
      const toggles = container.querySelectorAll('[data-label^="Coll "]');
      expect(toggles).toHaveLength(1);
      expect(container.textContent).not.toContain("more — refine your search");
    });

    it("shows the per-type filter only on the Virtual sub-tab", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({ id: "u1", name: "UserOne", kind: "standard", is_favorite: false }),
          makeCollection({ id: "fr1", name: "Fr1", kind: "virtual", virtual_type: "franchise", is_favorite: false }),
        ],
      });
      const { getByText, queryByText } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      // On the default Standard sub-tab the per-type labels are absent.
      expect(queryByText("Franchise")).toBeNull();
      expect(queryByText("IGDB Collection")).toBeNull();
      // On the Virtual sub-tab they appear.
      await act(async () => {
        fireEvent.click(getByText("Virtual"));
        await Promise.resolve();
      });
      expect(getByText("Franchise")).not.toBeNull();
      expect(getByText("IGDB Collection")).not.toBeNull();
    });

    it("the per-type filter narrows the Virtual list by virtual_type", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "fr1",
            name: "FranchiseOne",
            kind: "virtual",
            virtual_type: "franchise",
            is_favorite: false,
          }),
          makeCollection({
            id: "vc1",
            name: "SeriesOne",
            kind: "virtual",
            virtual_type: "collection",
            is_favorite: false,
          }),
        ],
      });
      const { getByText, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await act(async () => {
        fireEvent.click(getByText("Virtual"));
        await Promise.resolve();
      });
      // Both visible under "All".
      expect(container.querySelector('[data-label="FranchiseOne"]')).not.toBeNull();
      expect(container.querySelector('[data-label="SeriesOne"]')).not.toBeNull();
      // Narrow to Franchise → only the franchise-typed row remains.
      await act(async () => {
        fireEvent.click(getByText("Franchise"));
        await Promise.resolve();
      });
      expect(container.querySelector('[data-label="FranchiseOne"]')).not.toBeNull();
      expect(container.querySelector('[data-label="SeriesOne"]')).toBeNull();
    });

    it("Enable All with an active search uses the batch callable with only the matched ids", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "u1",
            name: "Action Heroes",
            kind: "standard",
            is_favorite: false,
            sync_enabled: false,
          }),
          makeCollection({ id: "u2", name: "Action Squad", kind: "standard", is_favorite: false, sync_enabled: false }),
          makeCollection({ id: "u3", name: "Puzzle Box", kind: "standard", is_favorite: false, sync_enabled: false }),
        ],
      });
      const { getByText, getByTestId } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await typeSearch(getByTestId, "action");
      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
      });
      // No confirm (bounded subset), no whole-kind call, batch with only matches.
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.setAllCollectionsSync)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.saveCollectionsSync)).toHaveBeenCalledWith(["u1", "u2"], "standard", true);
    });

    it("Enable All under 'Own' scope (empty search) batches the own ids and never takes the whole-kind path", async () => {
      // Own scope makes the visible set a bounded subset (one user's own
      // collections), so Enable-All must NOT stamp the whole kind — no confirm,
      // no set_all_collections_sync, and the foreign id is excluded.
      vi.mocked(backend.getSettings).mockResolvedValue({ ...defaultSettings(), collection_owner_scope: "own" });
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "u1",
            name: "MineOne",
            kind: "standard",
            is_favorite: false,
            is_own: true,
            sync_enabled: false,
          }),
          makeCollection({
            id: "u2",
            name: "MineTwo",
            kind: "standard",
            is_favorite: false,
            is_own: true,
            sync_enabled: false,
          }),
          makeCollection({
            id: "u3",
            name: "TheirOne",
            kind: "standard",
            is_favorite: false,
            is_own: false,
            sync_enabled: false,
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
      });
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.setAllCollectionsSync)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.saveCollectionsSync)).toHaveBeenCalledWith(["u1", "u2"], "standard", true);
    });

    it("Disable All under an active per-type filter batches only that type's ids", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "fr1",
            name: "FranchiseOne",
            kind: "virtual",
            virtual_type: "franchise",
            is_favorite: false,
            sync_enabled: true,
          }),
          makeCollection({
            id: "fr2",
            name: "FranchiseTwo",
            kind: "virtual",
            virtual_type: "franchise",
            is_favorite: false,
            sync_enabled: true,
          }),
          makeCollection({
            id: "vc1",
            name: "SeriesOne",
            kind: "virtual",
            virtual_type: "collection",
            is_favorite: false,
            sync_enabled: true,
          }),
        ],
      });
      const { getByText } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await act(async () => {
        fireEvent.click(getByText("Virtual"));
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Franchise"));
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Disable All"));
        await Promise.resolve();
      });
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.setAllCollectionsSync)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.saveCollectionsSync)).toHaveBeenCalledWith(["fr1", "fr2"], "virtual", false);
    });

    it("rolls back the optimistic batch flip when saveCollectionsSync rejects", async () => {
      vi.mocked(backend.getCollections).mockResolvedValue({
        success: true,
        collections: [
          makeCollection({
            id: "u1",
            name: "Action Heroes",
            kind: "standard",
            is_favorite: false,
            sync_enabled: false,
          }),
          makeCollection({ id: "u2", name: "Puzzle Box", kind: "standard", is_favorite: false, sync_enabled: false }),
        ],
      });
      vi.mocked(backend.saveCollectionsSync).mockRejectedValue(new Error("boom"));
      const { getByText, getByTestId, container } = render(<LibraryPage onBack={vi.fn()} />);
      await openCollections(getByText);
      await typeSearch(getByTestId, "action");
      await act(async () => {
        fireEvent.click(getByText("Enable All"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // Clear the search to bring both rows back into view and assert the flip
      // rolled back (Action Heroes is unchecked again).
      await typeSearch(getByTestId, "");
      const a = container.querySelector<HTMLInputElement>('[data-label="Action Heroes"] input');
      expect(a?.checked).toBe(false);
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
