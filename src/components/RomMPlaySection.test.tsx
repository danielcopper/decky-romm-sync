// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (toast captured via vi.mocked(toaster.toast),
// debugLog spy, captured prop on a child, etc.). Only truly-`/* ignore */`
// catches (no state change, no log call) are exempt — and even then, prefer
// dropping the test over keeping one with zero expects.
//
// The module-scope `artworkApplied: Set<number>` in RomMPlaySection.tsx
// persists across tests within this file. To avoid Set state bleeding
// between tests we use a unique `testAppId` per test (incremented in
// `beforeEach`) — Option A in the playbook.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, act } from "@testing-library/react";
import { createElement, type ComponentProps, type ReactElement } from "react";
import { RomMPlaySection } from "./RomMPlaySection";
import * as backend from "../api/backend";
import { showContextMenu, showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import {
  installDomEventListenerSpy,
  uninstallDomEventListenerSpy,
  domListenerCount,
} from "../test-utils/dom-event-listener-spy";
import { stubAppStore } from "../test-utils/steamStubs";
import * as cachedStore from "../utils/cachedGameDetailStore";
import * as connectionState from "../utils/connectionState";
import * as sectionRefresh from "../utils/sectionRefresh";
import * as playSectionUtils from "../utils/playSection";
import * as formatters from "../utils/formatters";
import { useVersionError } from "./VersionErrorCard";
import { useMigrationStatus } from "./MigrationBlockedPage";

// Type-only import — vi.mock("./CustomPlayButton", ...) below replaces the
// runtime impl, but pinning the captured-props shape to the real component
// keeps assertions in sync as the child's prop interface evolves.
import type { CustomPlayButton } from "./CustomPlayButton";

// ----- Sibling hook mocks -----
vi.mock("./VersionErrorCard", () => ({
  useVersionError: vi.fn(() => null),
}));
vi.mock("./MigrationBlockedPage", () => ({
  useMigrationStatus: vi.fn(() => ({ pending: false })),
}));

// ----- CustomPlayButton — capture props per render -----
type CapturedPlayButton = ComponentProps<typeof CustomPlayButton>;
const capturedPlayButton: CapturedPlayButton[] = [];
vi.mock("./CustomPlayButton", () => ({
  CustomPlayButton: (props: CapturedPlayButton) => {
    capturedPlayButton.push(props);
    return createElement("div", {
      "data-testid": "play-button",
      "data-appid": props.appId,
    });
  },
}));

// ----- Utils mocks (we own playSection helpers by mock so we don't have to
// reason about formatTimeAgo or hasAnySaveConflict transitively) -----
vi.mock("../utils/saveStatus", () => ({
  hasAnySaveConflict: vi.fn(() => false),
}));
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));
vi.mock("../utils/events", () => ({
  getEventTarget: vi.fn((e: { target?: unknown } | null) => e?.target ?? null),
}));
vi.mock("../utils/formatters", () => ({
  formatLastPlayed: vi.fn((rt: number) => (rt ? "2024-01-15" : "")),
  formatPlaytime: vi.fn((m: number) => (m ? "1h 30m" : "")),
}));
vi.mock("../utils/playSection", () => ({
  applySaveSyncDisplay: vi.fn(() => ({ status: null, label: "" })),
  extractBiosInfo: vi.fn(() => ({
    biosNeeded: true,
    biosStatus: "ok",
    biosLabel: "OK",
  })),
  extractCoreInfo: vi.fn(
    (c: {
      active_core_label?: string | null;
      platform_core_label?: string | null;
      has_game_override?: boolean;
      cores?: unknown[];
    }) => ({
      activeCoreLabel: c.active_core_label ?? null,
      activeCoreIsDefault: true,
      availableCores: c.cores ?? [],
      platformCoreLabel: c.platform_core_label ?? null,
      hasGameOverride: c.has_game_override ?? false,
    }),
  ),
  resolveSaveSyncLabel: vi.fn(() => "synced label"),
  // timeoutMs returns a Promise that never resolves — Promise.race with
  // testConnection always wins. Tests can override per-case to drive the
  // timeout branch.
  timeoutMs: vi.fn(() => new Promise(() => {})),
}));
vi.mock("../utils/sectionRefresh", () => ({
  refreshAchievementsInBackground: vi.fn(),
  refreshActiveSlotInBackground: vi.fn(),
  refreshBiosInBackground: vi.fn(),
  refreshCoreInfoInBackground: vi.fn(),
}));
vi.mock("../utils/connectionState", () => ({
  setRommConnectionState: vi.fn(),
  setVersionError: vi.fn(),
}));

// ----- cachedGameDetailStore — getCachedGameDetail / invalidateCachedGameDetail
// are re-exported through backend.ts but their canonical home is utils. Mock
// the store so all consumers route through the same vi.fn. -----
vi.mock("../utils/cachedGameDetailStore", () => ({
  getCachedGameDetail: vi.fn(),
  invalidateCachedGameDetail: vi.fn(),
}));

// ----- steamShortcuts — the set/clear core apply flow confirms the re-baked
// launch_options landed via setLaunchOptionsConfirmed (the fire-then-poll
// helper). Mock it so tests drive the confirmed (true) / unconfirmed (false)
// branches without touching the real RegisterForAppDetails poll. -----
vi.mock("../utils/steamShortcuts", () => ({
  setLaunchOptionsConfirmed: vi.fn(),
}));
import { setLaunchOptionsConfirmed } from "../utils/steamShortcuts";

// ----- metadataPatches.updatePlaytimeDisplay — the overview write-chokepoint.
// Mock it so the reconcile-on-view test can assert the reconciled total is
// pushed through, and so we control whether the romm_playtime_changed signal
// fires (the section's reactive PLAYTIME effect listens for it). -----
vi.mock("../patches/metadataPatches", () => ({
  updatePlaytimeDisplay: vi.fn(),
}));
import { updatePlaytimeDisplay } from "../patches/metadataPatches";

// ----- @decky/ui — global stub from test-setup.ts covers Focusable,
// DialogButton (with disabled), Menu, MenuItem, showContextMenu, showModal,
// ConfirmModal. It does NOT export MenuSeparator. Local re-mock adds the
// missing piece and tweaks Menu/MenuItem to expose props for context-menu
// assertions. (Vitest mock hoisting means this file's mock wins over the
// global one.) -----
type AnyProps = Record<string, unknown> & { children?: unknown };
vi.mock("@decky/ui", async () => {
  const { createElement: ce } = await import("react");
  return {
    basicAppDetailsSectionStylerClasses: { PlaySection: "play-section-cls" },
    // deckyUiInternals re-exports these @decky/ui internals; they must exist on
    // the mock even when this suite doesn't assert on them.
    appActionButtonClasses: undefined,
    appDetailsClasses: undefined,
    playSectionClasses: undefined,
    findSP: vi.fn(() => undefined),
    ConfirmModal: (p: AnyProps) => ce("div", { "data-testid": "confirm-modal" }, p.children as never),
    DialogButton: ({
      children,
      onClick,
      disabled,
      title,
    }: AnyProps & {
      onClick?: (e: unknown) => void;
      disabled?: boolean;
      title?: string;
    }) =>
      ce(
        "button",
        {
          onClick: (e: unknown) => onClick?.(e),
          disabled,
          title,
          "data-testid": "dialog-button",
        },
        children as never,
      ),
    Focusable: (p: AnyProps) => ce("div", p, p.children as never),
    Menu: (p: AnyProps & { label?: string }) =>
      ce("div", { "data-testid": "menu", "data-menu-label": p.label }, p.children as never),
    MenuItem: ({
      children,
      onClick,
      disabled,
      tone,
    }: AnyProps & {
      onClick?: () => void;
      disabled?: boolean;
      tone?: string;
    }) =>
      ce(
        "button",
        {
          onClick,
          disabled,
          "data-tone": tone,
          "data-testid": "menu-item",
        },
        children as never,
      ),
    MenuSeparator: () => ce("hr", { "data-testid": "menu-separator" }),
    showContextMenu: vi.fn(),
    showModal: vi.fn(),
  };
});

// ----- Helpers -----
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

// Inspect the most recent showContextMenu(menuElement, target) call.
function lastContextMenuElement(): ReactElement | null {
  const calls = vi.mocked(showContextMenu).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement | undefined;
  return el ?? null;
}

// Pull MenuItem children out of the context-menu element by index. We
// reconstruct via React.Children to avoid coupling to the runtime shape.
type MenuItemProps = {
  children?: unknown;
  onClick?: (...args: never[]) => unknown;
  disabled?: boolean;
  tone?: string;
};
type MenuItemElement = ReactElement<MenuItemProps>;
function getMenuItemsFromElement(el: ReactElement): MenuItemElement[] {
  const children = (el.props as { children?: unknown }).children;
  const arr = Array.isArray(children) ? children.flat(Infinity) : [children];
  return arr.filter((c): c is MenuItemElement => typeof c === "object" && c !== null && "props" in (c as object));
}

// Render the modal element captured by `showModal` so we can drive its
// `onOK` / `onCancel` props end-to-end. We don't need to interact with
// the DOM — the captured props are enough.
function lastShowModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

let testAppId = 1000;

