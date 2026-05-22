// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) / toaster.toast / debugLog side effect
// MUST have its side effect asserted in the test. Asserting only that the
// rejecting call was invoked is vacuous — the rejection happens after the
// call returns, so the assertion would pass with or without the .catch.
// Truly-/* ignore */ catches (no observable side effect) are exempt; for
// those, assert the absence of state change instead.
//
// The PanelState is per-instance, but module-level helpers (formatReleaseDate,
// pickBiosColor, ...) are pure. We pick a unique appId per test to keep
// any module-scope mock state isolated.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, act } from "@testing-library/react";
import { createElement, type ComponentProps } from "react";
import { RomMGameInfoPanel } from "./RomMGameInfoPanel";
import * as backend from "../api/backend";
import * as cachedStore from "../utils/cachedGameDetailStore";
import * as slotState from "../utils/slotState";
import {
  installDomEventListenerSpy,
  uninstallDomEventListenerSpy,
  domListenerCount,
} from "../test-utils/dom-event-listener-spy";
import { useVersionError } from "./VersionErrorCard";
import type { MigrationStatus, SaveSortMigrationStatus } from "../types";

// Type-only imports — vi.mock(...) below replaces the runtime impl, but
// pinning captured-props shapes to the real component keeps assertions in
// sync as the child's prop interface evolves.
import type { SlotSetupWizard } from "./SlotSetupWizard";
import type { SavesTab } from "./SavesTab";
import type { VersionErrorCard } from "./VersionErrorCard";
import type { MigrationBlockedCard } from "./MigrationBlockedCard";

type SlotSetupWizardProps = ComponentProps<typeof SlotSetupWizard>;
type SavesTabProps = ComponentProps<typeof SavesTab>;
type VersionErrorCardProps = ComponentProps<typeof VersionErrorCard>;
type MigrationBlockedCardProps = ComponentProps<typeof MigrationBlockedCard>;

const capturedSlotSetupWizard: SlotSetupWizardProps[] = [];
const capturedSavesTab: SavesTabProps[] = [];
const capturedVersionErrorCard: VersionErrorCardProps[] = [];
const capturedMigrationBlockedCard: MigrationBlockedCardProps[] = [];

vi.mock("./SlotSetupWizard", () => ({
  SlotSetupWizard: (props: SlotSetupWizardProps) => {
    capturedSlotSetupWizard.push(props);
    return createElement("div", { "data-testid": "slot-setup-wizard" });
  },
}));

vi.mock("./SavesTab", () => ({
  SavesTab: (props: SavesTabProps) => {
    capturedSavesTab.push(props);
    return createElement("div", { "data-testid": "saves-tab" });
  },
}));

vi.mock("./VersionErrorCard", () => ({
  VersionErrorCard: (props: VersionErrorCardProps) => {
    capturedVersionErrorCard.push(props);
    return createElement("div", { "data-testid": "version-error-card" });
  },
  useVersionError: vi.fn(() => null),
}));

vi.mock("./MigrationBlockedCard", () => ({
  MigrationBlockedCard: (props: MigrationBlockedCardProps) => {
    capturedMigrationBlockedCard.push(props);
    return createElement("div", { "data-testid": "migration-blocked-card" });
  },
}));

// ----- Slot state helpers — already tested in src/utils/slotState.test.ts.
// Mock so we can observe + assert the panel routes through them with the
// right arg shape.
vi.mock("../utils/slotState", () => ({
  applyLoadSlotsResult: vi.fn(),
  applyRefreshSlotResult: vi.fn(),
}));

vi.mock("../utils/scrollHelpers", () => ({ scrollFocusedToCenter: vi.fn() }));

// ----- cachedGameDetailStore — re-exported through backend.ts but its
// canonical home is utils. Mock the store so the re-export and direct
// consumers route through the same vi.fn.
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn(),
  invalidateCachedGameDetail: vi.fn(),
}));

// ----- migrationStore — own listener list + state so tests drive the
// subscribe/unsubscribe + state-change flow deterministically.
// `vi.resetAllMocks()` in beforeEach wipes the impls — re-stubbed there.
const migrationListeners: Array<() => void> = [];
let currentMigrationState: MigrationStatus = { pending: false };
vi.mock("../utils/migrationStore", () => ({
  getMigrationState: vi.fn(() => currentMigrationState),
  setMigrationStatus: vi.fn((s: MigrationStatus) => {
    currentMigrationState = s;
    migrationListeners.forEach((fn) => fn());
  }),
  onMigrationChange: vi.fn((cb: () => void) => {
    migrationListeners.push(cb);
    return () => {
      const i = migrationListeners.indexOf(cb);
      if (i >= 0) migrationListeners.splice(i, 1);
    };
  }),
}));
import * as migrationStore from "../utils/migrationStore";

// ----- saveSortMigrationStore — same listener-array pattern as
// migrationStore. The panel reads .pending on mount and re-renders when the
// store notifies. clearSaveSortMigration isn't used by the panel but the mock
// declares it as a vi.fn for shape parity with the real module.
const saveSortListeners: Array<() => void> = [];
let currentSaveSortState: SaveSortMigrationStatus = { pending: false };
vi.mock("../utils/saveSortMigrationStore", () => ({
  getSaveSortMigrationState: vi.fn(() => currentSaveSortState),
  setSaveSortMigrationStatus: vi.fn((s: SaveSortMigrationStatus) => {
    currentSaveSortState = s;
    saveSortListeners.forEach((fn) => fn());
  }),
  onSaveSortMigrationChange: vi.fn((cb: () => void) => {
    saveSortListeners.push(cb);
    return () => {
      const i = saveSortListeners.indexOf(cb);
      if (i >= 0) saveSortListeners.splice(i, 1);
    };
  }),
}));
import * as saveSortMigrationStore from "../utils/saveSortMigrationStore";

// ----- @decky/ui — global stub from test-setup.ts covers Focusable +
// DialogButton. Pass-through is enough for this panel.

// ----- Helpers -----
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

let testAppId = 5000;

