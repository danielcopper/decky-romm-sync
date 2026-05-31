// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (status string surfaced via Field label, captured prop
// on a child, logError spy, etc.). Only truly-`/* ignore */` catches (no state
// change, no log call) are exempt — and even then, prefer dropping the test
// over keeping one with zero expects.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { DangerZone } from "./DangerZone";
import * as backend from "../api/backend";
import { showModal } from "@decky/ui";
import { removeShortcut } from "../utils/steamShortcuts";
import { clearPlatformCollection, clearAllRomMCollections } from "../utils/collections";
import { formatUninstallStatus } from "../utils/formatters";
import { stubCollectionStore, stubAppStore } from "../test-utils/steamStubs";

vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));
vi.mock("../utils/steamShortcuts", () => ({ removeShortcut: vi.fn() }));
vi.mock("../utils/collections", () => ({
  clearPlatformCollection: vi.fn(),
  clearAllRomMCollections: vi.fn(),
}));
vi.mock("../utils/formatters", () => ({
  formatUninstallStatus: vi.fn((removed: number, errors: number) => `Removed ${removed}, ${errors} errors`),
}));

// flushAsync: drain the mount-time useEffect chain. DangerZone fires three
// parallel async loads (refreshPlatforms, loadNonSteamApps, getWhitelistSettings)
// — double-await pattern mirrors SettingsPage.test.tsx.
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

function makeOverview(id: number, name: string, useDisplay = false) {
  return useDisplay
    ? { strDisplayName: undefined, display_name: name, appid: id }
    : { strDisplayName: name, display_name: undefined, appid: id };
}

function lastShownModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

function shownModalPropsAt<T = Record<string, unknown>>(idx: number): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  const el = calls[idx]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