describe("RomMPlaySection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    capturedPlayButton.length = 0;
    testAppId++;
    installDomEventListenerSpy();

    // resetAllMocks wipes module-mock impls — re-stub below.
    // Default sibling-hook stubs — no version error, no migration pending.
    vi.mocked(useVersionError).mockReturnValue(null);
    vi.mocked(useMigrationStatus).mockReturnValue({ pending: false });

    // Re-stub the playSection helpers (they were reset by resetAllMocks).
    vi.mocked(playSectionUtils.applySaveSyncDisplay).mockReturnValue({
      status: "synced",
      label: "synced label",
    });
    vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
      biosNeeded: true,
      biosStatus: "ok",
      biosLabel: "OK",
    });
    vi.mocked(playSectionUtils.extractCoreInfo).mockReturnValue({
      activeCoreLabel: null,
      activeCoreIsDefault: true,
      availableCores: [],
      platformCoreLabel: null,
      hasGameOverride: false,
    });
    // refreshCoreInfoInBackground (mocked) merges the current extractCoreInfo
    // mock result into state so the core button / menu render as in production,
    // where the dedicated core-info path (#923) populates these fields. The
    // extractCoreInfo mock ignores its argument, so the dummy CoreInfo is fine.
    vi.mocked(sectionRefresh.refreshCoreInfoInBackground).mockImplementation((_romId, cancelled, setter) => {
      if (cancelled()) return;
      const coreFields = playSectionUtils.extractCoreInfo({
        cores: [],
        active_core: null,
        active_core_label: null,
        platform_core_label: null,
        has_game_override: false,
      });
      act(() => {
        setter((prev) => ({ ...prev, ...coreFields }));
      });
    });
    vi.mocked(playSectionUtils.resolveSaveSyncLabel).mockReturnValue("synced label");
    vi.mocked(playSectionUtils.timeoutMs).mockImplementation(() => new Promise(() => {}));
    // Default: the re-baked launch_options confirm-set succeeds. Tests opt into
    // the unconfirmed (false) branch per case.
    vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(true);
    // Default core-info path — empty cores. Tests opt into specific shapes.
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
      cores: [],
      active_core: null,
      active_core_label: null,
      platform_core_label: null,
      has_game_override: false,
    });

    // Steam globals — appStore for the synchronous overview read.
    stubAppStore({ [testAppId]: {} });
    vi.stubGlobal("SteamClient", {
      Apps: {
        SetCustomArtworkForApp: vi.fn().mockResolvedValue(undefined),
        OpenAppSettingsDialog: vi.fn(),
      },
    });

    // Defaults — cached detail "not found", testConnection success but
    // tests opt into specific shapes per case.
    vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
      found: false,
    });
    vi.mocked(backend.testConnection).mockResolvedValue({
      success: true,
      message: "Connected",
    });
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
    // reconcilePlaytime defaults to a server-unreachable no-op so the
    // connection effect's fire-and-forget reconcile doesn't push playtime or
    // spew debugLogs into unrelated tests. Tests opt into the success shape.
    vi.mocked(backend.reconcilePlaytime).mockResolvedValue({
      total_seconds: 0,
      session_count: 0,
      server_query_failed: true,
    });
    // refreshCoverArtwork defaults to success so the artwork-refresh action
    // doesn't spew "refreshCoverArtwork failed" debugLogs into unrelated tests.
    vi.mocked(backend.refreshCoverArtwork).mockResolvedValue({
      success: true,
      message: "Cover refreshed",
      cover_path: "/grid/p.png",
    });
  });

  afterEach(() => {
    uninstallDomEventListenerSpy();
  });

  // ------------------------------------------------------------------
  // A. Top-level render gating
  // ------------------------------------------------------------------

  describe("top-level render gating", () => {
    it("returns null when useVersionError surfaces a string", async () => {
      vi.mocked(useVersionError).mockReturnValue("server too old");
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.firstChild).toBeNull();
    });

    it("returns null when migration is pending", async () => {
      vi.mocked(useMigrationStatus).mockReturnValue({ pending: true });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.firstChild).toBeNull();
    });

    it("renders the play-section row with CustomPlayButton when neither gate fires", async () => {
      const { queryByTestId } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(queryByTestId("play-button")).not.toBeNull();
      expect(capturedPlayButton[0]?.appId).toBe(testAppId);
    });
  });

  // ------------------------------------------------------------------
  // B. Initial render from appStore
  // ------------------------------------------------------------------

  describe("initial render from appStore", () => {
    it("forwards rt_last_time_played + minutes_playtime_forever through the formatters", async () => {
      stubAppStore({
        [testAppId]: {
          rt_last_time_played: 1234567890,
          minutes_playtime_forever: 90,
        },
      });
      const formatters = await import("../utils/formatters");
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(formatters.formatLastPlayed).toHaveBeenCalledWith(1234567890);
      expect(formatters.formatPlaytime).toHaveBeenCalledWith(90);
    });

    it("falls back to 0 when the overview lacks the fields", async () => {
      stubAppStore({ [testAppId]: {} });
      const formatters = await import("../utils/formatters");
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(formatters.formatLastPlayed).toHaveBeenCalledWith(0);
      expect(formatters.formatPlaytime).toHaveBeenCalledWith(0);
    });
  });

  // ------------------------------------------------------------------
  // C. loadCached flow
  // ------------------------------------------------------------------

  describe("loadCached mount flow", () => {
    it("does nothing when cached.found is false", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(sectionRefresh.refreshActiveSlotInBackground).not.toHaveBeenCalled();
      expect(sectionRefresh.refreshAchievementsInBackground).not.toHaveBeenCalled();
      expect(sectionRefresh.refreshBiosInBackground).not.toHaveBeenCalled();
    });

    it("applies cached fields and dispatches active-slot/achievements/BIOS/core background refreshes", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        rom_name: "Test ROM",
        platform_slug: "snes",
        rom_file: "test.sfc",
        save_sync_enabled: true,
        save_sync_display: { status: "synced", label: "label", last_sync_check_at: null },
        ra_id: 7,
        achievement_summary: { earned: 3, total: 50, earned_hardcore: 0 },
        stale_fields: ["metadata", "achievements", "bios"],
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(backend.getRomMetadata).mockResolvedValue({} as never);

      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      expect(sectionRefresh.refreshActiveSlotInBackground).toHaveBeenCalledWith(
        99,
        expect.any(Function),
        expect.any(Function),
      );
      expect(backend.getRomMetadata).toHaveBeenCalledWith(99);
      expect(sectionRefresh.refreshAchievementsInBackground).toHaveBeenCalledWith(
        99,
        expect.any(Function),
        expect.any(Function),
      );
      expect(sectionRefresh.refreshBiosInBackground).toHaveBeenCalledWith(
        99,
        expect.any(Function),
        expect.any(Function),
      );
      // Core info is fetched from its own path (#923), keyed on the rom_id so
      // the active core reflects a per-game DB override (epic #945), independent
      // of the BIOS refresh.
      expect(sectionRefresh.refreshCoreInfoInBackground).toHaveBeenCalledWith(
        99,
        expect.any(Function),
        expect.any(Function),
      );
      // Assert exact arg shape: (cached.bios_level, cached.bios_label). The BIOS
      // status dict is no longer passed — extractBiosInfo takes only the
      // pre-computed level/label (#923). Catches arg-order regressions that a
      // bare .toHaveBeenCalled() would miss.
      expect(playSectionUtils.extractBiosInfo).toHaveBeenCalledWith("ok", "OK");
      // resolveSaveSyncLabel is called with the cached save_sync_display.
      expect(playSectionUtils.resolveSaveSyncLabel).toHaveBeenCalledWith(
        expect.objectContaining({ status: "synced", label: "label" }),
      );
    });

    it("triggers applyArtwork (4 SGDB calls) on first visit when not already applied", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 50,
      });
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.getSgdbArtworkBase64)).toHaveBeenCalledTimes(4);
    });

    it("skips metadata background fetch when 'metadata' is not in stale_fields", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 50,
        stale_fields: [],
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(backend.getRomMetadata).not.toHaveBeenCalled();
    });

    it("skips achievements refresh when ra_id is null even if stale", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 50,
        ra_id: null,
        stale_fields: ["achievements"],
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(sectionRefresh.refreshAchievementsInBackground).not.toHaveBeenCalled();
    });

    it("logs via debugLog when getCachedGameDetail rejects", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockRejectedValue(new Error("boom"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("loadCached error"));
    });

    it("logs 'Auto-artwork error' via debugLog when SteamClient.SetCustomArtworkForApp rejects on auto-apply", async () => {
      // SGDB returns a real base64 so applyArtwork progresses past the
      // per-call .catch swallowers into SetCustomArtworkForApp — which then
      // rejects, surfacing the outer `.catch((e) => debugLog(...))` at the
      // applyArtwork(...).then(...).catch(...) site in loadCached.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 50,
      });
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: "AAAA",
        no_api_key: false,
      });
      vi.stubGlobal("SteamClient", {
        Apps: {
          SetCustomArtworkForApp: vi.fn().mockRejectedValue(new Error("boom")),
          OpenAppSettingsDialog: vi.fn(),
        },
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("Auto-artwork error"));
    });

    it("logs 'Background metadata fetch error' via debugLog when background getRomMetadata rejects", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
        stale_fields: ["metadata"],
      });
      vi.mocked(backend.getRomMetadata).mockRejectedValue(new Error("net"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
        expect.stringContaining("Background metadata fetch error"),
      );
    });
  });

  // ------------------------------------------------------------------
  // D. handleRefreshArtwork — getSgdbResolution-driven SGDB step
  // ------------------------------------------------------------------

  describe("handleRefreshArtwork SGDB resolution flow", () => {
    async function setupForArtworkAction(romId = 77) {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: romId,
        rom_name: "Test ROM",
      });
      // Auto-apply on mount routes through getSgdbArtworkBase64; default it
      // to all-null so the mount path is inert and the action drives the test.
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      return openRomMMenuAndGetItems(testAppId);
    }

    it("decision=no_api_key → 'Set a SteamGridDB API key' toast, no applyArtwork", async () => {
      const items = await setupForArtworkAction();
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "no_api_key" });
      vi.mocked(showModal).mockClear();
      vi.mocked(backend.getSgdbArtworkBase64).mockClear();
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(backend.getSgdbResolution)).toHaveBeenCalledWith(77);
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Set a SteamGridDB API key in settings first" }),
      );
      // No artwork apply, no picker modal.
      expect(vi.mocked(backend.getSgdbArtworkBase64)).not.toHaveBeenCalled();
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
    });

    it("decision=resolved → applyArtwork runs, toasts 'Artwork refreshed (N/4 images applied)'", async () => {
      const items = await setupForArtworkAction();
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "resolved", sgdb_id: 42 });
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(backend.getSgdbArtworkBase64)
        .mockResolvedValueOnce({ base64: "AA==", no_api_key: false })
        .mockResolvedValueOnce({ base64: "BB==", no_api_key: false })
        .mockResolvedValueOnce({ base64: "CC==", no_api_key: false })
        .mockResolvedValueOnce({ base64: "DD==", no_api_key: false });
      vi.mocked(backend.saveShortcutIcon).mockResolvedValue({ success: true });
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(SteamClient.Apps.SetCustomArtworkForApp)).toHaveBeenCalledTimes(3);
      expect(vi.mocked(backend.saveShortcutIcon)).toHaveBeenCalledWith(testAppId, "DD==");
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Artwork refreshed (4/4 images applied)" }),
      );
    });

    it("decision=resolved with all-null artwork → toasts 'No artwork available for this game'", async () => {
      const items = await setupForArtworkAction();
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "resolved", sgdb_id: 42 });
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "No artwork available for this game" }),
      );
    });

    it("decision=resolved but applyArtwork sees no_api_key → key toast", async () => {
      const items = await setupForArtworkAction();
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "resolved", sgdb_id: 42 });
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: true,
      });
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Set a SteamGridDB API key in settings first" }),
      );
    });

    it("decision=needs_pick → opens SgdbGamePickerModal with candidates", async () => {
      const items = await setupForArtworkAction();
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({
        decision: "needs_pick",
        candidates: [{ id: 1, name: "Game A", release_year: 1999, thumb_url: null }],
      });
      vi.mocked(showModal).mockClear();
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      const props = lastShowModalProps<{
        candidates?: Array<{ id: number; name: string }>;
        romName?: string;
      }>();
      expect(props?.candidates).toHaveLength(1);
      expect(props?.candidates?.[0]?.name).toBe("Game A");
      expect(props?.romName).toBe("Test ROM");
    });

    it("getSgdbResolution rejection → 'Failed to refresh artwork' toast + debugLog (non-vacuous catch)", async () => {
      const items = await setupForArtworkAction();
      vi.mocked(backend.getSgdbResolution).mockRejectedValue(new Error("net"));
      vi.mocked(showModal).mockClear();
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(backend.debugLog).mockClear();
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      // Catch's observable effects: the failure toast + the debugLog message.
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to refresh artwork" }),
      );
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("getSgdbResolution rejected"));
      // No modal opened on failure.
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------
  // E. Connection check useEffect
  // ------------------------------------------------------------------

  describe("connection check useEffect", () => {
    it("on testConnection success → setRommConnectionState('connected') + dispatches romm_connection_changed", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: true,
        message: "ok",
      });
      const listener = vi.fn();
      globalThis.addEventListener("romm_connection_changed", listener);
      try {
        render(<RomMPlaySection appId={testAppId} />);
        await flushAsync();
        expect(vi.mocked(connectionState.setRommConnectionState)).toHaveBeenCalledWith("connected");
        // First "checking" then "connected".
        const states = listener.mock.calls.map((c) => (c[0] as CustomEvent).detail.state);
        expect(states).toContain("checking");
        expect(states).toContain("connected");
      } finally {
        globalThis.removeEventListener("romm_connection_changed", listener);
      }
    });

    it("on testConnection success=false → 'offline'", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: false,
        message: "",
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(connectionState.setRommConnectionState)).toHaveBeenCalledWith("offline");
    });

    it("on reason=version_error → calls setVersionError and stays offline", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: false,
        message: "Update required",
        reason: "version_error",
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(connectionState.setVersionError)).toHaveBeenCalledWith("Update required");
      expect(vi.mocked(connectionState.setRommConnectionState)).toHaveBeenCalledWith("offline");
    });

    it("on testConnection throw → catch sets 'offline'", async () => {
      vi.mocked(backend.testConnection).mockRejectedValue(new Error("net"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(connectionState.setRommConnectionState)).toHaveBeenCalledWith("offline");
    });

    it("on timeout (timeoutMs rejects first) → catch sets 'offline'", async () => {
      vi.mocked(playSectionUtils.timeoutMs).mockReturnValue(Promise.reject(new Error("timeout")));
      // testConnection never resolves — race goes to timeoutMs.
      vi.mocked(backend.testConnection).mockImplementation(() => new Promise(() => {}));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(connectionState.setRommConnectionState)).toHaveBeenCalledWith("offline");
    });

    it("dispatches romm_data_changed with has_conflict when connected + save_sync_enabled", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        save_sync_enabled: true,
        save_sync_display: { status: "synced", label: "ok", last_sync_check_at: null },
      });
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: true,
        message: "ok",
      });
      vi.mocked(backend.getSaveStatus).mockResolvedValue({
        rom_id: 88,
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
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        render(<RomMPlaySection appId={testAppId} />);
        await flushAsync();
        // Find the save_sync dispatch
        const saveSyncEv = listener.mock.calls
          .map((c) => c[0] as CustomEvent)
          .find((e) => e.detail.type === "save_sync");
        expect(saveSyncEv).toBeDefined();
        expect(saveSyncEv?.detail).toMatchObject({
          type: "save_sync",
          rom_id: 88,
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("debugLog fires when the save status fetch inside doSaveCheck throws", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 88,
        save_sync_enabled: true,
        save_sync_display: { status: "synced", label: "ok", last_sync_check_at: null },
      });
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: true,
        message: "ok",
      });
      vi.mocked(backend.getSaveStatus).mockRejectedValue(new Error("savesfail"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("background save check error"));
    });
  });

  // ------------------------------------------------------------------
  // E2. Playtime reconcile-on-view (#868) + reactive PLAYTIME display (#869)
  // ------------------------------------------------------------------

  describe("playtime reconcile-on-view (#868) and reactive display (#869)", () => {
    // formatPlaytime maps minutes 1:1 to "<n>m" so stale vs fresh totals are
    // distinguishable in the rendered text (the default mock collapses every
    // non-zero value to "1h 30m").
    function useDistinctPlaytimeFormatter(): void {
      vi.mocked(formatters.formatPlaytime).mockImplementation((m: number) => (m > 0 ? `${m}m` : "None"));
    }

    it("fires reconcilePlaytime on enter when connected, then pushes the total via updatePlaytimeDisplay", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 314,
        save_sync_enabled: false,
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "ok" });
      vi.mocked(backend.reconcilePlaytime).mockResolvedValue({
        total_seconds: 7200,
        session_count: 4,
        server_query_failed: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.reconcilePlaytime)).toHaveBeenCalledWith(314);
      // Reconciled total pushed to the overview write-chokepoint, updateLastPlayed=false.
      expect(vi.mocked(updatePlaytimeDisplay)).toHaveBeenCalledWith(testAppId, 7200, false);
    });

    it("is NOT gated on saveSyncEnabled — reconcile fires even when save-sync is OFF", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 271,
        save_sync_enabled: false, // save-sync OFF
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "ok" });
      vi.mocked(backend.reconcilePlaytime).mockResolvedValue({
        total_seconds: 3600,
        session_count: 1,
        server_query_failed: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      // getSaveStatus stays untouched (save-sync gate held) but reconcile still ran.
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.reconcilePlaytime)).toHaveBeenCalledWith(271);
      expect(vi.mocked(updatePlaytimeDisplay)).toHaveBeenCalledWith(testAppId, 3600, false);
    });

    it("does NOT reconcile when offline (server not reachable)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 99,
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: false, message: "" });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.reconcilePlaytime)).not.toHaveBeenCalled();
      expect(vi.mocked(updatePlaytimeDisplay)).not.toHaveBeenCalled();
    });

    it("server_query_failed=true → no overview push; PLAYTIME stays on the local value", async () => {
      useDistinctPlaytimeFormatter();
      // Mount with a known local playtime of 30 minutes.
      stubAppStore({ [testAppId]: { minutes_playtime_forever: 30 } });
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "ok" });
      vi.mocked(backend.reconcilePlaytime).mockResolvedValue({
        total_seconds: 0,
        session_count: 0,
        server_query_failed: true,
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.reconcilePlaytime)).toHaveBeenCalledWith(42);
      // server_query_failed → no push.
      expect(vi.mocked(updatePlaytimeDisplay)).not.toHaveBeenCalled();
      // Display stays on the local 30-minute value (non-vacuous: assert the text).
      expect(container.textContent).toContain("30m");
    });

    it("debugLog fires when reconcilePlaytime rejects (catch is non-vacuous)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 77,
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "ok" });
      vi.mocked(backend.reconcilePlaytime).mockRejectedValue(new Error("boom"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("playtime reconcile error"));
      expect(vi.mocked(updatePlaytimeDisplay)).not.toHaveBeenCalled();
    });

    it("#869 — a romm_playtime_changed event refreshes PLAYTIME on the same mount (no remount)", async () => {
      useDistinctPlaytimeFormatter();
      // Mount with a stale local total of 10 minutes.
      stubAppStore({ [testAppId]: { minutes_playtime_forever: 10 } });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("10m"); // stale value visible

      // A later write (session end OR reconcile) raised the overview to 95 min
      // and fired the chokepoint signal. The section must re-read and refresh.
      stubAppStore({ [testAppId]: { minutes_playtime_forever: 95 } });
      act(() => {
        globalThis.dispatchEvent(new CustomEvent("romm_playtime_changed", { detail: { appId: testAppId } }));
      });
      expect(container.textContent).toContain("95m"); // fresh value, same mount
      expect(container.textContent).not.toContain("10m");
    });

    it("#869 — ignores romm_playtime_changed for a different appId", async () => {
      useDistinctPlaytimeFormatter();
      stubAppStore({ [testAppId]: { minutes_playtime_forever: 10 } });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("10m");

      // Event for a different app — overview lookup for testAppId still returns
      // 10 min; the mismatched-appId guard must no-op (no re-read, no change).
      act(() => {
        globalThis.dispatchEvent(new CustomEvent("romm_playtime_changed", { detail: { appId: testAppId + 9999 } }));
      });
      expect(container.textContent).toContain("10m");
    });

    it("#869 — reconcile result reaches the display end-to-end via the chokepoint signal", async () => {
      useDistinctPlaytimeFormatter();
      // Mount stale at 5 minutes.
      stubAppStore({ [testAppId]: { minutes_playtime_forever: 5 } });
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 868,
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "ok" });
      vi.mocked(backend.reconcilePlaytime).mockResolvedValue({
        total_seconds: 7200, // 120 min
        session_count: 4,
        server_query_failed: false,
      });
      // Simulate the real chokepoint: when the section pushes the reconciled
      // total, raise the overview and emit the signal the section listens for.
      vi.mocked(updatePlaytimeDisplay).mockImplementation((id: number, totalSeconds: number) => {
        stubAppStore({ [testAppId]: { minutes_playtime_forever: Math.floor(totalSeconds / 60) } });
        globalThis.dispatchEvent(new CustomEvent("romm_playtime_changed", { detail: { appId: id } }));
        return true;
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(updatePlaytimeDisplay)).toHaveBeenCalledWith(testAppId, 7200, false);
      expect(container.textContent).toContain("120m"); // reconciled total live, no remount
      expect(container.textContent).not.toContain("5m");
    });

    it("#869 — registers + removes the romm_playtime_changed listener across mount/unmount", async () => {
      const before = domListenerCount("romm_playtime_changed");
      const { unmount } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(domListenerCount("romm_playtime_changed")).toBe(before + 1);
      unmount();
      expect(domListenerCount("romm_playtime_changed")).toBe(before);
    });
  });

  // ------------------------------------------------------------------
  // F. romm_data_changed DOM event handler
  // ------------------------------------------------------------------

  describe("romm_data_changed DOM event handler", () => {
    it("registers a listener on mount and removes it on unmount", async () => {
      const before = domListenerCount("romm_data_changed");
      const { unmount } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(domListenerCount("romm_data_changed")).toBe(before + 1);
      unmount();
      expect(domListenerCount("romm_data_changed")).toBe(before);
    });

    it("save_sync_settings: enabled=true with rom_id → calls getSaveStatus and updates display", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 55,
      });
      const fetchedSaveStatus = {
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
        save_sync_display: { status: "synced" as const, label: "from-fetch", last_sync_check_at: null },
      };
      vi.mocked(backend.getSaveStatus).mockResolvedValue(fetchedSaveStatus);
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(playSectionUtils.applySaveSyncDisplay).mockClear();
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
      // Assert exact arg shape: (saveStatus.save_sync_display, saveStatus).
      // Bare .toHaveBeenCalled() would pass even with the wrong arguments.
      expect(vi.mocked(playSectionUtils.applySaveSyncDisplay)).toHaveBeenCalledWith(
        expect.objectContaining({ status: "synced", label: "from-fetch" }),
        fetchedSaveStatus,
      );
    });

    it("save_sync_settings: enabled=true with no rom_id → skips getSaveStatus", async () => {
      // cached.found=false → romIdRef stays null
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getSaveStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync_settings", save_sync_enabled: true },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });

    it("save_sync_settings: enabled=false → resets saveSync state (no fetch)", async () => {
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
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
    });

    it("core_changed: reads core data from getPlatformCoreInfo (not BIOS) + BIOS level from getBiosStatus", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      // Core data comes from the dedicated path (#923), keyed on the rom_id so
      // the active core reflects a per-game DB override (epic #945).
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "blastem.so",
        active_core_label: "BlastEm",
        platform_core_label: null,
        has_game_override: false,
        cores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
      });
      // BIOS level/label come from the (now core-free) BIOS status.
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: {
          platform_slug: "snes",
          server_count: 0,
          local_count: 0,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "All BIOS present",
      });

      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // Keyed on rom_id (#945), not the event slug + filename.
      expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(60);
      expect(vi.mocked(backend.getBiosStatus)).toHaveBeenCalledWith(60);
    });

    it("core_changed: no-BIOS → needs-BIOS switch surfaces the missing-BIOS badge (#923)", async () => {
      // Mount with a core that needs no BIOS: cached has no bios_status, so the
      // badge starts hidden (biosNeeded=false).
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 60,
        platform_slug: "snes",
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("BIOS");

      // Switch to a core whose BIOS is missing — getBiosStatus now returns a
      // populated bios_status + missing level. The badge must appear: biosNeeded
      // is re-derived from the refreshed status, not the (stale) mount value.
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "blastem.so",
        active_core_label: "BlastEm",
        platform_core_label: null,
        has_game_override: false,
        cores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
      });
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: {
          platform_slug: "snes",
          server_count: 3,
          local_count: 0,
          all_downloaded: false,
        },
        bios_level: "missing",
        bios_label: "0/3",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });

      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(container.textContent).toContain("BIOS");
      expect(container.textContent).toContain("0/3");
    });

    it("core_changed: skips when no romId", async () => {
      // cached.found=false → no romId
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getBiosStatus).mockClear();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "core_changed", platform_slug: "snes" },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getBiosStatus)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getPlatformCoreInfo)).not.toHaveBeenCalled();
    });

    it("save_sync: matching rom_id → fetches save status (when detail.save_status not provided)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 33,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      vi.mocked(backend.getSaveStatus).mockResolvedValue({
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
      });
      vi.mocked(backend.getSaveStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync", rom_id: 33 },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).toHaveBeenCalledWith(33);
    });

    it("save_sync: mismatching rom_id → early return", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 33,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
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

    it("save_sync: uses detail.save_status when present (no fetch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 33,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(playSectionUtils.applySaveSyncDisplay).mockClear();
      const inlineSaveStatus = {
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
        save_sync_display: { status: "synced" as const, label: "inline-label", last_sync_check_at: null },
      };
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: {
              type: "save_sync",
              rom_id: 33,
              save_status: inlineSaveStatus,
            },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      // applySaveSyncDisplay receives the inline save_status — not a fetched one.
      // Catches a wiring regression that would route through getSaveStatus instead.
      expect(vi.mocked(playSectionUtils.applySaveSyncDisplay)).toHaveBeenCalledWith(
        expect.objectContaining({ status: "synced", label: "inline-label" }),
        inlineSaveStatus,
      );
    });

    it("unknown detail.type → no-op (no fetch, no throw)", async () => {
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getSaveStatus).mockClear();
      vi.mocked(backend.getBiosStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "definitely_not_a_real_event" },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.getBiosStatus)).not.toHaveBeenCalled();
    });

    it("dispatch handler throw → onDataChanged outer try/catch fires debugLog", async () => {
      // Drive the outer try/catch in onDataChanged by routing through the
      // core_changed branch (no inline .catch on getBiosStatus) and making
      // getBiosStatus reject. The throw escapes handleCoreChange, propagates
      // to onDataChanged's catch, and surfaces via debugLog.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 33,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("handler-boom"));
      vi.mocked(backend.debugLog).mockClear();
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

    it("save_sync rom_id absent and no romIdRef → early return", async () => {
      // cached.found=false → romIdRef remains null AND detail.rom_id absent → early return
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      vi.mocked(backend.getSaveStatus).mockClear();
      await act(async () => {
        globalThis.dispatchEvent(
          new CustomEvent("romm_data_changed", {
            detail: { type: "save_sync" },
          }),
        );
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------
  // G. handleRefreshArtwork (covered partly above; rest of paths below)
  // ------------------------------------------------------------------

  describe("handleRefreshArtwork branches", () => {
    it("toasts 'ROM info not loaded yet' when romId is null", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "ROM info not loaded yet" }),
      );
      // No backend call when romId is null
      expect(vi.mocked(backend.refreshCoverArtwork)).not.toHaveBeenCalled();
    });

    it("calls refreshCoverArtwork BEFORE getSgdbResolution and dispatches 'cover_refreshed' on success", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 77,
      });
      vi.mocked(backend.refreshCoverArtwork).mockResolvedValue({
        success: true,
        message: "Cover refreshed",
        cover_path: "/grid/999p.png",
      });
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "no_api_key" });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(backend.refreshCoverArtwork).mockClear();
      vi.mocked(backend.getSgdbResolution).mockClear();

      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await items[0]!.props.onClick?.();
        });

        // refreshCoverArtwork called once with the rom_id
        expect(vi.mocked(backend.refreshCoverArtwork)).toHaveBeenCalledWith(77);

        // Order: refreshCoverArtwork before getSgdbResolution
        const refreshOrder = vi.mocked(backend.refreshCoverArtwork).mock.invocationCallOrder[0]!;
        const resolutionOrder = vi.mocked(backend.getSgdbResolution).mock.invocationCallOrder[0]!;
        expect(refreshOrder).toBeLessThan(resolutionOrder);

        // cover_refreshed event dispatched with the rom_id
        const ev = listener.mock.calls
          .map((c) => c[0] as CustomEvent)
          .find((e) => e.detail?.type === "cover_refreshed");
        expect(ev?.detail).toEqual({ type: "cover_refreshed", rom_id: 77 });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("on refreshCoverArtwork {success: false}, logs and STILL runs the SGDB resolution step", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 77,
      });
      vi.mocked(backend.refreshCoverArtwork).mockResolvedValue({
        success: false,
        reason: "not_synced",
        message: "ROM is not synced to Steam",
      });
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "resolved", sgdb_id: 42 });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(backend.getSgdbResolution).mockClear();
      vi.mocked(backend.debugLog).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await items[0]!.props.onClick?.();
        });
        // SGDB resolution step still runs (graceful fall-through)
        expect(vi.mocked(backend.getSgdbResolution)).toHaveBeenCalledWith(77);
        // debugLog surfaced the failure reason + message
        expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("not_synced"));
        // No cover_refreshed event on failure
        const ev = listener.mock.calls
          .map((c) => c[0] as CustomEvent)
          .find((e) => e.detail?.type === "cover_refreshed");
        expect(ev).toBeUndefined();
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("on refreshCoverArtwork rejection, debugLogs the rejection and continues to the SGDB resolution step", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 77,
      });
      vi.mocked(backend.refreshCoverArtwork).mockRejectedValue(new Error("network down"));
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "resolved", sgdb_id: 42 });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(backend.getSgdbResolution).mockClear();
      vi.mocked(backend.debugLog).mockClear();
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      // Non-vacuous catch assertion: debugLog observed the rejection.
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("refreshCoverArtwork rejected"));
      // SGDB resolution step still runs
      expect(vi.mocked(backend.getSgdbResolution)).toHaveBeenCalledWith(77);
    });

    it("toasts 'Failed to refresh artwork' when applyArtwork's SetCustomArtworkForApp throws (resolved decision)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 77,
      });
      // Mount auto-apply with all-null base64 (no SteamClient calls), then
      // the manual refresh resolves and re-runs applyArtwork which throws.
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: null,
        no_api_key: false,
      });
      vi.mocked(backend.getSgdbResolution).mockResolvedValue({ decision: "resolved", sgdb_id: 42 });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      // Now SGDB returns base64 and SteamClient throws on apply.
      vi.mocked(backend.getSgdbArtworkBase64).mockResolvedValue({
        base64: "AA==",
        no_api_key: false,
      });
      vi.stubGlobal("SteamClient", {
        Apps: {
          SetCustomArtworkForApp: vi.fn().mockRejectedValue(new Error("io")),
          OpenAppSettingsDialog: vi.fn(),
        },
      });
      await act(async () => {
        await items[0]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to refresh artwork" }),
      );
    });
  });

  // ------------------------------------------------------------------
  // H. handleRefreshMetadata
  // ------------------------------------------------------------------

  describe("handleRefreshMetadata", () => {
    it("happy path: getRomMetadata + 'Metadata refreshed' toast + dispatches metadata event", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
      });
      vi.mocked(backend.getRomMetadata).mockResolvedValue({} as never);
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await items[1]!.props.onClick?.();
        });
        expect(vi.mocked(backend.getRomMetadata)).toHaveBeenCalledWith(42);
        expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Metadata refreshed" }));
        const ev = listener.mock.calls.map((c) => c[0] as CustomEvent).find((e) => e.detail.type === "metadata");
        expect(ev?.detail).toEqual({ type: "metadata", rom_id: 42 });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("rejection: toasts 'Failed to refresh metadata'", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
      });
      vi.mocked(backend.getRomMetadata).mockRejectedValue(new Error("net"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[1]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to refresh metadata" }),
      );
    });
  });

  // ------------------------------------------------------------------
  // I. handleSyncSaves
  // ------------------------------------------------------------------

  describe("handleSyncSaves", () => {
    async function setupSavesAction(romId = 42) {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: romId,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      return openRomMMenuAndGetItems(testAppId);
    }

    it("success with synced=0 → label 'no files updated'", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockResolvedValue({
        success: true,
        message: "ok",
        synced: 0,
        conflicts: [],
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          body: "Saves synced (no files updated)",
        }),
      );
    });

    it("success with synced=1 → singular form", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockResolvedValue({
        success: true,
        message: "",
        synced: 1,
        conflicts: [],
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          body: "Saves synced (1 file updated)",
        }),
      );
    });

    it("success with synced=N + conflicts → label '... N files updated, M conflict(s) need resolution'", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockResolvedValue({
        success: true,
        message: "",
        synced: 3,
        conflicts: [{ filename: "x" } as never, { filename: "y" } as never],
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          body: "Saves synced (3 files updated, 2 conflict(s) need resolution)",
        }),
      );
    });

    it("success with synced=0 / conflicts=undefined → treats conflicts as 0", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockResolvedValue({
        success: true,
        message: "",
        synced: 0,
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          body: "Saves synced (no files updated)",
        }),
      );
    });

    it("failure → surfaces result.message", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockResolvedValue({
        success: false,
        message: "Server refused",
        synced: 0,
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Server refused" }));
    });

    it("failure with empty message → falls back to 'Save sync failed'", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockResolvedValue({
        success: false,
        message: "",
        synced: 0,
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Save sync failed" }));
    });

    it("throw → toasts 'Save sync failed'", async () => {
      const items = await setupSavesAction();
      vi.mocked(backend.syncRomSaves).mockRejectedValue(new Error("crash"));
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[2]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Save sync failed" }));
    });
  });

  // ------------------------------------------------------------------
  // J. handleDownloadBios
  // ------------------------------------------------------------------

  describe("handleDownloadBios", () => {
    async function setupBiosAction() {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "ps1",
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      return openRomMMenuAndGetItems(testAppId);
    }

    it("happy path: downloads BIOS, dispatches bios event, refreshes status", async () => {
      const items = await setupBiosAction();
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        message: "",
        downloaded: 3,
        errors: [],
      } as never);
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: {
          platform_slug: "ps1",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(toaster.toast).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await items[3]!.props.onClick?.();
        });
        expect(vi.mocked(backend.downloadAllFirmware)).toHaveBeenCalledWith("ps1");
        expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
          expect.objectContaining({ body: "BIOS downloaded (3 files)" }),
        );
        const biosEv = listener.mock.calls.map((c) => c[0] as CustomEvent).find((e) => e.detail.type === "bios");
        expect(biosEv?.detail).toMatchObject({
          type: "bios",
          platform_slug: "ps1",
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("getBiosStatus rejection during refresh → uses safe fallback, no setInfo write", async () => {
      const items = await setupBiosAction();
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: true,
        message: "",
        downloaded: 0,
        errors: [],
      } as never);
      vi.mocked(backend.getBiosStatus).mockRejectedValue(new Error("net"));
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[3]!.props.onClick?.();
      });
      // Toast still fires; fallback has bios_status null → skips setInfo.
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "BIOS downloaded (0 files)" }),
      );
    });

    it("failure with message → toasts the message", async () => {
      const items = await setupBiosAction();
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: false,
        message: "no internet",
      } as never);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[3]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "no internet" }));
    });

    it("failure with empty message → falls back to 'BIOS download failed'", async () => {
      const items = await setupBiosAction();
      vi.mocked(backend.downloadAllFirmware).mockResolvedValue({
        success: false,
        message: "",
      } as never);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[3]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "BIOS download failed" }));
    });

    it("throw → toasts 'BIOS download failed'", async () => {
      const items = await setupBiosAction();
      vi.mocked(backend.downloadAllFirmware).mockRejectedValue(new Error("io"));
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[3]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "BIOS download failed" }));
    });

    it("no platformSlug → early return, no fetch", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "",
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const items = await openRomMMenuAndGetItems(testAppId);
      vi.mocked(backend.downloadAllFirmware).mockClear();
      await act(async () => {
        await items[3]!.props.onClick?.();
      });
      expect(vi.mocked(backend.downloadAllFirmware)).not.toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------
  // K. handleUninstall (separator is at index 4, so uninstall is at index 6)
  // ------------------------------------------------------------------

  describe("handleUninstall", () => {
    async function setupUninstallAction(romName = "Some ROM") {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        rom_name: romName,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      return openRomMMenuAndGetItems(testAppId);
    }

    it("happy path: dispatches romm_rom_uninstalled + toast", async () => {
      const items = await setupUninstallAction("Mario");
      vi.mocked(backend.removeRom).mockResolvedValue({
        success: true,
        message: "removed",
      });
      vi.mocked(toaster.toast).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_rom_uninstalled", listener);
      try {
        await act(async () => {
          // Last MenuItem (uninstall) — find via tone or position. The order
          // is [refresh-artwork, refresh-metadata, sync-saves, download-bios,
          // separator, delete-saves, uninstall] but we filtered to MenuItems
          // only, so it's index 5 (after delete-saves at 4).
          // Actually after dropping the separator from filtered MenuItems,
          // uninstall is the last one (index 5).
          const uninstall = items[items.length - 1]!;
          await uninstall.props.onClick?.();
        });
        expect(vi.mocked(backend.removeRom)).toHaveBeenCalledWith(42);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({ rom_id: 42 });
        expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Mario uninstalled" }));
      } finally {
        globalThis.removeEventListener("romm_rom_uninstalled", listener);
      }
    });

    it("uses 'ROM' as fallback display name when rom_name empty", async () => {
      const items = await setupUninstallAction("");
      vi.mocked(backend.removeRom).mockResolvedValue({
        success: true,
        message: "ok",
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[items.length - 1]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "ROM uninstalled" }));
    });

    it("failure → surfaces result.message", async () => {
      const items = await setupUninstallAction();
      vi.mocked(backend.removeRom).mockResolvedValue({
        success: false,
        message: "locked",
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[items.length - 1]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "locked" }));
    });

    it("failure with empty message → falls back to 'Uninstall failed'", async () => {
      const items = await setupUninstallAction();
      vi.mocked(backend.removeRom).mockResolvedValue({
        success: false,
        message: "",
      });
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[items.length - 1]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Uninstall failed" }));
    });

    it("throw → toasts 'Uninstall failed'", async () => {
      const items = await setupUninstallAction();
      vi.mocked(backend.removeRom).mockRejectedValue(new Error("io"));
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await items[items.length - 1]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Uninstall failed" }));
    });
  });

  // ------------------------------------------------------------------
  // L. handleDeleteSaves — opens ConfirmModal
  // ------------------------------------------------------------------

  describe("handleDeleteSaves", () => {
    async function openDeleteSavesModal() {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const items = await openRomMMenuAndGetItems(testAppId);
      // delete-saves: items.length - 2 (uninstall is last; delete-saves is
      // the one before).
      const deleteSaves = items[items.length - 2]!;
      act(() => {
        deleteSaves.props.onClick?.();
      });
      return lastShowModalProps<{
        strTitle?: string;
        strDescription?: string;
        onOK?: () => Promise<void>;
      }>();
    }

    it("opens a ConfirmModal with the right copy", async () => {
      const props = await openDeleteSavesModal();
      expect(props?.strTitle).toBe("Delete Local Saves");
      expect(props?.strDescription).toContain("local save files");
    });

    it("OK happy path: deleteLocalSaves + dispatches save_sync + setInfo + toast", async () => {
      vi.mocked(backend.deleteLocalSaves).mockResolvedValue({
        success: true,
        deleted_count: 4,
        message: "Deleted 4",
      });
      const props = await openDeleteSavesModal();
      vi.mocked(toaster.toast).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await props?.onOK?.();
        });
        expect(vi.mocked(backend.deleteLocalSaves)).toHaveBeenCalledWith(42);
        expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Deleted 4" }));
        const ev = listener.mock.calls.map((c) => c[0] as CustomEvent).find((e) => e.detail.type === "save_sync");
        expect(ev?.detail).toEqual({ type: "save_sync", rom_id: 42 });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("OK failure → toasts result.message", async () => {
      vi.mocked(backend.deleteLocalSaves).mockResolvedValue({
        success: false,
        deleted_count: 0,
        message: "perm denied",
      });
      const props = await openDeleteSavesModal();
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await props?.onOK?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "perm denied" }));
    });

    it("OK failure with empty message → 'Failed to delete saves'", async () => {
      vi.mocked(backend.deleteLocalSaves).mockResolvedValue({
        success: false,
        deleted_count: 0,
        message: "",
      });
      const props = await openDeleteSavesModal();
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await props?.onOK?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to delete saves" }),
      );
    });

    it("OK throw → 'Failed to delete saves'", async () => {
      vi.mocked(backend.deleteLocalSaves).mockRejectedValue(new Error("io"));
      const props = await openDeleteSavesModal();
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await props?.onOK?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Failed to delete saves" }),
      );
    });
  });

  // ------------------------------------------------------------------
  // M. handleChangeGameCore (via Core context menu)
  // ------------------------------------------------------------------

  describe("handleChangeGameCore", () => {
    async function setupCoreAction() {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "snes",
        rom_file: "mario.sfc",
        bios_status: {
          platform_slug: "snes",
          server_count: 0,
          local_count: 0,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "OK",
      });
      // Core data is sourced from the dedicated get_platform_core_info path (#923),
      // which feeds extractCoreInfo. The core button gates on availableCores > 1.
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "snes9x.so",
        active_core_label: "Snes9x",
        platform_core_label: null,
        has_game_override: false,
        cores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
      });
      vi.mocked(playSectionUtils.extractCoreInfo).mockReturnValue({
        activeCoreLabel: "Snes9x",
        activeCoreIsDefault: true,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: null,
        hasGameOverride: false,
      });
    }

    // Core menu after the #211 reset-item re-introduction: [compat(disabled),
    // Use System Override, Snes9x (default), BlastEm] — the two separators are
    // filtered out by isMenuItem. Every core (including the default) now PINS;
    // the dedicated "Use System Override" item at index 1 is the clear path.
    const BLASTEM_IDX = 3;

    it("happy path: setGameCore(rom_id, label) → confirms re-baked launch_options, toasts, invalidates + dispatches core_changed", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({
        success: true,
        launch_options: 'flatpak run net.retrodeck.retrodeck -e "...blastem.so..." "/roms/mario.sfc"',
        app_id: 777,
      });
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: null,
        bios_level: null,
        bios_label: null,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();

      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await coreItems[BLASTEM_IDX]!.props.onClick?.();
        });
        // Keyed by rom_id + label (#945) — no platform_slug/romPath args.
        expect(vi.mocked(backend.setGameCore)).toHaveBeenCalledWith(42, "BlastEm");
        // The re-baked launch_options is confirm-set on the bound shortcut BEFORE
        // toasting success (R1).
        expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(
          777,
          'flatpak run net.retrodeck.retrodeck -e "...blastem.so..." "/roms/mario.sfc"',
        );
        // Core display refreshed via the dedicated rom_id path (#923/#945).
        expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(42);
        expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Core set to BlastEm" }));
        expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(testAppId);
        const ev = listener.mock.calls.map((c) => c[0] as CustomEvent).find((e) => e.detail.type === "core_changed");
        expect(ev?.detail).toMatchObject({
          type: "core_changed",
          platform_slug: "snes",
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    // R1 — the silent-success guard. An unconfirmed bake must NOT toast success;
    // it shows the DISTINCT "restart Steam" toast and keeps the DB row.
    it("false-confirm: launch_options set but setLaunchOptionsConfirmed → false yields the DISTINCT restart toast, NOT success", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({
        success: true,
        launch_options: 'flatpak run net.retrodeck.retrodeck -e "...blastem.so..." "/roms/mario.sfc"',
        app_id: 777,
      });
      // The confirm poll never sees the read-back match → false.
      vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(false);
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(cachedStore.invalidateCachedGameDetail).mockClear();
      await act(async () => {
        await coreItems[BLASTEM_IDX]!.props.onClick?.();
      });
      // Post-confirm state (non-vacuous): the DISTINCT restart toast fired …
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Core saved — restart Steam to apply" }),
      );
      // … and the success toast did NOT.
      expect(vi.mocked(toaster.toast)).not.toHaveBeenCalledWith(
        expect.objectContaining({ body: "Core set to BlastEm" }),
      );
      // The DB row is kept; no cache invalidate / refresh happens on the
      // unconfirmed branch (re-sync/migration re-bake from the pin).
      expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).not.toHaveBeenCalled();
    });

    // Uninstalled/unbound: backend persists the pin but returns no launch_options
    // / app_id → no SetAppLaunchOptions, still toasts the saved state.
    it("uninstalled/unbound: success without launch_options/app_id → success toast, no confirm-set", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({ success: true });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[BLASTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Core set to BlastEm" }));
    });

    it("core_unavailable: {success:false} → toasts result.message", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({
        success: false,
        reason: "core_unavailable",
        message: "Core BlastEm not available for snes",
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[BLASTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Core BlastEm not available for snes" }),
      );
    });

    it("setGameCore failure with empty message → 'Failed to set core'", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({
        success: false,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[BLASTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Failed to set core" }));
    });

    it("setGameCore throw → 'Failed to set core'", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockRejectedValue(new Error("boom"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[BLASTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Failed to set core" }));
    });

    // Regression guard (#945): a successful set with a valid rom_id must NOT
    // silently no-op — it must reach the confirm + success toast.
    it("does NOT silently no-op: a valid rom_id drives the confirm + success toast", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({
        success: true,
        launch_options: "lo",
        app_id: 5,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[BLASTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(backend.setGameCore)).toHaveBeenCalledWith(42, "BlastEm");
      expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(5, "lo");
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Core set to BlastEm" }));
    });

    it("missing romId or platformSlug → no-op", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "",
        rom_file: "",
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      // Can't open the core menu because availableCores is empty → core button doesn't render.
      // Verify setGameCore is never called via direct dispatch.
      expect(vi.mocked(backend.setGameCore)).not.toHaveBeenCalled();
    });
  });

  // ------------------------------------------------------------------
  // M2. handleResetGameCore — triggered by the dedicated "Use System Override"
  // item. Picking a core (incl. the default) PINS it; the reset item is the
  // only clear path (#211).
  // ------------------------------------------------------------------

  describe("handleResetGameCore (Use System Override item)", () => {
    async function setupCoreAction() {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "snes",
        rom_file: "mario.sfc",
        bios_status: {
          platform_slug: "snes",
          server_count: 0,
          local_count: 0,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "OK",
      });
      vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue({
        active_core: "blastem.so",
        active_core_label: "BlastEm",
        platform_core_label: null,
        has_game_override: true,
        cores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
      });
      // A per-game core (BlastEm) is pinned (hasGameOverride=true), so the
      // "Use System Override" item carries no ✓ but still clears the pin.
      vi.mocked(playSectionUtils.extractCoreInfo).mockReturnValue({
        activeCoreLabel: "BlastEm",
        activeCoreIsDefault: false,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: null,
        hasGameOverride: true,
      });
    }

    // Menu (#211): [compat(disabled), Use System Override, Snes9x (default),
    // BlastEm] — separators filtered out by isMenuItem.
    const FOLLOW_SYSTEM_IDX = 1;
    const DEFAULT_CORE_IDX = 2;

    it("the Use System Override item calls clearGameCore(rom_id) + confirms the PLAIN launch_options + toasts 'Now following the system core'", async () => {
      await setupCoreAction();
      vi.mocked(backend.clearGameCore).mockResolvedValue({
        success: true,
        launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/mario.sfc"',
        app_id: 888,
      });
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: null,
        bios_level: null,
        bios_label: null,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      // The reset item is the dedicated clear path. A per-game core is pinned
      // (hasGameOverride=true), so the item carries NO ✓; the fallback label is
      // the es_systems default (Snes9x) since no per-platform override is set.
      expect(coreItems[FOLLOW_SYSTEM_IDX]!.props.children).toBe("Use System Override (Snes9x)");
      vi.mocked(toaster.toast).mockClear();
      vi.mocked(backend.getPlatformCoreInfo).mockClear();
      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          await coreItems[FOLLOW_SYSTEM_IDX]!.props.onClick?.();
        });
        expect(vi.mocked(backend.clearGameCore)).toHaveBeenCalledWith(42);
        // The reset item must NOT pin a core.
        expect(vi.mocked(backend.setGameCore)).not.toHaveBeenCalled();
        // The PLAIN (no -e) launch_options is confirm-set on the bound shortcut.
        expect(vi.mocked(setLaunchOptionsConfirmed)).toHaveBeenCalledWith(
          888,
          'flatpak run net.retrodeck.retrodeck "/roms/mario.sfc"',
        );
        expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
          expect.objectContaining({ body: "Now following the system core" }),
        );
        expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledWith(42);
        expect(vi.mocked(cachedStore.invalidateCachedGameDetail)).toHaveBeenCalledWith(testAppId);
        const ev = listener.mock.calls.map((c) => c[0] as CustomEvent).find((e) => e.detail.type === "core_changed");
        expect(ev?.detail).toMatchObject({ type: "core_changed", platform_slug: "snes" });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("picking the default-marked core PINS it via setGameCore (no longer clears) (#211)", async () => {
      await setupCoreAction();
      vi.mocked(backend.setGameCore).mockResolvedValue({
        success: true,
        launch_options: 'flatpak run net.retrodeck.retrodeck -e "...snes9x.so..." "/roms/mario.sfc"',
        app_id: 777,
      });
      vi.mocked(backend.getBiosStatus).mockResolvedValue({
        bios_status: null,
        bios_level: null,
        bios_label: null,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      expect(coreItems[DEFAULT_CORE_IDX]!.props.children).toBe("Snes9x (default)");
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[DEFAULT_CORE_IDX]!.props.onClick?.();
      });
      // The default core now PINS via setGameCore — the clear path is the
      // dedicated reset item only.
      expect(vi.mocked(backend.setGameCore)).toHaveBeenCalledWith(42, "Snes9x");
      expect(vi.mocked(backend.clearGameCore)).not.toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Core set to Snes9x" }));
    });

    it("reset-item false-confirm → DISTINCT restart toast, NOT success", async () => {
      await setupCoreAction();
      vi.mocked(backend.clearGameCore).mockResolvedValue({
        success: true,
        launch_options: 'flatpak run net.retrodeck.retrodeck "/roms/mario.sfc"',
        app_id: 888,
      });
      vi.mocked(setLaunchOptionsConfirmed).mockResolvedValue(false);
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[FOLLOW_SYSTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Core saved — restart Steam to apply" }),
      );
      expect(vi.mocked(toaster.toast)).not.toHaveBeenCalledWith(
        expect.objectContaining({ body: "Now following the system core" }),
      );
    });

    it("reset-item uninstalled/unbound: success without launch_options/app_id → success toast, no confirm-set", async () => {
      await setupCoreAction();
      vi.mocked(backend.clearGameCore).mockResolvedValue({ success: true });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[FOLLOW_SYSTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(setLaunchOptionsConfirmed)).not.toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({ body: "Now following the system core" }),
      );
    });

    it("reset-item {success:false} → toasts result.message", async () => {
      await setupCoreAction();
      vi.mocked(backend.clearGameCore).mockResolvedValue({ success: false, message: "clear failed" });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[FOLLOW_SYSTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "clear failed" }));
    });

    it("reset-item throw → 'Failed to reset core'", async () => {
      await setupCoreAction();
      vi.mocked(backend.clearGameCore).mockRejectedValue(new Error("boom"));
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const coreItems = await openCoreMenuAndGetItems(testAppId);
      vi.mocked(toaster.toast).mockClear();
      await act(async () => {
        await coreItems[FOLLOW_SYSTEM_IDX]!.props.onClick?.();
      });
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(expect.objectContaining({ body: "Failed to reset core" }));
    });
  });

  // ------------------------------------------------------------------
  // N. Context menus structure (RomM / Core / Steam)
  // ------------------------------------------------------------------

  describe("context menus", () => {
    it("showRomMMenu yields 6 MenuItems + 1 separator (Refresh artwork/metadata/saves/bios + delete-saves + uninstall)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
      });
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const items = await openRomMMenuAndGetItems(testAppId);
      expect(items).toHaveLength(6);
      // tone="destructive" on the delete-saves + uninstall ones.
      const destructive = items.filter((i) => i.props.tone === "destructive");
      expect(destructive).toHaveLength(2);
    });

    async function setupCoreMenuStructure(extractCore: ReturnType<typeof playSectionUtils.extractCoreInfo>) {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "snes",
        rom_file: "mario.sfc",
        bios_status: {
          platform_slug: "snes",
          server_count: 0,
          local_count: 0,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "OK",
      });
      vi.mocked(playSectionUtils.extractCoreInfo).mockReturnValue(extractCore);
      render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      return openCoreMenuAndGetItems(testAppId);
    }

    it("showCoreMenu yields compat note + Use System Override item + 1 MenuItem per core; ✓ on the active default core (#945/#211)", async () => {
      // Default core active, no per-game override → following the system, so the
      // ✓ sits on BOTH the reset item and the active default core (#211).
      const items = await setupCoreMenuStructure({
        activeCoreLabel: "Snes9x",
        activeCoreIsDefault: true,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: null,
        hasGameOverride: false,
      });
      // 1 disabled compat note + Use System Override + 2 core items (both
      // separators filtered out by isMenuItem).
      expect(items).toHaveLength(4);
      // The obsolete RetroDECK-bug warning MenuItem is gone — only ONE disabled item.
      expect(items.filter((i) => i.props.disabled === true)).toHaveLength(1);
      expect(items[0]!.props.disabled).toBe(true);
      // The reset item carries the ✓ (no per-game override) and the fallback
      // label is the es_systems default (no per-platform override set).
      expect(items[1]!.props.children).toBe("Use System Override (Snes9x) ✓");
      // The active default core ALSO carries the ✓ (in effect).
      expect(items[2]!.props.children).toBe("Snes9x (default) ✓");
      expect(items[3]!.props.children).toBe("BlastEm");
    });

    it("showCoreMenu marks the pinned non-default core with ✓ (not the default entry, not the reset item) (#945/#211)", async () => {
      const items = await setupCoreMenuStructure({
        activeCoreLabel: "BlastEm",
        activeCoreIsDefault: false,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: null,
        hasGameOverride: true,
      });
      expect(items).toHaveLength(4);
      // A per-game core is pinned → the reset item has NO ✓ …
      expect(items[1]!.props.children).toBe("Use System Override (Snes9x)");
      // … the default entry has no ✓ …
      expect(items[2]!.props.children).toBe("Snes9x (default)");
      // … and only the pinned core carries it.
      expect(items[3]!.props.children).toBe("BlastEm ✓");
    });

    it("showCoreMenu marks the per-platform override core with (system), and only that core (#954)", async () => {
      // BlastEm is the per-platform override set on the System page; the active
      // core is the default Snes9x. The (system) marker sits on BlastEm only —
      // Snes9x carries (default) ✓ but NOT (system).
      const items = await setupCoreMenuStructure({
        activeCoreLabel: "Snes9x",
        activeCoreIsDefault: true,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: "BlastEm",
        hasGameOverride: false,
      });
      expect(items).toHaveLength(4);
      // The per-platform override core carries (system); a different core does not.
      expect(items[3]!.props.children).toBe("BlastEm (system)");
      expect(items[2]!.props.children).toBe("Snes9x (default) ✓");
      expect(items[2]!.props.children).not.toContain("(system)");
    });

    it("Use System Override fallback label is the per-platform override when one is set (#211)", async () => {
      // A per-platform override (BlastEm) is set → the reset item's fallback
      // label is the per-platform core, NOT the es_systems default. No per-game
      // override → the reset item carries the ✓.
      const items = await setupCoreMenuStructure({
        activeCoreLabel: "BlastEm",
        activeCoreIsDefault: false,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: "BlastEm",
        hasGameOverride: false,
      });
      expect(items[1]!.props.children).toBe("Use System Override (BlastEm) ✓");
    });

    it("following the system: BOTH the reset item AND the resolved active core carry ✓ (#211)", async () => {
      // No per-game override, per-platform override (BlastEm) is the active core
      // → the game follows the system, so the ✓ appears on the reset item and on
      // the resolved active core (BlastEm), but NOT on the default Snes9x.
      const items = await setupCoreMenuStructure({
        activeCoreLabel: "BlastEm",
        activeCoreIsDefault: false,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: "BlastEm",
        hasGameOverride: false,
      });
      expect(items[1]!.props.children).toContain("✓");
      expect(items[1]!.props.children).toBe("Use System Override (BlastEm) ✓");
      // The resolved active core carries (system) + ✓.
      expect(items[3]!.props.children).toBe("BlastEm (system) ✓");
      // The default (not active) carries neither.
      expect(items[2]!.props.children).toBe("Snes9x (default)");
    });

    it("per-game core pinned: only the pinned core carries ✓, the reset item does not (#211)", async () => {
      // A per-game override pins BlastEm (also the per-platform override). Only
      // BlastEm carries the ✓; the reset item does NOT (hasGameOverride=true).
      const items = await setupCoreMenuStructure({
        activeCoreLabel: "BlastEm",
        activeCoreIsDefault: false,
        availableCores: [
          { core_so: "snes9x.so", label: "Snes9x", is_default: true },
          { core_so: "blastem.so", label: "BlastEm", is_default: false },
        ],
        platformCoreLabel: "BlastEm",
        hasGameOverride: true,
      });
      expect(items[1]!.props.children).not.toContain("✓");
      expect(items[1]!.props.children).toBe("Use System Override (BlastEm)");
      expect(items[3]!.props.children).toBe("BlastEm (system) ✓");
    });

    it("showSteamMenu Properties → SteamClient.Apps.OpenAppSettingsDialog(appId, 'general')", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: false,
      });
      const { getAllByTitle } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      const steamBtn = getAllByTitle("Steam Properties")[0]!;
      vi.mocked(showContextMenu).mockClear();
      act(() => {
        steamBtn.click();
      });
      const menuEl = lastContextMenuElement();
      expect(menuEl).not.toBeNull();
      const items = getMenuItemsFromElement(menuEl!);
      // Properties is the only item.
      expect(items).toHaveLength(1);
      act(() => {
        items[0]!.props.onClick?.();
      });
      expect(vi.mocked(SteamClient.Apps.OpenAppSettingsDialog)).toHaveBeenCalledWith(testAppId, "general");
    });
  });

  // ------------------------------------------------------------------
  // O. Conditional info items
  // ------------------------------------------------------------------

  describe("conditional info items", () => {
    it("offline indicator renders when connectionState is offline", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: false,
        message: "",
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("RomM offline");
    });

    it("lastPlayed item renders when info.lastPlayed is truthy", async () => {
      stubAppStore({
        [testAppId]: { rt_last_time_played: 1234, minutes_playtime_forever: 0 },
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("LAST PLAYED");
      expect(container.textContent).toContain("2024-01-15");
    });

    it("playtime item renders when info.playtime is truthy", async () => {
      stubAppStore({
        [testAppId]: { rt_last_time_played: 0, minutes_playtime_forever: 90 },
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("PLAYTIME");
      expect(container.textContent).toContain("1h 30m");
    });

    it("achievements item renders when raId is set; clicking dispatches romm_tab_switch with tab=achievements", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        ra_id: 12345,
        achievement_summary: { earned: 3, total: 50, earned_hardcore: 0 },
      });
      const { container, getByText } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("ACHIEVEMENTS");
      const listener = vi.fn();
      globalThis.addEventListener("romm_tab_switch", listener);
      try {
        act(() => {
          getByText("ACHIEVEMENTS").click();
        });
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({ tab: "achievements" });
      } finally {
        globalThis.removeEventListener("romm_tab_switch", listener);
      }
    });

    it("legacy slot warning shows when activeSlot null and saveSyncEnabled true", async () => {
      // The component's initial activeSlot is "default" (not null); we'd need
      // refreshActiveSlotInBackground to set it to null. Easier: re-mock the
      // refresh helper to apply the null directly via the setter callback.
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
        save_sync_display: { status: "none", label: "No saves", last_sync_check_at: null },
      });
      vi.mocked(sectionRefresh.refreshActiveSlotInBackground).mockImplementation((_romId, _cancelled, setter) => {
        act(() => {
          setter((prev) => ({ ...prev, activeSlot: null }));
        });
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("Legacy save slot");
    });

    it("BIOS warning shows when biosNeeded + biosStatus is 'partial' or 'missing'; click dispatches romm_tab_switch with tab=bios", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 0,
          all_downloaded: false,
        },
        bios_level: "missing",
        bios_label: "0/3",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "missing",
        biosLabel: "0/3",
      });
      const { container, getByText } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).toContain("BIOS");
      const listener = vi.fn();
      globalThis.addEventListener("romm_tab_switch", listener);
      try {
        act(() => {
          getByText("BIOS").click();
        });
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({ tab: "bios" });
      } finally {
        globalThis.removeEventListener("romm_tab_switch", listener);
      }
    });

    it("BIOS warning suppressed when biosStatus is 'ok'", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        bios_status: {
          platform_slug: "snes",
          server_count: 1,
          local_count: 1,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "OK",
      });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      // Render row exists but no BIOS info item.
      expect(container.textContent).not.toContain("BIOS");
    });

    it("core button only renders when availableCores.length > 1", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        platform_slug: "snes",
        bios_status: {
          platform_slug: "snes",
          server_count: 0,
          local_count: 0,
          all_downloaded: true,
        },
        bios_level: "ok",
        bios_label: "OK",
      });
      vi.mocked(playSectionUtils.extractBiosInfo).mockReturnValue({
        biosNeeded: true,
        biosStatus: "ok",
        biosLabel: "OK",
      });
      // Single core from the dedicated path → button hidden.
      vi.mocked(playSectionUtils.extractCoreInfo).mockReturnValue({
        activeCoreLabel: "OnlyOne",
        activeCoreIsDefault: true,
        availableCores: [{ core_so: "x.so", label: "OnlyOne", is_default: true }],
        platformCoreLabel: null,
        hasGameOverride: false,
      });
      const { queryByTitle } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(queryByTitle("Emulator Core")).toBeNull();
    });
  });

  // ------------------------------------------------------------------
  // G. savefiles_in_content_dir warning banner (#239)
  // ------------------------------------------------------------------

  describe("savefiles_in_content_dir warning banner (#239)", () => {
    // The content-dir probe reads getSaveStatus live (NOT getCachedGameDetail),
    // gated only on romId + saveSyncEnabled — intentionally NOT on connectivity,
    // so the banner surfaces offline. These tests force the connection check
    // offline to prove the banner does not depend on a connected server.
    function stubSaveStatus(romId: number, savefilesInContentDir: boolean) {
      vi.mocked(backend.getSaveStatus).mockResolvedValue({
        rom_id: romId,
        files: [],
        playtime: { total_seconds: 0, session_count: 0, last_session_start: null, last_session_duration_sec: null },
        device_id: "d",
        last_sync_check_at: null,
        savefiles_in_content_dir: savefilesInContentDir,
        save_sync_display: { status: "none", label: "Save sync off — saves in content dir", last_sync_check_at: null },
      });
    }

    it("renders the WarningCard with the explanatory message when the flag is true", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
      });
      stubSaveStatus(42, true);
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      // Non-vacuous: assert the visible banner copy, not just a flag.
      expect(container.textContent).toContain("Save sync off");
      expect(container.textContent).toContain(
        "RetroArch's 'Write Saves to Content Directory' is enabled, so saves go next to the ROM and can't be synced.",
      );
      expect(container.textContent).toContain("RetroArch → Settings → Saving");
    });

    it("surfaces the banner even when the RomM server is OFFLINE (flag is local)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 7,
        save_sync_enabled: true,
      });
      // Server unreachable — the connection effect goes offline, but the
      // local-derived content-dir flag must still populate the banner.
      vi.mocked(backend.testConnection).mockResolvedValue({ success: false, message: "" });
      stubSaveStatus(7, true);
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      // Offline indicator AND the banner are both present.
      expect(container.textContent).toContain("RomM offline");
      expect(container.textContent).toContain("Save sync off");
      expect(container.textContent).toContain("can't be synced");
    });

    it("does NOT render the banner when the flag is false", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
      });
      stubSaveStatus(42, false);
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(container.textContent).not.toContain("Write Saves to Content Directory");
      // The play row still renders (game remains playable).
      expect(container.querySelector('[data-testid="play-button"]')).not.toBeNull();
    });

    it("does NOT probe getSaveStatus for the flag when save sync is disabled", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: false,
      });
      vi.mocked(backend.testConnection).mockResolvedValue({ success: false, message: "" });
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.getSaveStatus)).not.toHaveBeenCalled();
      expect(container.textContent).not.toContain("Write Saves to Content Directory");
    });

    it("logs via debugLog when the content-dir probe rejects (non-vacuous catch)", async () => {
      vi.mocked(cachedStore.getCachedGameDetail).mockResolvedValue({
        found: true,
        rom_id: 42,
        save_sync_enabled: true,
      });
      // Reject from getSaveStatus — the probe's catch must log, and the banner
      // must stay hidden (flag defaults to false).
      vi.mocked(backend.getSaveStatus).mockRejectedValue(new Error("cfgfail"));
      const { container } = render(<RomMPlaySection appId={testAppId} />);
      await flushAsync();
      expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(expect.stringContaining("content-dir probe error"));
      expect(container.textContent).not.toContain("Write Saves to Content Directory");
    });
  });
});