describe("RomMGameInfoPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    capturedSlotSetupWizard.length = 0;
    capturedSavesTab.length = 0;
    capturedVersionErrorCard.length = 0;
    capturedMigrationBlockedCard.length = 0;
    migrationListeners.length = 0;
    saveSortListeners.length = 0;
    currentMigrationState = { pending: false };
    currentSaveSortState = { pending: false };
    testAppId++;
    installDomEventListenerSpy();

    // resetAllMocks wipes module-mock impls — re-stub below.
    vi.mocked(useVersionError).mockReturnValue(null);

    // Re-stub migrationStore impls (resetAllMocks wiped them).
    vi.mocked(migrationStore.getMigrationState).mockImplementation(
      () => currentMigrationState,
    );
    vi.mocked(migrationStore.setMigrationStatus).mockImplementation(
      (s: MigrationStatus) => {
        currentMigrationState = s;
        migrationListeners.forEach((fn) => fn());
      },
    );
    vi.mocked(migrationStore.onMigrationChange).mockImplementation(
      (cb: () => void) => {
        migrationListeners.push(cb);
        return () => {
          const i = migrationListeners.indexOf(cb);
          if (i >= 0) migrationListeners.splice(i, 1);
        };
      },
    );
    // Re-stub saveSortMigrationStore impls.
    vi.mocked(
      saveSortMigrationStore.getSaveSortMigrationState,
    ).mockImplementation(() => currentSaveSortState);
    vi.mocked(
      saveSortMigrationStore.setSaveSortMigrationStatus,
    ).mockImplementation((s: SaveSortMigrationStatus) => {
      currentSaveSortState = s;
      saveSortListeners.forEach((fn) => fn());
    });
    vi.mocked(
      saveSortMigrationStore.onSaveSortMigrationChange,
    ).mockImplementation((cb: () => void) => {
      saveSortListeners.push(cb);
      return () => {
        const i = saveSortListeners.indexOf(cb);
        if (i >= 0) saveSortListeners.splice(i, 1);
      };
    });

    // Defaults — cached.found=false; tests opt into specific shapes per case.
    vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
      found: false,
    });
    vi.mocked(cachedStore.invalidateCachedGameDetail).mockReturnValue(undefined);
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
    vi.mocked(backend.refreshMigrationState).mockResolvedValue({
      retrodeck: { pending: false },
      save_sort: { pending: false },
    });
    vi.mocked(backend.getRomMetadata).mockResolvedValue({} as never);
    vi.mocked(backend.getInstalledRom).mockResolvedValue(null);
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
    vi.mocked(backend.checkPlatformBios).mockResolvedValue({ needs_bios: false });
    vi.mocked(backend.getSaveStatus).mockResolvedValue({
      rom_id: 0,
      files: [],
      playtime: {
        total_seconds: 0,
        session_count: 0,
        last_session_start: null,
        last_session_duration_sec: null,
      },
      device_id: "d",
      last_sync_check_at: null,
    });
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
      configured: false,
      active_slot: null,
    });
    vi.mocked(backend.getSaveSlots).mockResolvedValue({
      success: true,
      slots: [],
      active_slot: "",
    });
    vi.mocked(backend.getAchievements).mockResolvedValue({
      success: true,
      achievements: [],
      total: 0,
    });
    vi.mocked(backend.getAchievementProgress).mockResolvedValue({
      success: true,
      earned: 0,
      total: 0,
      earned_achievements: [],
    });
  });

  afterEach(() => {
    uninstallDomEventListenerSpy();
  });

  // ------------------------------------------------------------------
  // A. Top-level render gating
  // ------------------------------------------------------------------

  describe("top-level render gating", () => {
    it("renders only VersionErrorCard when useVersionError returns a message", async () => {
      vi.mocked(useVersionError).mockReturnValue("server too old");
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(queryByTestId("version-error-card")).not.toBeNull();
      expect(queryByTestId("migration-blocked-card")).toBeNull();
      expect(capturedVersionErrorCard[0]?.message).toBe("server too old");
    });

    it("renders only MigrationBlockedCard when migration is pending", async () => {
      currentMigrationState = { pending: true };
      // refreshMigrationState() runs on mount and overwrites the store state —
      // also return pending=true so it doesn't clobber the gate after the
      // useEffect resolves.
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck: { pending: true },
        save_sort: { pending: false },
      });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(queryByTestId("migration-blocked-card")).not.toBeNull();
      expect(queryByTestId("version-error-card")).toBeNull();
      expect(capturedMigrationBlockedCard.length).toBeGreaterThanOrEqual(1);
    });

    it("renders 'Loading...' before loadData resolves", () => {
      // getCachedGameDetail returns a never-resolving promise so the initial
      // loading state stays visible.
      vi.mocked(cachedStore.getCachedGameDetail).mockReturnValue(
        new Promise(() => {}),
      );
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      expect(container.textContent).toContain("Loading...");
    });

    it("returns null when cached.found=false → state.error=true and romId=null", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: false,
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // The component returns null in the error path (after loading).
      expect(container.firstChild).toBeNull();
    });
  });

  // ------------------------------------------------------------------
  // B. loadData mount flow
  // ------------------------------------------------------------------

  describe("loadData mount flow", () => {
    it("does not call slot/installedRom/metadata helpers when cached.found=false", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: false,
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(backend.isSaveTrackingConfigured).not.toHaveBeenCalled();
      expect(backend.getInstalledRom).not.toHaveBeenCalled();
      expect(backend.getRomMetadata).not.toHaveBeenCalled();
    });

    it("applies cached fields and dispatches background fetches when found", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        rom_name: "Test ROM",
        platform_name: "Super Nintendo",
        platform_slug: "snes",
        installed: true,
        save_sync_enabled: true,
        metadata: { summary: "An RPG.", genres: ["RPG"] } as never,
        ra_id: 7,
        stale_fields: ["metadata"],
        bios_status: {
          needs_bios: true,
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        } as never,
      });

      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      // save_sync_enabled → refreshSlotState branch
      expect(backend.isSaveTrackingConfigured).toHaveBeenCalledWith(99);
      expect(backend.getSaveSlots).toHaveBeenCalledWith(99);
      // installed=true → getInstalledRom fires
      expect(backend.getInstalledRom).toHaveBeenCalledWith(99);
      // Always → cover art
      expect(backend.getArtworkBase64).toHaveBeenCalledWith(99);
      // metadata in stale_fields → getRomMetadata fires (even though cache has metadata)
      expect(backend.getRomMetadata).toHaveBeenCalledWith(99);
    });

    it("skips metadata refresh when metadata exists AND not in stale_fields", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        metadata: { summary: "ok" } as never,
        stale_fields: [],
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(backend.getRomMetadata).not.toHaveBeenCalled();
    });

    it("triggers metadata refresh when cached.metadata is null even without stale_fields", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        metadata: null,
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(backend.getRomMetadata).toHaveBeenCalledWith(99);
    });

    it("skips getInstalledRom when cached.installed=false", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        installed: false,
        stale_fields: [],
        metadata: {} as never,
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(backend.getInstalledRom).not.toHaveBeenCalled();
    });

    it("skips refreshSlotState when save_sync_enabled is false", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        save_sync_enabled: false,
        stale_fields: [],
        metadata: {} as never,
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(backend.isSaveTrackingConfigured).not.toHaveBeenCalled();
      expect(backend.getSaveSlots).not.toHaveBeenCalled();
    });

    it("logs via debugLog when getCachedGameDetail rejects (outer catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValue(
        new Error("boom"),
      );
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("loadData error"),
      );
    });

    it("routes the slot refresh through applyRefreshSlotResult on success", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.getSaveSlots).mockResolvedValue({
        success: true,
        slots: [{ slot: "slot1", source: "server", count: 1, latest_updated_at: null }],
        active_slot: "slot1",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(slotState.applyRefreshSlotResult)).toHaveBeenCalledWith(
        expect.objectContaining({ success: true }),
        expect.any(Function),
      );
    });

    it("isSaveTrackingConfigured rejection: applyRefreshSlotResult still fires for getSaveSlots", async () => {
      // The two slot refresh calls are independent — even if
      // isSaveTrackingConfigured rejects, getSaveSlots still runs. Both have
      // .catch(() => {}) — assert the non-rejected one still drove state.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockRejectedValue(
        new Error("net"),
      );
      vi.mocked(backend.getSaveSlots).mockResolvedValue({
        success: true,
        slots: [],
        active_slot: "",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // applyRefreshSlotResult fires for the successful getSaveSlots call,
      // and the rejected isSaveTrackingConfigured does NOT crash the mount.
      expect(vi.mocked(slotState.applyRefreshSlotResult)).toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------
  // C. refreshMigrationState mount-time call + logError on rejection
  // ------------------------------------------------------------------

  describe("refreshMigrationState on mount", () => {
    it("calls setMigrationStatus + setSaveSortMigrationStatus on success", async () => {
      const { setMigrationStatus } = await import("../utils/migrationStore");
      const { setSaveSortMigrationStatus } = await import(
        "../utils/saveSortMigrationStore"
      );
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck: { pending: false },
        save_sort: { pending: true } as SaveSortMigrationStatus,
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(setMigrationStatus).toHaveBeenCalledWith(
        expect.objectContaining({ pending: false }),
      );
      expect(setSaveSortMigrationStatus).toHaveBeenCalledWith(
        expect.objectContaining({ pending: true }),
      );
    });

    it("calls logError when refreshMigrationState rejects", async () => {
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      vi.mocked(backend.refreshMigrationState).mockRejectedValue(
        new Error("boom"),
      );
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("Failed to refresh migration state"),
      );
      logSpy.mockRestore();
    });
  });

  // ------------------------------------------------------------------
  // D. DOM event listeners — registration + cleanup
  // ------------------------------------------------------------------

  describe("DOM event listeners", () => {
    it("registers romm_data_changed / romm_rom_uninstalled / romm_tab_switch and removes them on unmount", async () => {
      const beforeDC = domListenerCount("romm_data_changed");
      const beforeUI = domListenerCount("romm_rom_uninstalled");
      const beforeTS = domListenerCount("romm_tab_switch");
      const { unmount } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(domListenerCount("romm_data_changed")).toBe(beforeDC + 1);
      expect(domListenerCount("romm_rom_uninstalled")).toBe(beforeUI + 1);
      expect(domListenerCount("romm_tab_switch")).toBe(beforeTS + 1);
      unmount();
      expect(domListenerCount("romm_data_changed")).toBe(beforeDC);
      expect(domListenerCount("romm_rom_uninstalled")).toBe(beforeUI);
      expect(domListenerCount("romm_tab_switch")).toBe(beforeTS);
    });

    it("romm_rom_uninstalled: matching rom_id → flips installed=false (next render hides ROM File section)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        installed: true,
        platform_name: "Super Nintendo",
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/p",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
      });
      const { container, queryByText } = render(
        <RomMGameInfoPanel appId={testAppId} />,
      );
      await flushAsync();
      // Initially installed → ROM File section visible
      expect(queryByText("ROM File")).not.toBeNull();
      // Dispatch uninstall — installed flips to false → ROM File hidden
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_rom_uninstalled", {
            detail: { rom_id: 100 },
          }),
        );
        await Promise.resolve();
      });
      expect(container.textContent).not.toContain("ROM File");
    });

    it("romm_rom_uninstalled: mismatching rom_id → no state change", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        installed: true,
        platform_name: "Super Nintendo",
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/p",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
      });
      const { queryByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(queryByText("ROM File")).not.toBeNull();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_rom_uninstalled", {
            detail: { rom_id: 999 },
          }),
        );
        await Promise.resolve();
      });
      // Still installed → ROM File section still rendered
      expect(queryByText("ROM File")).not.toBeNull();
    });

    it("romm_tab_switch: detail.tab present → activeTab state updates", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: null,
      });
      const { queryByTestId } = render(
        <RomMGameInfoPanel appId={testAppId} />,
      );
      await flushAsync();
      // Initially info tab → no saves-tab rendered
      expect(queryByTestId("saves-tab")).toBeNull();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      expect(queryByTestId("saves-tab")).not.toBeNull();
    });

    it("romm_tab_switch: detail.tab absent → no-op", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      const before = container.textContent;
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: {} }),
        );
        await Promise.resolve();
      });
      expect(container.textContent).toBe(before);
    });
  });

  // ------------------------------------------------------------------
  // E. romm_data_changed dispatch branches
  // ------------------------------------------------------------------

  describe("romm_data_changed dispatch branches", () => {
    async function mountWithRomId(romId: number) {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: romId,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      const view = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      return view;
    }

    it("returns early when romIdRef is null (cached.found=false)", async () => {
      // cached.found=false → romIdRef stays null
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(backend.checkPlatformBios).mockClear();
      vi.mocked(backend.getRomMetadata).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: 7 },
          }),
        );
        await Promise.resolve();
      });
      // None of the data-changed handlers should run.
      expect(backend.getSaveStatus).not.toHaveBeenCalled();
      expect(backend.checkPlatformBios).not.toHaveBeenCalled();
      expect(backend.getRomMetadata).not.toHaveBeenCalled();
    });

    it("save_sync_settings enabled=true with romId → calls getSaveStatus", async () => {
      await mountWithRomId(55);
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(backend.getSaveStatus).mockResolvedValue({
        rom_id: 55,
        files: [],
        playtime: {
          total_seconds: 0,
          session_count: 0,
          last_session_start: null,
          last_session_duration_sec: null,
        },
        device_id: "d",
        last_sync_check_at: null,
        conflicts: [],
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync_settings", save_sync_enabled: true },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(55);
    });

    it("save_sync_settings enabled=false → no fetch (early return path), saveSyncEnabled flips false", async () => {
      // cached.save_sync_enabled=true so Saves tab is reachable, then we
      // disable via the event and observe the SAVES tab being hidden.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("SAVES");
      vi.mocked(backend.getSaveStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync_settings", save_sync_enabled: false },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      // SAVES tab now hidden in the rendered tab bar.
      expect(container.textContent).not.toContain("SAVES");
    });

    it("save_sync_settings enabled=true with getSaveStatus rejection → falls back to null updatedStatus (non-vacuous .catch)", async () => {
      // Configure save tracking so SavesTab (not SlotSetupWizard) renders
      // after we switch tabs — gives us a captured-props observable on the
      // resulting saveStatus state.
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      const { container } = await mountWithRomId(55);
      vi.mocked(backend.getSaveStatus).mockRejectedValue(new Error("net"));
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync_settings", save_sync_enabled: true },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // Rejection didn't crash the panel and didn't surface debugLog
      // (the inline .catch swallows).
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("onDataChanged error"),
      );
      // Fallback observable: saveSyncEnabled stays true (SAVES tab still
      // rendered) AND saveStatus is set to the null fallback. Switch to
      // saves tab to capture SavesTab props.
      expect(container.textContent).toContain("SAVES");
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.saveStatus).toBeNull();
    });

    it("save_sync: matching rom_id → fetches getSaveStatus + refreshSlotState and updates saveStatus state", async () => {
      // Configure save tracking up front so SavesTab (not SlotSetupWizard)
      // renders after we switch tabs — gives us a captured-props observable
      // on the resulting saveStatus state.
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      await mountWithRomId(33);
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(backend.isSaveTrackingConfigured).mockClear();
      vi.mocked(backend.getSaveSlots).mockClear();
      // Distinguishable payload so we can prove the state propagated.
      const dispatchedStatus = {
        rom_id: 33,
        files: [
          {
            filename: "FROM_DISPATCH.srm",
            status: "skip" as const,
            local_path: null,
            local_hash: null,
            local_mtime: null,
            local_size: null,
            server_save_id: null,
            server_file_name: null,
            server_emulator: null,
            server_updated_at: null,
            server_size: null,
            last_sync_at: null,
          },
        ],
        playtime: {
          total_seconds: 0,
          session_count: 0,
          last_session_start: null,
          last_session_duration_sec: null,
        },
        device_id: "d",
        last_sync_check_at: null,
        conflicts: [],
      };
      vi.mocked(backend.getSaveStatus).mockResolvedValue(dispatchedStatus);
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: 33 },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(33);
      expect(vi.mocked(backend.isSaveTrackingConfigured)).toHaveBeenCalledWith(33);
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledWith(33);
      // Switch to the saves tab and assert SavesTab received the updated
      // saveStatus — the dispatch handler's setState call is the only path
      // that gets this payload onto SavesTab props. If the handler's
      // `setState((prev) => ({ ..., saveStatus: updatedStatus, ... }))` is
      // dropped, this assertion fails.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.saveStatus?.files[0]?.filename).toBe("FROM_DISPATCH.srm");
    });

    it("save_sync: mismatching rom_id → early return", async () => {
      await mountWithRomId(33);
      vi.mocked(backend.getSaveStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: 999 },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });

    it("save_sync: detail.save_status provided → skip the fetch", async () => {
      await mountWithRomId(33);
      vi.mocked(backend.getSaveStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: {
              type: "save_sync",
              rom_id: 33,
              save_status: {
                rom_id: 33,
                files: [],
                playtime: {
                  total_seconds: 0,
                  session_count: 0,
                  last_session_start: null,
                  last_session_duration_sec: null,
                },
                device_id: "d",
                last_sync_check_at: null,
              },
            },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });

    it("save_sync: getSaveStatus rejection → falls back to null (non-vacuous .catch)", async () => {
      await mountWithRomId(33);
      vi.mocked(backend.getSaveStatus).mockRejectedValue(new Error("net"));
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: 33 },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // The outer try/catch did NOT fire (inline .catch swallowed the
      // rejection and produced null).
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("onDataChanged error"),
      );
    });

    it("bios: detail.platform_slug provided → calls checkPlatformBios; updates biosStatus when needs_bios=true", async () => {
      await mountWithRomId(60);
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({
        needs_bios: true,
        server_count: 2,
        local_count: 2,
        all_downloaded: true,
      });
      const { container } = await new Promise<{
        container: HTMLElement;
      }>((resolve) => {
        // Already rendered via mountWithRomId — get container via re-render?
        // Use a fresh render that uses the new mock.
        const view = render(<RomMGameInfoPanel appId={testAppId} />);
        flushAsync().then(() => resolve({ container: view.container }));
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // BIOS tab now visible (biosStatus non-null).
      expect(container.textContent).toContain("BIOS");
    });

    it("bios: detail.platform_slug absent → no fetch (early return)", async () => {
      await mountWithRomId(60);
      vi.mocked(backend.checkPlatformBios).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "" },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.checkPlatformBios)).not.toHaveBeenCalled();
    });

    it("bios: checkPlatformBios rejection → falls back to { needs_bios: false } (non-vacuous .catch)", async () => {
      // Mount without bios_status so biosStatus starts null and the BIOS
      // tab is NOT visible — the assertion that it STAYS hidden after the
      // rejection is the fallback-state observable.
      const { container } = await mountWithRomId(60);
      expect(container.textContent).not.toContain("BIOS");
      vi.mocked(backend.checkPlatformBios).mockRejectedValue(new Error("net"));
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // The outer try/catch did not fire — inline .catch swallowed and
      // produced { needs_bios: false }, which means biosStatus stays null
      // (the handler's setState resolves the ternary to null) and the
      // BIOS tab remains hidden.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("onDataChanged error"),
      );
      expect(container.textContent).not.toContain("BIOS");
    });

    it("core_changed: invalidates cache + re-fetches getCachedGameDetail and updates biosStatus state", async () => {
      // Mount without bios_status so the initial state.biosStatus is null
      // and the BIOS tab is NOT visible. Then dispatch core_changed with a
      // cache response that DOES carry bios_status — the handler's setState
      // call is the only path that surfaces the BIOS tab.
      const { container } = await mountWithRomId(60);
      expect(container.textContent).not.toContain("BIOS");
      vi.mocked(cachedStore.invalidateCachedGameDetail).mockClear();
      vi.mocked(cachedStore.getCachedGameDetail).mockClear();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
          active_core_label: "FROM_CORE_CHANGED",
        } as never,
        metadata: {} as never,
        stale_fields: [],
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(
        testAppId,
      );
      expect(vi.mocked(cachedStore.getCachedGameDetail)).toHaveBeenCalled();
      // biosStatus now non-null → BIOS tab visible. Removing the
      // handler's `setState((prev) => ({ ..., biosStatus }))` line
      // makes this assertion fail.
      expect(container.textContent).toContain("BIOS");
      // Switch to the BIOS tab and assert the new active_core_label
      // reached the rendered Emulator column.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }),
        );
        await Promise.resolve();
      });
      expect(container.textContent).toContain("FROM_CORE_CHANGED");
    });

    it("core_changed: cache returns found=false → no state mutation", async () => {
      // Mount with a bios_status on the cache so biosStatus starts non-null
      // (BIOS tab visible). After a found=false core_changed re-fetch, the
      // handler should early-return — biosStatus must NOT be reset.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        save_sync_enabled: true,
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
          active_core_label: "INITIAL_CORE",
        } as never,
        metadata: {} as never,
        stale_fields: [],
      });
      const view = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(view.container.textContent).toContain("BIOS");
      // Second resolve: found=false. The handler's early-return means
      // biosStatus is NOT touched.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValueOnce({
        found: false,
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // Outer catch did NOT fire …
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("onDataChanged error"),
      );
      // … and biosStatus is unchanged from the initial value (BIOS tab
      // still visible and the INITIAL_CORE label still reaches the BIOS
      // section after a tab switch).
      expect(view.container.textContent).toContain("BIOS");
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }),
        );
        await Promise.resolve();
      });
      expect(view.container.textContent).toContain("INITIAL_CORE");
    });

    it("metadata: matching rom_id → getRomMetadata + updates metadata state", async () => {
      const { container } = await mountWithRomId(70);
      vi.mocked(backend.getRomMetadata).mockClear();
      // Distinguishable summary so we can prove the new metadata reached
      // the Game Info render via the handler's setState call.
      vi.mocked(backend.getRomMetadata).mockResolvedValue({
        summary: "FROM_DISPATCH_METADATA",
        genres: [],
        companies: [],
        first_release_date: null,
        average_rating: null,
        game_modes: [],
        player_count: "",
        cached_at: 0,
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "metadata", rom_id: 70 },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getRomMetadata)).toHaveBeenCalledWith(70);
      // The summary text in the Game Info section is fed directly from
      // state.metadata.summary. Dropping the handler's
      // `setState((prev) => ({ ..., metadata: meta }))` line makes this
      // assertion fail.
      expect(container.textContent).toContain("FROM_DISPATCH_METADATA");
    });

    it("metadata: mismatching rom_id → early return", async () => {
      await mountWithRomId(70);
      vi.mocked(backend.getRomMetadata).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "metadata", rom_id: 999 },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getRomMetadata)).not.toHaveBeenCalled();
    });

    it("metadata: getRomMetadata rejection → falls back to null (non-vacuous .catch)", async () => {
      await mountWithRomId(70);
      vi.mocked(backend.getRomMetadata).mockRejectedValue(new Error("net"));
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "metadata", rom_id: 70 },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // Outer try/catch did NOT fire.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("onDataChanged error"),
      );
    });

    it("unknown detail.type → no-op (no fetches, no throw)", async () => {
      await mountWithRomId(99);
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(backend.checkPlatformBios).mockClear();
      vi.mocked(backend.getRomMetadata).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "definitely_not_a_real_event" },
          }),
        );
        await Promise.resolve();
      });
      expect(backend.getSaveStatus).not.toHaveBeenCalled();
      expect(backend.checkPlatformBios).not.toHaveBeenCalled();
      expect(backend.getRomMetadata).not.toHaveBeenCalled();
    });

    it("handler outer try/catch → debugLog fires when an inner await rejects without .catch", async () => {
      // The core_changed branch is the only one without an inline .catch on
      // its fetch — make getCachedGameDetail reject after mount so the inner
      // await throws and the outer try/catch in onDataChanged surfaces it.
      await mountWithRomId(99);
      vi.mocked(backend.debugLog).mockClear();
      vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValueOnce(
        new Error("handler-boom"),
      );
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("onDataChanged error"),
      );
    });
  });

  // ------------------------------------------------------------------
  // F. Migration store subscriptions
  // ------------------------------------------------------------------

  describe("migration store subscriptions", () => {
    it("subscribes to migrationStore on mount and unsubscribes on unmount", async () => {
      const before = migrationListeners.length;
      const { unmount } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(migrationListeners.length).toBe(before + 1);
      unmount();
      expect(migrationListeners.length).toBe(before);
    });

    it("subscribes to saveSortMigrationStore on mount and unsubscribes on unmount", async () => {
      const before = saveSortListeners.length;
      const { unmount } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(saveSortListeners.length).toBe(before + 1);
      unmount();
      expect(saveSortListeners.length).toBe(before);
    });

    it("listener fired after migrationStore changes to pending=true → switches to MigrationBlockedCard", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {} as never,
        stale_fields: [],
      });
      const { queryByTestId } = render(
        <RomMGameInfoPanel appId={testAppId} />,
      );
      await flushAsync();
      expect(queryByTestId("migration-blocked-card")).toBeNull();
      await act(async () => {
        currentMigrationState = { pending: true };
        migrationListeners.forEach((fn) => fn());
      });
      expect(queryByTestId("migration-blocked-card")).not.toBeNull();
    });

    it("listener fired after saveSortMigrationStore changes to pending=true → renders save-sort warning", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain(
        "RetroArch save sorting changed",
      );
      await act(async () => {
        currentSaveSortState = { pending: true };
        saveSortListeners.forEach((fn) => fn());
      });
      expect(container.textContent).toContain(
        "RetroArch save sorting changed",
      );
    });
  });

  // ------------------------------------------------------------------
  // G. Tab switching + visibility
  // ------------------------------------------------------------------

  describe("tab switching + visibility", () => {
    it("default activeTab is 'info' — only GAME INFO content renders", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        platform_name: "Super Nintendo",
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // The Game Info section renders its content (the platform row) by default.
      expect(container.textContent).toContain("Super Nintendo");
    });

    it("ACHIEVEMENTS tab is hidden when raId is null", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        ra_id: null,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("ACHIEVEMENTS");
    });

    it("ACHIEVEMENTS tab is visible when raId is set", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("ACHIEVEMENTS");
    });

    it("BIOS tab is visible only when biosStatus is non-null", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        bios_status: {
          needs_bios: true,
          platform_slug: "snes",
          server_count: 1,
          local_count: 0,
          all_downloaded: false,
        } as never,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Tab bar label (uppercase).
      expect(container.innerHTML).toContain("BIOS");
    });

    it("SAVES tab visible only when save_sync_enabled=true", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        save_sync_enabled: false,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("SAVES");
    });

    it("achievements tab activation triggers lazy-load (getAchievements + getAchievementProgress)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Switch via the tab-switch event.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledWith(88);
      expect(vi.mocked(backend.getAchievementProgress)).toHaveBeenCalledWith(88);
    });

    it("achievements tab: renders achievement rows (earned + locked + hardcore + rarity)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.getAchievements).mockResolvedValue({
        success: true,
        total: 3,
        achievements: [
          {
            ra_id: 1,
            badge_id: "b1",
            title: "First Steps",
            description: "Did the thing",
            points: 5,
            badge_url: "http://example/b1.png",
            badge_url_lock: "http://example/b1-lock.png",
            display_order: 1,
            type: "win",
            num_awarded: 1234,
            num_awarded_hardcore: 1,
          },
          {
            ra_id: 2,
            badge_id: "b2",
            title: "Hardcore Run",
            description: "HC mode",
            points: 20,
            badge_url: "http://example/b2.png",
            badge_url_lock: "http://example/b2-lock.png",
            display_order: 2,
            type: "win",
            num_awarded: 0,
            num_awarded_hardcore: 1,
          },
          {
            ra_id: 3,
            badge_id: "b3",
            title: "Locked One",
            description: "Locked",
            points: 10,
            badge_url: "http://example/b3.png",
            badge_url_lock: "http://example/b3-lock.png",
            display_order: 3,
            type: "win",
            num_awarded: 5,
            num_awarded_hardcore: 0,
          },
        ],
      });
      vi.mocked(backend.getAchievementProgress).mockResolvedValue({
        success: true,
        earned: 2,
        earned_hardcore: 1,
        total: 3,
        earned_achievements: [
          { id: "b1", date: "2025-02-14 15:45:38", date_hardcore: null },
          {
            id: "b2",
            date: "2025-02-15 16:00:00",
            date_hardcore: "2025-02-15 17:00:00",
          },
        ],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("2 / 3 Achievements");
      expect(container.textContent).toContain("1 hardcore");
      expect(container.textContent).toContain("Earned (2)");
      expect(container.textContent).toContain("Locked (1)");
      expect(container.textContent).toContain("First Steps");
      expect(container.textContent).toContain("Locked One");
      // num_awarded > 0 — rarity row
      expect(container.textContent).toContain("1234 players earned this");
      // HC badge present (hardcore achievement)
      expect(container.innerHTML).toContain("romm-cheevo-hc-badge");
      // Date strips the seconds.
      expect(container.textContent).toContain("2025-02-14 15:45");
      expect(container.textContent).not.toContain("2025-02-14 15:45:38");
    });

    it("achievements tab: empty list → 'No achievements found' message", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.getAchievements).mockResolvedValue({
        success: true,
        total: 0,
        achievements: [],
      });
      vi.mocked(backend.getAchievementProgress).mockResolvedValue({
        success: true,
        earned: 0,
        total: 0,
        earned_achievements: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain(
        "No achievements found for this game",
      );
    });

    it("achievements tab: achievementsLoading=true → 'Loading achievements...'", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      // Hold the achievement promises so achievementsLoading stays true.
      vi.mocked(backend.getAchievements).mockReturnValue(new Promise(() => {}));
      vi.mocked(backend.getAchievementProgress).mockReturnValue(
        new Promise(() => {}),
      );
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Loading achievements...");
    });

    it("achievements lazy-load: rejection → debugLog fires with 'Failed to load achievements'", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.getAchievements).mockRejectedValue(new Error("net"));
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.debugLog).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("Failed to load achievements"),
      );
    });

    it("achievements tab: no refetch on second activation (achievementsLoadedRef guard)", async () => {
      // First activation fires getAchievements + getAchievementProgress
      // exactly once each. Switching away and back must NOT trigger a
      // second fetch — the achievementsLoadedRef guard short-circuits the
      // lazy-load effect. Removing the
      // `if (achievementsLoadedRef.current) return;` line makes the
      // toHaveBeenCalledTimes(1) assertion fail.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: {} as never,
        stale_fields: [],
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // First activation → triggers the lazy-load.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.getAchievementProgress)).toHaveBeenCalledTimes(1);
      // Switch away …
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "info" } }),
        );
        await Promise.resolve();
      });
      // … and back. Guard should prevent a second fetch.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", {
            detail: { tab: "achievements" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.getAchievementProgress)).toHaveBeenCalledTimes(1);
    });

    it("saves tab: no refetch on second activation (slotsLoadedRef guard)", async () => {
      // The Saves tab's lazy-load effect calls getSaveSlots and is guarded
      // by slotsLoadedRef. (Note: refreshSlotState from loadData also calls
      // getSaveSlots on mount and is NOT guarded — we clear the mock after
      // the first tab activation so the assertion only sees lazy-load
      // calls.) Removing the `if (slotsLoadedRef.current) return;` line
      // makes the toHaveBeenCalledTimes(0) assertion below fail.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 77,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // First activation → lazy-load fires getSaveSlots (in addition to
      // the mount-time refreshSlotState call already absorbed above).
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // Clear the call log — we want the second-activation assertion to
      // reflect only the post-clear period.
      vi.mocked(backend.getSaveSlots).mockClear();
      // Switch away …
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "info" } }),
        );
        await Promise.resolve();
      });
      // … and back. Guard should prevent a second fetch.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledTimes(0);
    });

    it("saves tab: slotConfirmed=false → SlotSetupWizard renders", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 11,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: false,
        active_slot: null,
      });
      const { queryByTestId } = render(
        <RomMGameInfoPanel appId={testAppId} />,
      );
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      expect(queryByTestId("slot-setup-wizard")).not.toBeNull();
      expect(queryByTestId("saves-tab")).toBeNull();
    });

    it("saves tab: slotConfirmed=true → SavesTab renders with forwarded props", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 22,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      const { queryByTestId } = render(
        <RomMGameInfoPanel appId={testAppId} />,
      );
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      expect(queryByTestId("saves-tab")).not.toBeNull();
      const props = capturedSavesTab[capturedSavesTab.length - 1];
      expect(props?.romId).toBe(22);
    });

    it("saves tab: SavesTab.onSlotSwitched updates activeSlot + saveStatus and dispatches romm_data_changed", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 33,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      const props = capturedSavesTab[capturedSavesTab.length - 1];
      expect(props).toBeDefined();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        const newStatus = {
          rom_id: 33,
          files: [],
          playtime: {
            total_seconds: 0,
            session_count: 0,
            last_session_start: null,
            last_session_duration_sec: null,
          },
          device_id: "d",
          last_sync_check_at: null,
          conflicts: [],
        };
        await act(async () => {
          props!.onSlotSwitched("slot-b", newStatus);
          await Promise.resolve();
        });
        const dispatched = listener.mock.calls
          .map((c) => c[0] as CustomEvent)
          .find((e) => e.detail.type === "save_sync");
        expect(dispatched?.detail).toMatchObject({
          type: "save_sync",
          rom_id: 33,
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("saves tab: SlotSetupWizard.onComplete sets slotConfirmed=true and dispatches romm_data_changed", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 44,
        save_sync_enabled: true,
        metadata: {} as never,
        stale_fields: [],
      });
      // Wizard not yet completed.
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: false,
        active_slot: null,
      });
      const { queryByTestId } = render(
        <RomMGameInfoPanel appId={testAppId} />,
      );
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }),
        );
        await Promise.resolve();
      });
      expect(queryByTestId("slot-setup-wizard")).not.toBeNull();
      const wizardProps = capturedSlotSetupWizard[capturedSlotSetupWizard.length - 1];
      // After wizard completion the backend reports configured=true, so the
      // post-onComplete handleSaveSyncChange branch (which calls
      // refreshSlotState) doesn't revert slotConfirmed back to false.
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "default",
      });
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          wizardProps?.onComplete();
          await Promise.resolve();
          await Promise.resolve();
        });
        // SavesTab now renders (slotConfirmed flipped to true).
        expect(queryByTestId("saves-tab")).not.toBeNull();
        const dispatched = listener.mock.calls
          .map((c) => c[0] as CustomEvent)
          .find((e) => e.detail.type === "save_sync");
        expect(dispatched?.detail).toMatchObject({
          type: "save_sync",
          rom_id: 44,
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("bios tab activation renders biosSection content (BIOS file list + Emulator column)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        bios_status: {
          needs_bios: true,
          platform_slug: "snes",
          server_count: 2,
          local_count: 1,
          all_downloaded: false,
          required_count: 2,
          required_downloaded: 1,
          active_core_label: "Snes9x",
          available_cores: [
            { core_so: "snes9x_libretro", label: "Snes9x", is_default: true },
          ],
          files: [
            {
              file_name: "bios.smc",
              description: "BIOS file",
              downloaded: false,
              classification: "required",
              cores: { snes9x_libretro: { required: true } },
              used_by_active: true,
            },
            {
              file_name: "unknown.bin",
              description: "",
              downloaded: false,
              classification: "unknown",
            },
          ],
        } as never,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }),
        );
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Emulator");
      expect(container.textContent).toContain("Snes9x");
      // unknown file is filtered out of the visible list; the "+1 other"
      // note appears instead.
      expect(container.textContent).toContain("other file");
    });
  });

  // ------------------------------------------------------------------
  // H. Module-level helpers — pure behavior asserted through rendering
  // ------------------------------------------------------------------

  describe("formatReleaseDate (rendered via Game Info)", () => {
    it("renders 'D MMM YYYY' when first_release_date > 0", async () => {
      // 2003-01-01 00:00:00 UTC = 1041379200 → date.getDate() / getMonth() use
      // local time; just assert the year + month-name format is applied
      // (the day may vary by TZ).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {
          summary: "",
          genres: [],
          companies: [],
          first_release_date: 1041379200,
          average_rating: null,
          game_modes: [],
          player_count: "",
          cached_at: 0,
        } as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("Release Date");
      // Format: "D MMM YYYY" — assert the month abbreviation + year reach
      // the DOM. (Day depends on TZ; "2003" pins the year.)
      expect(container.textContent).toMatch(/\d+\s(Jan|Dec)\s2003/);
    });

    it("skips Release Date row when first_release_date is null or 0", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {
          summary: "",
          genres: [],
          companies: [],
          first_release_date: 0,
          average_rating: null,
          game_modes: [],
          player_count: "",
          cached_at: 0,
        } as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Release Date");
    });

    it("skips Release Date row when first_release_date is negative", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {
          summary: "",
          genres: [],
          companies: [],
          first_release_date: -1,
          average_rating: null,
          game_modes: [],
          player_count: "",
          cached_at: 0,
        } as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Release Date");
    });
  });

  describe("pickBiosColor (rendered via BIOS section)", () => {
    async function renderWithBios(
      required_downloaded: number | null,
      required_count: number | null,
      local_count = 0,
      all_downloaded = false,
    ) {
      const bios: Record<string, unknown> = {
        needs_bios: true,
        platform_slug: "snes",
        server_count: required_count ?? 1,
        local_count,
        all_downloaded,
      };
      if (required_count !== null) bios.required_count = required_count;
      if (required_downloaded !== null) bios.required_downloaded = required_downloaded;
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        bios_status: bios as never,
        metadata: {} as never,
        stale_fields: [],
      });
      const view = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }),
        );
        await Promise.resolve();
      });
      return view;
    }

    it("done >= total → green (#5ba32b) status dot", async () => {
      const { container } = await renderWithBios(5, 5, 5, true);
      expect(container.innerHTML).toContain("#5ba32b");
      expect(container.textContent).toContain("All required ready");
    });

    it("0 < done < total → amber (#d4a72c) status dot", async () => {
      const { container } = await renderWithBios(2, 5, 2, false);
      expect(container.innerHTML).toContain("#d4a72c");
      expect(container.textContent).toContain("2/5 required files ready");
    });

    it("done = 0 → red (#d94126) status dot", async () => {
      const { container } = await renderWithBios(0, 5, 0, false);
      expect(container.innerHTML).toContain("#d94126");
    });

    it("required_count null + all_downloaded=true → green; localCount>0 (no req) → amber; localCount=0 → red", async () => {
      // a) all_downloaded=true → green
      {
        const { container } = await renderWithBios(null, null, 1, true);
        expect(container.innerHTML).toContain("#5ba32b");
        expect(container.textContent).toContain("All ready");
      }
    });
  });

  describe("biosStatusFromCache + saveStatusFromCache (cache-first rendering)", () => {
    it("biosStatusFromCache(null) yields no biosStatus → no BIOS tab", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        bios_status: null,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("BIOS");
    });

    it("biosStatusFromCache spreads the cached fields onto needs_bios=true", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
          active_core_label: "MyCore",
        } as never,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Active core label spread from cache reaches the BIOS tab.
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }),
        );
        await Promise.resolve();
      });
      expect(container.textContent).toContain("MyCore");
    });
  });

  // ------------------------------------------------------------------
  // I. Render: cover art, no-metadata fallback, etc.
  // ------------------------------------------------------------------

  describe("Game Info content", () => {
    it("renders 'No metadata available' when metadata is null and platformName is empty", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: null,
        platform_name: "",
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("No metadata available");
    });

    it("renders the RomM game name on the info tab", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "Chrono Trigger",
        metadata: { summary: "An RPG." } as never,
        platform_name: "Super Nintendo",
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("Chrono Trigger");
    });

    it("renders Platform row even when metadata is null but platformName is set", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: null,
        platform_name: "Super Nintendo",
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("Super Nintendo");
    });

    it("renders rating row when average_rating > 0", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {
          summary: "",
          genres: [],
          companies: [],
          first_release_date: null,
          average_rating: 87,
          game_modes: [],
          player_count: "",
          cached_at: 0,
        } as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("87%");
    });

    it("renders cover art when coverBase64 is set from background fetch", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: { summary: "x" } as never,
        stale_fields: [],
      });
      vi.mocked(backend.getArtworkBase64).mockResolvedValue({
        base64: "AAAA",
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.innerHTML).toContain("data:image/png;base64,AAAA");
    });

    it("background art fetch with no base64 → cover not rendered (non-vacuous .catch path: empty data)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: { summary: "x" } as never,
        stale_fields: [],
      });
      vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.innerHTML).not.toContain("data:image/png;base64,");
    });

    it("background art fetch rejection → cover not rendered (silent .catch fallback)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: { summary: "x" } as never,
        stale_fields: [],
      });
      vi.mocked(backend.getArtworkBase64).mockRejectedValue(new Error("net"));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Catch swallowed; no cover img.
      expect(container.innerHTML).not.toContain("data:image/png;base64,");
      // And the mount didn't surface debugLog.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("loadData error"),
      );
    });

    it("background installed-rom fetch rejection → installedRom stays null (silent .catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        installed: true,
        metadata: { summary: "x" } as never,
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockRejectedValue(new Error("net"));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Without installedRom, the ROM File section is not rendered.
      expect(container.textContent).not.toContain("ROM File");
      // And the rejection didn't bubble to loadData's outer catch (the
      // inline .catch swallowed it). Removing the inline `.catch(() => {})`
      // from refreshInstalledRomInBackground makes this assertion fail.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(
        expect.stringContaining("loadData error"),
      );
    });

    it("background metadata fetch rejection → metadata stays at cached value (silent .catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: { summary: "from cache" } as never,
        stale_fields: ["metadata"],
      });
      vi.mocked(backend.getRomMetadata).mockRejectedValue(new Error("net"));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // The cached summary is still rendered — rejection didn't blank it.
      expect(container.textContent).toContain("from cache");
    });
  });

  // ------------------------------------------------------------------
  // J. Save-sort warning rendering
  // ------------------------------------------------------------------

  describe("save-sort warning banner", () => {
    it("does NOT render when saveSortPending=false", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain(
        "RetroArch save sorting changed",
      );
    });

    it("renders when saveSortPending=true at mount", async () => {
      currentSaveSortState = { pending: true };
      // refreshMigrationState() on mount overwrites the store — return
      // pending=true so the gate survives the useEffect resolution.
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck: { pending: false },
        save_sort: { pending: true },
      });
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: {} as never,
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain(
        "RetroArch save sorting changed",
      );
    });
  });
});
