// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (status string surfaced via Field label, captured prop
// on a child, logError spy, etc.). Only truly-`/* ignore */` catches (no state
// change, no log call) are exempt — and even then, prefer dropping the test
// over keeping one with zero expects.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, act, waitFor, within } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { DangerZone } from "./DangerZone";
import * as backend from "../api/backend";
import { showModal } from "@decky/ui";
import {
  removeShortcut,
  setLaunchOptionsConfirmed,
  getAllNonSteamShortcutAppIds,
  getLiveRomMShortcutAppIds,
} from "../utils/steamShortcuts";
import { clearPlatformCollection, clearAllRomMCollections } from "../utils/collections";
import { formatUninstallStatus } from "../utils/formatters";
import { setSyncProgress } from "../utils/syncProgress";
import { stubCollectionStore, stubAppStore } from "../test-utils/steamStubs";

vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));
// setLaunchOptionsConfirmed is exercised through the real batchConfirmLaunchOptions
// (launchOptionsReconcile stays unmocked) — mock the leaf so the bulk-uninstall
// launch-options reset (#1146) is asserted without touching SteamClient.
vi.mock("../utils/steamShortcuts", () => ({
  removeShortcut: vi.fn(),
  setLaunchOptionsConfirmed: vi.fn(),
  getAllNonSteamShortcutAppIds: vi.fn(),
  getLiveRomMShortcutAppIds: vi.fn(),
}));
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
      app_ids: [],
    });
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
    vi.mocked(getAllNonSteamShortcutAppIds).mockReturnValue([]);
    // Default: the live exe-ownership scan finds nothing beyond the backend
    // list, so the union removal is a no-op unless a test overrides it.
    vi.mocked(getLiveRomMShortcutAppIds).mockResolvedValue([]);
    vi.mocked(backend.cleanupOrphanedGridImages).mockResolvedValue({
      success: true,
      candidate_count: 0,
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
    // The mock drops the app from the collection store, mirroring Steam's real
    // behavior, so the post-removal settle-poll (recountAfterStoreSettles) sees
    // the store shrink and re-counts immediately instead of waiting out its
    // timeout on a real timer (#1381).
    vi.stubGlobal("SteamClient", {
      Apps: {
        RemoveShortcut: vi.fn((appId: number) => {
          if (typeof collectionStore !== "undefined") {
            collectionStore.deckDesktopApps?.apps.delete(appId);
          }
        }),
      },
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

    it("dispatches a romm_data_changed {type:'bios', platform_slug} event on success", async () => {
      setupOnePlatform();
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: true,
        deleted_count: 2,
        message: "Deleted 2 BIOS files",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteBios?: () => void }>();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          platformModal?.onDeleteBios?.();
          await Promise.resolve();
          await Promise.resolve();
        });
        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({ type: "bios", platform_slug: "snes" });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("does NOT dispatch romm_data_changed when deletePlatformBios reports success=false", async () => {
      setupOnePlatform();
      vi.mocked(backend.deletePlatformBios).mockResolvedValue({
        success: false,
        deleted_count: 0,
        message: "Nothing to delete",
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const platformModal = lastShownModalProps<{ onDeleteBios?: () => void }>();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          platformModal?.onDeleteBios?.();
          await Promise.resolve();
          await Promise.resolve();
        });
        // Failure branch surfaces the message but emits no refresh event.
        expect(container.textContent).toContain("Nothing to delete");
        expect(listener).not.toHaveBeenCalled();
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
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

    it("removes the UNION of backend app_ids and live-scanned orphans, deduped (#1381)", async () => {
      // Backend binding map returns [1, 2]; the live exe-ownership scan also
      // finds 3 — an orphan a crashed sync left in Steam with no DB binding.
      // All three are removed; 2 (present in both lists) is removed once.
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: [1, 2],
        rom_ids: [50, 51],
      });
      vi.mocked(getLiveRomMShortcutAppIds).mockResolvedValue([2, 3]);
      const logSpy = vi.spyOn(backend, "logInfo").mockImplementation(() => {});
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // The orphan (3) is removed only after the scan resolves — wait for it.
      await waitFor(() => {
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(3);
      });
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(1);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(2);
      // 2 is in both lists → removed once, not twice: three calls total.
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(3);
      // rom_ids stay the backend set exactly — orphans have no DB row.
      expect(vi.mocked(backend.reportRemovalResults)).toHaveBeenCalledWith([50, 51]);
      // Non-vacuous: the one orphan not in the backend list is logged.
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("1 live-scanned RomM shortcut"));
      logSpy.mockRestore();
    });

    it("falls back to backend app_ids and warns when the live scan is unavailable (#1381)", async () => {
      // A null scan means Steam's shortcut store was unreadable — remove the
      // backend-bound set alone rather than skip removal entirely.
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: [1, 2],
        rom_ids: [50],
      });
      vi.mocked(getLiveRomMShortcutAppIds).mockResolvedValue(null);
      const warnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // The flow completes: the success message is surfaced.
      await waitFor(() => {
        expect(container.textContent).toContain("Removed all");
      });
      // Backend-bound shortcuts still removed; no orphan sweep ran.
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(1);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(2);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(2);
      // Non-vacuous null-branch assertion: the warning text is surfaced.
      expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("Live RomM shortcut scan unavailable"));
      warnSpy.mockRestore();
    });
  });

  describe("post-removal re-count settles the store (#1381)", () => {
    it("waits for the shortcut store to shrink before re-counting the non-steam games", async () => {
      // Two RomM-owned shortcuts live in Steam. Removing them via the backend
      // path leaves the store momentarily unchanged (Steam settles async), so
      // the "Remove N" label must not re-count until the store actually shrinks.
      stubCollectionStore([10, 20]);
      stubAppStore({ 10: { strDisplayName: "Game Ten" }, 20: { strDisplayName: "Game Twenty" } });
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: [10, 20],
        rom_ids: [1, 2],
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Mount re-counted the store: two non-steam games.
      expect(container.textContent).toContain("Remove 2 Non-Steam Games");
      // Arm the confirm under real timers, then drive the settle poll under fake
      // timers so the 250ms cadence fires without a real wait.
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
          // Drain the removal chain (mock promise awaits), then run one poll
          // cadence. removeShortcut is the mocked util (no-op) so the store is
          // still full — the label must stay at 2.
          for (let i = 0; i < 8; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(300);
        });
        expect(container.textContent).toContain("Remove 2 Non-Steam Games");
        // Steam settles: the two shortcuts leave the collection store.
        collectionStore.deckDesktopApps!.apps.delete(10);
        collectionStore.deckDesktopApps!.apps.delete(20);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(300);
        });
        // The settle poll saw the drop → re-count ran → the list is now empty.
        expect(container.textContent).toContain("No non-steam games found");
      } finally {
        vi.useRealTimers();
      }
    });

    it("re-counts after the timeout even if the store never shrinks", async () => {
      stubCollectionStore([10, 20]);
      stubAppStore({ 10: { strDisplayName: "Game Ten" }, 20: { strDisplayName: "Game Twenty" } });
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: [10, 20],
        rom_ids: [1, 2],
      });
      const logSpy = vi.spyOn(backend, "logInfo").mockImplementation(() => {});
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // loadNonSteamApps logs the store size on every re-count; count the
      // size-2 logs emitted by the mount re-count.
      const isSizeTwoLog = (c: unknown[]) => String(c[0]).includes("deckDesktopApps.apps size: 2");
      const countsAtMount = logSpy.mock.calls.filter(isSizeTwoLog).length;
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
          // The store never shrinks (removeShortcut util is a no-op). Advance
          // past the 3s deadline — the poll gives up and re-counts anyway.
          for (let i = 0; i < 8; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(3500);
        });
        // loadNonSteamApps re-ran after the timeout, re-reading the (unchanged)
        // store — its size-2 log fired at least once more than at mount.
        const countsAfter = logSpy.mock.calls.filter(isSizeTwoLog).length;
        expect(countsAfter).toBeGreaterThan(countsAtMount);
      } finally {
        vi.useRealTimers();
        logSpy.mockRestore();
      }
    });

    it("re-counts immediately when the collection store is unreadable during removal (#1381)", async () => {
      // Readable at mount, then unreadable before the removal completes:
      // recountAfterStoreSettles must read null and re-count right away rather
      // than poll a store it can't read (exercises the null store-size branch).
      stubCollectionStore([10, 20]);
      stubAppStore({ 10: { strDisplayName: "Game Ten" }, 20: { strDisplayName: "Game Twenty" } });
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: [10, 20],
        rom_ids: [1, 2],
      });
      const warnSpy = vi.spyOn(backend, "logWarn").mockImplementation(() => {});
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      // Mount re-counted against the readable store — no warning yet.
      expect(container.textContent).toContain("Remove 2 Non-Steam Games");
      expect(warnSpy).not.toHaveBeenCalled();
      // The store goes unreadable between arming and confirming.
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      vi.stubGlobal("collectionStore", undefined);
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // The settle poll was skipped (null baseline) and loadNonSteamApps still
      // ran immediately: it warned about the unreadable store and cleared the
      // list — a non-vacuous observable of the immediate re-count.
      await waitFor(() => {
        expect(container.textContent).toContain("No non-steam games found");
      });
      expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("collectionStore not available"));
      warnSpy.mockRestore();
    });
  });

  describe("removal paths are chunk-paced (#977)", () => {
    // 26 shortcuts = one full 25-item chunk + a remainder, so exactly one 50ms
    // breather must fall between the two chunks.
    const manyAppIds = Array.from({ length: 26 }, (_, i) => i + 1);

    it("per-platform removal: 25 removals back-to-back, one 50ms breather, then the post-removal steps", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 26 }],
      });
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: true,
        app_ids: manyAppIds,
        rom_ids: [1, 2],
        platform_name: "Super Nintendo",
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (26)"));
      const modalProps = lastShownModalProps<{ onRemoveShortcuts?: () => void }>();

      vi.useFakeTimers();
      try {
        await act(async () => {
          modalProps?.onRemoveShortcuts?.();
          // Drain: removePlatformShortcuts resolves, then the paced loop runs the
          // first 25-item chunk back-to-back and blocks on the breather.
          for (let i = 0; i < 40; i++) await Promise.resolve();
        });
        // First chunk done; the 26th removal + the post-removal steps are gated
        // behind the not-yet-elapsed breather.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(25);
        expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
        expect(vi.mocked(clearPlatformCollection)).not.toHaveBeenCalled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
          for (let i = 0; i < 20; i++) await Promise.resolve();
        });
        // The breather elapsed → the last removal ran, THEN the post-removal steps.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(26);
        expect(vi.mocked(backend.reportRemovalResults)).toHaveBeenCalledWith([1, 2]);
        expect(vi.mocked(clearPlatformCollection)).toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it("remove-all: the backend list is chunk-paced before the orphan sweep and reporting run", async () => {
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: manyAppIds,
        rom_ids: [7],
      });
      // No orphans beyond the backend list — isolate the pacing to the first loop.
      vi.mocked(getLiveRomMShortcutAppIds).mockResolvedValue([]);
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove All RomM Shortcuts"));

      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
          for (let i = 0; i < 40; i++) await Promise.resolve();
        });
        // First chunk done; the orphan scan + reporting + collection clear are all
        // gated behind the breather.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(25);
        expect(vi.mocked(getLiveRomMShortcutAppIds)).not.toHaveBeenCalled();
        expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
        expect(vi.mocked(clearAllRomMCollections)).not.toHaveBeenCalled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
          for (let i = 0; i < 20; i++) await Promise.resolve();
        });
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(26);
        expect(vi.mocked(getLiveRomMShortcutAppIds)).toHaveBeenCalled();
        expect(vi.mocked(backend.reportRemovalResults)).toHaveBeenCalledWith([7]);
        expect(vi.mocked(clearAllRomMCollections)).toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it("bulk non-steam removal: 25 back-to-back, one breather, then the 'Removed' status", async () => {
      stubCollectionStore(manyAppIds);
      stubAppStore(Object.fromEntries(manyAppIds.map((id) => [id, { strDisplayName: `Game ${id}` }])));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove 26 Non-Steam Games"));

      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText(/Are you sure\? Remove 26 games/));
          for (let i = 0; i < 40; i++) await Promise.resolve();
        });
        // First 25-item chunk done; the 26th removal + the "Removed" status (a
        // post-removal step) are gated behind the breather.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(25);
        expect(container.textContent).toContain("Removing 26 non-steam games...");

        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
          for (let i = 0; i < 20; i++) await Promise.resolve();
          // Drain the post-removal settle poll (store never shrinks — removeShortcut is mocked).
          await vi.advanceTimersByTimeAsync(3500);
        });
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(26);
        // Routed through the wrapper, never the direct SteamClient call.
        expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).not.toHaveBeenCalled();
        expect(container.textContent).toContain("Removed 26 non-steam games");
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe("ShortcutRemovalSection — handleUninstallAll", () => {
    it("first click arms confirm + shows the warning Field; second click triggers uninstallAllRoms", async () => {
      vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
        success: true,
        removed_count: 7,
        errors: [],
        app_ids: [],
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
        app_ids: [],
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

  describe("ShortcutRemovalSection — bulk uninstall resets kept shortcut launch_options (#1146)", () => {
    // Two clicks: first arms the confirm, second fires uninstallAllRoms + the reset.
    async function confirmUninstall(getByText: (t: string) => HTMLElement): Promise<void> {
      fireEvent.click(getByText("Uninstall All Installed ROMs"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: delete all ROM files?"));
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    it("resets each kept shortcut's launch command to the '' placeholder for every returned app_id", async () => {
      vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
        success: true,
        removed_count: 2,
        errors: [],
        app_ids: [100, 200],
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      await confirmUninstall(getByText);

      // Every kept shortcut is reset to "" so a raced-past not_installed can't
      // exec a stale command into the now-deleted ROM path.
      await waitFor(() => {
        expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(100, "");
        expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(200, "");
      });
      expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledTimes(2);
      // The final status is surfaced only after the reset completes (it is awaited).
      expect(container.textContent).toContain("Removed 2, 0 errors");
    });

    it("resets no launch_options when no kept shortcut is bound (empty app_ids)", async () => {
      vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
        success: true,
        removed_count: 1,
        errors: [],
        app_ids: [],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      await confirmUninstall(getByText);

      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
    });

    it("does not reset launch_options when the bulk uninstall call rejects", async () => {
      vi.mocked(backend.uninstallAllRoms).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      await confirmUninstall(getByText);

      // The reset lives after the awaited uninstall inside the try — a rejection
      // short-circuits to the catch, leaving every shortcut untouched.
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(container.textContent).toContain("Failed to uninstall ROMs");
    });
  });

  describe("OrphanedGridCleanupSection", () => {
    it("first tap dry-runs and arms the confirm with the counted label; second tap executes", async () => {
      vi.mocked(getAllNonSteamShortcutAppIds).mockReturnValue([111, 222]);
      vi.mocked(backend.cleanupOrphanedGridImages)
        .mockResolvedValueOnce({ success: true, candidate_count: 3 })
        .mockResolvedValueOnce({ success: true, removed_count: 3 });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      // First tap — DRY-RUN only: the callable runs with dry_run=true and
      // nothing is deleted; the confirm label carries the real count.
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.cleanupOrphanedGridImages)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.cleanupOrphanedGridImages)).toHaveBeenCalledWith([111, 222], true);
      expect(container.textContent).toContain("Confirm: remove 3 orphaned images?");

      // Second tap — the real run.
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove 3 orphaned images?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.cleanupOrphanedGridImages)).toHaveBeenCalledTimes(2);
      expect(vi.mocked(backend.cleanupOrphanedGridImages)).toHaveBeenLastCalledWith([111, 222], false);
      expect(container.textContent).toContain("Removed 3 orphaned images");
    });

    it("uses the singular label form for a single candidate", async () => {
      vi.mocked(getAllNonSteamShortcutAppIds).mockReturnValue([111]);
      vi.mocked(backend.cleanupOrphanedGridImages).mockResolvedValue({ success: true, candidate_count: 1 });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Confirm: remove 1 orphaned image?");
      expect(container.textContent).not.toContain("Confirm: remove 1 orphaned images?");
    });

    it("aborts without calling the backend when the live-shortcut scan returns null", async () => {
      // "Scan couldn't run → delete nothing": without the live keep-set the
      // backend must never be asked to delete anything.
      vi.mocked(getAllNonSteamShortcutAppIds).mockReturnValue(null);
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
      });
      expect(vi.mocked(backend.cleanupOrphanedGridImages)).not.toHaveBeenCalled();
      expect(container.textContent).toContain("Could not read Steam's shortcut list — nothing was removed.");
    });

    it("reports zero candidates without arming the confirm", async () => {
      vi.mocked(backend.cleanupOrphanedGridImages).mockResolvedValue({ success: true, candidate_count: 0 });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("No orphaned grid images found");
      expect(container.textContent).not.toContain("Confirm: remove");
    });

    it("surfaces a gate refusal message on the first tap and does not arm", async () => {
      // Mirrors the @migration_blocked / @sync_active_blocked short-circuit
      // shape: success false + message, no counts.
      vi.mocked(backend.cleanupOrphanedGridImages).mockResolvedValue({
        success: false,
        message: "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
        blocked_by_migration: true,
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Pending RetroDECK migration");
      expect(container.textContent).not.toContain("Confirm: remove");
    });

    it("surfaces the incomplete_scan refusal on the second tap", async () => {
      vi.mocked(getAllNonSteamShortcutAppIds).mockReturnValue([111]);
      vi.mocked(backend.cleanupOrphanedGridImages)
        .mockResolvedValueOnce({ success: true, candidate_count: 2 })
        .mockResolvedValueOnce({
          success: false,
          reason: "incomplete_scan",
          message:
            "Steam's shortcut scan is missing 1 synced shortcut(s) — the scan is incomplete, nothing was removed.",
        });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove 2 orphaned images?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("nothing was removed");
    });

    it("surfaces 'Failed to scan for orphaned images' when the dry run rejects", async () => {
      vi.mocked(backend.cleanupOrphanedGridImages).mockRejectedValue(new Error("io"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to scan for orphaned images");
    });

    it("surfaces 'Failed to remove orphaned images' when the real run rejects", async () => {
      vi.mocked(getAllNonSteamShortcutAppIds).mockReturnValue([111]);
      vi.mocked(backend.cleanupOrphanedGridImages)
        .mockResolvedValueOnce({ success: true, candidate_count: 2 })
        .mockRejectedValueOnce(new Error("io"));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(getByText("Remove Orphaned Grid Images"));
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove 2 orphaned images?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Failed to remove orphaned images");
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
    it("first click arms confirm; second click removes via the removeShortcut wrapper (#977)", async () => {
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
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();

      // Loop 3 now routes through the paced removeShortcut wrapper, never the
      // direct SteamClient.Apps.RemoveShortcut. Drive the paced removal + the
      // post-removal settle poll under fake timers so nothing waits on a real timer.
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Are you sure? Remove 2 games (0 whitelisted)?"));
          for (let i = 0; i < 8; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(3500);
        });
      } finally {
        vi.useRealTimers();
      }
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(1);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(2);
      // The direct SteamClient call is gone — removal goes through the wrapper only.
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).not.toHaveBeenCalled();
      // status surfaced.
      expect(container.textContent).toContain("Removed 2 non-steam games");
    });

    it("singular 'game' for 1 removed", async () => {
      stubCollectionStore([1]);
      stubAppStore({ 1: { strDisplayName: "OnlyGame" } });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove 1 Non-Steam Games"));
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Are you sure? Remove 1 games (0 whitelisted)?"));
          for (let i = 0; i < 8; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(3500);
        });
      } finally {
        vi.useRealTimers();
      }
      expect(container.textContent).toContain("Removed 1 non-steam game");
      expect(container.textContent).not.toContain("Removed 1 non-steam games");
    });

    it("disarms the confirm as the paced removal STARTS, not after it finishes (#977 re-entry guard)", async () => {
      // 26 games → the paced removal parks on a 50ms breather after the first
      // 25-item chunk. The confirm must already be disarmed at that mid-removal
      // point (reset moved before the await), so a stray tap can't re-enter and
      // start a second concurrent run. With the old post-await reset, the button
      // would still read "Are you sure?" here.
      const ids = Array.from({ length: 26 }, (_, i) => i + 1);
      stubCollectionStore(ids);
      stubAppStore(Object.fromEntries(ids.map((id) => [id, { strDisplayName: `Game ${id}` }])));
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove 26 Non-Steam Games"));
      expect(container.textContent).toContain("Are you sure? Remove 26 games (0 whitelisted)?");

      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Are you sure? Remove 26 games (0 whitelisted)?"));
          for (let i = 0; i < 40; i++) await Promise.resolve();
        });
        // Mid-removal: first chunk removed, loop parked on the breather.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(25);
        // The confirm is ALREADY disarmed — the armed label is gone, the button
        // reads its default form again.
        expect(container.textContent).not.toContain("Are you sure?");
        expect(container.textContent).toContain("Remove 26 Non-Steam Games");

        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
          for (let i = 0; i < 20; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(3500);
        });
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(26);
      } finally {
        vi.useRealTimers();
      }
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
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();

      // Second click — RETRODECK warning escalation.
      fireEvent.click(getByText("WARNING: RetroDECK not protected! Remove 2 games?"));
      expect(container.textContent).toContain("!! RETRODECK WILL BE REMOVED !!");
      expect(container.textContent).toContain("RetroDECK is NOT in the whitelist and will be permanently removed!");
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();

      // Third click — actually remove, through the paced removeShortcut wrapper.
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText(/!! RETRODECK WILL BE REMOVED !!/));
          for (let i = 0; i < 8; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(3500);
        });
      } finally {
        vi.useRealTimers();
      }
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(1);
      expect(vi.mocked(removeShortcut)).toHaveBeenCalledWith(2);
      expect(vi.mocked(SteamClient.Apps.RemoveShortcut)).not.toHaveBeenCalled();
    });
  });

  describe("busy state during removals (#1449)", () => {
    it("disables every removal button, shows the spinner + counter mid-removal, then re-enables", async () => {
      const ids = Array.from({ length: 26 }, (_, i) => i + 1);
      stubCollectionStore(ids);
      stubAppStore(Object.fromEntries(ids.map((id) => [id, { strDisplayName: `Game ${id}` }])));
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 2 }],
      });
      const { getByText, queryAllByTestId, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      // Before the removal: no busy spinner, every removal button enabled.
      expect(queryAllByTestId("spinner").length).toBe(0);
      expect(getByText("Remove All RomM Shortcuts")).not.toBeDisabled();
      expect(getByText("Super Nintendo (2)")).not.toBeDisabled();

      fireEvent.click(getByText("Remove 26 Non-Steam Games"));
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText(/Are you sure\? Remove 26 games/));
          for (let i = 0; i < 40; i++) await Promise.resolve();
        });
        // Mid-removal, parked on the breather after the first 25-item chunk.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(25);
        // Spinner visible + live counter reflects progress.
        expect(queryAllByTestId("spinner").length).toBeGreaterThan(0);
        expect(container.textContent).toContain("Removing 25 of 26...");
        // Every removal button — across BOTH sections — is disabled: a second
        // concurrent run is impossible via the UI, not merely harmless.
        expect(getByText("Remove 26 Non-Steam Games")).toBeDisabled();
        expect(getByText("Remove All RomM Shortcuts")).toBeDisabled();
        expect(getByText("Uninstall All Installed ROMs")).toBeDisabled();
        expect(getByText("Super Nintendo (2)")).toBeDisabled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
          for (let i = 0; i < 20; i++) await Promise.resolve();
          await vi.advanceTimersByTimeAsync(3500); // drain the settle poll
        });
        // After completion: last removal ran, spinner gone, buttons re-enabled,
        // final status text shown.
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(26);
        expect(queryAllByTestId("spinner").length).toBe(0);
        expect(getByText("Remove 26 Non-Steam Games")).not.toBeDisabled();
        expect(getByText("Remove All RomM Shortcuts")).not.toBeDisabled();
        expect(getByText("Super Nintendo (2)")).not.toBeDisabled();
        expect(container.textContent).toContain("Removed 26 non-steam games");
      } finally {
        vi.useRealTimers();
      }
    });

    it("remove-all-RomM drives the counter and clears busy on completion", async () => {
      const appIds = Array.from({ length: 26 }, (_, i) => i + 1);
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: true,
        message: "Removed all",
        app_ids: appIds,
        rom_ids: [1],
      });
      vi.mocked(getLiveRomMShortcutAppIds).mockResolvedValue([]);
      const { getByText, container, queryAllByTestId } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      vi.useFakeTimers();
      try {
        await act(async () => {
          fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
          for (let i = 0; i < 40; i++) await Promise.resolve();
        });
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(25);
        expect(container.textContent).toContain("Removing 25 of 26...");
        expect(queryAllByTestId("spinner").length).toBeGreaterThan(0);
        expect(getByText("Remove All RomM Shortcuts")).toBeDisabled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(50);
          for (let i = 0; i < 20; i++) await Promise.resolve();
        });
        expect(vi.mocked(removeShortcut)).toHaveBeenCalledTimes(26);
        expect(container.textContent).toContain("Removed all");
        expect(queryAllByTestId("spinner").length).toBe(0);
        expect(getByText("Remove All RomM Shortcuts")).not.toBeDisabled();
      } finally {
        vi.useRealTimers();
      }
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

  describe("sync-running guard (#1390)", () => {
    const HINT = "Unavailable while a library sync is running.";
    const REFUSAL_MESSAGE =
      "A library sync is in progress — wait for it to finish or cancel it before removing shortcuts or ROMs.";

    // The syncProgress store is real module state (not a vi mock) — it
    // survives vi.resetAllMocks(), so restore the idle default after each test.
    afterEach(() => {
      setSyncProgress({ running: false, stage: "", current: 0, total: 0, message: "", runId: "" });
    });

    it("disables the bulk removal buttons and shows the hint while a sync runs", async () => {
      setSyncProgress({ running: true, stage: "applying", current: 1, total: 5, message: "", runId: "run-1" });
      const { getByText, getAllByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      expect(getByText("Remove All RomM Shortcuts")).toBeDisabled();
      expect(getByText("Uninstall All Installed ROMs")).toBeDisabled();
      expect(getByText("Remove Orphaned Grid Images")).toBeDisabled();
      // All three disabled buttons carry the hint description.
      expect(getAllByText(HINT)).toHaveLength(3);
      // Clicking the disabled buttons must not arm the confirm flow.
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      fireEvent.click(getByText("Uninstall All Installed ROMs"));
      fireEvent.click(getByText("Remove Orphaned Grid Images"));
      expect(vi.mocked(backend.removeAllShortcuts)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.uninstallAllRoms)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.cleanupOrphanedGridImages)).not.toHaveBeenCalled();
    });

    it("keeps the buttons enabled with no hint when no sync runs", async () => {
      const { getByText, queryByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();

      expect(getByText("Remove All RomM Shortcuts")).not.toBeDisabled();
      expect(getByText("Uninstall All Installed ROMs")).not.toBeDisabled();
      expect(getByText("Remove Orphaned Grid Images")).not.toBeDisabled();
      expect(queryByText(HINT)).toBeNull();
    });

    it("disables only the modal's Remove Shortcuts button while a sync runs", async () => {
      setSyncProgress({ running: true, stage: "applying", current: 1, total: 5, message: "", runId: "run-1" });
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 1 }],
      });
      const { getByText } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Super Nintendo (1)"));
      const modalEl = vi.mocked(showModal).mock.calls[0]?.[0];
      // Scope queries to the modal's own container — the panel render in the
      // same document also shows the hint on its two disabled bulk buttons.
      const { container: modalContainer } = render(modalEl as ReactElement);
      const modal = within(modalContainer as HTMLElement);

      expect(modal.getByText("Remove Shortcuts (1 game)")).toBeDisabled();
      expect(modal.getByText(HINT)).toBeTruthy();
      // The save/BIOS deletions are not sync-gated — they stay pressable.
      expect(modal.getByText("Delete Save Files")).not.toBeDisabled();
      expect(modal.getByText("Delete BIOS Files")).not.toBeDisabled();
    });

    it("surfaces the sync_active refusal and removes nothing on Remove All (raced past the disable)", async () => {
      // The backend gate is the authority: a sync that starts between render
      // and click still refuses. The handler must surface the message and
      // must not touch shortcuts or collections.
      vi.mocked(backend.removeAllShortcuts).mockResolvedValue({
        success: false,
        reason: "sync_active",
        message: REFUSAL_MESSAGE,
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Remove All RomM Shortcuts"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: remove all RomM shortcuts?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain(REFUSAL_MESSAGE);
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.reportRemovalResults)).not.toHaveBeenCalled();
      expect(vi.mocked(clearAllRomMCollections)).not.toHaveBeenCalled();
    });

    it("surfaces the sync_active refusal without crashing on Uninstall All (no app_ids in the refusal)", async () => {
      // Regression guard: the old handler dereferenced result.app_ids.map(...)
      // unconditionally — a payload-less refusal threw instead of rendering.
      vi.mocked(backend.uninstallAllRoms).mockResolvedValue({
        success: false,
        reason: "sync_active",
        message: REFUSAL_MESSAGE,
      });
      const { getByText, container } = render(<DangerZone onBack={vi.fn()} />);
      await flushAsync();
      fireEvent.click(getByText("Uninstall All Installed ROMs"));
      await act(async () => {
        fireEvent.click(getByText("Confirm: delete all ROM files?"));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain(REFUSAL_MESSAGE);
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(vi.mocked(formatUninstallStatus)).not.toHaveBeenCalled();
    });

    it("surfaces the sync_active refusal on a per-platform removal", async () => {
      vi.mocked(backend.getRegistryPlatforms).mockResolvedValue({
        platforms: [{ slug: "snes", name: "Super Nintendo", count: 2 }],
      });
      vi.mocked(backend.removePlatformShortcuts).mockResolvedValue({
        success: false,
        reason: "sync_active",
        message: REFUSAL_MESSAGE,
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
      expect(container.textContent).toContain(REFUSAL_MESSAGE);
      expect(vi.mocked(removeShortcut)).not.toHaveBeenCalled();
      expect(vi.mocked(clearPlatformCollection)).not.toHaveBeenCalled();
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
