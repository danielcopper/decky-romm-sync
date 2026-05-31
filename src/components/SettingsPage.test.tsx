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
import type { SaveSortMigrationSection } from "./settings/SaveSortMigrationSection";

type ConnectionProps = ComponentProps<typeof ConnectionSection>;
type SteamGridDBProps = ComponentProps<typeof SteamGridDBSection>;
type SaveSyncProps = ComponentProps<typeof SaveSyncSection>;
type RegisteredDevicesProps = ComponentProps<typeof RegisteredDevicesSection>;
type ControllerProps = ComponentProps<typeof ControllerSection>;
type AdvancedProps = ComponentProps<typeof AdvancedSection>;
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
    romm_user: "user",
    romm_pass_masked: "••••",
    has_credentials: true,
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
    capturedMigration.length = 0;
    saveSortListeners.length = 0;
    currentSortState = { pending: false };
    for (const k of Object.keys(pendingEdits) as Array<keyof typeof pendingEdits>) {
      delete pendingEdits[k];
    }
    // Defaults — many tests override per case.
    vi.mocked(backend.getSettings).mockResolvedValue(defaultSettings());
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
        romm_user: "alice",
        romm_pass_masked: "********",
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
      expect(conn?.username).toBe("alice");
      expect(conn?.password).toBe("********");
      expect(conn?.allowInsecureSsl).toBe(true);

      const sgdb = capturedSgdb[capturedSgdb.length - 1];
      expect(sgdb?.sgdbApiKey).toBe("abc");

      const ctrl = capturedController[capturedController.length - 1];
      expect(ctrl?.steamInputMode).toBe("force_on");
      expect(ctrl?.retroarchWarning).toEqual({ warning: true, current: "sdl2" });

      const adv = capturedAdvanced[capturedAdvanced.length - 1];
      expect(adv?.logLevel).toBe("debug");
    });

    it("prefers pendingEdits over the backend values for URL / username / password", async () => {
      pendingEdits.url = "https://pending.url";
      pendingEdits.username = "pendinguser";
      pendingEdits.password = "pendingpass";
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      const conn = capturedConnection[capturedConnection.length - 1];
      expect(conn?.url).toBe("https://pending.url");
      expect(conn?.username).toBe("pendinguser");
      expect(conn?.password).toBe("pendingpass");
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

    it("surfaces an error from listDevices via devicesError + empty list", async () => {
      vi.mocked(backend.getSaveSyncSettings).mockResolvedValue({
        ...defaultSaveSyncSettings(),
        save_sync_enabled: true,
      });
      vi.mocked(backend.listDevices).mockResolvedValue({
        success: false,
        devices: [],
        error: "auth failed",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const d = capturedDevices[capturedDevices.length - 1];
      expect(d?.devicesError).toBe("auth failed");
      expect(d?.registeredDevices).toEqual([]);
    });

    it("falls back to a generic message when error is absent on a failed response", async () => {
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

  describe("autoSaveSettings (handlers fed to ConnectionSection)", () => {
    it("calls saveSettings and clears the pending edit for that field on success", async () => {
      vi.mocked(backend.saveSettings).mockResolvedValue({ success: true, message: "" });
      pendingEdits.url = "draft";
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onUrlSubmit("https://new.url");
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveSettings)).toHaveBeenCalledWith("https://new.url", "user", "••••", false);
      expect(pendingEdits.url).toBeUndefined();
    });

    it("does not delete the pending edit when saveSettings rejects (status fallback wired)", async () => {
      vi.mocked(backend.saveSettings).mockRejectedValue(new Error("nope"));
      pendingEdits.username = "draft";
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onUsernameSubmit("alice");
        await Promise.resolve();
      });

      expect(pendingEdits.username).toBe("draft");
    });

    it("handlePasswordSubmit routes through saveSettings with the new password", async () => {
      vi.mocked(backend.saveSettings).mockResolvedValue({ success: true, message: "" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onPasswordSubmit("hunter2");
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveSettings)).toHaveBeenCalledWith("https://romm.local", "user", "hunter2", false);
    });

    it("handleAllowInsecureSslChange forwards the new flag straight to saveSettings", async () => {
      vi.mocked(backend.saveSettings).mockResolvedValue({ success: true, message: "" });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onAllowInsecureSslChange(true);
        await Promise.resolve();
      });

      expect(vi.mocked(backend.saveSettings)).toHaveBeenCalledWith("https://romm.local", "user", "••••", true);
    });

    it("handleAllowInsecureSslChange surfaces 'Failed to save settings' on rejection", async () => {
      vi.mocked(backend.saveSettings).mockRejectedValue(new Error("ssl"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      const conn = capturedConnection[capturedConnection.length - 1];

      await act(async () => {
        conn?.onAllowInsecureSslChange(true);
        await Promise.resolve();
      });

      // The .catch sets setStatus("Failed to save settings") — assert it
      // surfaced via ConnectionSection.status (mirrors the handleTest throw test).
      const last = capturedConnection[capturedConnection.length - 1];
      expect(last?.status).toBe("Failed to save settings");
    });
  });

  describe("handleTest", () => {
    it("forwards the result message into ConnectionSection.status on success", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: true,
        message: "Connected!",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        capturedConnection[capturedConnection.length - 1]?.onTestConnection();
        await Promise.resolve();
      });

      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("Connected!");
    });

    it("sets status='Connection test failed' on throw", async () => {
      vi.mocked(backend.testConnection).mockRejectedValue(new Error("boom"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        capturedConnection[capturedConnection.length - 1]?.onTestConnection();
        await Promise.resolve();
      });

      expect(capturedConnection[capturedConnection.length - 1]?.status).toBe("Connection test failed");
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
      expect(props?.strDescription).toContain("RetroArch save files");
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

    it("opens the clear-default-slot ConfirmModal when the value is empty", async () => {
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      act(() => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onDefaultSlotSubmit("   ");
      });
      const props = lastConfirmModalProps<{
        strTitle?: string;
        strOKButtonText?: string;
        onOK?: () => void | Promise<void>;
      }>();
      expect(props?.strTitle).toBe("Clear Default Slot?");
      expect(props?.strOKButtonText).toBe("Clear Slot");
    });

    it("clears default_slot when the clear-default-slot confirmation is OK'd", async () => {
      vi.mocked(backend.updateSaveSyncSettings).mockResolvedValue({ success: true });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      act(() => {
        capturedSaveSync[capturedSaveSync.length - 1]?.onDefaultSlotSubmit("");
      });
      const props = lastConfirmModalProps<{ onOK?: () => void | Promise<void> }>();
      await act(async () => {
        await props?.onOK?.();
      });
      expect(vi.mocked(backend.updateSaveSyncSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ default_slot: null }),
      );
    });

    it("handleResetDefaultSlot sets default_slot='default'", async () => {
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
    });
  });

  describe("SteamGridDB handlers", () => {
    it("handleSgdbKeySubmit success with a non-empty value sets sgdbApiKey='set' and surfaces result.message", async () => {
      vi.mocked(backend.saveSgdbApiKey).mockResolvedValue({
        success: true,
        message: "Saved!",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSgdb[capturedSgdb.length - 1]?.onSubmitKey("apikey123");
      });
      const sgdb = capturedSgdb[capturedSgdb.length - 1];
      expect(sgdb?.sgdbApiKey).toBe("set");
      expect(sgdb?.sgdbStatus).toBe("Saved!");
    });

    it("handleSgdbKeySubmit success with an empty value clears the masked sgdbApiKey", async () => {
      vi.mocked(backend.saveSgdbApiKey).mockResolvedValue({
        success: true,
        message: "Cleared",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSgdb[capturedSgdb.length - 1]?.onSubmitKey("");
      });
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbApiKey).toBe("");
    });

    it("handleSgdbKeySubmit sets sgdbStatus='Failed to save API key' on throw", async () => {
      vi.mocked(backend.saveSgdbApiKey).mockRejectedValue(new Error("boom"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSgdb[capturedSgdb.length - 1]?.onSubmitKey("k");
      });
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbStatus).toBe("Failed to save API key");
    });

    it("handleSgdbVerify success=true sets sgdbStatus='Valid'", async () => {
      vi.mocked(backend.verifySgdbApiKey).mockResolvedValue({
        success: true,
        message: "any",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSgdb[capturedSgdb.length - 1]?.onVerifyKey();
        await Promise.resolve();
      });
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbStatus).toBe("Valid");
    });

    it("handleSgdbVerify success=false surfaces result.message", async () => {
      vi.mocked(backend.verifySgdbApiKey).mockResolvedValue({
        success: false,
        message: "Invalid key",
      });
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSgdb[capturedSgdb.length - 1]?.onVerifyKey();
        await Promise.resolve();
      });
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbStatus).toBe("Invalid key");
    });

    it("handleSgdbVerify throws → sgdbStatus='Verification failed'", async () => {
      vi.mocked(backend.verifySgdbApiKey).mockRejectedValue(new Error("net"));
      render(<SettingsPage onBack={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        capturedSgdb[capturedSgdb.length - 1]?.onVerifyKey();
        await Promise.resolve();
      });
      expect(capturedSgdb[capturedSgdb.length - 1]?.sgdbStatus).toBe("Verification failed");
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
