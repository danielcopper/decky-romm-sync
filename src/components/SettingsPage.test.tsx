// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) side effect MUST have its side effect
// asserted in the test (status string, captured prop on a child, logError
// spy, etc.). Only truly-`/* ignore */` catches (no state change, no log
// call) are exempt — and even then, prefer dropping the test over keeping
// one with zero expects.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ComponentProps, type ReactElement } from "react";
import { SettingsPage } from "./SettingsPage";
import * as backend from "../api/backend";
import type { SaveSortMigrationStatus, RegisteredDevice } from "../types";
import { showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import {
  setSaveSortMigrationStatus,
  clearSaveSortMigration,
  onSaveSortMigrationChange,
} from "../utils/saveSortMigrationStore";
import { pendingEdits } from "./settings/TextInputModal";

// Type-only imports — vi.mock(...) below replaces the runtime implementations,
// but capturing props off the real prop interfaces keeps assertions in sync as
// the sub-sections evolve.
import type { ConnectionSection } from "./settings/ConnectionSection";
import type { SteamGridDBSection } from "./settings/SteamGridDBSection";
import type { SaveSyncSection } from "./settings/SaveSyncSection";
import type { RegisteredDevicesSection } from "./settings/RegisteredDevicesSection";
import type { ControllerSection } from "./settings/ControllerSection";
import type { AdvancedSection } from "./settings/AdvancedSection";
import type { LibrarySection } from "./settings/LibrarySection";
import { showPreferredRegionModal } from "./settings/PreferredRegionModal";
import type { SaveSortMigrationSection } from "./settings/SaveSortMigrationSection";

type ConnectionProps = ComponentProps<typeof ConnectionSection>;
type SteamGridDBProps = ComponentProps<typeof SteamGridDBSection>;
type SaveSyncProps = ComponentProps<typeof SaveSyncSection>;
type RegisteredDevicesProps = ComponentProps<typeof RegisteredDevicesSection>;
type ControllerProps = ComponentProps<typeof ControllerSection>;
type AdvancedProps = ComponentProps<typeof AdvancedSection>;
type LibraryProps = ComponentProps<typeof LibrarySection>;
type SaveSortMigrationProps = ComponentProps<typeof SaveSortMigrationSection>;

// Captured props arrays — reset in beforeEach. Each child mock pushes the
// props it was called with so tests can inspect handler wiring + state
// passed down without re-rendering the real (already-tested) children.
const capturedConnection: ConnectionProps[] = [];
const capturedSgdb: SteamGridDBProps[] = [];
const capturedSaveSync: SaveSyncProps[] = [];
const capturedDevices: RegisteredDevicesProps[] = [];
const capturedController: ControllerProps[] = [];
const capturedAdvanced: AdvancedProps[] = [];
const capturedLibrary: LibraryProps[] = [];
const capturedMigration: SaveSortMigrationProps[] = [];

vi.mock("./settings/ConnectionSection", () => ({
  ConnectionSection: (p: ConnectionProps) => {
    capturedConnection.push(p);
    return createElement("div", { "data-testid": "connection-section" });
  },
}));
vi.mock("./settings/SteamGridDBSection", () => ({
  SteamGridDBSection: (p: SteamGridDBProps) => {
    capturedSgdb.push(p);
    return createElement("div", { "data-testid": "sgdb-section" });
  },
}));
vi.mock("./settings/SaveSyncSection", () => ({
  SaveSyncSection: (p: SaveSyncProps) => {
    capturedSaveSync.push(p);
    return createElement("div", { "data-testid": "savesync-section" });
  },
}));
vi.mock("./settings/RegisteredDevicesSection", () => ({
  RegisteredDevicesSection: (p: RegisteredDevicesProps) => {
    capturedDevices.push(p);
    return createElement("div", { "data-testid": "devices-section" });
  },
}));
vi.mock("./settings/ControllerSection", () => ({
  ControllerSection: (p: ControllerProps) => {
    capturedController.push(p);
    return createElement("div", { "data-testid": "controller-section" });
  },
}));
vi.mock("./settings/AdvancedSection", () => ({
  AdvancedSection: (p: AdvancedProps) => {
    capturedAdvanced.push(p);
    return createElement("div", { "data-testid": "advanced-section" });
  },
}));
vi.mock("./settings/LibrarySection", async (importOriginal) => {
  // Keep the real AUTO_REGION / DEFAULT_REGION_LABEL constants (SettingsPage
  // imports them), but stub the component to capture props.
  const actual = await importOriginal<typeof import("./settings/LibrarySection")>();
  return {
    ...actual,
    LibrarySection: (p: LibraryProps) => {
      capturedLibrary.push(p);
      return createElement("div", { "data-testid": "library-section" });
    },
  };
});

// The Preferred-region change modal — mocked so tests control confirm/cancel.
vi.mock("./settings/PreferredRegionModal", () => ({
  showPreferredRegionModal: vi.fn(() => Promise.resolve(true)),
}));
vi.mock("./settings/SaveSortMigrationSection", () => ({
  SaveSortMigrationSection: (p: SaveSortMigrationProps) => {
    capturedMigration.push(p);
    return createElement("div", { "data-testid": "migration-section" });
  },
}));

// pendingEdits is a mutable module-level object — tests may pre-populate it
// before render to verify the mount-time override path.
vi.mock("./settings/TextInputModal", () => ({
  pendingEdits: {} as { url?: string; username?: string; password?: string },
}));

// Local @decky/ui re-mock — the global stub in src/test-setup.ts doesn't ship
// a ButtonItem (used here for Back), so render() would crash on "Element type
// is invalid". We mirror the stubs we need (ButtonItem + ConfirmModal +
// showModal + PanelSection/Row) and keep showModal a vi.fn so the call-capture
// pattern still works.
type AnyProps = Record<string, unknown> & { children?: unknown };
vi.mock("@decky/ui", () => ({
  PanelSection: (p: AnyProps) => createElement("section", null, p.children as never),
  PanelSectionRow: (p: AnyProps) => createElement("div", null, p.children as never),
  ButtonItem: (p: AnyProps & { onClick?: () => void }) =>
    createElement("button", { onClick: p.onClick }, p.children as never),
  ConfirmModal: (p: AnyProps) => createElement("div", { "data-testid": "confirm-modal" }, p.children as never),
  showModal: vi.fn(),
}));

// scrollToTop is a no-op in jsdom; mock for cleanliness.
vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// Mock the saveSortMigrationStore — own listener list + state so tests can
// drive the subscribe/unsubscribe + state-change flow deterministically.
const saveSortListeners: Array<() => void> = [];
let currentSortState: SaveSortMigrationStatus = { pending: false };
vi.mock("../utils/saveSortMigrationStore", () => ({
  getSaveSortMigrationState: vi.fn(() => currentSortState),
  setSaveSortMigrationStatus: vi.fn((s: SaveSortMigrationStatus) => {
    currentSortState = s;
    saveSortListeners.forEach((fn) => fn());
  }),
  clearSaveSortMigration: vi.fn(() => {
    currentSortState = { pending: false };
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

// Wait one microtask for the mount-time useEffect promises to resolve.
const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

// logError isn't a callable — it's a plain function wrapping a frontendLog
// callable. We can't `vi.mocked(backend.logError)` it; instead replace it via
// `vi.spyOn(backend, "logError")` per-test and inspect the spy directly.
// (Inlined as `vi.spyOn(backend, "logError")` at each call site — the spyOn
// generic constraint is brittle to alias under our TS config.)

// Default settings payload — tests override per case.
function defaultSettings(): import("../types").PluginSettings {
  return {
    romm_url: "https://romm.local",
    has_token: true,
    steam_input_mode: "default",
    sgdb_api_key_masked: "",
    log_level: "warn",
    romm_allow_insecure_ssl: false,
  };
}

function defaultSaveSyncSettings(): import("../types").SaveSyncSettings {
  return {
    save_sync_enabled: false,
    sync_before_launch: true,
    sync_after_exit: true,
    default_slot: "default",
    autocleanup_limit: 10,
  };
}

function lastConfirmModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    capturedConnection.length = 0;
    capturedSgdb.length = 0;
    capturedSaveSync.length = 0;
    capturedDevices.length = 0;
    capturedController.length = 0;
    capturedAdvanced.length = 0;
    capturedLibrary.length = 0;
    capturedMigration.length = 0;
    saveSortListeners.length = 0;
    currentSortState = { pending: false };
    for (const k of Object.keys(pendingEdits) as Array<keyof typeof pendingEdits>) {
      delete pendingEdits[k];
    }
    // Defaults — many tests override per case.
    vi.mocked(backend.getSettings).mockResolvedValue(defaultSettings());
    vi.mocked(backend.getKnownRegions).mockResolvedValue([]);
    vi.mocked(showPreferredRegionModal).mockResolvedValue(true);
    vi.mocked(backend.getSaveSyncSettings).mockResolvedValue(defaultSaveSyncSettings());
    vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({ pending: false });
    vi.mocked(backend.listDevices).mockResolvedValue({ success: true, devices: [] });
    vi.mocked(backend.ensureDeviceRegistered).mockResolvedValue({
      success: true,
      device_id: "dev-1",
      device_name: "Test Deck",
    });
  });

  describe("initial mount — getSettings", () => {
    it("applies the full settings payload to ConnectionSection / SteamGridDBSection / ControllerSection / AdvancedSection", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        romm_url: "https://my.romm",
        has_token: true,
        romm_allow_insecure_ssl: true,
        sgdb_api_key_masked: "abc",
        steam_input_mode: "force_on",
        log_level: "debug",
        retroarch_input_check: { warning: true, current: "sdl2" },
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.url).toBe("https://my.romm");
      expect(conn?.hasToken).toBe(true);
      expect(conn?.allowInsecureSsl).toBe(true);

      const sgdb = capturedSgdb[capturedSgdb.length - 1];
      expect(sgdb?.sgdbApiKey).toBe("abc");

      const ctrl = capturedController[capturedController.length - 1];
      expect(ctrl?.steamInputMode).toBe("force_on");
      expect(ctrl?.retroarchWarning).toEqual({ warning: true, current: "sdl2" });

      const adv = capturedAdvanced[capturedAdvanced.length - 1];
      expect(adv?.logLevel).toBe("debug");
    });

    it("prefers a pendingEdits URL over the backend value", async () => {
      pendingEdits.url = "https://pending.url";
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.url).toBe("https://pending.url");
    });

    it("hydrates hasToken from getSettings", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedConnection[capturedConnection.length - 1]?.hasToken).toBe(false);
    });

    it("does not set retroarchWarning when retroarch_input_check is absent", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedController[capturedController.length - 1]?.retroarchWarning).toBeNull();
    });

    it("logs the failure and surfaces 'Failed to load settings' when getSettings rejects", async () => {
      vi.mocked(backend.getSettings).mockRejectedValue(new Error("boom"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to load settings"));
      logSpy.mockRestore();
    });
  });

  describe("initial mount — getSaveSyncSettings", () => {
    it("forwards the fetched settings to SaveSyncSection", async () => {
      const s = { ...defaultSaveSyncSettings(), save_sync_enabled: true };
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue(s);
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const ss = capturedSaveSync[capturedSaveSync.length - 1];
      expect(ss?.saveSyncSettings).toEqual(s);
    });

    it("calls ensureDeviceRegistered + listDevices when save_sync_enabled is true", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(backend.ensureDeviceRegistered)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.listDevices)).toHaveBeenCalledTimes(1);
      // deviceInfo flowed through to SaveSyncSection
      const ss = capturedSaveSync[capturedSaveSync.length - 1];
      expect(ss?.deviceInfo).toEqual({ device_id: "dev-1", device_name: "Test Deck" });
    });

    it("does NOT call ensureDeviceRegistered / listDevices when disabled", async () => {
      // defaults to disabled
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(backend.ensureDeviceRegistered)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.listDevices)).not.toHaveBeenCalled();
    });

    it("does NOT set deviceInfo when ensureDeviceRegistered returns success=false", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.ensureDeviceRegistered).mockResolvedValue({
        success: false,
        device_id: "",
        device_name: "",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedSaveSync[capturedSaveSync.length - 1]?.deviceInfo).toBeNull();
    });

    it("swallows an ensureDeviceRegistered rejection (no logError)", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.ensureDeviceRegistered).mockRejectedValue(new Error("net"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedSaveSync[capturedSaveSync.length - 1]?.deviceInfo).toBeNull();
      // Catch is `.catch(() => {})` — rejection must NOT escape to logError.
      expect(logSpy).not.toHaveBeenCalled();
      logSpy.mockRestore();
    });

    it("logs the failure when getSaveSyncSettings rejects", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockRejectedValue(new Error("denied"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to load save sync settings"));
      logSpy.mockRestore();
    });
  });

  describe("initial mount — getSaveSortMigrationStatus", () => {
    it("forwards a pending status into the store and into local state", async () => {
      const pending: SaveSortMigrationStatus = {
        pending: true,
        old_settings: { sort_by_content: true, sort_by_core: false },
        new_settings: { sort_by_content: false, sort_by_core: false },
        saves_count: 5,
      };
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue(pending);
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(setSaveSortMigrationStatus)).toHaveBeenCalledWith(pending);
      expect(capturedMigration[capturedMigration.length - 1]?.migration).toEqual(pending);
    });

    it("does nothing when the status is not pending", async () => {
      // defaults — pending=false
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(setSaveSortMigrationStatus)).not.toHaveBeenCalled();
    });

    it("silently swallows a getSaveSortMigrationStatus rejection", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockRejectedValue(new Error("oops"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      // No logError for this branch — it's a fire-and-forget probe.
      const calls = logSpy.mock.calls.map((c) => c[0]);
      expect(calls.some((m) => m.includes("save_sort_migration"))).toBe(false);
      logSpy.mockRestore();
    });
  });

  describe("loadDevices flow", () => {
    it("forwards the devices list down on listDevices success", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      const devices: RegisteredDevice[] = [
        {
          id: "x",
          name: "X",
          platform: null,
          client: null,
          client_version: null,
          last_seen: null,
          created_at: "",
          is_current_device: false,
        },
      ];
      vi.mocked(backend.listDevices).mockResolvedValue({ success: true, devices });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedDevices[capturedDevices.length - 1]?.registeredDevices).toEqual(devices);
    });

    it("hides the section (registeredDevices=null) when listDevices returns disabled", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.listDevices).mockResolvedValue({
        success: false,
        devices: [],
        disabled: true,
      });
      const { queryByTestId } = render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("devices-section")).toBeNull();
    });

    it("surfaces the human-readable message (not the slug) from listDevices via devicesError + empty list", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      // Canonical failure shape: the routing slug lives on `reason`, the
      // human-readable text on `message`. The UI must render `message`, never
      // the raw slug (the #972 user-visible bug: "Could not load devices —
      // list_failed" leaked the slug).
      vi.mocked(backend.listDevices).mockResolvedValue({
        success: false,
        devices: [],
        reason: "server_unreachable",
        message: "Could not load devices",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const d = capturedDevices[capturedDevices.length - 1];
      expect(d?.devicesError).toBe("Could not load devices");
      // The raw slug must NOT surface to the user.
      expect(d?.devicesError).not.toBe("server_unreachable");
      expect(d?.registeredDevices).toEqual([]);
    });

    it("falls back to a generic message when message is absent on a failed response", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.listDevices).mockResolvedValue({
        success: false,
        devices: [],
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedDevices[capturedDevices.length - 1]?.devicesError).toBe("Failed to load devices");
    });

    it("surfaces a thrown listDevices via devicesError (Error.message)", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.listDevices).mockRejectedValue(new Error("network down"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const d = capturedDevices[capturedDevices.length - 1];
      expect(d?.devicesError).toBe("network down");
      expect(d?.registeredDevices).toEqual([]);
    });

    it("falls back to 'Failed to load devices' when listDevices throws a non-Error", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.listDevices).mockRejectedValue("string error");
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedDevices[capturedDevices.length - 1]?.devicesError).toBe("Failed to load devices");
    });
  });

  describe("connection handlers fed to ConnectionSection", () => {
    it("handleUrlChange persists URL + SSL via saveServerUrl and clears the pending URL edit", async () => {
      vi.mocked(backend.saveServerUrl).mockResolvedValue({ success: true, message: "" });
      pendingEdits.url = "draft";
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onUrlChange("https://new.url");
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveServerUrl)).toHaveBeenCalledWith("https://new.url", false);
      expect(pendingEdits.url).toBeUndefined();
    });

    it("rejects an invalid URL inline without calling saveServerUrl", async () => {
      vi.mocked(backend.saveServerUrl).mockResolvedValue({ success: true, message: "" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onUrlChange("romm.local"); // no scheme
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveServerUrl)).not.toHaveBeenCalled();
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe(
        "Enter a valid http:// or https:// server URL",
      );
    });

    it("trims the URL before persisting", async () => {
      vi.mocked(backend.saveServerUrl).mockResolvedValue({ success: true, message: "" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onUrlChange("  https://new.url  ");
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveServerUrl)).toHaveBeenCalledWith("https://new.url", false);
    });

    it("does not delete the pending URL edit when saveServerUrl rejects (status fallback wired)", async () => {
      vi.mocked(backend.saveServerUrl).mockRejectedValue(new Error("nope"));
      pendingEdits.url = "draft";
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onUrlChange("https://new.url");
        await Promise.resolve();
      });

      expect(pendingEdits.url).toBe("draft");
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("Failed to save settings");
    });

    it("handleAllowInsecureSslChange forwards the URL + new flag to saveServerUrl", async () => {
      vi.mocked(backend.saveServerUrl).mockResolvedValue({ success: true, message: "" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onAllowInsecureSslChange(true);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveServerUrl)).toHaveBeenCalledWith("https://romm.local", true);
    });

    it("handleAllowInsecureSslChange surfaces 'Failed to save settings' on rejection", async () => {
      vi.mocked(backend.saveServerUrl).mockRejectedValue(new Error("ssl"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onAllowInsecureSslChange(true);
        await Promise.resolve();
      });

      // The .catch sets setStatus("Failed to save settings") — assert it
      // surfaced via ConnectionSection.status.
      const last = capturedConnection[capturedConnection.length - 1];
      expect(last?.status).toBe("Failed to save settings");
    });
  });

  describe("handleConnect (credential → token flow)", () => {
    it("calls connectWithCredentials with url + creds + ssl, surfaces the message, and sets hasToken on success", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      vi.mocked(backend.connectWithCredentials).mockResolvedValue({
        success: true,
        message: "Connected!",
        romm_version: "4.8.1",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      // Precondition: not yet connected.
      expect(capturedConnection[capturedConnection.length - 1]?.hasToken).toBe(false);

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnect("daniel", "hunter2");
      });

      expect(vi.mocked(backend.connectWithCredentials)).toHaveBeenCalledWith(
        "https://romm.local",
        "daniel",
        "hunter2",
        false,
      );
      expect(result).toMatchObject({ success: true, message: "Connected!" });
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("Connected!");
      expect(conn?.hasToken).toBe(true);
    });

    it("returns the failure to the modal without setting hasToken or the bottom status (e.g. 403 auth_failed)", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      vi.mocked(backend.connectWithCredentials).mockResolvedValue({
        success: false,
        message: "This account cannot create API tokens.",
        reason: "auth_failed",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnect("admin", "pw");
      });

      // The still-open modal surfaces the message; the bottom status stays clear.
      expect(result).toMatchObject({ success: false, message: "This account cannot create API tokens." });
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("");
      expect(conn?.hasToken).toBe(false);
    });

    it("returns an invalid-URL failure to the modal without calling connectWithCredentials", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        romm_url: "romm.local", // scheme-less — invalid
        has_token: false,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnect("daniel", "hunter2");
      });

      expect(vi.mocked(backend.connectWithCredentials)).not.toHaveBeenCalled();
      expect(result).toEqual({ success: false, message: "Enter a valid http:// or https:// server URL" });
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("");
    });

    it("returns a generic failure to the modal when connectWithCredentials throws", async () => {
      vi.mocked(backend.connectWithCredentials).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnect("daniel", "hunter2");
      });

      expect(result).toEqual({ success: false, message: "Sign-in failed. Check your connection and try again." });
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("");
    });
  });

  describe("handleConnectToken (pasted API token flow)", () => {
    it("calls connectWithToken with url + token + ssl, surfaces the message, and sets hasToken on success", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      vi.mocked(backend.connectWithToken).mockResolvedValue({
        success: true,
        message: "Connected!",
        romm_version: "4.9.0",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedConnection[capturedConnection.length - 1]?.hasToken).toBe(false);

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectToken("rmm_pasted");
      });

      expect(vi.mocked(backend.connectWithToken)).toHaveBeenCalledWith("https://romm.local", "rmm_pasted", false);
      expect(result).toMatchObject({ success: true, message: "Connected!" });
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("Connected!");
      expect(conn?.hasToken).toBe(true);
    });

    it("returns the failure to the modal without setting hasToken or the bottom status (e.g. 403 scope error)", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      vi.mocked(backend.connectWithToken).mockResolvedValue({
        success: false,
        message: "The API token is missing required permissions (scopes).",
        reason: "auth_failed",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectToken("rmm_readonly");
      });

      expect(result).toMatchObject({
        success: false,
        message: "The API token is missing required permissions (scopes).",
      });
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("");
      expect(conn?.hasToken).toBe(false);
    });

    it("returns an invalid-URL failure to the modal without calling connectWithToken", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        romm_url: "romm.local", // scheme-less — invalid
        has_token: false,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectToken("rmm_pasted");
      });

      expect(vi.mocked(backend.connectWithToken)).not.toHaveBeenCalled();
      expect(result).toEqual({ success: false, message: "Enter a valid http:// or https:// server URL" });
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("");
    });

    it("returns a generic failure to the modal when connectWithToken throws", async () => {
      vi.mocked(backend.connectWithToken).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectToken("rmm_pasted");
      });

      expect(result).toEqual({ success: false, message: "Sign-in failed. Check your connection and try again." });
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("");
    });
  });

  describe("handleConnectPairing (pairing-code flow)", () => {
    it("calls connectWithPairingCode with url + code + ssl, surfaces the message, and sets hasToken on success", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      vi.mocked(backend.connectWithPairingCode).mockResolvedValue({
        success: true,
        message: "Connected!",
        romm_version: "4.9.0",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedConnection[capturedConnection.length - 1]?.hasToken).toBe(false);

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectPairing("ABCD2345");
      });

      expect(vi.mocked(backend.connectWithPairingCode)).toHaveBeenCalledWith("https://romm.local", "ABCD2345", false);
      // The paired flow must not fall through to the pasted-token callable.
      expect(vi.mocked(backend.connectWithToken)).not.toHaveBeenCalled();
      expect(result).toMatchObject({ success: true, message: "Connected!" });
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("Connected!");
      expect(conn?.hasToken).toBe(true);
    });

    it("returns the failure to the modal without setting hasToken or the bottom status (e.g. expired code)", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: false,
      });
      vi.mocked(backend.connectWithPairingCode).mockResolvedValue({
        success: false,
        message: "Pairing code is invalid or has expired.",
        reason: "auth_failed",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectPairing("BADCODE1");
      });

      expect(result).toMatchObject({ success: false, message: "Pairing code is invalid or has expired." });
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("");
      expect(conn?.hasToken).toBe(false);
    });

    it("returns an invalid-URL failure to the modal without calling connectWithPairingCode", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        romm_url: "romm.local", // scheme-less — invalid
        has_token: false,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectPairing("ABCD2345");
      });

      expect(vi.mocked(backend.connectWithPairingCode)).not.toHaveBeenCalled();
      expect(result).toEqual({ success: false, message: "Enter a valid http:// or https:// server URL" });
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("");
    });

    it("returns a generic failure to the modal when connectWithPairingCode throws", async () => {
      vi.mocked(backend.connectWithPairingCode).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      let result: { success: boolean; message: string } | undefined;
      await act(async () => {
        result = await capturedConnection[capturedConnection.length - 1]?.onConnectPairing("ABCD2345");
      });

      expect(result).toEqual({ success: false, message: "Sign-in failed. Check your connection and try again." });
      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("");
    });
  });

  describe("handleSignOut (local token forget)", () => {
    it("calls signOut, surfaces the message, and flips hasToken to false on success", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: true,
      });
      vi.mocked(backend.signOut).mockResolvedValue({
        success: true,
        message: "Signed out. The token is still valid in RomM.",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      // Precondition: signed in.
      expect(capturedConnection[capturedConnection.length - 1]?.hasToken).toBe(true);

      await act(async () => {
        capturedConnection[capturedConnection.length - 1]?.onSignOut();
        await Promise.resolve();
      });

      expect(vi.mocked(backend.signOut)).toHaveBeenCalledTimes(1);
      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("Signed out. The token is still valid in RomM.");
      expect(conn?.hasToken).toBe(false);
    });

    it("keeps hasToken true when signOut reports failure", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: true,
      });
      vi.mocked(backend.signOut).mockResolvedValue({
        success: false,
        message: "Could not save settings.",
        reason: "config_error",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        capturedConnection[capturedConnection.length - 1]?.onSignOut();
        await Promise.resolve();
      });

      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("Could not save settings.");
      expect(conn?.hasToken).toBe(true);
    });

    it("sets status='Sign-out failed' when signOut throws", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        has_token: true,
      });
      vi.mocked(backend.signOut).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        capturedConnection[capturedConnection.length - 1]?.onSignOut();
        await Promise.resolve();
      });

      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.status).toBe("Sign-out failed");
      // A thrown sign-out leaves the session intact.
      expect(conn?.hasToken).toBe(true);
    });
  });

  describe("handleSaveSyncSettingChange", () => {
    it("does nothing when saveSyncSettings is still null", async () => {
      // Cause getSaveSyncSettings to never resolve — saveSyncSettings stays null.
      vi.mocked(backend.getSaveSyncSettings).mockImplementation(
        () =>
          new Promise(() => {
            /* never */
          }),
      );
      render(<SettingsPage onBack={vi.fn()} />);
      // No flush — initial state null. capturedSaveSync still has at least one
      // entry from the synchronous first render.
      const ss = capturedSaveSync[capturedSaveSync.length - 1];
      expect(ss?.saveSyncSettings).toBeNull();
      await act(async () => {
        ss?.onSettingChange({ sync_before_launch: false });
      });
      expect(vi.mocked(backend.updateSaveSyncSettings)).not.toHaveBeenCalled();
    });

    it("updates a non-enabled partial via updateSaveSyncSettings without dispatching", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          capturedSaveSync[capturedSaveSync.length - 1]?.onSettingChange({
            sync_before_launch: false,
          });
        });

        expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
          expect.objectContaining({ sync_before_launch: false }),
        );
        expect(listener).not.toHaveBeenCalled();
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("dispatches romm_data_changed with detail.save_sync_enabled=true and triggers loadDevices on enable", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      vi.mocked(backend.listDevices).mockResolvedValue({ success: true, devices: [] });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          capturedSaveSync[capturedSaveSync.length - 1]?.onSettingChange({
            save_sync_enabled: true,
          });
        });

        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({
          type: "save_sync_settings",
          save_sync_enabled: true,
        });
        // loadDevices triggered after enable
        expect(vi.mocked(backend.listDevices)).toHaveBeenCalledTimes(1);
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("dispatches romm_data_changed with save_sync_enabled=false on disable", async () => {
      // Start enabled so that toggle-off path runs
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          capturedSaveSync[capturedSaveSync.length - 1]?.onSettingChange({
            save_sync_enabled: false,
          });
        });
        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({
          type: "save_sync_settings",
          save_sync_enabled: false,
        });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("clears registeredDevices on disable (probed via re-enable with a stalled listDevices)", async () => {
      // Mount enabled so that the initial listDevices populates
      // registeredDevices to [] (a non-null value).
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      vi.mocked(backend.listDevices).mockResolvedValue({ success: true, devices: [] });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      // Sanity: before disable, devices section is mounted with [] (non-null).
      const preDisable = capturedDevices[capturedDevices.length - 1];
      expect(preDisable?.registeredDevices).toEqual([]);

      // Disable — this should run setRegisteredDevices(null).
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onSettingChange({
          save_sync_enabled: false,
        });
      });

      // Now re-enable, but stall listDevices so devicesLoading stays true and
      // the section mounts immediately (guard: enabled && (loading || devices !== null)).
      // The captured registeredDevices prop on this mount reveals whether
      // setRegisteredDevices(null) ran during the disable step:
      //   - If line 194 ran  → registeredDevices is null
      //   - If line 194 did NOT run → registeredDevices is still []
      vi.mocked(backend.listDevices).mockImplementation(
        () =>
          new Promise(() => {
            /* stall */
          }),
      );
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onSettingChange({
          save_sync_enabled: true,
        });
      });

      // Probe: the most recent RegisteredDevicesSection render (during the
      // loading state of the second loadDevices call) must see null.
      const postReEnable = capturedDevices[capturedDevices.length - 1];
      expect(postReEnable?.devicesLoading).toBe(true);
      expect(postReEnable?.registeredDevices).toBeNull();
    });

    it("logs the failure when updateSaveSyncSettings rejects", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockRejectedValue(new Error("denied"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onSettingChange({
          sync_before_launch: false,
        });
      });
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to save settings"));
      logSpy.mockRestore();
    });
  });

  describe("handleSyncAll", () => {
    it("forwards syncAllSaves result.message to syncStatus and dispatches romm_data_changed on success", async () => {
      vi.mocked(backend.syncAllSaves).mockResolvedValue({
        success: true,
        message: "Synced 4",
        synced: 4,
        conflicts: 0,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const listener = vi.fn();
      globalThis.addEventListener("romm_data_changed", listener);
      try {
        await act(async () => {
          capturedSaveSync[capturedSaveSync.length - 1]?.onSyncAll();
        });
        expect(capturedSaveSync[capturedSaveSync.length - 1]?.syncStatus).toBe("Synced 4");
        expect(listener).toHaveBeenCalledTimes(1);
        const ev = listener.mock.calls[0]?.[0] as CustomEvent;
        expect(ev.detail).toEqual({ type: "save_sync" });
      } finally {
        globalThis.removeEventListener("romm_data_changed", listener);
      }
    });

    it("sets syncStatus='Sync failed' on throw", async () => {
      vi.mocked(backend.syncAllSaves).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onSyncAll();
      });
      expect(capturedSaveSync[capturedSaveSync.length - 1]?.syncStatus).toBe("Sync failed");
    });
  });

  describe("handleToggleSaveSync — enable confirmation flow", () => {
    it("opens the enable-save-sync ConfirmModal when toggled to true", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      act(() => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onToggleSaveSync(true);
      });
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      const props = lastConfirmModalProps<{
        strTitle?: string;
        strDescription?: string;
        strOKButtonText?: string;
        strCancelButtonText?: string;
        onOK?: () => void;
        onCancel?: () => void;
      }>();
      expect(props?.strTitle).toBe("Enable Save Sync?");
      expect(props?.strDescription).toContain("RetroArch game saves");
      // #1189: the copy must not claim only .srm files sync, and points at the support matrix instead.
      expect(props?.strDescription).not.toContain("(.srm)");
      expect(props?.strDescription).toContain("support matrix");
      expect(props?.strOKButtonText).toBe("I am sure");
      expect(props?.strCancelButtonText).toBe("Cancel");
    });

    it("invokes handleSaveSyncSettingChange({save_sync_enabled:true}) when OK is clicked", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      act(() => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onToggleSaveSync(true);
      });
      const props = lastConfirmModalProps<{ onOK?: () => void | Promise<void> }>();
      await act(async () => {
        await props?.onOK?.();
      });
      expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ save_sync_enabled: true }),
      );
    });

    it("bumps saveSyncToggleKey when Cancel is clicked", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const initialKey = capturedSaveSync[capturedSaveSync.length - 1]?.saveSyncToggleKey;
      expect(initialKey).toBe(0);

      act(() => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onToggleSaveSync(true);
      });
      const props = lastConfirmModalProps<{ onCancel?: () => void }>();
      act(() => {
        props?.onCancel?.();
      });
      const newKey = capturedSaveSync[capturedSaveSync.length - 1]?.saveSyncToggleKey;
      expect(newKey).toBe(1);
    });
  });

  describe("handleToggleSaveSync — disable path", () => {
    it("calls handleSaveSyncSettingChange({save_sync_enabled:false}) directly without showing a modal", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onToggleSaveSync(false);
        await Promise.resolve();
      });
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ save_sync_enabled: false }),
      );
    });
  });

  describe("default-slot submit + reset", () => {
    it("forwards a trimmed non-empty value to handleSaveSyncSettingChange", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onDefaultSlotSubmit("  alpha  ");
        await Promise.resolve();
      });
      expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ default_slot: "alpha" }),
      );
    });

    it("resets to 'default' without a confirm modal when the value is empty", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onDefaultSlotSubmit("   ");
        await Promise.resolve();
      });
      expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ default_slot: "default" }),
      );
      // The old "enables legacy mode" clear-slot confirm modal is gone.
      expect(vi.mocked(showModal)).not.toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "RomM Sync",
          body: 'Default save slot reset to "default".',
        }),
      );
    });

    it("handleResetDefaultSlot sets default_slot='default' and toasts", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onResetDefaultSlot();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ default_slot: "default" }),
      );
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "RomM Sync",
          body: 'Default save slot reset to "default".',
        }),
      );
    });
  });

  describe("SteamGridDB handlers", () => {
    it("wires onVerifyKey directly to the verifySgdbApiKey callable (tests the key without persisting)", async () => {
      vi.mocked(backend.verifySgdbApiKey).mockResolvedValue({ success: true, message: "API key is valid" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const sgdb = capturedSgdb[capturedSgdb.length - 1];
      await act(async () => {
        await sgdb?.onVerifyKey("apikey123");
      });
      expect(vi.mocked(backend.verifySgdbApiKey)).toHaveBeenCalledWith("apikey123");
      // Verifying never persists — that is the modal's second step (onSaveKey).
      expect(vi.mocked(backend.saveSgdbApiKey)).not.toHaveBeenCalled();
    });

    it("handleSaveSgdbKey persists via saveSgdbApiKey and flips the masked display to a configured key", async () => {
      vi.mocked(backend.saveSgdbApiKey).mockResolvedValue({ success: true, message: "Saved!" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        await capturedSgdb[capturedSgdb.length - 1]?.onSaveKey("apikey123");
      });
      expect(vi.mocked(backend.saveSgdbApiKey)).toHaveBeenCalledWith("apikey123");
      // A configured key renders as the masked "••••" (sgdbApiKey truthy).
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbApiKey).toBe("set");
    });

    it("handleSaveSgdbKey lets a save rejection propagate so the modal can surface it", async () => {
      vi.mocked(backend.saveSgdbApiKey).mockRejectedValue(new Error("boom"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const sgdb = capturedSgdb[capturedSgdb.length - 1];
      // The handler does not swallow — the modal's own try/catch owns the error
      // UI, so onSaveKey must reject rather than resolve.
      await expect(sgdb?.onSaveKey("k")).rejects.toThrow("boom");
      // A failed save must not flip the masked display to configured.
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbApiKey).toBe("");
    });
  });

  describe("Controller handlers", () => {
    it("handleSteamInputModeChange persists via saveSteamInputSetting and updates the dropdown", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      act(() => {
        capturedController[capturedController.length - 1]?.onModeChange("force_on");
      });
      expect(vi.mocked(backend.saveSteamInputSetting)).toHaveBeenCalledWith("force_on");
      expect(capturedController[capturedController.length - 1]?.steamInputMode).toBe("force_on");
    });

    it("handleApplySteamInput success forwards result.message into steamInputStatus", async () => {
      vi.mocked(backend.applySteamInputSetting).mockResolvedValue({
        success: true,
        message: "Applied",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedController[capturedController.length - 1]?.onApplyMode();
        await Promise.resolve();
      });
      expect(capturedController[capturedController.length - 1]?.steamInputStatus).toBe("Applied");
    });

    it("handleApplySteamInput throw → steamInputStatus='Failed to apply'", async () => {
      vi.mocked(backend.applySteamInputSetting).mockRejectedValue(new Error("boom"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedController[capturedController.length - 1]?.onApplyMode();
        await Promise.resolve();
      });
      expect(capturedController[capturedController.length - 1]?.steamInputStatus).toBe("Failed to apply");
    });

    it("handleFixInputDriver success=true clears the retroarchWarning + surfaces result.message", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        retroarch_input_check: { warning: true, current: "sdl2" },
      });
      vi.mocked(backend.fixRetroarchInputDriver).mockResolvedValue({
        success: true,
        message: "Fixed",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      // Pre-condition: warning is set
      expect(capturedController[capturedController.length - 1]?.retroarchWarning).not.toBeNull();

      await act(async () => {
        capturedController[capturedController.length - 1]?.onFixInputDriver();
        await Promise.resolve();
      });
      const ctrl = capturedController[capturedController.length - 1];
      expect(ctrl?.retroarchWarning).toBeNull();
      expect(ctrl?.retroarchFixStatus).toBe("Fixed");
    });

    it("handleFixInputDriver success=false leaves the warning + surfaces the message", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        retroarch_input_check: { warning: true, current: "sdl2" },
      });
      vi.mocked(backend.fixRetroarchInputDriver).mockResolvedValue({
        success: false,
        message: "Could not write",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedController[capturedController.length - 1]?.onFixInputDriver();
        await Promise.resolve();
      });
      const ctrl = capturedController[capturedController.length - 1];
      expect(ctrl?.retroarchWarning).not.toBeNull();
      expect(ctrl?.retroarchFixStatus).toBe("Could not write");
    });

    it("handleFixInputDriver throw → retroarchFixStatus='Failed to apply fix'", async () => {
      vi.mocked(backend.fixRetroarchInputDriver).mockRejectedValue(new Error("perm"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedController[capturedController.length - 1]?.onFixInputDriver();
        await Promise.resolve();
      });
      expect(capturedController[capturedController.length - 1]?.retroarchFixStatus).toBe("Failed to apply fix");
    });
  });

  describe("Advanced handlers", () => {
    it("handleLogLevelChange persists via saveLogLevel and updates the dropdown", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      act(() => {
        capturedAdvanced[capturedAdvanced.length - 1]?.onLogLevelChange("debug");
      });
      expect(vi.mocked(backend.saveLogLevel)).toHaveBeenCalledWith("debug");
      expect(capturedAdvanced[capturedAdvanced.length - 1]?.logLevel).toBe("debug");
    });
  });

  describe("Library handlers", () => {
    it("hydrates preferredRegion from getSettings", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        preferred_region: "Japan",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedLibrary[capturedLibrary.length - 1]?.preferredRegion).toBe("Japan");
    });

    it("defaults preferredRegion to 'auto' when getSettings omits it", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedLibrary[capturedLibrary.length - 1]?.preferredRegion).toBe("auto");
    });

    it("forwards library regions from getKnownRegions to LibrarySection", async () => {
      vi.mocked(backend.getKnownRegions).mockResolvedValue(["Korea", "Brazil"]);
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedLibrary[capturedLibrary.length - 1]?.libraryRegions).toEqual(["Korea", "Brazil"]);
    });

    it("change → confirm shows the explanation modal, then persists and updates the dropdown", async () => {
      vi.mocked(showPreferredRegionModal).mockResolvedValue(true);
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedLibrary[capturedLibrary.length - 1]?.onPreferredRegionChange("Japan");
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // The modal is shown with the human-readable old→new labels ("auto" → default label).
      expect(vi.mocked(showPreferredRegionModal)).toHaveBeenCalledWith("Default (World > USA > Europe)", "Japan");
      expect(vi.mocked(backend.savePreferredRegion)).toHaveBeenCalledWith("Japan");
      expect(capturedLibrary[capturedLibrary.length - 1]?.preferredRegion).toBe("Japan");
    });

    it("change → cancel shows the modal but does NOT persist and leaves the dropdown unchanged", async () => {
      vi.mocked(showPreferredRegionModal).mockResolvedValue(false);
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedLibrary[capturedLibrary.length - 1]?.onPreferredRegionChange("Japan");
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(showPreferredRegionModal)).toHaveBeenCalledTimes(1);
      expect(vi.mocked(backend.savePreferredRegion)).not.toHaveBeenCalled();
      expect(capturedLibrary[capturedLibrary.length - 1]?.preferredRegion).toBe("auto");
    });

    it("selecting the already-current region is a no-op (no modal, no save)", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedLibrary[capturedLibrary.length - 1]?.onPreferredRegionChange("auto");
        await Promise.resolve();
      });
      expect(vi.mocked(showPreferredRegionModal)).not.toHaveBeenCalled();
      expect(vi.mocked(backend.savePreferredRegion)).not.toHaveBeenCalled();
    });

    it("hydrates platformGroups from getSettings", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        collection_create_platform_groups: true,
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedLibrary[capturedLibrary.length - 1]?.platformGroups).toBe(true);
    });

    it("defaults platformGroups to false when getSettings omits it", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(capturedLibrary[capturedLibrary.length - 1]?.platformGroups).toBe(false);
    });

    it("onPlatformGroupsChange persists via saveCollectionPlatformGroups and flips the value", async () => {
      vi.mocked(backend.saveCollectionPlatformGroups).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedLibrary[capturedLibrary.length - 1]?.onPlatformGroupsChange(true);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.saveCollectionPlatformGroups)).toHaveBeenCalledWith(true);
      expect(capturedLibrary[capturedLibrary.length - 1]?.platformGroups).toBe(true);
    });

    it("reverts platformGroups when saveCollectionPlatformGroups rejects", async () => {
      vi.mocked(backend.saveCollectionPlatformGroups).mockRejectedValue(new Error("boom"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedLibrary[capturedLibrary.length - 1]?.onPlatformGroupsChange(true);
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
      });
      // CATCH-REJECTION assert: rolled back to false.
      expect(capturedLibrary[capturedLibrary.length - 1]?.platformGroups).toBe(false);
    });
  });

  describe("save-sort migration handlers", () => {
    it("handleMigrateSaveSort success clears the store, toasts, and forwards result.message", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({
        pending: true,
        old_settings: { sort_by_content: true, sort_by_core: false },
        new_settings: { sort_by_content: false, sort_by_core: false },
        saves_count: 2,
      });
      vi.mocked(backend.migrateSaveSortFiles).mockResolvedValue({
        success: true,
        message: "Moved 2 files",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        capturedMigration[capturedMigration.length - 1]?.onMigrate();
        await Promise.resolve();
      });

      expect(vi.mocked(clearSaveSortMigration)).toHaveBeenCalled();
      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "RomM Sync",
          body: "Moved 2 files",
        }),
      );
    });

    it("handleMigrateSaveSort success with empty message falls back to 'Migration complete.'", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({
        pending: true,
      });
      vi.mocked(backend.migrateSaveSortFiles).mockResolvedValue({
        success: true,
        message: "",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        capturedMigration[capturedMigration.length - 1]?.onMigrate();
        await Promise.resolve();
      });

      expect(vi.mocked(toaster.toast)).toHaveBeenCalledWith(
        expect.objectContaining({
          body: "Migration complete.",
        }),
      );
    });

    it("handleMigrateSaveSort throw → onMigrate result='Migration failed'", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({
        pending: true,
      });
      vi.mocked(backend.migrateSaveSortFiles).mockRejectedValue(new Error("io"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedMigration[capturedMigration.length - 1]?.onMigrate();
        await Promise.resolve();
      });
      expect(capturedMigration[capturedMigration.length - 1]?.result).toBe("Migration failed");
    });

    it("handleDismissSaveSort calls dismissSaveSortMigration + clearSaveSortMigration", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({
        pending: true,
      });
      vi.mocked(backend.dismissSaveSortMigration).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedMigration[capturedMigration.length - 1]?.onDismiss();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.dismissSaveSortMigration)).toHaveBeenCalled();
      expect(vi.mocked(clearSaveSortMigration)).toHaveBeenCalled();
    });

    it("handleDismissSaveSort silently swallows a rejection", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({
        pending: true,
      });
      vi.mocked(backend.dismissSaveSortMigration).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedMigration[capturedMigration.length - 1]?.onDismiss();
        await Promise.resolve();
      });
      // No assertion beyond "did not throw" — the catch block is `/* ignore */`.
    });
  });

  describe("saveSortMigrationStore subscribe / unsubscribe", () => {
    it("subscribes on mount and unsubscribes on unmount", async () => {
      const { unmount } = render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(onSaveSortMigrationChange)).toHaveBeenCalledTimes(1);
      expect(saveSortListeners.length).toBe(1);
      unmount();
      expect(saveSortListeners.length).toBe(0);
    });

    it("re-renders the migration section when the store flips to pending", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      // Initially: no migration section because pending=false.
      expect(capturedMigration.length).toBe(0);

      // Drive a store flip — simulates a different surface (e.g. launch
      // interceptor) setting the pending state while SettingsPage is mounted.
      await act(async () => {
        vi.mocked(setSaveSortMigrationStatus)({
          pending: true,
          saves_count: 1,
        });
      });
      expect(capturedMigration.length).toBeGreaterThan(0);
      expect(capturedMigration[capturedMigration.length - 1]?.migration.pending).toBe(true);
    });
  });

  describe("conditional renders", () => {
    it("hides RegisteredDevicesSection when save sync is disabled", async () => {
      const { queryByTestId } = render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("devices-section")).toBeNull();
    });

    it("shows RegisteredDevicesSection when save sync is enabled and a device list arrives", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      const { queryByTestId } = render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("devices-section")).not.toBeNull();
    });

    it("hides SaveSortMigrationSection when pending=false", async () => {
      const { queryByTestId } = render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("migration-section")).toBeNull();
    });

    it("shows SaveSortMigrationSection when pending=true", async () => {
      vi.mocked(backend.getSaveSortMigrationStatus).mockResolvedValue({
        pending: true,
        saves_count: 3,
      });
      const { queryByTestId } = render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("migration-section")).not.toBeNull();
    });
  });

  describe("back button", () => {
    it("calls onBack when the Back button is clicked", async () => {
      const onBack = vi.fn();
      const { getByText } = render(<SettingsPage onBack={onBack} />);
      await flushAsync();
      fireEvent.click(getByText("Back"));
      expect(onBack).toHaveBeenCalledTimes(1);
    });
  });
});