// ----- Shared helpers — placed at the bottom of the file so they read like
// declarations. They open a context menu by clicking the corresponding
// DialogButton (RomM Actions / Emulator Core), then return the MenuItem
// children from the captured Menu element. -----

// MenuSeparator filter: the mocked MenuSeparator component has no `children`
// prop and no `onClick`. MenuItems always carry children (label text). Use the
// presence of `children` as the signal — it's stable across both menus.
function isMenuItem(c: MenuItemElement): boolean {
  return c.props.children !== undefined;
}

async function openRomMMenuAndGetItems(_appId: number): Promise<MenuItemElement[]> {
  // Find the RomM Actions button via title attribute.
  const btn = document.querySelector('button[title="RomM Actions"]') as HTMLButtonElement | null;
  if (!btn) throw new Error("RomM Actions button not found");
  vi.mocked(showContextMenu).mockClear();
  act(() => {
    btn.click();
  });
  const calls = vi.mocked(showContextMenu).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement | undefined;
  if (!el) throw new Error("No context menu shown");
  return getMenuItemsFromElement(el).filter(isMenuItem);
}

async function openCoreMenuAndGetItems(_appId: number): Promise<MenuItemElement[]> {
  const btn = document.querySelector('button[title="Emulator Core"]') as HTMLButtonElement | null;
  if (!btn) throw new Error("Emulator Core button not found");
  vi.mocked(showContextMenu).mockClear();
  act(() => {
    btn.click();
  });
  const calls = vi.mocked(showContextMenu).mock.calls;
  const el = calls[calls.length - 1]?.[0] as ReactElement | undefined;
  if (!el) throw new Error("No context menu shown");
  // Core menu has: 1 disabled compat note + separator + Use System Override
  // MenuItem + separator + N core MenuItems (separators dropped by isMenuItem).
  // (#211)
  return getMenuItemsFromElement(el).filter(isMenuItem);
}
