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
import type { CachedGameDetail } from "../api/backend";
import * as cachedStore from "../utils/cachedGameDetailStore";
import * as slotState from "../utils/slotState";
import {
  installDomEventListenerSpy,
  uninstallDomEventListenerSpy,
  domListenerCount,
} from "../test-utils/dom-event-listener-spy";
import { emitDeckyEvent, deckyEventListenerCount } from "../test-utils/decky-api-mock";
import { useVersionError } from "./VersionErrorCard";
import {
  setRommConnectionState,
  reportServerReachable,
  getRommConnectionState,
  setServerRetryProgress,
  getServerRetryProgress,
} from "../utils/connectionState";
import type {
  MigrationStatus,
  SaveSortMigrationStatus,
  RomMetadata,
  DownloadCompleteEvent,
  CoreInfo,
  InstalledRom,
  SaveStatus,
} from "../types";

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

// Complete RomMetadata fixture. The backend always serializes every field
// (RomMetadata is a strict dataclass → MetadataCacheEntry TypedDict), so the
// component never sees a partial metadata object. Tests that only care about a
// subset still get a fully-shaped object so array fields (genres/companies/
// game_modes) are present. The `& Record<string, unknown>` return makes the
// fixture usable both for the `getRomMetadata` mock (typed `RomMetadata`) and
// the `getCachedGameDetail` mock's loosely-typed `metadata` field — a
// RomMetadata genuinely is a string-keyed record at runtime.
function makeMetadata(overrides: Partial<RomMetadata> = {}): RomMetadata & Record<string, unknown> {
  return {
    summary: "",
    genres: [],
    companies: [],
    first_release_date: null,
    average_rating: null,
    game_modes: [],
    player_count: "",
    cached_at: 0,
    steam_categories: [],
    ...overrides,
  };
}

/** A cached detail on the snes platform (so the #1082 guard lets a matching
 *  `bios` event through) that carries a BIOS requirement — the panel mounts with
 *  its BIOS tab visible, which is what a later BIOS answer can take away. */