describe("DangerZone", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // Defaults — tests override per case. These resolve fine; the catch paths
    // explicitly switch to mockRejectedValue.
    vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({ platforms: [] });
    vi.mocked(backend.getWhitelistSettings).mockResolvedValue({
      disabled_defaults: [],
      custom_names: [],
    });
    vi.mocked(backend.updateWhitelistSettings).mockResolvedValue({
      success: true,
    });
    vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
      success: true,
      app_ids: [],
      rom_ids: [],
      platform_name: "",
    });
    vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
      success: true,
      message: "",
      app_ids: [],
      rom_ids: [],
    });
    vi.mocked(backend.reportRemovalResults).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
      success: true,
      removed_count: 0,
      errors: [],
    });
    vi.mocked(backend.deletePlatformSaves).mockResolvedValue({
      success: true,
      deleted_count: 0,
      message: "",
    });
    vi.mocked(backend.deletePlatformBios).mockResolvedValue({
      success: true,
      deleted_count: 0,
      message: "",
    });
    vi.mocked(clearPlatformCollection).mockResolvedValue(undefined);
    vi.mocked(clearAllRomMCollections).mockResolvedValue(undefined);
    // Default app store / collection store — empty.
    stubCollectionStore([]);
    stubAppStore({});
    // test-setup's vi.stubGlobal calls run once at module-load; afterEach's
    // vi.unstubAllGlobals() strips them. Re-stub SteamClient.Apps.RemoveShortcut
    // here so RetroDeckSection.handleRemoveAll can fire without ReferenceError.
    vi.stubGlobal("SteamClient", {
      Apps: { RemoveShortcut: vi.fn() },
    });
  });

  describe("mount", () => {
    it("renders the Back button and triggers onBack on click", async () => {
      const onBack = vi.fn();
      const { getByText } = render(<DangerZone onBack={onBack} />);
      await flushAsync();
      fireEvent.click(getByText("Back"));
      expect(onBack).toHaveBeenCalledTimes(1);
    });

    it("calls getRegistryPlatforms + getWhitelistSettings on mount", async () => {
      render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(backend.getRegistryPlatforms)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.getWhitelistSettings)).toHaveBeenCalledTimes(1);
    });

    it("applies the fetched platform list", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [
          { slug: "snes", name: "Super Nintendo", count: 3 },
          { slug: "nes", name: "NES", count: 1 },
        ],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(getByText("Super Nintendo (3)")).toBeTruthy();
      expect(getByText("NES (1)")).toBeTruthy();
    });

    it("applies fetched whitelist settings (disabled defaults + custom names)", async () => {
      vi.mocked(backend.getWhitelistSettings).mockResolvedValue({
        disabled_defaults: ["firefox"],
        custom_names: ["MyCustomApp"],
      });
      // Two apps: "MyCustomApp" custom-whitelisted, "Firefox" default-pattern
      // but disabled → NOT in whitelistedIds.
      stubCollectionStore([1, 2]);
      stubAppStore({
        1: { strDisplayName: "MyCustomApp" },
        2: { strDisplayName: "Firefox" },
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // 1 protected: MyCustomApp. Firefox excluded because firefox is disabled.
      expect(getByText("Configure Whitelist (1 protected)")).toBeTruthy();
    });

    it("falls back to empty platforms when getRegistryPlatforms rejects", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockRejectedValue(new Error("net"));
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("No synced platforms");
    });

    it("shows the loading Spinner before refreshPlatforms resolves", () => {
      vi.mocked(backend.getRegistryPlatforms).mockImplementation(
        () =>
          new Promise(() => {
            /* stall */
          }),
      );
      const { queryAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      // initial render runs before any effect — but useEffect fires before
      // the assert below; loading state is still true while the promise stalls.
      expect(queryAllByTestId("spinner").length).toBeGreaterThan(0);
    });

    it("logs the failure when getWhitelistSettings rejects on mount", async () => {
      vi.mocked(backend.getWhitelistSettings).mockRejectedValue(new Error("offline"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // The .catch((e) => logError(...)) on the mount-time load must fire.
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to load whitelist settings"));
      logSpy.mockRestore();
    });
  });

  describe("loadNonSteamApps", () => {
    it("warns and clears the list when collectionStore is undefined", async () => {
      vi.stubGlobal("collectionStore", undefined);
      const logSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("collectionStore not available"));
      expect(container.textContent).toContain("No non-steam games found");
      logSpy.mockRestore();
    });

    it("warns and clears the list when deckDesktopApps.apps is missing", async () => {
      vi.stubGlobal("collectionStore", {
        deckDesktopApps: undefined,
        userCollections: [],
      });
      const logSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("deckDesktopApps.apps not available"));
      expect(container.textContent).toContain("No non-steam games found");
      logSpy.mockRestore();
    });

    it("enumerates apps and resolves display names alphabetically", async () => {
      // Intentionally not in alphabetical order — DangerZone.loadNonSteamApps
      // sorts the list before setState. Opening the whitelist surfaces the
      // toggle list in render order; we assert it matches the sorted order.
      stubCollectionStore([101, 102, 103]);
      stubAppStore({
        101: { strDisplayName: "Zebra App" },
        102: { strDisplayName: "Apple App" },
        103: { strDisplayName: "Mango App" },
      });
      const logSpy = vi.spyOn(backend, "logInfo").mockImplementation(() => {});
      const { getByText, getAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // logInfo fires with size — confirms enumeration ran.
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("deckDesktopApps.apps size: 3"));
      // Open the whitelist so the per-app ToggleField rows render. The
      // toggle <div> wraps the input + label text node; the parent's
      // textContent gives us the visible label per row.
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      const toggleRows = getAllByTestId("toggle");
      const renderedNames = toggleRows.map((row) => row.textContent);
      expect(renderedNames).toEqual(["Apple App", "Mango App", "Zebra App"]);
      logSpy.mockRestore();
    });

    it("falls back to display_name when strDisplayName is missing", async () => {
      stubCollectionStore([200]);
      vi.stubGlobal("appStore", {
        GetAppOverviewByAppID: vi.fn(() => makeOverview(200, "DisplayOnly", true)),
        allApps: [],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Open whitelist to surface name; first click resets confirm flags.
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      // The toggle label echoes the display name; query via textContent on the
      // container's toggles.
      expect(getByText("DisplayOnly")).toBeTruthy();
    });

    it("falls back to 'Unknown (id)' when no overview is returned", async () => {
      stubCollectionStore([999]);
      vi.stubGlobal("appStore", {
        GetAppOverviewByAppID: vi.fn(() => null),
        allApps: [],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      expect(getByText("Unknown (999)")).toBeTruthy();
    });

    it("falls back to 'Unknown (id)' when appStore is undefined", async () => {
      stubCollectionStore([42]);
      vi.stubGlobal("appStore", undefined);
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      expect(getByText("Unknown (42)")).toBeTruthy();
    });

    it("logs an error when enumeration throws", async () => {
      // Force iteration to throw — set keys() to throw.
      vi.stubGlobal("collectionStore", {
        deckDesktopApps: {
          apps: {
            get size() {
              return 1;
            },
            keys() {
              throw new Error("iteration boom");
            },
          },
        },
        userCollections: [],
      });
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to enumerate non-steam games"));
      // After catch, the list remains empty.
      expect(container.textContent).toContain("No non-steam games found");
      logSpy.mockRestore();
    });
  });

  describe("ShortcutRemovalSection — empty / loading", () => {
    it("renders 'No synced platforms' when platforms is empty and not loading", async () => {
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("No synced platforms");
    });
  });

  describe("ShortcutRemovalSection — handleRemoveShortcuts", () => {
    function setupOnePlatform() {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 2 }],
      });
    }

    it("calls removePlatformShortcuts + removeShortcut per app + reportRemovalResults + clearPlatformCollection on happy path", async () => {
      setupOnePlatform();
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [11, 12],
        rom_ids: [1, 2],
        platform_name: "Super Nintendo",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Open the platform modal.
      fireEvent.click(getByText("Super Nintendo (2)"));
      const modalProps = lastShownModalProps<{
        onRemoveShortcuts?: () => void;
      }>();
      await act(async () => {
        modalProps?.onRemoveShortcuts?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.removePlatformShortcuts)).toHaveBeenCalledWith("snes");
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(11);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(12);
      expect(vi.mocked(backend.reportRemovalResults)).toHaveBeenCalledWith([1, 2]);
      expect(vi.mocked(clearPlatformCollection)).toHaveBeenCalledWith("Super Nintendo");
    });

    it("falls back to p.name for clearPlatformCollection when platform_name is empty", async () => {
      setupOnePlatform();
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [],
        rom_ids: [],
        platform_name: "",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (2)"));
      const modalProps = lastShownModalProps<{
        onRemoveShortcuts?: () => void;
      }>();
      await act(async () => {
        modalProps?.onRemoveShortcuts?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(clearPlatformCollection)).toHaveBeenCalledWith("Super Nintendo");
    });

    it("skips reportRemovalResults when rom_ids is empty", async () => {
      setupOnePlatform();
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [],
        rom_ids: [],
        platform_name: "Super Nintendo",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (2)"));
      const modalProps = lastShownModalProps<{ onRemoveShortcuts?: () => void }>();
      await act(async () => {
        modalProps?.onRemoveShortcuts?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
    });

    it("surfaces 'Failed to remove shortcuts' via the actionStatus Field on rejection", async () => {
      setupOnePlatform();
      vi.mocked(backend.removePlatformShortcuts).mockRejectedValue(new Error("boom"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (2)"));
      const modalProps = lastShownModalProps<{ onRemoveShortcuts?: () => void }>();
      await act(async () => {
        modalProps?.onRemoveShortcuts?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to remove shortcuts");
    });

    it("surfaces the migration-blocked message and skips removal when success is false", async () => {
      setupOnePlatform();
      // The @migration_blocked gate returns no app_ids/rom_ids — the handler
      // must surface the message and not attempt any removal.
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: false,
        message: "Blocked: RetroDECK migration pending",
        blocked_by_migration: true,
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (2)"));
      const modalProps = lastShownModalProps<{ onRemoveShortcuts?: () => void }>();
      await act(async () => {
        modalProps?.onRemoveShortcuts?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Blocked: RetroDECK migration pending");
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
      expect(vi.mocked(clearPlatformCollection)).not.toHaveBeenCalled();
    });

    it("renders the singular form for a 1-game platform", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 1 }],
      });
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [11],
        rom_ids: [],
        platform_name: "Super Nintendo",
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const modalProps = lastShownModalProps<{ onRemoveShortcuts?: () => void }>();
      await act(async () => {
        modalProps?.onRemoveShortcuts?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Removed 1 Super Nintendo game");
      // The singular form must NOT carry the trailing "s".
      expect(container.textContent).not.toContain("Removed 1 Super Nintendo games");
    });
  });

  describe("ShortcutRemovalSection — handleDeleteSaves", () => {
    function setupOnePlatform() {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 1 }],
      });
    }

    it("opens a ConfirmModal with the correct title + description", async () => {
      setupOnePlatform();
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteSaves?: () => void }>();
      act(() => {
        platformModal?.onDeleteSaves?.();
      });
      const confirm = lastShownModalProps<{
        strTitle?: string;
        strDescription?: string;
        strOKButtonText?: string;
      }>();
      expect(confirm?.strTitle).toBe("Delete all save files for Super Nintendo?");
      expect(confirm?.strDescription).toContain("local save file");
      expect(confirm?.strOKButtonText).toBe("Delete Save Files");
    });

    it("falls back to p.slug when p.name is empty", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "", count: 1 }],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Button still uses p.name (empty) + " (1)"; click via the count.
      fireEvent.click(getByText(/\(1\)/));
      const platformModal = lastShownModalProps<{ onDeleteSaves?: () => void }>();
      act(() => {
        platformModal?.onDeleteSaves?.();
      });
      const confirm = lastShownModalProps<{ strTitle?: string }>();
      expect(confirm?.strTitle).toBe("Delete all save files for snes?");
    });

    it("calls deletePlatformSaves + dispatches romm_data_changed on OK", async () => {
      setupOnePlatform();
      vi.mocked(backend.deletePlatformSaves).mockResolvedValue({
        success: true,
        deleted_count: 3,
        message: "Deleted 3 save files",
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteSaves?: () => void }>();
      act(() => {
        platformModal?.onDeleteSaves?.();
      });
      const confirm = lastShownModalProps<{ onOK?: () => void | Promise<void> }>();

      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await confirm?.onOK?.();
        });
        expect(vi.mocked(backend.deletePlatformSaves)).toHaveBeenCalledWith("snes");
        expect(container.textContent).toContain("Deleted 3 save files");
        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({ type: "save_sync" });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("surfaces 'Failed to delete saves' on rejection", async () => {
      setupOnePlatform();
      vi.mocked(backend.deletePlatformSaves).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteSaves?: () => void }>();
      act(() => {
        platformModal?.onDeleteSaves?.();
      });
      const confirm = lastShownModalProps<{ onOK?: () => void | Promise<void> }>();
      await act(async () => {
        await confirm?.onOK?.();
      });
      expect(container.textContent).toContain("Failed to delete saves");
    });
  });

  describe("ShortcutRemovalSection — handleDeleteBios", () => {
    function setupOnePlatform() {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 1 }],
      });
    }

    it("calls deletePlatformBios and surfaces result.message", async () => {
      setupOnePlatform();
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: true,
        deleted_count: 2,
        message: "Deleted 2 BIOS files",
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteBios?: () => void }>();
      await act(async () => {
        platformModal?.onDeleteBios?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.deletePlatformBios)).toHaveBeenCalledWith("snes");
      expect(container.textContent).toContain("Deleted 2 BIOS files");
    });

    it("surfaces 'Failed to delete BIOS files' on rejection", async () => {
      setupOnePlatform();
      vi.mocked(backend.deletePlatformBios).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteBios?: () => void }>();
      await act(async () => {
        platformModal?.onDeleteBios?.();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to delete BIOS files");
    });
  });

  describe("ShortcutRemovalSection — handleRemoveAllRomm", () => {
    it("first click arms confirm + relabels the button; second click triggers removeAllShortcuts", async () => {
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed 5",
        app_ids: [10, 20],
        rom_ids: [1, 2],
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // First click — arms confirm.
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      expect(container.textContent).toContain("Confirm: remove all RomM shortcuts?");
      expect(vi.mocked(backend.removeAllShortcuts)).not.toHaveBeenCalled();

      // Second click — runs the removal.
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.removeAllShortcuts)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(10);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(20);
      expect(vi.mocked(backend.reportRemovalResults)).toHaveBeenCalledWith([1, 2]);
      expect(vi.mocked(clearAllRomMCollections)).toHaveBeenCalled();
      expect(container.textContent).toContain("Removed 5");
    });

    it("skips reportRemovalResults when rom_ids is empty", async () => {
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed 0",
        app_ids: [],
        rom_ids: [],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
    });
  });

  describe("ShortcutRemovalSection — handleUninstallAll", () => {
    it("first click arms confirm + shows the warning Field; second click triggers uninstallAllRoms", async () => {
      vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
        success: true,
        removed_count: 7,
        errors: [],
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      fireEvent.click(getByText("Uninstall All Installed ROMs"));
      // Warning Field and confirm-state button label both visible.
      expect(container.textContent).toContain("Confirm: delete all ROM files?");
      expect(container.textContent).toContain("This will delete all downloaded ROM files");
      expect(vi.mocked(backend.uninstallAllRoms)).not.toHaveBeenCalled();

      await act(async () => {
        fireEvent.click(getByText("Confirm: delete all ROM files?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.uninstallAllRoms)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(formatUninstallStatus)).toHaveBeenCalledWith(7, 0);
      // formatUninstallStatus mock returns "Removed 7, 0 errors"
      expect(container.textContent).toContain("Removed 7, 0 errors");
    });

    it("surfaces 'Failed to uninstall ROMs' on rejection and still refreshes", async () => {
      vi.mocked(backend.uninstallAllRoms).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Uninstall All Installed ROMs"));
      const refreshBefore = vi.mocked(backend.getRegistryPlatforms).mock.calls.length;
      await act(async () => {
        fireEvent.click(getByText("Confirm: delete all ROM files?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to uninstall ROMs");
      // confirmUninstall reset → button label returns to original.
      expect(container.textContent).toContain("Uninstall All Installed ROMs");
      // refreshPlatforms still ran after catch.
      expect(vi.mocked(backend.getRegistryPlatforms).mock.calls.length).toBeGreaterThan(refreshBefore);
    });

    it("counts errors via formatUninstallStatus when errors.length > 0", async () => {
      vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
        success: true,
        removed_count: 4,
        errors: [
          { rom_id: "1", error: "x" },
          { rom_id: "2", error: "y" },
        ],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Uninstall All Installed ROMs"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: delete all ROM files?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(formatUninstallStatus)).toHaveBeenCalledWith(4, 2);
    });
  });

  describe("PlatformActionModal", () => {
    it("renders 'game' (singular) for count=1 and 'games' (plural) for count>1", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [
          { slug: "a", name: "PlatA", count: 1 },
          { slug: "b", name: "PlatB", count: 2 },
        ],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      fireEvent.click(getByText("PlatA (1)"));
      const firstModal = vi.mocked(showModal).mock.calls.length - 1;
      // The modal renders inline DialogButton children including the label.
      // We can't easily assert on the inner text via showModal capture without
      // rendering the modal — instead, render the modal element directly.
      const platformAModalEl = vi.mocked(showModal).mock.calls[firstModal]?.[0];
      // Use textContent by rendering the modal in its own tree.
      const { container: containerA } = render(platformAModalEl as ReactElement);
      expect(containerA.textContent).toContain("Remove Shortcuts (1 game)");
      expect(containerA.textContent).not.toContain("Remove Shortcuts (1 games)");

      fireEvent.click(getByText("PlatB (2)"));
      const secondIdx = vi.mocked(showModal).mock.calls.length - 1;
      const platformBModalEl = vi.mocked(showModal).mock.calls[secondIdx]?.[0];
      const { container: containerB } = render(platformBModalEl as ReactElement);
      expect(containerB.textContent).toContain("Remove Shortcuts (2 games)");
    });

    it("Cancel closeModal does not trigger any backend call", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "a", name: "PlatA", count: 1 }],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("PlatA (1)"));
      const modalEl = vi.mocked(showModal).mock.calls[0]?.[0];
      const closeModal = vi.fn();
      // Render the modal with our own closeModal so we can assert it fired.
      const cloned = createElement((modalEl as ReactElement).type, {
        ...(modalEl as ReactElement<Record<string, unknown>>).props,
        closeModal,
      });
      const { getByText: getByTextModal } = render(cloned);
      fireEvent.click(getByTextModal("Cancel"));
      expect(closeModal).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.removePlatformShortcuts)).not.toHaveBeenCalled();
    });
  });

  describe("RetroDeckSection — empty / populated", () => {
    it("renders 'No non-steam games found' when there are no apps", async () => {
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("No non-steam games found");
    });

    it("renders the remove button when apps are present", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "MyGame" } });
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // 1 non-protected game (no default pattern match).
      expect(container.textContent).toContain("Remove 1 Non-Steam Games");
    });

    it("shows the ' (N excluded)' suffix when some apps are whitelisted", async () => {
      stubCollectionStore([1, 2]);
      stubAppStore({
        1: { strDisplayName: "Firefox" },
        2: { strDisplayName: "MyGame" },
      });
      const { container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Firefox auto-protected → 1 remaining + 1 excluded.
      expect(container.textContent).toContain("Remove 1 Non-Steam Games (1 excluded)");
    });
  });

  describe("RetroDeckSection — handleRemoveAll (no retrodeck risk)", () => {
    it("first click arms confirm; second click removes via SteamClient.Apps.RemoveShortcut", async () => {
      stubCollectionStore([1, 2]);
      stubAppStore({
        1: { strDisplayName: "GameOne" },
        2: { strDisplayName: "GameTwo" },
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove 2 Non-Steam Games"));
      // After first click — confirm copy without retrodeck warning.
      expect(container.textContent).toContain("Are you sure? Remove 2 games (0 whitelisted)?");
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).not.toHaveBeenCalled();

      await act(async () => {
        fireEvent.click(getByText("Are you sure? Remove 2 games (0 whitelisted)?"));
        await Promise.resolve();
      });
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).toHaveBeenCalledWith(1);
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).toHaveBeenCalledWith(2);
      // status surfaced.
      expect(container.textContent).toContain("Removed 2 non-steam games");
    });

    it("singular 'game' for 1 removed", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "OnlyGame" } });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove 1 Non-Steam Games"));
      await act(async () => {
        fireEvent.click(getByText("Are you sure? Remove 1 games (0 whitelisted)?"));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Removed 1 non-steam game");
      expect(container.textContent).not.toContain("Removed 1 non-steam games");
    });
  });

  describe("RetroDeckSection — handleRemoveAll (retrodeck at risk)", () => {
    it("first click → warn; second click → confirmRetrodeck; third click → execute", async () => {
      stubCollectionStore([1, 2]);
      stubAppStore({
        1: { strDisplayName: "RetroDECK" },
        2: { strDisplayName: "GameTwo" },
      });
      // Disable the retrodeck default pattern so it's NOT auto-protected.
      vi.mocked(backend.getWhitelistSettings).mockResolvedValue({
        disabled_defaults: ["retrodeck"],
        custom_names: [],
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      // First click — generic confirm with retrodeck warning copy.
      fireEvent.click(getByText("Remove 2 Non-Steam Games"));
      expect(container.textContent).toContain("WARNING: RetroDECK not protected! Remove 2 games?");
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).not.toHaveBeenCalled();

      // Second click — RETRODECK warning escalation.
      fireEvent.click(getByText("WARNING: RetroDECK not protected! Remove 2 games?"));
      expect(container.textContent).toContain("!! RETRODECK WILL BE REMOVED !!");
      expect(container.textContent).toContain("RetroDECK is NOT in the whitelist and will be permanently removed!");
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).not.toHaveBeenCalled();

      // Third click — actually remove.
      await act(async () => {
        fireEvent.click(getByText(/!! RETRODECK WILL BE REMOVED !!/));
        await Promise.resolve();
      });
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).toHaveBeenCalledWith(1);
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).toHaveBeenCalledWith(2);
    });
  });

  describe("WhitelistSection — collapse / expand + spinner", () => {
    it("collapsed by default; click reveals search + toggle list", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "Some App" } });
      const { getByText, queryByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Before click — no text field or toggle visible.
      expect(queryByTestId("text-field")).toBeNull();
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      // After click — text field + toggle present.
      expect(queryByTestId("text-field")).not.toBeNull();
      // Re-clicking hides.
      fireEvent.click(getByText("Hide Whitelist"));
      expect(queryByTestId("text-field")).toBeNull();
    });

    it("shows a Spinner when expanded but settings not yet loaded", async () => {
      vi.mocked(backend.getWhitelistSettings).mockImplementation(
        () =>
          new Promise(() => {
            /* stall */
          }),
      );
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "Some App" } });
      const { getByText, queryAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      // Spinner present after expansion.
      expect(queryAllByTestId("spinner").length).toBeGreaterThan(0);
    });
  });

  describe("WhitelistSection — filtering + toggle handlers", () => {
    function setupApps() {
      stubCollectionStore([1, 2, 3]);
      stubAppStore({
        1: { strDisplayName: "Alpha" },
        2: { strDisplayName: "Beta" },
        3: { strDisplayName: "Firefox" },
      });
    }

    it("filters via fuzzyMatch on TextField input", async () => {
      setupApps();
      const { getByText, getByTestId, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (1 protected)"));
      // Show 3/3 before filter.
      expect(container.textContent).toContain("Toggle ON to protect (3/3)");
      fireEvent.change(getByTestId("text-field"), {
        target: { value: "alp" },
      });
      expect(container.textContent).toContain("Toggle ON to protect (1/3)");
    });

    it("appends ' (auto)' suffix to default-pattern apps", async () => {
      setupApps();
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (1 protected)"));
      // Firefox matches default pattern → "(auto)" suffix in label.
      expect(container.textContent).toContain("Firefox (auto)");
    });

    it("toggle OFF on default-pattern app adds it to disabledDefaults", async () => {
      setupApps();
      const { getByText, getAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (1 protected)"));
      // Find the Firefox toggle (the only one already checked) and click it.
      const inputs = getAllByTestId("toggle-input") as HTMLInputElement[];
      const firefoxInput = inputs.find((i) => i.checked);
      if (!firefoxInput) throw new Error("Firefox toggle not found");
      fireEvent.click(firefoxInput);
      expect(vi.mocked(backend.updateWhitelistSettings)).toHaveBeenCalledWith(expect.arrayContaining(["firefox"]), []);
    });

    it("toggle ON on non-default app adds it to customNames", async () => {
      setupApps();
      const { getByText, getAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (1 protected)"));
      // Click an unchecked toggle (Alpha or Beta).
      const inputs = getAllByTestId("toggle-input") as HTMLInputElement[];
      const alphaInput = inputs.find((i) => !i.checked);
      if (!alphaInput) throw new Error("unchecked toggle not found");
      fireEvent.click(alphaInput);
      // The first unchecked alphabetically would be Alpha.
      expect(vi.mocked(backend.updateWhitelistSettings)).toHaveBeenCalledWith([], expect.arrayContaining(["Alpha"]));
    });

    it("logs the failure when updateWhitelistSettings rejects on toggle", async () => {
      setupApps();
      vi.mocked(backend.updateWhitelistSettings).mockRejectedValue(new Error("disk full"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      const { getByText, getAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (1 protected)"));
      const alphaInput = (getAllByTestId("toggle-input") as HTMLInputElement[]).find((i) => !i.checked);
      if (!alphaInput) throw new Error("unchecked toggle not found");
      fireEvent.click(alphaInput);
      // The .catch((e) => logError(...)) must surface the rejection.
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to update whitelist settings"));
      logSpy.mockRestore();
    });

    it("toggle OFF on custom-listed app removes it from customNames", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "MyCustom" } });
      vi.mocked(backend.getWhitelistSettings).mockResolvedValue({
        disabled_defaults: [],
        custom_names: ["MyCustom"],
      });
      const { getByText, getAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Configure Whitelist (1 protected)"));
      const inputs = getAllByTestId("toggle-input") as HTMLInputElement[];
      const customInput = inputs.find((i) => i.checked);
      if (!customInput) throw new Error("MyCustom toggle not found");
      fireEvent.click(customInput);
      // Last call should be the toggle (after the click).
      expect(vi.mocked(backend.updateWhitelistSettings)).toHaveBeenLastCalledWith([], []);
    });

    it("toggle ON on default-pattern app already-disabled removes from disabledDefaults", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "Firefox" } });
      vi.mocked(backend.getWhitelistSettings).mockResolvedValue({
        disabled_defaults: ["firefox"],
        custom_names: [],
      });
      const { getByText, getAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // 0 protected since firefox is disabled.
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      const inputs = getAllByTestId("toggle-input") as HTMLInputElement[];
      const firefoxInput = inputs.find((i) => !i.checked);
      if (!firefoxInput) throw new Error("Firefox toggle not found");
      fireEvent.click(firefoxInput);
      // Re-enabling firefox: disabledDefaults filter removes it.
      expect(vi.mocked(backend.updateWhitelistSettings)).toHaveBeenLastCalledWith([], []);
    });

    it("resets RetroDeckSection's confirm state when a toggle changes mid-flow", async () => {
      stubCollectionStore([1, 2]);
      stubAppStore({
        1: { strDisplayName: "Alpha" },
        2: { strDisplayName: "Beta" },
      });
      const { getByText, getAllByTestId, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      // Arm confirm.
      fireEvent.click(getByText("Remove 2 Non-Steam Games"));
      expect(container.textContent).toContain("Are you sure?");

      // Open whitelist and toggle Alpha — resets confirm.
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      const inputs = getAllByTestId("toggle-input") as HTMLInputElement[];
      fireEvent.click(inputs[0]!);
      // Button label should be back to the unconfirmed form.
      expect(container.textContent).not.toContain("Are you sure?");
    });

    it("clicking 'Hide Whitelist' resets RetroDeck confirm state", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "Alpha" } });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Open whitelist.
      fireEvent.click(getByText("Configure Whitelist (0 protected)"));
      // Arm confirm.
      fireEvent.click(getByText("Remove 1 Non-Steam Games"));
      expect(container.textContent).toContain("Are you sure?");
      // Hide whitelist — also clears confirms via resetRemoveConfirms.
      fireEvent.click(getByText("Hide Whitelist"));
      expect(container.textContent).not.toContain("Are you sure?");
    });
  });

  describe("PlatformActionModal action wiring", () => {
    it("Delete Save Files DialogButton fires closeModal + opens the saves ConfirmModal", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "a", name: "PlatA", count: 1 }],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("PlatA (1)"));
      const modalEl = vi.mocked(showModal).mock.calls[0]?.[0];
      const closeModal = vi.fn();
      const cloned = createElement((modalEl as ReactElement).type, {
        ...(modalEl as ReactElement<Record<string, unknown>>).props,
        closeModal,
      });
      const { getByText: getByTextModal } = render(cloned);
      // Click delete saves — fires closeModal + opens the ConfirmModal.
      fireEvent.click(getByTextModal("Delete Save Files"));
      expect(closeModal).toHaveBeenCalled();
      // showModal has been called once for the platform-action modal and now
      // once more for the ConfirmModal.
      expect(vi.mocked(showModal).mock.calls.length).toBeGreaterThan(1);
      const props = shownModalPropsAt<{ strTitle?: string }>(1);
      expect(props?.strTitle).toContain("Delete all save files");
    });

    it("Remove Shortcuts DialogButton fires closeModal + triggers handleRemoveShortcuts", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "a", name: "PlatA", count: 1 }],
      });
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: [],
        rom_ids: [],
        platform_name: "PlatA",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("PlatA (1)"));
      const modalEl = vi.mocked(showModal).mock.calls[0]?.[0];
      const closeModal = vi.fn();
      const cloned = createElement((modalEl as ReactElement).type, {
        ...(modalEl as ReactElement<Record<string, unknown>>).props,
        closeModal,
      });
      const { getByText: getByTextModal } = render(cloned);
      await act(async () => {
        fireEvent.click(getByTextModal("Remove Shortcuts (1 game)"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(closeModal).toHaveBeenCalled();
      expect(vi.mocked(backend.removePlatformShortcuts)).toHaveBeenCalledWith("a");
    });

    it("Delete BIOS Files DialogButton fires closeModal + triggers handleDeleteBios", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "a", name: "PlatA", count: 1 }],
      });
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: true,
        deleted_count: 0,
        message: "ok",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("PlatA (1)"));
      const modalEl = vi.mocked(showModal).mock.calls[0]?.[0];
      const closeModal = vi.fn();
      const cloned = createElement((modalEl as ReactElement).type, {
        ...(modalEl as ReactElement<Record<string, unknown>>).props,
        closeModal,
      });
      const { getByText: getByTextModal } = render(cloned);
      await act(async () => {
        fireEvent.click(getByTextModal("Delete BIOS Files"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(closeModal).toHaveBeenCalled();
      expect(vi.mocked(backend.deletePlatformBios)).toHaveBeenCalledWith("a");
    });
  });
});