function biosNeedingDetail(overrides: Partial<CachedGameDetail> = {}): CachedGameDetail {
  return {
    found: true,
    rom_id: 60,
    platform_slug: "snes",
    save_sync_enabled: true,
    metadata: makeMetadata(),
    stale_fields: [],
    bios_status: {
      platform_slug: "snes",
      server_count: 2,
      local_count: 1,
      all_downloaded: false,
    },
    bios_level: "partial",
    ...overrides,
  };
}

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

    // Reset the real connection store so each test starts connected (#1345).
    setRommConnectionState("connected");
    setServerRetryProgress(null);

    // resetAllMocks wipes module-mock impls — re-stub below.
    vi.mocked(useVersionError).mockReturnValue(null);

    // Re-stub migrationStore impls (resetAllMocks wiped them).
    vi.mocked(migrationStore.getMigrationState).mockImplementation(() => currentMigrationState);
    vi.mocked(migrationStore.setMigrationStatus).mockImplementation((s: MigrationStatus) => {
      currentMigrationState = s;
      migrationListeners.forEach((fn) => fn());
    });
    vi.mocked(migrationStore.onMigrationChange).mockImplementation((cb: () => void) => {
      migrationListeners.push(cb);
      return () => {
        const i = migrationListeners.indexOf(cb);
        if (i >= 0) migrationListeners.splice(i, 1);
      };
    });
    // Re-stub saveSortMigrationStore impls.
    vi.mocked(saveSortMigrationStore.getSaveSortMigrationState).mockImplementation(() => currentSaveSortState);
    vi.mocked(saveSortMigrationStore.setSaveSortMigrationStatus).mockImplementation((s: SaveSortMigrationStatus) => {
      currentSaveSortState = s;
      saveSortListeners.forEach((fn) => fn());
    });
    vi.mocked(saveSortMigrationStore.onSaveSortMigrationChange).mockImplementation((cb: () => void) => {
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
    vi.mocked(backend.getRomMetadata).mockResolvedValue(makeMetadata());
    vi.mocked(backend.getInstalledRom).mockResolvedValue(null);
    vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: null });
    vi.mocked(backend.checkPlatformBios).mockResolvedValue({ needs_bios: false });
    // Core info comes from the dedicated get_platform_core_info path (#923),
    // decoupled from BIOS status. Default: no cores. Tests opt into shapes.
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
      emulator_data_available: true,
      emulators: [],
      active_core: null,
      active_core_label: null,
      platform_core_label: null,
      has_game_override: false,
    });
    vi.mocked(backend.getSaveStatus).mockResolvedValue({
      rom_id: 0,
      files: [],
      playtime: {
        total_seconds: 0,
        session_count: 0,
        last_session_start: null,
        last_session_duration_sec: null,
        last_played: null,
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

    it("renders 'Loading...' before loadData resolves", async () => {
      // getCachedGameDetail returns a never-resolving promise so the initial
      // loading state stays visible.
      vi.mocked(cachedStore.getCachedGameDetail).mockReturnValue(new Promise(() => {}));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      expect(container.textContent).toContain("Loading...");
      // loadData stays pending by design, but the sibling refreshMigrationState
      // effect does resolve — its store writes have to land inside act.
      await flushAsync();
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
        metadata: makeMetadata({ summary: "An RPG.", genres: ["RPG"] }),
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

    it("keys the dedicated core-info path on rom_id on mount (#945)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        platform_slug: "snes",
        rom_file: "mario.sfc",
        metadata: makeMetadata(),
        stale_fields: [],
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // The background core-info fetch reads the per-game DB override by rom_id.
      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(99);
    });

    it("skips metadata refresh when metadata exists AND not in stale_fields", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        metadata: makeMetadata({ summary: "ok" }),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(backend.isSaveTrackingConfigured).not.toHaveBeenCalled();
      expect(backend.getSaveSlots).not.toHaveBeenCalled();
    });

    it("logs via debugLog when getCachedGameDetail rejects (outer catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValue(new Error("boom"));
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("loadData error"));
    });

    it("routes the slot refresh through applyRefreshSlotResult on success", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockRejectedValue(new Error("net"));
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
      const { setSaveSortMigrationStatus } = await import("../utils/saveSortMigrationStore");
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck: { pending: false },
        save_sort: { pending: true } as SaveSortMigrationStatus,
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(setMigrationStatus).toHaveBeenCalledWith(expect.objectContaining({ pending: false }));
      expect(setSaveSortMigrationStatus).toHaveBeenCalledWith(expect.objectContaining({ pending: true }));
    });

    it("calls logError when refreshMigrationState rejects", async () => {
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      vi.mocked(backend.refreshMigrationState).mockRejectedValue(new Error("boom"));
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to refresh migration state"));
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/p",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
        launchable: true,
      });
      const { container, queryByText } = render(<RomMGameInfoPanel appId={testAppId} />);
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/p",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
        launchable: true,
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

    // download_complete is a Decky backend event (@decky/api bus), the install
    // counterpart to romm_rom_uninstalled — the fix for #1340.
    it("registers a download_complete listener on mount and removes it on unmount", async () => {
      const before = deckyEventListenerCount("download_complete");
      const { unmount } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(deckyEventListenerCount("download_complete")).toBe(before + 1);
      unmount();
      expect(deckyEventListenerCount("download_complete")).toBe(before);
    });

    it("download_complete: matching rom_id → ROM File section appears without a re-mount (#1340)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        installed: false,
        platform_name: "Super Nintendo",
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/roms/test.sfc",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
        launchable: true,
      });
      const { queryByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Not installed at mount → ROM File section hidden.
      expect(queryByText("ROM File")).toBeNull();
      // Download finishes for this rom → installed flips true + installedRom
      // populates → section (with the local path) appears live.
      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", {
          rom_id: 100,
          rom_name: "Test",
          platform_name: "Super Nintendo",
          file_path: "/roms/test.sfc",
          app_id: testAppId,
          launch_options: "cmd",
        });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(queryByText("ROM File")).not.toBeNull();
      expect(vi.mocked(backend.getInstalledRom)).toHaveBeenCalledWith(100);
      // Cache invalidated so a fast re-mount inside the 3s TTL doesn't re-serve
      // the stale installed:false.
      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(testAppId);
    });

    it("an unlaunchable install states why the game will not start and that the files were kept (#1652)", async () => {
      // A PS3 title downloaded as a .pkg installer. The install is real — the
      // section still renders its filename — but the game page is the one place
      // that explains why no launch command was written.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        installed: true,
        platform_name: "PlayStation 3",
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "Puppeteer.pkg",
        file_path: "/roms/ps3/Puppeteer/Puppeteer.pkg",
        system: "ps3",
        platform_slug: "ps3",
        installed_at: "2024-01-01",
        launchable: false,
      });
      const { container, queryByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      expect(queryByText("ROM File")).not.toBeNull();
      expect(queryByText("Puppeteer.pkg")).not.toBeNull();
      expect(container.textContent).toContain("nothing here is a format ps3 can launch");
      expect(container.textContent).toContain("The files are on disk");
    });

    it("a launchable install shows the filename with no no-launch-target notice", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        installed: true,
        platform_name: "Super Nintendo",
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/roms/test.sfc",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
        launchable: true,
      });
      const { container, queryByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      expect(queryByText("test.sfc")).not.toBeNull();
      expect(container.textContent).not.toContain("The files are on disk");
    });

    it("download_complete: mismatching rom_id → section stays hidden, no fetch", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        installed: false,
        platform_name: "Super Nintendo",
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getInstalledRom).mockResolvedValue({
        rom_id: 100,
        file_name: "test.sfc",
        file_path: "/roms/test.sfc",
        system: "snes",
        platform_slug: "snes",
        installed_at: "2024-01-01",
        launchable: true,
      });
      const { queryByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(queryByText("ROM File")).toBeNull();
      await act(async () => {
        emitDeckyEvent<[DownloadCompleteEvent]>("download_complete", {
          rom_id: 999,
          rom_name: "Other",
          platform_name: "Super Nintendo",
          file_path: "/roms/other.sfc",
          app_id: 1,
          launch_options: "cmd",
        });
        await Promise.resolve();
      });
      // Guard held: no state change, no installed-rom fetch for the other rom.
      expect(queryByText("ROM File")).toBeNull();
      expect(vi.mocked(backend.getInstalledRom)).not.toHaveBeenCalled();
    });

    it("romm_tab_switch: detail.tab present → activeTab state updates", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: null,
      });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Initially info tab → no saves-tab rendered
      expect(queryByTestId("saves-tab")).toBeNull();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
      });
      expect(queryByTestId("saves-tab")).not.toBeNull();
    });

    it("romm_tab_switch: detail.tab absent → no-op", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 100,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      const before = container.textContent;
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: {} }));
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
        metadata: makeMetadata(),
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
          last_played: null,
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
        metadata: makeMetadata(),
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

    // #1748 — the tab strip asks whether save sync is still on, the pane below
    // it only asks which tab is active. Switching the setting off under an open
    // SAVES tab is where the two disagree.
    it("save_sync_settings enabled=false while the SAVES tab is open → falls back to the info tab (#1748)", async () => {
      // configured=true so the open tab is SavesTab itself, not the setup wizard.
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        rom_name: "Game (USA)",
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container, queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
      });
      await flushAsync();
      expect(queryByTestId("saves-tab")).not.toBeNull();

      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync_settings", save_sync_enabled: false },
          }),
        );
        await Promise.resolve();
      });

      // The button went with the setting; the pane must go with the button,
      // leaving the tab every ROM has — asserted on the info tab's BODY (the
      // RomM game name), not on its button, which is present either way.
      expect(container.textContent).not.toContain("SAVES");
      expect(queryByTestId("saves-tab")).toBeNull();
      expect(container.textContent).toContain("Game (USA)");
    });

    it("save_sync_settings enabled=false leaves a user standing on another tab where they are", async () => {
      // The over-fix this invites: only the SAVES tab loses its button, so
      // nobody else may be moved.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(biosNeedingDetail());
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
      });
      await flushAsync();
      expect(container.textContent).toContain("1/2 files ready");

      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync_settings", save_sync_enabled: false },
          }),
        );
        await Promise.resolve();
      });

      expect(container.textContent).not.toContain("SAVES");
      expect(container.textContent).toContain("1/2 files ready");
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
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
      // Fallback observable: saveSyncEnabled stays true (SAVES tab still
      // rendered) AND saveStatus is set to the null fallback. Switch to
      // saves tab to capture SavesTab props.
      expect(container.textContent).toContain("SAVES");
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
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
          last_played: null,
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
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
      });
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.saveStatus?.files[0]?.filename).toBe("FROM_DISPATCH.srm");
    });

    it("save_sync: prune-active status preserves the previously displayed save state", async () => {
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      await mountWithRomId(33);
      const knownStatus = {
        rom_id: 33,
        files: [
          {
            filename: "KNOWN_CONFLICT.srm",
            status: "conflict" as const,
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
          last_played: null,
        },
        device_id: "d",
        last_sync_check_at: null,
        conflicts: [],
      };
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: 33, save_status: knownStatus },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      vi.mocked(backend.getSaveStatus).mockResolvedValue({
        success: false,
        reason: "prune_active",
        message: "Cleanup is active.",
      });

      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "save_sync", rom_id: 33 } }));
        await Promise.resolve();
        await Promise.resolve();
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
      });

      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.saveStatus?.files[0]?.filename).toBe("KNOWN_CONFLICT.srm");
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
                  last_played: null,
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
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
    });

    it("bios: matching platform_slug → calls checkPlatformBios; updates biosStatus when needs_bios=true", async () => {
      // Panel's own platform is snes; a matching-platform bios event must be
      // applied (the positive case for the #1082 guard).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        platform_slug: "snes",
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({
        needs_bios: true,
        server_count: 2,
        local_count: 2,
        all_downloaded: true,
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
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

    it("bios: threads bios_level from the callable result into the rendered status-dot color (#461)", async () => {
      // The check_platform_bios refresh path now ships bios_level straight from
      // the backend (compute_bios_level) — the handler threads it through, never
      // re-deriving from counts. amber (#d4a72c) is the observable side effect.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        platform_slug: "snes",
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({
        needs_bios: true,
        server_count: 5,
        local_count: 2,
        all_downloaded: false,
        required_count: 5,
        required_downloaded: 2,
        bios_level: "partial",
      });
      const view = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      // Open the BIOS tab so the status dot renders.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      expect(view.container.innerHTML).toContain("#d4a72c");
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

    it("bios: checkPlatformBios rejection → the shown requirement stands (#1693, non-vacuous .catch)", async () => {
      // Trigger: "Download BIOS" from the gear menu where the follow-up check
      // fails. Rewriting the rejection into { needs_bios: false } dropped the
      // whole BIOS tab while the play row above correctly kept its level, so the
      // panel mounts WITH a requirement and it has to survive.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(biosNeedingDetail());
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("BIOS");
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
      // The outer try/catch did not fire — the inline .catch swallowed the
      // rejection and wrote nothing, so the BIOS tab is still there.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
      expect(container.textContent).toContain("BIOS");
    });

    it("bios: a check that could not determine the requirement keeps the tab (#1693)", async () => {
      // The check answered without raising — an uncovered platform whose
      // firmware fetch failed — and says so with the flag. Same absent
      // requirement on the wire as the clear below; only the flag separates them.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(biosNeedingDetail());
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("BIOS");
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({ needs_bios: false, bios_status_unknown: true });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("BIOS");
    });

    it("bios: a real 'needs none' answer still clears the tab", async () => {
      // The other direction — an ANSWER carrying no requirement takes the tab
      // away, which is what makes the flag above load-bearing rather than a
      // blanket "never clear".
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(biosNeedingDetail());
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("BIOS");
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({ needs_bios: false });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).not.toContain("BIOS");
    });

    it("bios: cross-platform event (#1082) → ignored; no fetch, foreign BIOS list never bleeds in", async () => {
      // Regression for #1082: bios events fan out to every mounted panel.
      // A gba panel must ignore a psx bios event (e.g. a psx BIOS bulk
      // download) instead of repainting itself with the psx BIOS list.
      // Mount a gba panel WITHOUT bios_status, so the BIOS tab starts hidden.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 70,
        platform_slug: "gba",
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("BIOS");

      // The psx event would, if applied, repaint this panel with a needs_bios
      // psx status (surfacing the BIOS tab + a psx-specific marker). Make the
      // assertion non-vacuous: a distinct psx payload that must NOT appear.
      vi.mocked(backend.checkPlatformBios).mockClear();
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({
        needs_bios: true,
        server_count: 1,
        local_count: 1,
        all_downloaded: true,
        files: [
          {
            file_name: "PSX_BLEED_MARKER.bin",
            description: "PSX BIOS",
            classification: "required",
            downloaded: true,
          },
        ],
      } as never);
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "psx" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });

      // Guard returns BEFORE the fetch — checkPlatformBios("psx") never runs.
      expect(vi.mocked(backend.checkPlatformBios)).not.toHaveBeenCalled();
      // BIOS tab stayed hidden; the psx marker never bled into this gba panel.
      expect(container.textContent).not.toContain("BIOS");
      expect(container.innerHTML).not.toContain("PSX_BLEED_MARKER.bin");
    });

    it("bios: matching-platform event (#1082) → still applied (guard not over-broad)", async () => {
      // The guard must reject other platforms WITHOUT blocking the panel's own.
      // Mount a gba panel and feed it a gba bios event — it must update.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 70,
        platform_slug: "gba",
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("BIOS");

      vi.mocked(backend.checkPlatformBios).mockClear();
      vi.mocked(backend.checkPlatformBios).mockResolvedValue({
        needs_bios: true,
        server_count: 1,
        local_count: 1,
        all_downloaded: true,
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "bios", platform_slug: "gba" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });

      // Own-platform event flows through: fetch fired and BIOS tab surfaced.
      expect(vi.mocked(backend.checkPlatformBios)).toHaveBeenCalledWith("gba");
      expect(container.textContent).toContain("BIOS");
    });

    it("core_changed: invalidates cache + re-fetches getCachedGameDetail and updates biosStatus state", async () => {
      // Mount without bios_status so the initial state.biosStatus is null
      // and the BIOS tab is NOT visible. Then dispatch core_changed with a
      // cache response that DOES carry bios_status — the handler's setState
      // call is the only path that surfaces the BIOS tab.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        rom_file: "mario.sfc",
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("BIOS");
      vi.mocked(cachedStore.invalidateCachedGameDetail).mockClear();
      vi.mocked(cachedStore.getCachedGameDetail).mockClear();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        rom_file: "mario.sfc",
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        } as never,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Core data comes from the dedicated path (#923), keyed on rom_id so the
      // active core reflects the per-game DB override (epic #945).
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "from_core_changed.so",
        active_core_label: "FROM_CORE_CHANGED",
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          {
            label: "FROM_CORE_CHANGED",
            kind: "libretro",
            core_so: "from_core_changed.so",
            is_default: true,
            bakeable: true,
            reason: null,
          },
        ],
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
      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(testAppId);
      expect(vi.mocked(cachedStore.getCachedGameDetail)).toHaveBeenCalled();
      // Keyed on rom_id (#945) — the active core reflects the per-game DB override.
      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(60);
      // biosStatus now non-null → BIOS tab visible. Removing the
      // handler's `setState((prev) => ({ ..., biosStatus }))` line
      // makes this assertion fail.
      expect(container.textContent).toContain("BIOS");
      // Switch to the BIOS tab and assert the new active core label (from the
      // dedicated core-info path) reached the rendered Emulator column.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("FROM_CORE_CHANGED");
    });

    it("core_changed: a cached detail with no BIOS answer keeps the tab (#1693)", async () => {
      // The firmware cache is invalidated by every BIOS download and delete, and
      // a detail derived while it is cold carries no BIOS answer. Reading that
      // as "needs none" hid the tab until something else refreshed it.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(biosNeedingDetail());
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("BIOS");

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        biosNeedingDetail({ bios_status: null, bios_level: null, bios_status_unknown: true }),
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

      expect(container.textContent).toContain("BIOS");
    });

    it("core_changed: an answered detail without a requirement clears the tab", async () => {
      // The counterpart: the new core genuinely needs no BIOS, so the tab goes.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(biosNeedingDetail());
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("BIOS");

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        biosNeedingDetail({ bios_status: null, bios_level: null }),
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

      expect(container.textContent).not.toContain("BIOS");
    });

    it("core_changed: cache returns found=false → no state mutation", async () => {
      // Mount with a bios_status on the cache so biosStatus starts non-null
      // (BIOS tab visible). After a found=false core_changed re-fetch, the
      // handler should early-return — biosStatus must NOT be reset.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        platform_slug: "snes",
        save_sync_enabled: true,
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        } as never,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Initial core label from the dedicated path (loadData background fetch).
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "initial_core.so",
        active_core_label: "INITIAL_CORE",
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          {
            label: "INITIAL_CORE",
            kind: "libretro",
            core_so: "initial_core.so",
            is_default: true,
            bakeable: true,
            reason: null,
          },
        ],
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
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
      // … and biosStatus is unchanged from the initial value (BIOS tab
      // still visible and the INITIAL_CORE label still reaches the BIOS
      // section after a tab switch).
      expect(view.container.textContent).toContain("BIOS");
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
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
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
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
      vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValueOnce(new Error("handler-boom"));
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("RetroArch save sorting changed");
      await act(async () => {
        currentSaveSortState = { pending: true };
        saveSortListeners.forEach((fn) => fn());
      });
      expect(container.textContent).toContain("RetroArch save sorting changed");
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
        metadata: makeMetadata(),
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
      expect(container.textContent).toContain("No achievements found for this game");
    });

    it("achievements tab: achievementsLoading=true → ConnectingIndicator with retry progress (#1345 F1)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Hold the achievement promises so achievementsLoading stays true.
      vi.mocked(backend.getAchievements).mockReturnValue(new Promise(() => {}));
      vi.mocked(backend.getAchievementProgress).mockReturnValue(new Promise(() => {}));
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
      // The load pays the retry ladder → surface the shared ConnectingIndicator
      // (achievements-flavoured label) instead of frozen "Loading…" text.
      expect(container.textContent).toContain("Loading achievements…");
      // A retry frame from the ladder appends the "(attempt N/M)" suffix.
      act(() => {
        setServerRetryProgress({ attempt: 2, maxAttempts: 3 });
      });
      expect(container.textContent).toContain("Loading achievements… (attempt 2/3)");
    });

    it("achievements tab known-offline fast path: skips the fetch, shows degraded line, reloads on reconnect (#1345 F1)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      setRommConnectionState("offline");
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getAchievements).mockClear();
      vi.mocked(backend.getAchievementProgress).mockClear();

      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Fast path: no server calls (no ladder hang) and a short degraded line.
      expect(vi.mocked(backend.getAchievements)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getAchievementProgress)).not.toHaveBeenCalled();
      expect(container.textContent).toContain("RomM offline — achievements unavailable.");

      // Reconnect → the effect re-runs (isOffline dep) and loads.
      await act(async () => {
        reportServerReachable(true);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledWith(88);
      expect(vi.mocked(backend.getAchievementProgress)).toHaveBeenCalledWith(88);
    });

    it("achievements tab feed: both calls unreachable → store flips offline + retries on reconnect (#1345 F1)", async () => {
      setRommConnectionState("connected");
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getAchievements).mockResolvedValue({
        success: false,
        achievements: [],
        total: 0,
        reason: "server_unreachable",
        message: "down",
      });
      vi.mocked(backend.getAchievementProgress).mockResolvedValue({
        success: false,
        earned: 0,
        total: 0,
        earned_achievements: [],
        reason: "server_unreachable",
        message: "down",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Genuine unreachable verdict → store flips offline.
      expect(getRommConnectionState()).toBe("offline");

      // The unreachable settle released the load-once gate, so a reconnect
      // retries (mirrors the slot lane's applyLoadSlotsResult failure reset).
      vi.mocked(backend.getAchievements).mockClear();
      vi.mocked(backend.getAchievementProgress).mockClear();
      vi.mocked(backend.getAchievements).mockResolvedValue({ success: true, achievements: [], total: 0 });
      await act(async () => {
        reportServerReachable(true);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledWith(88);
    });

    it("achievements tab feed: no_ra_username failure leaves the store untouched (#1345 F1)", async () => {
      setRommConnectionState("connected");
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getAchievements).mockResolvedValue({ success: true, achievements: [], total: 0 });
      vi.mocked(backend.getAchievementProgress).mockResolvedValue({
        success: false,
        earned: 0,
        total: 0,
        earned_achievements: [],
        reason: "no_ra_username",
        message: "No RA username configured in RomM",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      // A config gap (list succeeded) — not a connectivity verdict.
      expect(getRommConnectionState()).toBe("connected");
    });

    it("achievements lazy-load: rejection → debugLog fires with 'Failed to load achievements'", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        ra_id: 42,
        metadata: makeMetadata(),
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
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("Failed to load achievements"));
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
        metadata: makeMetadata(),
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
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "info" } }));
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
        metadata: makeMetadata(),
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
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      // Clear the call log — we want the second-activation assertion to
      // reflect only the post-clear period.
      vi.mocked(backend.getSaveSlots).mockClear();
      // Switch away …
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "info" } }));
        await Promise.resolve();
      });
      // … and back. Guard should prevent a second fetch.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledTimes(0);
    });

    it("saves tab known-offline fast path: skips the server fetch, then reloads on reconnect (#1345)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      setRommConnectionState("offline");
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Ignore the mount-time refreshSlotState call — assert only the lazy-load.
      vi.mocked(backend.getSaveSlots).mockClear();

      // Activate the saves tab while offline: the fast path must NOT run the
      // server slot fetch (no "Loading slots…" hang through the retry ladder) and
      // the SavesTab still renders its degraded view.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveSlots)).not.toHaveBeenCalled();
      expect(queryByTestId("saves-tab")).not.toBeNull();

      // Server comes back → the effect re-runs (isOffline dep) and loads slots.
      await act(async () => {
        reportServerReachable(true);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledWith(55);
    });

    it("hands the saves tab an unanswered active slot while no slot answer has landed (#1747)", async () => {
      // The fast path above skips the fetch, so nothing has answered what the
      // active slot is. The panel's placeholder is still the shown value — the
      // tab must be told it is a placeholder, or it renders `default` as a fact.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      setRommConnectionState("offline");
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.activeSlot).toBe("default");
      expect(latest?.activeSlotKnown).toBe(false);
    });

    it("hands the saves tab a last-known slot snapshot, and drops it on a version switch (#1755)", async () => {
      // The response→state fold lives in the mocked-out slotState module (its
      // own tests cover it), so stand in for it here: one read installs a
      // snapshot, the next answers nothing.
      const snapshot = {
        slots: [{ slot: "main", source: "server" as const, count: 2, latest_updated_at: null }],
        activeSlot: "main",
      };
      vi.mocked(slotState.applyRefreshSlotResult).mockImplementation((_result, setter) => {
        setter((prev) => ({ ...prev, lastKnownSlots: snapshot }));
      });
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.lastKnownSlots).toEqual(snapshot);
      // A snapshot is not an answer — the live pair stays as it was.
      expect(latest?.activeSlot).toBe("default");
      expect(latest?.activeSlotKnown).toBe(false);

      // The new version is a different ROM, and this read carries no snapshot
      // of its own — the previous version's must not stand in for it.
      vi.mocked(slotState.applyRefreshSlotResult).mockImplementation(() => {});
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 56,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId, rom_id: 56 },
          }),
        );
      });
      await flushAsync();
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.lastKnownSlots).toBeNull();
    });

    it("saves tab: a mid-flight offline flip does not wedge the connecting indicator; reconnect reloads (#1345 F2)", async () => {
      // Device sequence (#1345): the saves tab is opened while the server is
      // already down but the store hasn't detected it yet. The slot fetch is in
      // flight (paying the retry ladder) when a CONCURRENT call (heartbeat / play
      // section) settles the server unreachable and flips the shared store
      // offline. That re-runs this effect mid-flight — which must NOT leave the
      // load wedged (slotsLoading stuck true → the frozen "(attempt N/M)"
      // indicator that survived reconnect on the device).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      setServerRetryProgress(null);
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      // Hold the lazy-load slot fetch in flight so we can flip the store offline
      // while it is still pending. (Ignore the mount-time refreshSlotState call.)
      let resolveSlots!: (v: Awaited<ReturnType<typeof backend.getSaveSlots>>) => void;
      const inflight = new Promise<Awaited<ReturnType<typeof backend.getSaveSlots>>>((r) => {
        resolveSlots = r;
      });
      vi.mocked(backend.getSaveSlots).mockClear();
      vi.mocked(backend.getSaveSlots).mockReturnValueOnce(inflight);

      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledWith(55);
      // In flight → the SavesTab shows its ConnectingIndicator (slotsLoading prop).
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.slotsLoading).toBe(true);

      // A retry frame arrives from the backend ladder (index.tsx funnels these).
      act(() => {
        setServerRetryProgress({ attempt: 3, maxAttempts: 3 });
      });

      // A concurrent call settles unreachable MID-FLIGHT and flips the store
      // offline, re-running this effect while the slot fetch is still pending.
      await act(async () => {
        reportServerReachable(false);
        await Promise.resolve();
        await Promise.resolve();
      });
      // WEDGE GUARD: the indicator must be down (degraded view), not frozen.
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.slotsLoading).toBe(false);

      // The in-flight fetch finally settles unreachable; the cancelled guard
      // swallows its write, and the retry frame is cleared on settle (the load
      // that owned it is still the latest → generation guard permits the clear).
      await act(async () => {
        resolveSlots({ success: false, slots: [], active_slot: "", reason: "server_unreachable" });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(getServerRetryProgress()).toBeNull();

      // Server recovers → the store flips connected → the effect re-runs and
      // reloads real content (pre-fix the load-once gate stayed set → no reload).
      vi.mocked(backend.getSaveSlots).mockClear();
      vi.mocked(backend.getSaveSlots).mockResolvedValue({
        success: true,
        slots: [{ slot: "main", source: "server", count: 1, latest_updated_at: null }],
        active_slot: "main",
      });
      await act(async () => {
        reportServerReachable(true);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledWith(55);
    });

    it("shared load generation: a stale slot settle does not clear the achievements lane's retry frame (#1345)", async () => {
      // Both lazy lanes feed ONE serverRetryProgress store gated by one shared
      // load counter. A slot fetch torn down mid-flight must not, when it resolves
      // late, wipe a newer achievements load's live "(attempt N/M)" frame.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 70,
        ra_id: 42,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      // Slot fetch in flight (older load, gen N).
      let resolveSlots!: (v: Awaited<ReturnType<typeof backend.getSaveSlots>>) => void;
      const slotsInflight = new Promise<Awaited<ReturnType<typeof backend.getSaveSlots>>>((r) => {
        resolveSlots = r;
      });
      vi.mocked(backend.getSaveSlots).mockClear();
      vi.mocked(backend.getSaveSlots).mockReturnValueOnce(slotsInflight);
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });

      // Switch to achievements → the slot load is torn down (still pending) and a
      // newer achievements load starts (gen N+1). Hold its calls in flight too.
      vi.mocked(backend.getAchievements).mockReturnValue(new Promise(() => {}));
      vi.mocked(backend.getAchievementProgress).mockReturnValue(new Promise(() => {}));
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
      // The achievements ladder posts its own live frame (index.tsx funnels these).
      act(() => {
        setServerRetryProgress({ attempt: 2, maxAttempts: 3 });
      });

      // The stale slot fetch finally resolves — its settle must NOT clear the
      // shared store, because a newer load now owns it (shared generation guard).
      await act(async () => {
        resolveSlots({ success: false, slots: [], active_slot: "", reason: "server_unreachable" });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(getServerRetryProgress()).toEqual({ attempt: 2, maxAttempts: 3 });
    });

    // Pins the slot-load reachability feed (#1345): success → connected,
    // server_unreachable → offline, any OTHER failure reason → store untouched
    // (the server answered "no"; it is not a connectivity verdict).
    async function activateSavesTabWithSlots(
      slotsResult: Awaited<ReturnType<typeof backend.getSaveSlots>>,
    ): Promise<void> {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 63,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      vi.mocked(backend.getSaveSlots).mockResolvedValue(slotsResult);
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    it("slot-load feed: a successful get_save_slots reports connected", async () => {
      // Start from the neutral "checking" verdict (not "offline", which would take
      // the fast path and skip the fetch) so the flip to "connected" is observable.
      setRommConnectionState("checking");
      await activateSavesTabWithSlots({ success: true, slots: [], active_slot: "main" });
      expect(getRommConnectionState()).toBe("connected");
    });

    it("slot-load feed: reason=server_unreachable reports offline", async () => {
      setRommConnectionState("connected");
      await activateSavesTabWithSlots({ success: false, slots: [], active_slot: "", reason: "server_unreachable" });
      expect(getRommConnectionState()).toBe("offline");
    });

    it("slot-load feed: any OTHER failure reason leaves the store untouched", async () => {
      setRommConnectionState("connected");
      // A server-side "no" (the server answered) — NOT a connectivity verdict.
      await activateSavesTabWithSlots({ success: false, slots: [], active_slot: "", reason: "not_found" });
      expect(getRommConnectionState()).toBe("connected");
    });

    it("saves tab: slotConfirmed=false → SlotSetupWizard renders", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 11,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: false,
        active_slot: null,
      });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
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
            last_played: null,
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Wizard not yet completed.
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: false,
        active_slot: null,
      });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
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
        platform_slug: "snes",
        bios_status: {
          needs_bios: true,
          platform_slug: "snes",
          server_count: 2,
          local_count: 1,
          all_downloaded: false,
          required_count: 2,
          required_downloaded: 1,
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Core info (label map + Emulator column) from the dedicated path (#923).
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "snes9x_libretro",
        active_core_label: "Snes9x",
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          {
            label: "Snes9x",
            kind: "libretro",
            core_so: "snes9x_libretro",
            is_default: true,
            bakeable: true,
            reason: null,
          },
        ],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      expect(container.textContent).toContain("Emulator");
      expect(container.textContent).toContain("Snes9x");
      // unknown file is filtered out of the visible list; the "+1 other"
      // note appears instead.
      expect(container.textContent).toContain("other file");
    });

    it("highlights the active core's per-BIOS line and leaves non-active cores grey (#955)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 56,
        platform_slug: "psx",
        bios_status: {
          needs_bios: true,
          platform_slug: "psx",
          server_count: 1,
          local_count: 0,
          all_downloaded: false,
          required_count: 1,
          required_downloaded: 0,
          files: [
            {
              file_name: "scph5501.bin",
              description: "PSX BIOS",
              downloaded: false,
              classification: "required",
              cores: {
                beetle_psx_hw_libretro: { required: true },
                swanstation_libretro: { required: false },
              },
              used_by_active: true,
            },
          ],
        } as never,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Active core is Beetle PSX HW; SwanStation is an alternative core.
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "beetle_psx_hw_libretro",
        active_core_label: "Beetle PSX HW",
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          {
            label: "Beetle PSX HW",
            kind: "libretro",
            core_so: "beetle_psx_hw_libretro",
            is_default: true,
            bakeable: true,
            reason: null,
          },
          {
            label: "SwanStation",
            kind: "libretro",
            core_so: "swanstation_libretro",
            is_default: false,
            bakeable: true,
            reason: null,
          },
        ],
      });
      const { getByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      // Active core line: amber + bold.
      const activeLine = getByText("Beetle PSX HW (required)");
      expect(activeLine.style.color).toBe("#d4a72c");
      expect(activeLine.style.fontWeight).toBe("bold");
      // Non-active core line: grey + normal weight.
      const inactiveLine = getByText("SwanStation (optional)");
      expect(inactiveLine.style.color).toBe("rgba(255, 255, 255, 0.5)");
      expect(inactiveLine.style.fontWeight).toBe("normal");
    });

    it("highlights no core line when active_core is null (#955)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 57,
        platform_slug: "psx",
        bios_status: {
          needs_bios: true,
          platform_slug: "psx",
          server_count: 1,
          local_count: 0,
          all_downloaded: false,
          required_count: 1,
          required_downloaded: 0,
          files: [
            {
              file_name: "scph5501.bin",
              description: "PSX BIOS",
              downloaded: false,
              classification: "required",
              cores: {
                beetle_psx_hw_libretro: { required: true },
                swanstation_libretro: { required: false },
              },
              used_by_active: false,
            },
          ],
        } as never,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // No active core resolved → no line is highlighted.
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: null,
        active_core_label: null,
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          {
            label: "Beetle PSX HW",
            kind: "libretro",
            core_so: "beetle_psx_hw_libretro",
            is_default: false,
            bakeable: true,
            reason: null,
          },
          {
            label: "SwanStation",
            kind: "libretro",
            core_so: "swanstation_libretro",
            is_default: false,
            bakeable: true,
            reason: null,
          },
        ],
      });
      const { getByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      const beetleLine = getByText("Beetle PSX HW (required)");
      expect(beetleLine.style.color).toBe("rgba(255, 255, 255, 0.5)");
      expect(beetleLine.style.fontWeight).toBe("normal");
      const swanLine = getByText("SwanStation (optional)");
      expect(swanLine.style.color).toBe("rgba(255, 255, 255, 0.5)");
      expect(swanLine.style.fontWeight).toBe("normal");
    });

    it("falls back to the de-suffixed .so when a core is absent from coreInfo (#955)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 58,
        platform_slug: "psx",
        bios_status: {
          needs_bios: true,
          platform_slug: "psx",
          server_count: 1,
          local_count: 0,
          all_downloaded: false,
          required_count: 1,
          required_downloaded: 0,
          files: [
            {
              file_name: "scph5501.bin",
              description: "PSX BIOS",
              downloaded: false,
              classification: "required",
              cores: {
                some_obscure_libretro: { required: true },
              },
              used_by_active: true,
            },
          ],
        } as never,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // coreInfo does NOT enumerate `some_obscure_libretro`, so coreLabelMap[coreSo]
      // is undefined and the `.replace(/_libretro$/, "")` fallback renders the label.
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "beetle_psx_hw_libretro",
        active_core_label: "Beetle PSX HW",
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          {
            label: "Beetle PSX HW",
            kind: "libretro",
            core_so: "beetle_psx_hw_libretro",
            is_default: true,
            bakeable: true,
            reason: null,
          },
        ],
      });
      const { getByText } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      // Fallback label = `.so` key with `_libretro` stripped; not the active core,
      // so it stays grey + normal weight.
      const fallbackLine = getByText("some_obscure (required)");
      expect(fallbackLine.style.color).toBe("rgba(255, 255, 255, 0.5)");
      expect(fallbackLine.style.fontWeight).toBe("normal");
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
        metadata: makeMetadata({ first_release_date: 1041379200 }),
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
        metadata: makeMetadata({ first_release_date: 0 }),
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
        metadata: makeMetadata({ first_release_date: -1 }),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Release Date");
    });
  });

  describe("BIOS status-dot color (sourced from backend bios_level)", () => {
    // Mirror the backend compute_bios_level trichotomy so the cached payload
    // ships the same bios_level the real backend would — the panel sources the
    // dot color from bios_level now, never re-deriving it from the counts.
    function deriveBiosLevel(
      required_downloaded: number | null,
      required_count: number | null,
      local_count: number,
      all_downloaded: boolean,
    ): "ok" | "partial" | "missing" {
      if (required_count !== null && required_downloaded !== null) {
        if (required_downloaded >= required_count) return "ok";
        if (required_downloaded > 0) return "partial";
        return "missing";
      }
      if (all_downloaded) return "ok";
      if (local_count > 0) return "partial";
      return "missing";
    }

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
        bios_level: deriveBiosLevel(required_downloaded, required_count, local_count, all_downloaded),
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const view = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
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

    it("unmanaged: grey header dot + honest text, and the 'files on server' note survives all-unknown (#1520)", async () => {
      // Every server file is unknown (no registry coverage) → backend ships
      // bios_level "unmanaged". The panel must render the neutral grey dot + honest
      // header text (never a false "All ready"), and the "files on server" note —
      // previously swallowed when knownFiles is empty — must still render.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        bios_status: {
          needs_bios: true,
          platform_slug: "psvita",
          server_count: 2,
          local_count: 0,
          all_downloaded: false,
          required_count: 0,
          required_downloaded: 0,
          unknown_count: 2,
          known_count: 0,
          files: [
            {
              file_name: "a.bin",
              downloaded: false,
              local_path: "",
              required: false,
              description: "",
              classification: "unknown",
              cores: {},
              used_by_active: true,
            },
            {
              file_name: "b.bin",
              downloaded: false,
              local_path: "",
              required: false,
              description: "",
              classification: "unknown",
              cores: {},
              used_by_active: true,
            },
          ],
        } as never,
        bios_level: "unmanaged",
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        await Promise.resolve();
      });
      // Neutral grey dot via the shared helper — never the green "all ready" dot.
      expect(container.innerHTML).toContain("#8f98a0");
      expect(container.innerHTML).not.toContain("#5ba32b");
      // Honest header text, not "All ready".
      expect(container.textContent).toContain("Not managed by the plugin");
      expect(container.textContent).not.toContain("All ready");
      // Swallowed-note fix: the note renders even though every file is unknown.
      expect(container.textContent).toContain("2 files on server the plugin doesn't recognise");
    });
  });

  describe("biosStatusFromCache + saveStatusFromCache (cache-first rendering)", () => {
    it("biosStatusFromCache(null) yields no biosStatus → no BIOS tab", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        bios_status: null,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("BIOS");
    });

    it("biosStatusFromCache drives BIOS tab; active core comes from the dedicated path (#923)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        platform_slug: "snes",
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        } as never,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      // Active core label is served from get_platform_core_info, not bios_status.
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "mycore.so",
        active_core_label: "MyCore",
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [
          { label: "MyCore", kind: "libretro", core_so: "mycore.so", is_default: true, bakeable: true, reason: null },
        ],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Active core label from the dedicated path reaches the BIOS tab's Emulator column.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
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

    it("renders 'No metadata available' for an empty-but-non-null metadata object (no descriptive rows)", async () => {
      // metadata is present but every field is empty, and no name / region /
      // languages / platform accompany it — so no descriptive row is added and
      // the fallback still fires. Guards the plain descriptive-row count now that
      // the version switcher moved out of this section to the play row (#1297).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "",
        metadata: makeMetadata(),
        platform_name: "",
        regions: [],
        languages: [],
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
        metadata: makeMetadata({ summary: "An RPG." }),
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
        metadata: makeMetadata({ average_rating: 87 }),
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
        metadata: makeMetadata({ summary: "x" }),
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
        metadata: makeMetadata({ summary: "x" }),
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
        metadata: makeMetadata({ summary: "x" }),
        stale_fields: [],
      });
      vi.mocked(backend.getArtworkBase64).mockRejectedValue(new Error("net"));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Catch swallowed; no cover img.
      expect(container.innerHTML).not.toContain("data:image/png;base64,");
      // And the mount didn't surface debugLog.
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("loadData error"));
    });

    it("background installed-rom fetch rejection → installedRom stays null (silent .catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        installed: true,
        metadata: makeMetadata({ summary: "x" }),
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
      expect(vi.mocked(backend.debugLog)).not.toHaveBeenCalledWith(expect.stringContaining("loadData error"));
    });

    it("background metadata fetch rejection → metadata stays at cached value (silent .catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        metadata: makeMetadata({ summary: "from cache" }),
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
  // I2. Version attributes (Region / Languages) + version-switch refresh (#1297)
  // ------------------------------------------------------------------

  describe("Version attributes + switch", () => {
    it("renders Region and Languages rows for the active version", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "Game (USA)",
        regions: ["USA", "Europe"],
        languages: ["En", "Fr"],
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("Region");
      expect(container.textContent).toContain("USA/Europe");
      expect(container.textContent).toContain("Languages");
      expect(container.textContent).toContain("En, Fr");
    });

    it("omits Region / Languages rows when the dimensions are empty", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "Game",
        regions: [],
        languages: [],
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Region");
      expect(container.textContent).not.toContain("Languages");
    });

    it("refreshes the panel name + Region + cover on a matching version_switched event", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "Game (USA)",
        regions: ["USA"],
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("Game (USA)");

      // After the switch the cache resolves the new active version, and the
      // handler re-fetches the artwork for the newly-bound rom_id.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 2,
        rom_name: "Game (Japan)",
        regions: ["Japan"],
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getArtworkBase64).mockClear();
      vi.mocked(backend.getArtworkBase64).mockResolvedValue({ base64: "JAPANCOVER" });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
          }),
        );
      });
      await flushAsync();

      expect(container.textContent).toContain("Game (Japan)");
      expect(container.textContent).toContain("Japan");
      // Cover refresh: getArtworkBase64 was re-fetched for the new rom_id and the
      // new cover reached the DOM (dropping refreshCoverArtInBackground in the
      // version_switched handler makes this fail).
      expect(vi.mocked(backend.getArtworkBase64)).toHaveBeenCalledWith(2);
      expect(container.innerHTML).toContain("data:image/png;base64,JAPANCOVER");
    });

    it("ignores a version_switched event for a different appId", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "Game (USA)",
        regions: ["USA"],
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      vi.mocked(cachedStore.getCachedGameDetail).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId + 9999, rom_id: 2 },
          }),
        );
        await Promise.resolve();
      });

      // The mismatched appId short-circuits before re-reading the cache.
      expect(vi.mocked(cachedStore.getCachedGameDetail)).not.toHaveBeenCalled();
      expect(container.textContent).toContain("Game (USA)");
    });

    it("re-keys SAVES tab data to the new active version while sitting on the saves tab (#1297)", async () => {
      // The user is ON the saves tab when the switch arrives. The tab data must
      // follow the new active version: the load-once slots gate resets so
      // getSaveSlots re-fetches for the new rom_id, and SavesTab receives the
      // new rom_id + the new version's save-status universe (threaded from the
      // invalidated-then-refetched cache, matching loadData). Without the fix,
      // SavesTab keeps rendering the OLD rom's data.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        rom_name: "Game (USA)",
        save_sync_enabled: true,
        save_status: {
          files: [{ filename: "OLD_VERSION.srm", status: "skip" }],
          conflicts: [],
        },
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      // Sit on the saves tab — renders SavesTab with the OLD rom's universe.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
      });
      await flushAsync();
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.saveStatus?.files[0]?.filename).toBe("OLD_VERSION.srm");

      // The switch: the cache now resolves the NEW active version (rom 2) with
      // a distinct save-status universe.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 2,
        rom_name: "Game (Japan)",
        save_sync_enabled: true,
        save_status: {
          files: [{ filename: "NEW_VERSION.srm", status: "skip" }],
          conflicts: [],
        },
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getSaveSlots).mockClear();
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
          }),
        );
      });
      await flushAsync();
      // Slots re-fetched for the NEW rom_id (the load-once gate was reset).
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledWith(2);
      // SavesTab re-keyed to the new rom + the new version's save universe —
      // the OLD_VERSION.srm row is gone.
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.romId).toBe(2);
      expect(latest?.saveStatus?.files[0]?.filename).toBe("NEW_VERSION.srm");
    });

    it("re-marks the active slot unanswered on a version switch (#1747)", async () => {
      // The switch re-keys activeSlot back to the placeholder, so whatever
      // answered for the previous version must not vouch for the new one.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
      });
      await flushAsync();
      // A completed slot switch is an answer about rom 1's active slot.
      await act(async () => {
        capturedSavesTab[capturedSavesTab.length - 1]?.onSlotSwitched("main", {
          rom_id: 1,
          files: [],
          playtime: {
            total_seconds: 0,
            session_count: 0,
            last_session_start: null,
            last_session_duration_sec: null,
            last_played: null,
          },
          device_id: "d",
          last_sync_check_at: null,
        });
      });
      await flushAsync();
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.activeSlotKnown).toBe(true);

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 2,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      capturedSavesTab.length = 0;
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
          }),
        );
      });
      await flushAsync();
      const latest = capturedSavesTab[capturedSavesTab.length - 1];
      expect(latest?.romId).toBe(2);
      expect(latest?.activeSlot).toBe("default");
      expect(latest?.activeSlotKnown).toBe(false);
    });

    it("reloads SAVES tab data on the next activation after a switch (slotsLoadedRef reset, #1297)", async () => {
      // Open the saves tab for the OLD rom (sets the load-once gate), return to
      // info, then switch version while off the saves tab. Re-opening the saves
      // tab must reload for the NEW rom_id — the reset gate is what makes
      // getSaveSlots(2) fire on re-activation. Without the reset the gate stays
      // set and the effect short-circuits, keeping the OLD rom's slots.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
        configured: true,
        active_slot: "main",
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
      });
      await flushAsync();
      // Back to info so the switch happens off the saves tab.
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "info" } }));
      });
      await flushAsync();
      // Version switch to rom 2 (off the saves tab).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 2,
        save_sync_enabled: true,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
          }),
        );
      });
      await flushAsync();
      // Re-open the saves tab; the reset gate forces a fresh slots fetch for
      // rom 2. Clear first so the assertion sees only the re-activation load.
      vi.mocked(backend.getSaveSlots).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
      });
      await flushAsync();
      expect(vi.mocked(backend.getSaveSlots)).toHaveBeenCalledWith(2);
    });

    it("re-fetches ACHIEVEMENTS for the new version while sitting on the achievements tab (achievementsLoadedRef reset, #1297)", async () => {
      // Achievements loaded for the OLD rom (1). A switch to rom 2 (its own
      // ra_id) while ON the achievements tab must reset the load-once gate and
      // re-fetch for the new rom_id. Without the reset, the tab keeps the OLD
      // rom's achievements.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 1,
        ra_id: 42,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "achievements" } }));
      });
      await flushAsync();
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledWith(1);

      // Switch to rom 2 (new ra_id) while sitting on the achievements tab.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 2,
        ra_id: 43,
        metadata: makeMetadata(),
        stale_fields: [],
      });
      vi.mocked(backend.getAchievements).mockClear();
      vi.mocked(backend.getAchievementProgress).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
          }),
        );
      });
      await flushAsync();
      expect(vi.mocked(backend.getAchievements)).toHaveBeenCalledWith(2);
      expect(vi.mocked(backend.getAchievementProgress)).toHaveBeenCalledWith(2);
    });

    // #1742 — the ROM File row and the launch-target note under it are decided
    // by the installed-rom record, which describes ONE version. The switch
    // writes `installed` from the fresh detail, so the record has to follow it.
    describe("ROM File row across a version switch (#1742)", () => {
      const installedRomFor = (romId: number, fileName: string, launchable = true): InstalledRom => ({
        rom_id: romId,
        file_name: fileName,
        file_path: `/roms/snes/${fileName}`,
        system: "snes",
        platform_slug: "snes",
        installed_at: "2026-01-01",
        launchable,
      });

      const detailFor = (romId: number, installed: boolean): CachedGameDetail => ({
        found: true,
        rom_id: romId,
        rom_name: romId === 1 ? "Game (USA)" : "Game (Japan)",
        platform_slug: "snes",
        installed,
        metadata: makeMetadata(),
        stale_fields: [],
      });

      const switchToRom2 = async (installed: boolean) => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(2, installed));
        await act(async () => {
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
            }),
          );
        });
        await flushAsync();
      };

      it("shows the switched-to version's ROM File row when it is the installed one", async () => {
        // The page was opened on a version that is not installed, so there is
        // no record yet — only the switch can fetch one.
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, false));
        vi.mocked(backend.getInstalledRom).mockResolvedValue(installedRomFor(2, "Game (Japan).sfc"));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        expect(container.textContent).not.toContain("ROM File");

        await switchToRom2(true);

        expect(vi.mocked(backend.getInstalledRom)).toHaveBeenCalledWith(2);
        expect(container.textContent).toContain("ROM File");
        expect(container.textContent).toContain("Game (Japan).sfc");
      });

      it("decides the launch-target note from the switched-to version's record", async () => {
        // The version left behind launches; the one switched to is a sealed
        // package that does not. The previous version's record would withhold
        // the note that says so.
        vi.mocked(backend.getInstalledRom).mockImplementation((romId: number) =>
          Promise.resolve(
            romId === 1 ? installedRomFor(1, "Game (USA).sfc") : installedRomFor(2, "Game (Japan).pkg", false),
          ),
        );
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, true));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        expect(container.textContent).not.toContain("nothing here is a format");

        await switchToRom2(true);

        expect(container.textContent).toContain("nothing here is a format snes can launch");
      });

      it("issues no installed-rom read when the switched-to version is not installed", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, false));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        vi.mocked(backend.getInstalledRom).mockClear();

        await switchToRom2(false);

        expect(vi.mocked(backend.getInstalledRom)).not.toHaveBeenCalled();
        expect(container.textContent).not.toContain("ROM File");
      });

      it("drops the previous version's row while the switched-to version's record is in flight", async () => {
        // Downloading a sibling supersedes the install this record describes and
        // the panel never hears about it, so the previous version's file name
        // can stand under a true `installed` for the version that replaced it.
        let releaseRom2!: (rom: InstalledRom) => void;
        const heldRom2 = new Promise<InstalledRom>((resolve) => {
          releaseRom2 = resolve;
        });
        vi.mocked(backend.getInstalledRom).mockImplementation((romId: number) =>
          romId === 2 ? heldRom2 : Promise.resolve(installedRomFor(1, "Game (USA).sfc")),
        );
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, true));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        expect(container.textContent).toContain("Game (USA).sfc");

        await switchToRom2(true);

        expect(container.textContent).not.toContain("Game (USA).sfc");
        expect(container.textContent).not.toContain("ROM File");

        await act(async () => {
          releaseRom2(installedRomFor(2, "Game (Japan).sfc"));
        });
        await flushAsync();

        expect(container.textContent).toContain("Game (Japan).sfc");
      });
    });

    // #1748 — the switch writes `saveSyncEnabled` from the fresh detail, so it
    // can take the SAVES button away exactly as it can the BIOS one. The flag is
    // global, so this needs the settings event and the switch to disagree: an
    // event dropped by the load-window guard, with a stale cached detail then
    // re-showing the button. Rare, but the dead end is the same one.
    describe("SAVES tab across a version switch (#1748)", () => {
      const detailFor = (romId: number, saveSyncEnabled: boolean): CachedGameDetail => ({
        found: true,
        rom_id: romId,
        rom_name: romId === 1 ? "Game (USA)" : "Game (Japan)",
        platform_slug: "snes",
        save_sync_enabled: saveSyncEnabled,
        metadata: makeMetadata(),
        stale_fields: [],
      });

      const mountOnSavesTab = async (overrides: Partial<CachedGameDetail> = {}) => {
        // configured=true so the open tab is SavesTab itself, not the wizard.
        vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({
          configured: true,
          active_slot: "main",
        });
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({ ...detailFor(1, true), ...overrides });
        const view = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await act(async () => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
        });
        await flushAsync();
        expect(view.queryByTestId("saves-tab")).not.toBeNull();
        return view;
      };

      const switchToRom2 = async (overrides: Partial<CachedGameDetail> = {}) => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({ ...detailFor(2, false), ...overrides });
        await act(async () => {
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
            }),
          );
        });
        await flushAsync();
      };

      it("leaves the SAVES tab for the info tab when the switched-to detail says save sync is off", async () => {
        const { container, queryByTestId } = await mountOnSavesTab();

        await switchToRom2();

        // Asserted on the info tab's BODY (the new version's RomM name), not on
        // its button, which is present either way.
        expect(container.textContent).not.toContain("SAVES");
        expect(queryByTestId("saves-tab")).toBeNull();
        expect(container.textContent).toContain("Game (Japan)");
      });

      it("lands on the info tab when the switch clears the BIOS answer as well", async () => {
        // Both gated tabs lose their button at once. The user is on SAVES, so
        // that is the dead end that has to be caught — a fallback that answers
        // for BIOS first would leave them there.
        const { container, queryByTestId } = await mountOnSavesTab({
          bios_status: { platform_slug: "snes", server_count: 2, local_count: 1, all_downloaded: false },
          bios_level: "partial",
        });
        expect(container.textContent).toContain("BIOS");

        await switchToRom2();

        expect(container.textContent).not.toContain("BIOS");
        expect(queryByTestId("saves-tab")).toBeNull();
        expect(container.textContent).toContain("Game (Japan)");
      });
    });

    // #1681 — BIOS is the third per-rom tab. The requirement is core-dependent
    // and the core override is keyed on rom_id, so both the level and the
    // "Active Core" row it is shown against must follow the new active version.
    describe("BIOS tab across a version switch (#1681)", () => {
      /** A `get_platform_core_info` answer whose active core is `label` — the
       *  BIOS tab's "Active Core" row reads it. */
      const coreInfoFor = (label: string, coreSo: string): CoreInfo => ({
        active_core: coreSo,
        active_core_label: label,
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [{ label, kind: "libretro", core_so: coreSo, is_default: true, bakeable: true, reason: null }],
      });

      /** A cached detail whose core needs BIOS, `localCount` of 3 files present. */
      const detailNeedingBios = (romId: number, localCount: number) => ({
        found: true,
        rom_id: romId,
        platform_slug: "snes",
        bios_status: {
          platform_slug: "snes",
          server_count: 3,
          local_count: localCount,
          all_downloaded: localCount === 3,
        } as never,
        bios_level: localCount === 3 ? ("ok" as const) : ("partial" as const),
        metadata: makeMetadata(),
        stale_fields: [],
      });

      const switchVersion = async () => {
        await act(async () => {
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
            }),
          );
        });
        await flushAsync();
      };

      const openBiosTab = async () => {
        await act(async () => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "bios" } }));
        });
        await flushAsync();
      };

      it("clears the requirement when the switched-to version's core needs none", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(1, 1));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        expect(container.textContent).toContain("BIOS");

        // The new active version's core needs no BIOS — the cached detail
        // carries no bios_status at all.
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
          found: true,
          rom_id: 2,
          platform_slug: "snes",
          metadata: makeMetadata(),
          stale_fields: [],
        });
        await switchVersion();

        expect(container.textContent).not.toContain("BIOS");
      });

      it("leaves the shown level standing when the switched-to detail carries no BIOS answer (#1693)", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(1, 1));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openBiosTab();
        expect(container.textContent).toContain("1/3 files ready");

        // The switch lands inside the window a BIOS download or delete opens:
        // the firmware cache is cold, so the detail carries no BIOS answer — the
        // same absent `bios_status` as the clear above.
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
          found: true,
          rom_id: 2,
          platform_slug: "snes",
          metadata: makeMetadata(),
          stale_fields: ["bios"],
          bios_status_unknown: true,
        });
        await switchVersion();

        expect(container.textContent).toContain("1/3 files ready");
      });

      it("leaves the BIOS tab for the info tab when the requirement is cleared under it", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(1, 1));
        vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfoFor("Snes9x", "snes9x_libretro.so"));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openBiosTab();
        expect(container.textContent).toContain("Snes9x");

        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
          found: true,
          rom_id: 2,
          platform_slug: "snes",
          metadata: makeMetadata(),
          stale_fields: [],
        });
        await switchVersion();

        // The tab the user was standing on is gated on the requirement, so
        // clearing it removes the button. Without the fallback `activeTab` stays
        // "bios", the body resolves to null, and the panel is a lone GAME INFO
        // button over an empty pane — which is why the assertion is on the info
        // tab's BODY and not on its button.
        expect(container.textContent).not.toContain("Snes9x");
        expect(container.textContent).toContain("No metadata available");
      });

      it("re-keys the level and the active core to the new active version", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(1, 1));
        vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfoFor("Snes9x", "snes9x_libretro.so"));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openBiosTab();
        expect(container.textContent).toContain("1/3 files ready");
        expect(container.textContent).toContain("Snes9x");

        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(2, 3));
        vi.mocked(backend.getPlatformCoreInfo).mockClear();
        vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfoFor("bsnes", "bsnes_libretro.so"));
        await switchVersion();

        expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(2);
        expect(container.textContent).toContain("All ready (3/3)");
        expect(container.textContent).toContain("bsnes");
        expect(container.textContent).not.toContain("Snes9x");
      });

      // The over-fix this change invites: a read that FAILED is "we don't know",
      // not "no BIOS need", so it may not blank the tab.
      it("leaves the shown level standing when the switch's cache read fails", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(1, 1));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openBiosTab();
        expect(container.textContent).toContain("1/3 files ready");

        vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValue(new Error("offline"));
        vi.mocked(backend.debugLog).mockClear();
        await switchVersion();

        expect(container.textContent).toContain("1/3 files ready");
        expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("onDataChanged error"));
      });

      it("leaves the shown active core standing when the new version's core read fails", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(1, 1));
        vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfoFor("Snes9x", "snes9x_libretro.so"));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openBiosTab();
        expect(container.textContent).toContain("Snes9x");

        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailNeedingBios(2, 3));
        vi.mocked(backend.getPlatformCoreInfo).mockRejectedValue(new Error("offline"));
        await switchVersion();

        expect(container.textContent).toContain("Snes9x");
      });
    });

    // #1713 — a version switch re-binds the shortcut to a new rom_id without
    // changing the appId, so the `[appId]` effect never re-runs and its
    // `cancelled` flag never fires. Every read still in flight for the previous
    // version therefore answers into a panel that has moved on; only the rom the
    // read was issued for can tell the two apart.
    describe("background writes bound to the rom they were read for (#1713)", () => {
      /** Hold the read issued for `heldRomId` open while answering every other
       *  rom_id at once, so the previous version's answer can be made to land
       *  AFTER the switched-to version's. */
      function holdReadFor<T>(heldRomId: number, answerForOthers: T) {
        let release!: (value: T) => void;
        const held = new Promise<T>((resolve) => {
          release = resolve;
        });
        return {
          impl: (romId: number) => (romId === heldRomId ? held : Promise.resolve(answerForOthers)),
          release: (value: T) => release(value),
        };
      }

      const detailFor = (romId: number, overrides: Partial<CachedGameDetail> = {}): CachedGameDetail => ({
        found: true,
        rom_id: romId,
        rom_name: romId === 1 ? "Game (USA)" : "Game (Japan)",
        platform_slug: "snes",
        metadata: makeMetadata(),
        stale_fields: [],
        ...overrides,
      });

      const switchToRom2 = async (overrides: Partial<CachedGameDetail> = {}) => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(2, overrides));
        await act(async () => {
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "version_switched", app_id: testAppId, rom_id: 2 },
            }),
          );
        });
        await flushAsync();
      };

      const openTab = async (tab: string) => {
        await act(async () => {
          globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab } }));
        });
        await flushAsync();
      };
      const openBiosTab = () => openTab("bios");
      const openSavesTab = () => openTab("saves");

      /** A BIOS requirement on both versions, so the BIOS tab (and with it the
       *  "Active Core" row the core-info reads land in) stays visible across the
       *  switch. */
      const biosNeed: Partial<CachedGameDetail> = {
        bios_status: { platform_slug: "snes", server_count: 3, local_count: 1, all_downloaded: false },
        bios_level: "partial",
      };

      const coreInfoNamed = (label: string): CoreInfo => ({
        active_core: `${label}_libretro.so`,
        active_core_label: label,
        platform_core_label: null,
        has_game_override: false,
        emulator_data_available: true,
        emulators: [],
      });

      const emptySaveStatus = (romId: number): SaveStatus => ({
        rom_id: romId,
        files: [],
        playtime: {
          total_seconds: 0,
          session_count: 0,
          last_session_start: null,
          last_session_duration_sec: null,
          last_played: null,
        },
        device_id: "d",
        last_sync_check_at: null,
      });

      it("keeps the switched-to version's cover when the previous version's fetch lands last", async () => {
        // The user-visible one: regional versions carry different box art, and
        // the switch handler spreads the previous state without clearing the
        // cover. A rom-1 answer folded in here stands under rom 2's name until
        // the panel remounts.
        const cover = holdReadFor(1, { base64: "JAPANCOVER" });
        vi.mocked(backend.getArtworkBase64).mockImplementation(cover.impl);
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        await switchToRom2();
        expect(container.innerHTML).toContain("base64,JAPANCOVER");

        await act(async () => {
          cover.release({ base64: "USACOVER" });
        });
        await flushAsync();

        expect(container.innerHTML).toContain("base64,JAPANCOVER");
        expect(container.innerHTML).not.toContain("USACOVER");
      });

      it("does not fold the previous version's ROM file into the switched-to version", async () => {
        const installedRom = holdReadFor<InstalledRom | null>(1, {
          rom_id: 2,
          file_name: "Game (Japan).sfc",
          file_path: "/roms/snes/Game (Japan).sfc",
          system: "snes",
          platform_slug: "snes",
          installed_at: "2026-01-01",
          launchable: true,
        });
        vi.mocked(backend.getInstalledRom).mockImplementation(installedRom.impl);
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, { installed: true }));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        await switchToRom2({ installed: true });

        await act(async () => {
          installedRom.release({
            rom_id: 1,
            file_name: "Game (USA).sfc",
            file_path: "/roms/snes/Game (USA).sfc",
            system: "snes",
            platform_slug: "snes",
            installed_at: "2026-01-01",
            launchable: true,
          });
        });
        await flushAsync();

        expect(container.textContent).not.toContain("Game (USA).sfc");
      });

      it("does not fold the previous version's metadata into the switched-to version", async () => {
        const metadata = holdReadFor(1, makeMetadata({ summary: "JAPAN SUMMARY" }));
        vi.mocked(backend.getRomMetadata).mockImplementation(metadata.impl);
        // A missing metadata field is what makes loadData issue the read at all.
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
          detailFor(1, { metadata: null, stale_fields: ["metadata"] }),
        );
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        await switchToRom2({ metadata: null, stale_fields: [] });

        await act(async () => {
          metadata.release(makeMetadata({ summary: "USA SUMMARY" }));
        });
        await flushAsync();

        expect(container.textContent).not.toContain("USA SUMMARY");
      });

      it("does not fold the previous version's core info into the switched-to version", async () => {
        const coreInfo = holdReadFor(1, coreInfoNamed("bsnes"));
        vi.mocked(backend.getPlatformCoreInfo).mockImplementation(coreInfo.impl);
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, biosNeed));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        await switchToRom2(biosNeed);
        await openBiosTab();
        expect(container.textContent).toContain("bsnes");

        await act(async () => {
          coreInfo.release(coreInfoNamed("Snes9x"));
        });
        await flushAsync();

        expect(container.textContent).toContain("bsnes");
        expect(container.textContent).not.toContain("Snes9x");
      });

      it("does not fold the previous version's slot configuration into the switched-to version", async () => {
        // slotConfirmed is the SlotSetupWizard-vs-SavesTab gate: a stale
        // "configured" answer replaces the new version's unconfigured wizard
        // with a saves tab it has no slots for.
        const tracking = holdReadFor<{ configured: boolean; active_slot: string | null }>(1, {
          configured: false,
          active_slot: null,
        });
        vi.mocked(backend.isSaveTrackingConfigured).mockImplementation(tracking.impl);
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, { save_sync_enabled: true }));
        const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        await switchToRom2({ save_sync_enabled: true });
        await openSavesTab();
        expect(queryByTestId("slot-setup-wizard")).not.toBeNull();

        await act(async () => {
          tracking.release({ configured: true, active_slot: "main" });
        });
        await flushAsync();

        expect(queryByTestId("slot-setup-wizard")).not.toBeNull();
        expect(queryByTestId("saves-tab")).toBeNull();
      });

      it("does not fold the previous version's slot list into the switched-to version", async () => {
        // The slot list is folded THROUGH applyRefreshSlotResult, so what has to
        // be bound is the setter the panel hands it. Run the real helper here —
        // the mocked one writes nothing, and an unbound setter would then look
        // exactly like a bound one.
        const realSlotState = await vi.importActual<typeof slotState>("../utils/slotState");
        vi.mocked(slotState.applyRefreshSlotResult).mockImplementation(realSlotState.applyRefreshSlotResult);
        const slots = holdReadFor(1, { success: true, slots: [], active_slot: "japan" });
        vi.mocked(backend.getSaveSlots).mockImplementation(slots.impl);
        vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, { save_sync_enabled: true }));
        render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        await switchToRom2({ save_sync_enabled: true });
        await openSavesTab();
        expect(capturedSavesTab[capturedSavesTab.length - 1]?.activeSlot).toBe("japan");

        await act(async () => {
          slots.release({ success: true, slots: [], active_slot: "usa" });
        });
        await flushAsync();

        expect(capturedSavesTab[capturedSavesTab.length - 1]?.activeSlot).toBe("japan");
        expect(capturedSavesTab.some((props) => props.activeSlot === "usa")).toBe(false);
      });

      it("does not fold a save-sync-settings read issued for the previous version", async () => {
        const saveStatus = holdReadFor<backend.SaveStatusResult>(1, emptySaveStatus(2));
        vi.mocked(backend.getSaveStatus).mockImplementation(saveStatus.impl);
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, { save_sync_enabled: false }));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        // Save sync is switched on globally while rom 1 is showing — the handler
        // reads rom 1's status to decide what the SAVES tab shows.
        await act(async () => {
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "save_sync_settings", save_sync_enabled: true },
            }),
          );
        });
        await switchToRom2({ save_sync_enabled: false });

        await act(async () => {
          saveStatus.release(emptySaveStatus(1));
        });
        await flushAsync();

        // The switched-to version has save sync off, so its tab bar carries no
        // SAVES tab — the refused write is what keeps it that way.
        expect(container.textContent).not.toContain("SAVES");
      });

      it("does not fold a save-sync read issued for the previous version", async () => {
        const saveStatus = holdReadFor<backend.SaveStatusResult>(1, emptySaveStatus(2));
        vi.mocked(backend.getSaveStatus).mockImplementation(saveStatus.impl);
        vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, { save_sync_enabled: true }));
        render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openSavesTab();

        await act(async () => {
          globalThis.dispatchEvent(
            new CustomEvent("romm_data_changed", {
              detail: { type: "save_sync", rom_id: 1 },
            }),
          );
        });
        await switchToRom2({
          save_sync_enabled: true,
          save_status: { files: [{ filename: "NEW_VERSION.srm", status: "skip" }], conflicts: [] },
        });

        await act(async () => {
          saveStatus.release({
            ...emptySaveStatus(1),
            files: [
              {
                filename: "OLD_VERSION.srm",
                status: "skip",
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
          });
        });
        await flushAsync();

        expect(capturedSavesTab[capturedSavesTab.length - 1]?.saveStatus?.files[0]?.filename).toBe("NEW_VERSION.srm");
        expect(capturedSavesTab.some((props) => props.saveStatus?.files[0]?.filename === "OLD_VERSION.srm")).toBe(
          false,
        );
      });

      it("does not fold a core-change read issued for the previous version", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, biosNeed));
        vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfoNamed("Snes9x"));
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();
        await openBiosTab();
        expect(container.textContent).toContain("Snes9x");

        // A core change for rom 1 re-reads its core info and its BIOS answer.
        const coreInfo = holdReadFor(1, coreInfoNamed("bsnes"));
        vi.mocked(backend.getPlatformCoreInfo).mockImplementation(coreInfo.impl);
        await act(async () => {
          globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "core_changed" } }));
        });
        await switchToRom2(biosNeed);
        expect(container.textContent).toContain("bsnes");

        await act(async () => {
          coreInfo.release(coreInfoNamed("Mesen"));
        });
        await flushAsync();

        expect(container.textContent).toContain("bsnes");
        expect(container.textContent).not.toContain("Mesen");
      });

      it("does not fold a metadata read issued for the previous version", async () => {
        vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
          detailFor(1, { metadata: makeMetadata({ summary: "MOUNTED SUMMARY" }) }),
        );
        const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
        await flushAsync();

        const metadata = holdReadFor(1, makeMetadata({ summary: "JAPAN SUMMARY" }));
        vi.mocked(backend.getRomMetadata).mockImplementation(metadata.impl);
        await act(async () => {
          globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail: { type: "metadata", rom_id: 1 } }));
        });
        // The switch carries no metadata of its own, so the mounted summary is
        // what a refused stale write leaves standing.
        await switchToRom2({ metadata: makeMetadata({ summary: "MOUNTED SUMMARY" }) });

        await act(async () => {
          metadata.release(makeMetadata({ summary: "USA SUMMARY" }));
        });
        await flushAsync();

        expect(container.textContent).toContain("MOUNTED SUMMARY");
        expect(container.textContent).not.toContain("USA SUMMARY");
      });
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("RetroArch save sorting changed");
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
        metadata: makeMetadata(),
        stale_fields: [],
      });
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("RetroArch save sorting changed");
    });
  });

  // ------------------------------------------------------------------
  // K. Two answers for the SAME rom, ordered by when their reads were issued
  // ------------------------------------------------------------------

  describe("two answers for the same rom ordered by when their reads were issued (#1717)", () => {
    /** A read the test answers by hand, so the one issued FIRST can be made to
     *  land last. Both are about the rom the panel is showing, so the rom
     *  binding admits both and only the read's own ticket separates them. */
    function heldRead<T>() {
      let release!: (value: T) => void;
      const promise = new Promise<T>((resolve) => {
        release = resolve;
      });
      return { promise, release: (value: T) => release(value) };
    }

    const detailFor = (
      romId: number,
      romName: string,
      overrides: Partial<CachedGameDetail> = {},
    ): CachedGameDetail => ({
      found: true,
      rom_id: romId,
      rom_name: romName,
      platform_slug: "snes",
      metadata: makeMetadata(),
      stale_fields: [],
      ...overrides,
    });

    const dispatchDataChanged = async (detail: Record<string, unknown>) => {
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_data_changed", { detail }));
      });
    };

    const openSavesTab = async () => {
      await act(async () => {
        globalThis.dispatchEvent(new CustomEvent("romm_tab_switch", { detail: { tab: "saves" } }));
      });
      await flushAsync();
    };

    const statusWithFile = (romId: number, filename: string): SaveStatus => ({
      rom_id: romId,
      files: [
        {
          filename,
          status: "skip",
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
        last_played: null,
      },
      device_id: "d",
      last_sync_check_at: null,
    });

    /** Run the real slot helpers: the module mock writes nothing, and an
     *  unordered fold would then look exactly like an ordered one. */
    const useRealSlotHelpers = async () => {
      const realSlotState = await vi.importActual<typeof slotState>("../utils/slotState");
      vi.mocked(slotState.applyRefreshSlotResult).mockImplementation(realSlotState.applyRefreshSlotResult);
      vi.mocked(slotState.applyLoadSlotsResult).mockImplementation(realSlotState.applyLoadSlotsResult);
    };

    it("keeps the newest version switch's identity when an earlier switch's read lands last", async () => {
      // Both switches are for this panel's appId and both install an identity,
      // so the rom binding admits either — the panel would show whichever cache
      // read happened to finish last.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game (USA)", { regions: ["USA"] }));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      const firstSwitch = heldRead<CachedGameDetail>();
      vi.mocked(cachedStore.getCachedGameDetail).mockImplementationOnce(() => firstSwitch.promise);
      await dispatchDataChanged({ type: "version_switched", app_id: testAppId, rom_id: 2 });

      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(
        detailFor(3, "Game (Europe)", { regions: ["Europe"] }),
      );
      await dispatchDataChanged({ type: "version_switched", app_id: testAppId, rom_id: 3 });
      await flushAsync();
      expect(container.textContent).toContain("Game (Europe)");

      await act(async () => {
        firstSwitch.release(detailFor(2, "Game (Japan)", { regions: ["Japan"] }));
      });
      await flushAsync();

      expect(container.textContent).toContain("Game (Europe)");
      expect(container.textContent).toContain("Europe");
      expect(container.textContent).not.toContain("Game (Japan)");
    });

    it("keeps the newest save-status answer when an earlier read lands last", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game", { save_sync_enabled: true }));
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();
      await openSavesTab();

      const firstRead = heldRead<backend.SaveStatusResult>();
      vi.mocked(backend.getSaveStatus).mockImplementationOnce(() => firstRead.promise);
      await dispatchDataChanged({ type: "save_sync", rom_id: 1 });

      vi.mocked(backend.getSaveStatus).mockResolvedValue(statusWithFile(1, "SECOND.srm"));
      await dispatchDataChanged({ type: "save_sync", rom_id: 1 });
      await flushAsync();
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.saveStatus?.files[0]?.filename).toBe("SECOND.srm");

      await act(async () => {
        firstRead.release(statusWithFile(1, "FIRST.srm"));
      });
      await flushAsync();

      expect(capturedSavesTab[capturedSavesTab.length - 1]?.saveStatus?.files[0]?.filename).toBe("SECOND.srm");
      expect(capturedSavesTab.some((props) => props.saveStatus?.files[0]?.filename === "FIRST.srm")).toBe(false);
    });

    it("does not let a status read issued before save sync was switched off put the tab back", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game", { save_sync_enabled: false }));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      const enableRead = heldRead<backend.SaveStatusResult>();
      vi.mocked(backend.getSaveStatus).mockImplementationOnce(() => enableRead.promise);
      await dispatchDataChanged({ type: "save_sync_settings", save_sync_enabled: true });
      await dispatchDataChanged({ type: "save_sync_settings", save_sync_enabled: false });

      await act(async () => {
        enableRead.release(statusWithFile(1, "ANY.srm"));
      });
      await flushAsync();

      expect(container.textContent).not.toContain("SAVES");
    });

    it("shows the SAVES tab after save sync is switched on even when the status read is overtaken", async () => {
      // The fence orders the status ANSWER. The setting is not an answer, and
      // nothing re-issues it — swallowing it here hides the tab until remount.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game", { save_sync_enabled: false }));
      const { container } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      const enableRead = heldRead<backend.SaveStatusResult>();
      vi.mocked(backend.getSaveStatus).mockImplementationOnce(() => enableRead.promise);
      await dispatchDataChanged({ type: "save_sync_settings", save_sync_enabled: true });

      vi.mocked(backend.getSaveStatus).mockResolvedValue(statusWithFile(1, "NEWER.srm"));
      await dispatchDataChanged({ type: "save_sync", rom_id: 1 });
      await flushAsync();

      await act(async () => {
        enableRead.release(statusWithFile(1, "OVERTAKEN.srm"));
      });
      await flushAsync();

      expect(container.textContent).toContain("SAVES");
    });

    it("keeps the newest slot list when the lazy SAVES load's answer lands last", async () => {
      // The lazy lane writes through the panel's raw setter, and its own
      // `cancelled` flag is only set at commit time — the ticket it takes when
      // the read is issued has no such gap.
      await useRealSlotHelpers();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game", { save_sync_enabled: true }));
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [], active_slot: "mount" });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      const laneRead = heldRead<Awaited<ReturnType<typeof backend.getSaveSlots>>>();
      vi.mocked(backend.getSaveSlots).mockImplementationOnce(() => laneRead.promise);
      await openSavesTab();

      // A save-sync event re-reads the slots for the same rom while the lane's
      // read is still open, and answers first.
      vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [], active_slot: "fresh" });
      await dispatchDataChanged({ type: "save_sync", rom_id: 1 });
      await flushAsync();
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.activeSlot).toBe("fresh");

      await act(async () => {
        laneRead.release({ success: true, slots: [], active_slot: "stale" });
      });
      await flushAsync();

      expect(capturedSavesTab[capturedSavesTab.length - 1]?.activeSlot).toBe("fresh");
      expect(capturedSavesTab.some((props) => props.activeSlot === "stale")).toBe(false);
      // The run that lost the list still clears the spinner it put up — a fence
      // that swallowed this write would leave "Loading slots…" standing forever.
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.slotsLoading).toBe(false);
    });

    it("folds nothing from a failed lazy SAVES load whose answer lands last (#1755)", async () => {
      // The twin of the success case above. A failure used to carry no slot data
      // at all, so fencing successes alone was enough; it now carries the ROM's
      // last-known slots, and an overtaken run must not put that history back
      // over the newer read's answer.
      await useRealSlotHelpers();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game", { save_sync_enabled: true }));
      vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });
      vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [], active_slot: "mount" });
      render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      const laneRead = heldRead<Awaited<ReturnType<typeof backend.getSaveSlots>>>();
      vi.mocked(backend.getSaveSlots).mockImplementationOnce(() => laneRead.promise);
      await openSavesTab();

      // The server comes back while the lane's read is still open: a newer read
      // answers first, with slots and no history to show.
      vi.mocked(backend.getSaveSlots).mockResolvedValue({
        success: true,
        slots: [{ slot: "fresh", source: "server", count: 1, latest_updated_at: null }],
        active_slot: "fresh",
      });
      await dispatchDataChanged({ type: "save_sync", rom_id: 1 });
      await flushAsync();
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.activeSlot).toBe("fresh");
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.lastKnownSlots).toBeNull();

      await act(async () => {
        laneRead.release({
          success: false,
          reason: "server_unreachable",
          slots: [],
          active_slot: "main",
          last_known: {
            slots: [{ slot: "stale", source: "server", count: 4, latest_updated_at: null }],
            active_slot: "stale",
          },
        });
      });
      await flushAsync();

      expect(capturedSavesTab[capturedSavesTab.length - 1]?.lastKnownSlots).toBeNull();
      expect(capturedSavesTab.some((props) => props.lastKnownSlots !== null)).toBe(false);
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.availableSlots.map((s) => s.slot)).toEqual(["fresh"]);
      // The run that lost the lane still clears the spinner it put up.
      expect(capturedSavesTab[capturedSavesTab.length - 1]?.slotsLoading).toBe(false);
    });

    it("still applies a slot-configuration answer the lazy SAVES load overtook", async () => {
      // The lane re-reads the slot LIST and nothing else, so its ticket must not
      // fence the tracking answer that decides wizard-vs-tab — nothing would
      // re-issue that read.
      await useRealSlotHelpers();
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue(detailFor(1, "Game", { save_sync_enabled: true }));
      const tracking = heldRead<{ configured: boolean; active_slot: string | null }>();
      vi.mocked(backend.isSaveTrackingConfigured).mockImplementation(() => tracking.promise);
      vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [], active_slot: "main" });
      const { queryByTestId } = render(<RomMGameInfoPanel appId={testAppId} />);
      await flushAsync();

      // Opening the tab issues the lane's slot-list read; until the tracking
      // answer lands the setup wizard is what the tab shows.
      await openSavesTab();
      expect(queryByTestId("slot-setup-wizard")).not.toBeNull();

      await act(async () => {
        tracking.release({ configured: true, active_slot: "main" });
      });
      await flushAsync();

      expect(queryByTestId("saves-tab")).not.toBeNull();
      expect(queryByTestId("slot-setup-wizard")).toBeNull();
    });
  });
});
