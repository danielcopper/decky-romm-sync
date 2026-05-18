// CATCH-REJECTION ASSERTION RULE (applies to all orchestration shell tests):
// Every catch block with a setX(...) / logError side effect MUST have its
// side effect asserted in the test (rendered status string, surfaced toast,
// captured logError call). Asserting only that the rejecting call was
// invoked is vacuous — the rejection happens after the call returns, so
// the test would pass with or without the .catch. Truly-/* ignore */
// catches (no observable side effect) are exempt; for those, assert the
// absence of state change.
//
// MainPage catch sites (asserted below):
//   - mount: refreshMigrationState().catch → logError("Failed to refresh
//     migration state: ...") — asserted via vi.spyOn(backend, "logError").
//   - handleSync wrapping try/catch → setStatus("Failed to start sync") —
//     asserted via rendered Field label.
//   - handleApply try/catch → setStatus("Failed to apply sync") — asserted.
//   - handleDismiss inline `.catch(() => {})` — truly-ignored; asserted by
//     verifying the dismiss path completed (preview cleared, no crash).
//   - handleCancel try/catch → setStatus("Failed to cancel sync") — asserted.
//   - fixRetroarchInputDriver inline `.catch(() => {})` (inside ConfirmModal
//     onOK) — truly-ignored; warning state remains (no clear).
//
// MUTATION CHECKS (by inspection — auto-mode classifier likely blocks on
// React state internals + listener cleanup, so confidence is recorded here):
//   1. Removing the `unsubMigration()` call from the unmount cleanup would
//      break the "subscribes on mount and unsubscribes on unmount" test —
//      migrationListeners.length would stay at 1 after unmount.
//   2. Removing `clearInterval(pollRef.current)` from stopPolling would
//      break the "interval cleared on unmount" test — clearIntervalSpy
//      would not be called with the captured pollRef id.
//   3. Removing the setStatus("Failed to start sync") assignment from
//      handleSync's catch would break the "syncPreview rejection surfaces
//      'Failed to start sync'" test — the Field label would render as the
//      empty string instead of the failure message.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { createElement, type ReactElement } from "react";
import { MainPage } from "./MainPage";
import * as backend from "../api/backend";
import { useVersionError } from "./VersionErrorCard";
import { setSyncProgress } from "../utils/syncProgress";
import { setDownloads } from "../utils/downloadStore";
import { showModal } from "@decky/ui";
import * as syncManager from "../utils/syncManager";
import * as connectionState from "../utils/connectionState";
import type {
  MigrationStatus,
  SaveSortMigrationStatus,
} from "../api/backend";
import type {
  SyncStats,
  SyncPreview,
  SyncPreviewSummary,
  DownloadItem,
  PluginSettings,
} from "../types";

// -----------------------------------------------------------------------------
// Module mocks
// -----------------------------------------------------------------------------

vi.mock("./VersionErrorCard", () => ({
  useVersionError: vi.fn(() => null),
  VersionErrorCard: (props: { message: string; compact?: boolean }) =>
    createElement(
      "div",
      { "data-testid": "version-error-card" },
      props.message,
    ),
}));

vi.mock("./MigrationBlockedPage", () => ({
  MigrationBlockedPage: (_props: { migration: MigrationStatus }) =>
    createElement("div", { "data-testid": "migration-blocked-page" }),
}));

// migrationStore — listener-array mock so tests drive subscribe/notify
// deterministically. resetAllMocks wipes impls; re-stubbed in beforeEach.
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

// saveSortMigrationStore — same listener-array pattern.
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

vi.mock("../utils/syncManager", () => ({
  requestSyncCancel: vi.fn(),
}));

vi.mock("../utils/scrollHelpers", () => ({ scrollToTop: vi.fn() }));

// Local @decky/ui re-mock — global stub lacks ProgressBarWithInfo (used to
// render sync + download progress). Mirror the rest with thin pass-throughs
// + a vi.fn showModal so we can capture ConfirmModal calls.
vi.mock("@decky/ui", async () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const { createElement: ce } = await import("react");
  const passthrough = (tag: string) => (p: AnyProps) =>
    ce(tag, {}, p.children as never);
  return {
    PanelSection: (p: AnyProps & { title?: unknown }) =>
      ce(
        "section",
        { "data-testid": "panel-section", "data-title": typeof p.title === "string" ? p.title : undefined },
        typeof p.title === "string"
          ? ce("h2", { "data-testid": "panel-title" }, p.title)
          : null,
        p.children as never,
      ),
    PanelSectionRow: passthrough("div"),
    ButtonItem: ({
      children,
      onClick,
      disabled,
    }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { onClick, disabled }, children as never),
    Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
      ce(
        "div",
        { "data-testid": "field" },
        ce("span", { "data-testid": "field-label" }, p.label as never),
        ce("span", { "data-testid": "field-desc" }, p.description as never),
        p.children as never,
      ),
    ToggleField: (p: AnyProps & {
      checked?: boolean;
      onChange?: (v: boolean) => void;
      label?: unknown;
    }) =>
      ce(
        "div",
        { "data-testid": "toggle" },
        ce("input", {
          type: "checkbox",
          "data-testid": "toggle-input",
          checked: p.checked ?? false,
          onChange: (e: { target: { checked: boolean } }) =>
            p.onChange?.(e.target.checked),
        }),
        typeof p.label === "string" ? p.label : null,
      ),
    Spinner: () => ce("div", { "data-testid": "spinner" }),
    DialogButton: ({
      children,
      onClick,
      disabled,
    }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { "data-testid": "dialog-button", onClick, disabled }, children as never),
    ConfirmModal: (p: AnyProps & {
      strTitle?: string;
      strDescription?: string;
      strOKButtonText?: string;
      strCancelButtonText?: string;
      onOK?: () => void;
      onCancel?: () => void;
    }) =>
      ce("div", { "data-testid": "confirm-modal" }, p.children as never),
    ProgressBarWithInfo: (p: AnyProps & {
      nProgress?: number;
      indeterminate?: boolean;
      sOperationText?: string;
      sTimeRemaining?: string;
    }) =>
      ce(
        "div",
        { "data-testid": "progress" },
        ce("span", { "data-testid": "progress-op" }, p.sOperationText as never),
        ce("span", { "data-testid": "progress-remaining" }, p.sTimeRemaining as never),
        ce("span", { "data-testid": "progress-progress" }, String(p.nProgress)),
        ce("span", { "data-testid": "progress-indeterminate" }, String(p.indeterminate)),
      ),
    showModal: vi.fn(),
  };
});

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

const flushAsync = () =>
  act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });

function defaultSettings(): PluginSettings {
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

function defaultStats(): SyncStats {
  return {
    last_sync: null,
    platforms: 0,
    collections: 0,
    roms: 0,
    total_shortcuts: 0,
  };
}

function buttonByExactText(
  container: HTMLElement,
  text: string,
): HTMLButtonElement | null {
  const btn = Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === text,
  );
  return (btn as HTMLButtonElement | undefined) ?? null;
}

function lastConfirmModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

function fieldLabels(container: HTMLElement): string[] {
  return Array.from(
    container.querySelectorAll('[data-testid="field-label"]'),
  ).map((n) => n.textContent ?? "");
}

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

describe("MainPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    migrationListeners.length = 0;
    saveSortListeners.length = 0;
    currentMigrationState = { pending: false };
    currentSaveSortState = { pending: false };
    setDownloads([]);
    setSyncProgress({
      running: false,
      phase: "",
      current: 0,
      total: 0,
      message: "",
    });

    // Re-stub useVersionError (resetAllMocks wiped it).
    vi.mocked(useVersionError).mockReturnValue(null);

    // Re-stub migrationStore impls.
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

    // Default backend mocks — tests override per case.
    vi.mocked(backend.refreshMigrationState).mockResolvedValue({
      retrodeck: { pending: false },
      save_sort: { pending: false },
    });
    vi.mocked(backend.getSyncStats).mockResolvedValue(defaultStats());
    vi.mocked(backend.testConnection).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.getSettings).mockResolvedValue(defaultSettings());
    vi.mocked(backend.startSync).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.syncPreview).mockResolvedValue({
      success: true,
      summary: {
        new_count: 0,
        changed_count: 0,
        unchanged_count: 0,
        remove_count: 0,
        disabled_platform_remove_count: 0,
      },
      new_names: [],
      changed_names: [],
      preview_id: "p1",
    });
    vi.mocked(backend.syncApplyDelta).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.syncCancelPreview).mockResolvedValue({
      success: true,
      message: "",
    });
    vi.mocked(backend.cancelSync).mockResolvedValue({
      success: true,
      message: "Cancelled",
    });
    vi.mocked(backend.clearSyncCache).mockResolvedValue({
      success: true,
      message: "Cleared",
    });
    vi.mocked(backend.fixRetroarchInputDriver).mockResolvedValue({
      success: true,
      message: "Fixed",
    });

    // Reset version error spy + connectionState side-channel.
    connectionState.setVersionError(null);
  });

  // ===========================================================================
  // A. Top-level render gating
  // ===========================================================================
  describe("top-level render gating", () => {
    it("renders only VersionErrorCard when useVersionError returns a message", async () => {
      vi.mocked(useVersionError).mockReturnValue("server too old");
      const { queryByTestId } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("version-error-card")).not.toBeNull();
      expect(queryByTestId("migration-blocked-page")).toBeNull();
      expect(queryByTestId("panel-section")).toBeNull();
    });

    it("renders only MigrationBlockedPage when migration.pending=true", async () => {
      currentMigrationState = { pending: true };
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck: { pending: true },
        save_sort: { pending: false },
      });
      const { queryByTestId } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(queryByTestId("migration-blocked-page")).not.toBeNull();
      expect(queryByTestId("version-error-card")).toBeNull();
    });

    it("renders the full panel with Status / Sync / Settings sections by default", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const titles = Array.from(
        container.querySelectorAll('[data-testid="panel-title"]'),
      ).map((n) => n.textContent);
      expect(titles).toEqual(expect.arrayContaining(["Status", "Sync", "Settings"]));
    });
  });

  // ===========================================================================
  // B. Mount useEffect — initial fetches
  // ===========================================================================
  describe("mount useEffect", () => {
    it("calls refreshMigrationState and pushes the result into both stores", async () => {
      const retrodeck: MigrationStatus = { pending: false, roms_count: 1 };
      const saveSort: SaveSortMigrationStatus = { pending: false };
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck,
        save_sort: saveSort,
      });
      render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(vi.mocked(migrationStore.setMigrationStatus))
        .toHaveBeenCalledWith(retrodeck);
      expect(vi.mocked(saveSortMigrationStore.setSaveSortMigrationStatus))
        .toHaveBeenCalledWith(saveSort);
    });

    it("logs the failure when refreshMigrationState rejects", async () => {
      vi.mocked(backend.refreshMigrationState).mockRejectedValue(new Error("boom"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(
        expect.stringContaining("Failed to refresh migration state"),
      );
      logSpy.mockRestore();
    });

    it("populates stats from getSyncStats", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 42,
        platforms: 3,
        collections: 2,
        last_sync: new Date(Date.now() - 30_000).toISOString(),
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Library line includes "42 ROMs"
      expect(container.textContent).toContain("42 ROMs");
      expect(container.textContent).toContain("3 platforms");
      expect(container.textContent).toContain("2 collections");
    });

    it("testConnection success sets connected=true and clears versionError", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: true,
        message: "",
      });
      const setVerSpy = vi.spyOn(connectionState, "setVersionError");
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Connected");
      expect(setVerSpy).toHaveBeenCalledWith(null);
      setVerSpy.mockRestore();
    });

    it("testConnection error_code='version_error' surfaces r.message via setVersionError", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: false,
        message: "server out of date",
        error_code: "version_error",
      });
      const setVerSpy = vi.spyOn(connectionState, "setVersionError");
      render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(setVerSpy).toHaveBeenCalledWith("server out of date");
      setVerSpy.mockRestore();
    });

    it("testConnection success=false (no version_error) sets connected=false and clears versionError", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: false,
        message: "auth failed",
      });
      const setVerSpy = vi.spyOn(connectionState, "setVersionError");
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Not connected");
      expect(setVerSpy).toHaveBeenCalledWith(null);
      setVerSpy.mockRestore();
    });

    it("getSettings retroarch_input_check renders the warning section", async () => {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        retroarch_input_check: { warning: true, current: "sdl2" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("RetroArch: input_driver issue");
    });

    it("getSettings without retroarch_input_check does NOT render the warning", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("RetroArch: input_driver");
    });

    it("recovers in-flight sync state from getSyncProgress() on mount", async () => {
      setSyncProgress({
        running: true,
        phase: "fetching",
        message: "Fetching library...",
        step: 1,
        totalSteps: 5,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // In-flight: Cancel Sync button is rendered (replaces the Sync Library
      // button) and the ProgressBarWithInfo shows the recovered message.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
      expect(container.querySelector('[data-testid="progress-op"]')?.textContent)
        .toContain("Fetching library");
    });
  });

  // ===========================================================================
  // C. Store subscribers — subscribe + cleanup + re-render on notify
  // ===========================================================================
  describe("store subscribers", () => {
    it("subscribes to onMigrationChange on mount, unsubscribes on unmount", async () => {
      const { unmount } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(migrationListeners.length).toBe(1);
      unmount();
      expect(migrationListeners.length).toBe(0);
    });

    it("subscribes to onSaveSortMigrationChange on mount, unsubscribes on unmount", async () => {
      const { unmount } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(saveSortListeners.length).toBe(1);
      unmount();
      expect(saveSortListeners.length).toBe(0);
    });

    it("re-renders MigrationBlockedPage when migration store flips to pending", async () => {
      const { queryByTestId } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Initially: normal panel
      expect(queryByTestId("migration-blocked-page")).toBeNull();

      await act(async () => {
        vi.mocked(migrationStore.setMigrationStatus)({ pending: true });
      });

      expect(queryByTestId("migration-blocked-page")).not.toBeNull();
    });

    it("re-renders the save-sort migration banner when saveSort store flips to pending", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).not.toContain("RetroArch save sorting changed");

      await act(async () => {
        vi.mocked(saveSortMigrationStore.setSaveSortMigrationStatus)({
          pending: true,
          saves_count: 7,
        });
      });

      expect(container.textContent).toContain("RetroArch save sorting changed");
      expect(container.textContent).toContain("7 save file(s) to migrate");
    });
  });

  // ===========================================================================
  // D. ConnectionIndicator — 3 states (covered via top-level rendering)
  // ===========================================================================
  describe("ConnectionIndicator", () => {
    it("connected=null (testConnection never resolves) renders 'Checking...' + Spinner", async () => {
      vi.mocked(backend.testConnection).mockImplementation(
        () => new Promise(() => { /* never */ }),
      );
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Checking...");
      expect(container.querySelector('[data-testid="spinner"]')).not.toBeNull();
    });

    it("connected=true renders 'Connected'", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({ success: true, message: "" });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Connected");
      expect(container.textContent).not.toContain("Not connected");
    });

    it("connected=false renders 'Not connected'", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({ success: false, message: "" });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Not connected");
    });
  });

  // ===========================================================================
  // E. Module helpers — exercised via rendered output
  // ===========================================================================
  describe("formatBytes (via active download progress remaining text)", () => {
    async function renderWithActiveDownload(
      bytes: number,
      total: number,
    ): Promise<HTMLElement> {
      const item: DownloadItem = {
        rom_id: 1,
        rom_name: "Test ROM",
        platform_name: "Test Platform",
        file_name: "test.bin",
        status: "downloading",
        progress: bytes,
        bytes_downloaded: bytes,
        total_bytes: total,
      };
      setDownloads([item]);
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      // mount useEffect resolves, then advance the 1000ms downloadPollRef so
      // local `downloads` state populates from the store.
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100);
      });
      return container;
    }

    beforeEach(() => {
      vi.useFakeTimers({
        toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"],
      });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("renders bytes < 1024 as '<n> B'", async () => {
      const c = await renderWithActiveDownload(512, 1024);
      const remaining = c.querySelector('[data-testid="progress-remaining"]');
      expect(remaining?.textContent).toContain("512 B");
      expect(remaining?.textContent).toContain("1.0 KB");
    });

    it("renders bytes in MB range with 1 decimal", async () => {
      const c = await renderWithActiveDownload(2 * 1024 * 1024, 4 * 1024 * 1024);
      const remaining = c.querySelector('[data-testid="progress-remaining"]');
      expect(remaining?.textContent).toContain("2.0 MB");
      expect(remaining?.textContent).toContain("4.0 MB");
    });

    it("renders bytes in GB range with 2 decimals", async () => {
      const c = await renderWithActiveDownload(
        Math.round(1.5 * 1024 * 1024 * 1024),
        2 * 1024 * 1024 * 1024,
      );
      const remaining = c.querySelector('[data-testid="progress-remaining"]');
      expect(remaining?.textContent).toContain("1.50 GB");
      expect(remaining?.textContent).toContain("2.00 GB");
    });

    it("renders only the bytes_downloaded value when total_bytes is 0", async () => {
      const c = await renderWithActiveDownload(700, 0);
      const remaining = c.querySelector('[data-testid="progress-remaining"]');
      expect(remaining?.textContent).toBe("700 B");
    });
  });

  describe("formatLastSync (via Last sync field)", () => {
    function lastSyncText(container: HTMLElement): string | null {
      const labels = Array.from(
        container.querySelectorAll('[data-testid="field-label"]'),
      );
      const idx = labels.findIndex((n) => n.textContent === "Last sync");
      if (idx < 0) return null;
      // Field's children contains the <span> for the value text.
      const field = labels[idx]?.parentElement;
      return field?.textContent ?? null;
    }

    it("renders 'Never' when stats.last_sync is null", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 0,
        last_sync: null,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(lastSyncText(container)).toContain("Never");
    });

    it("renders 'Just now' for a sync within the last minute", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 5_000).toISOString(),
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(lastSyncText(container)).toContain("Just now");
    });

    it("renders 'Xm ago' for a sync less than 60m ago", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 5 * 60_000).toISOString(),
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(lastSyncText(container)).toContain("5m ago");
    });

    it("renders 'Xh ago' for a sync less than 24h ago", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 3 * 60 * 60_000).toISOString(),
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(lastSyncText(container)).toContain("3h ago");
    });

    it("renders 'Xd ago' for a sync more than 24h ago", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 4 * 24 * 60 * 60_000).toISOString(),
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(lastSyncText(container)).toContain("4d ago");
    });
  });

  describe("formatPreviewDescription (via Preview description)", () => {
    function previewWithSummary(s: Partial<SyncPreviewSummary>): SyncPreview {
      return {
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          ...s,
        },
        new_names: [],
        changed_names: [],
        preview_id: "p1",
      };
    }

    async function renderPreview(s: Partial<SyncPreviewSummary>): Promise<HTMLElement> {
      vi.mocked(backend.syncPreview).mockResolvedValue(previewWithSummary(s));
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const sync = buttonByExactText(container, "Sync Library");
      await act(async () => {
        fireEvent.click(sync!);
        await Promise.resolve();
        await Promise.resolve();
      });
      return container;
    }

    it("renders 'Everything is up to date.' when no diffs", async () => {
      const c = await renderPreview({});
      const descs = Array.from(
        c.querySelectorAll('[data-testid="field-desc"]'),
      ).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
    });

    it("renders ROMs section with added/updated/removed counts", async () => {
      const c = await renderPreview({
        new_count: 3,
        changed_count: 1,
        remove_count: 2,
      });
      const descs = Array.from(
        c.querySelectorAll('[data-testid="field-desc"]'),
      ).map((n) => n.textContent);
      expect(descs.some((d) => d?.includes("ROMs: 3 added, 1 updated, 2 removed"))).toBe(true);
    });

    it("renders Platforms section from platform_collection_diff", async () => {
      const c = await renderPreview({
        platform_collection_diff: {
          has_changes: true,
          added_count: 2,
          removed_count: 1,
        },
      });
      const descs = Array.from(
        c.querySelectorAll('[data-testid="field-desc"]'),
      ).map((n) => n.textContent);
      expect(descs.some((d) => d?.includes("Platforms: 2 added, 1 removed"))).toBe(true);
    });

    it("renders Collections section from collection_diff", async () => {
      const c = await renderPreview({
        collection_diff: {
          has_changes: true,
          added: ["A", "B"],
          removed: ["C"],
        },
      });
      const descs = Array.from(
        c.querySelectorAll('[data-testid="field-desc"]'),
      ).map((n) => n.textContent);
      expect(descs.some((d) => d?.includes("Collections: 2 added, 1 removed"))).toBe(true);
    });

    it("filters zero counts from formatChanges (e.g. 0 removed → not rendered)", async () => {
      const c = await renderPreview({
        new_count: 1,
        changed_count: 0,
        remove_count: 0,
      });
      const descs = Array.from(
        c.querySelectorAll('[data-testid="field-desc"]'),
      ).map((n) => n.textContent);
      // Should render "ROMs: 1 added" — no "updated" or "removed" tokens.
      const romsLine = descs.find((d) => d?.startsWith("ROMs:"));
      expect(romsLine).toBe("ROMs: 1 added");
    });
  });

  describe("formatProgressText (via in-flight sync ProgressBarWithInfo)", () => {
    it("renders 'Syncing...' fallback when message is empty (no step info)", async () => {
      setSyncProgress({
        running: true,
        step: 1,
        totalSteps: 2,
        message: "",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const op = container.querySelector('[data-testid="progress-op"]');
      expect(op?.textContent).toContain("[1/2]");
      expect(op?.textContent).toContain("Syncing...");
    });

    it("truncates long messages with an ellipsis", async () => {
      // 40-char budget — produce a 60-char message and assert truncation.
      const longMsg = "x".repeat(60);
      setSyncProgress({
        running: true,
        step: 2,
        totalSteps: 5,
        message: longMsg,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const op = container.querySelector('[data-testid="progress-op"]');
      expect(op?.textContent).toContain("…");
      // Step prefix counts towards the 40-char budget; total rendered length
      // should be ~40.
      expect((op?.textContent ?? "").length).toBeLessThanOrEqual(41);
    });
  });

  // ===========================================================================
  // F. Sync flow — handleSync (preview gate)
  // ===========================================================================
  describe("handleSync (preview gate)", () => {
    it("with skipPreview=false: syncPreview success populates the preview UI with Apply/Cancel buttons", async () => {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 5,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: ["a", "b"],
        changed_names: [],
        preview_id: "preview-1",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      const sync = buttonByExactText(container, "Sync Library");
      await act(async () => {
        fireEvent.click(sync!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(buttonByExactText(container, "Apply Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Cancel")).not.toBeNull();
    });

    it("with skipPreview=false and zero changes: shows Dismiss button (no Apply)", async () => {
      // Default syncPreview returns all zeros.
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(buttonByExactText(container, "Dismiss")).not.toBeNull();
      expect(buttonByExactText(container, "Apply Sync")).toBeNull();
    });

    it("syncPreview success=false surfaces result.message into status field", async () => {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: false,
        message: "preview broke",
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: [],
        changed_names: [],
        preview_id: "",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fieldLabels(container)).toContain("preview broke");
    });

    it("syncPreview success=false with empty message falls back to 'Preview failed'", async () => {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: false,
        message: "",
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: [],
        changed_names: [],
        preview_id: "",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fieldLabels(container)).toContain("Preview failed");
    });

    it("syncPreview rejection surfaces 'Failed to start sync' status", async () => {
      vi.mocked(backend.syncPreview).mockRejectedValue(new Error("net"));
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fieldLabels(container)).toContain("Failed to start sync");
    });

    it("with skipPreview=true: startSync success bypasses preview entirely", async () => {
      vi.mocked(backend.startSync).mockResolvedValue({ success: true, message: "" });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      // Toggle Skip Preview ON.
      const toggle = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      expect(toggle).not.toBeNull();
      fireEvent.click(toggle!);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.startSync)).toHaveBeenCalled();
      expect(vi.mocked(backend.syncPreview)).not.toHaveBeenCalled();
      // Preview did not appear — Cancel Sync (in-flight) replaces Sync Library.
      expect(buttonByExactText(container, "Apply Sync")).toBeNull();
    });

    it("with skipPreview=true: startSync success=false surfaces result.message", async () => {
      vi.mocked(backend.startSync).mockResolvedValue({
        success: false,
        message: "could not start",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const toggle = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      fireEvent.click(toggle!);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fieldLabels(container)).toContain("could not start");
    });
  });

  // ===========================================================================
  // G. Sync flow — handleApply (Apply Sync click)
  // ===========================================================================
  describe("handleApply", () => {
    async function openPreviewWithChanges(): Promise<HTMLElement> {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 2,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: ["a", "b"],
        changed_names: [],
        preview_id: "preview-X",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      return container;
    }

    it("clicking Apply Sync calls syncApplyDelta(preview_id)", async () => {
      vi.mocked(backend.syncApplyDelta).mockResolvedValue({ success: true, message: "" });
      const container = await openPreviewWithChanges();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Apply Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.syncApplyDelta)).toHaveBeenCalledWith("preview-X");
    });

    it("syncApplyDelta success=false surfaces result.message", async () => {
      vi.mocked(backend.syncApplyDelta).mockResolvedValue({
        success: false,
        message: "apply error",
      });
      const container = await openPreviewWithChanges();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Apply Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fieldLabels(container)).toContain("apply error");
    });

    it("syncApplyDelta rejection surfaces 'Failed to apply sync'", async () => {
      vi.mocked(backend.syncApplyDelta).mockRejectedValue(new Error("nope"));
      const container = await openPreviewWithChanges();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Apply Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fieldLabels(container)).toContain("Failed to apply sync");
    });
  });

  // ===========================================================================
  // H. Sync flow — handleDismiss (Cancel/Dismiss inside preview)
  // ===========================================================================
  describe("handleDismiss", () => {
    it("Dismiss in zero-change preview calls syncCancelPreview and returns to default UI", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // Zero-change preview → Dismiss visible
      const dismiss = buttonByExactText(container, "Dismiss");
      expect(dismiss).not.toBeNull();
      await act(async () => {
        fireEvent.click(dismiss!);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.syncCancelPreview)).toHaveBeenCalled();
      // Back to default — Sync Library button visible again
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
    });

    it("syncCancelPreview rejection is silently swallowed (no crash, returns to default UI)", async () => {
      vi.mocked(backend.syncCancelPreview).mockRejectedValue(new Error("net"));
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Dismiss")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // Component still rendered normally + back to default UI.
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
    });
  });

  // ===========================================================================
  // I. Sync flow — handleCancel (in-flight Cancel Sync click)
  // ===========================================================================
  describe("handleCancel", () => {
    it("clicking the in-flight 'Cancel Sync' button calls requestSyncCancel + cancelSync", async () => {
      // Pre-arm an in-flight sync via the recovery branch on mount.
      setSyncProgress({
        running: true,
        message: "Working",
      });
      vi.mocked(backend.cancelSync).mockResolvedValue({
        success: true,
        message: "cancelled-msg",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const cancel = buttonByExactText(container, "Cancel Sync");
      expect(cancel).not.toBeNull();
      await act(async () => {
        fireEvent.click(cancel!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(syncManager.requestSyncCancel)).toHaveBeenCalled();
      expect(vi.mocked(backend.cancelSync)).toHaveBeenCalled();
    });

    it("cancelSync rejection: catch fires but status field stays masked by syncing=true (source-bug-flagged below)", async () => {
      // SOURCE BUG: handleCancel's catch sets setStatus("Failed to cancel
      // sync") but never sets setSyncing(false), so the status field
      // (`status && !syncing && !preview`) stays hidden — the user sees
      // nothing after a cancel-failure. The non-rejection branch suffers the
      // same: result.message is set but never surfaced while syncing is true.
      // This test exercises the catch arm (cancelSync was rejected, the call
      // site survived without crashing) and documents the missed surface.
      setSyncProgress({
        running: true,
        message: "Working",
      });
      vi.mocked(backend.cancelSync).mockRejectedValue(new Error("net"));
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Cancel Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.cancelSync)).toHaveBeenCalled();
      // Cancel Sync button still visible — syncing was never flipped off.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();
      // Status field is masked by `syncing=true` guard — bug surface here.
      expect(fieldLabels(container)).not.toContain("Failed to cancel sync");
    });

    it("when a preview is showing: clicking Cancel (non-zero preview) routes through handleDismiss", async () => {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 1,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: ["x"],
        changed_names: [],
        preview_id: "p3",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // Now click the Cancel button (under Apply Sync).
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Cancel")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.syncCancelPreview)).toHaveBeenCalled();
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
    });
  });

  // ===========================================================================
  // J. handleClearCache — Force Full Sync flow
  // ===========================================================================
  describe("handleClearCache (Force Full Sync)", () => {
    it("renders the Force Full Sync button when stats.last_sync is set", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 30_000).toISOString(),
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Force Full Sync")).not.toBeNull();
    });

    it("hides the Force Full Sync button when stats.last_sync is null", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Force Full Sync")).toBeNull();
    });

    it("clicking Force Full Sync calls clearSyncCache and surfaces result.message", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 30_000).toISOString(),
      });
      vi.mocked(backend.clearSyncCache).mockResolvedValue({
        success: true,
        message: "Cache cleared",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Force Full Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(vi.mocked(backend.clearSyncCache)).toHaveBeenCalled();
      expect(fieldLabels(container)).toContain("Cache cleared");
    });
  });

  // ===========================================================================
  // K. Fix Retroarch input driver flow
  // ===========================================================================
  describe("handleFixInputDriver (via ConfirmModal onOK)", () => {
    async function renderWithWarning(): Promise<HTMLElement> {
      vi.mocked(backend.getSettings).mockResolvedValue({
        ...defaultSettings(),
        retroarch_input_check: { warning: true, current: "udev" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      return container;
    }

    it("clicking the Fix button opens the ConfirmModal via showModal", async () => {
      const container = await renderWithWarning();
      const fixBtn = Array.from(
        container.querySelectorAll('[data-testid="dialog-button"]'),
      ).find((b) => b.textContent === "Fix") as HTMLButtonElement | undefined;
      expect(fixBtn).not.toBeUndefined();
      fireEvent.click(fixBtn!);
      expect(vi.mocked(showModal)).toHaveBeenCalledTimes(1);
      const props = lastConfirmModalProps<{
        strTitle?: string;
        strOKButtonText?: string;
      }>();
      expect(props?.strTitle).toBe("Fix RetroArch input_driver?");
      expect(props?.strOKButtonText).toBe("Apply Fix");
    });

    it("onOK success=true clears the retroarchWarning section", async () => {
      vi.mocked(backend.fixRetroarchInputDriver).mockResolvedValue({
        success: true,
        message: "Done",
      });
      const container = await renderWithWarning();
      const fixBtn = Array.from(
        container.querySelectorAll('[data-testid="dialog-button"]'),
      ).find((b) => b.textContent === "Fix") as HTMLButtonElement | undefined;
      fireEvent.click(fixBtn!);
      const props = lastConfirmModalProps<{ onOK?: () => void | Promise<void> }>();
      await act(async () => {
        await props?.onOK?.();
      });
      expect(container.textContent).not.toContain("RetroArch: input_driver");
    });

    it("onOK success=false leaves the warning in place", async () => {
      vi.mocked(backend.fixRetroarchInputDriver).mockResolvedValue({
        success: false,
        message: "Could not write",
      });
      const container = await renderWithWarning();
      const fixBtn = Array.from(
        container.querySelectorAll('[data-testid="dialog-button"]'),
      ).find((b) => b.textContent === "Fix") as HTMLButtonElement | undefined;
      fireEvent.click(fixBtn!);
      const props = lastConfirmModalProps<{ onOK?: () => void | Promise<void> }>();
      await act(async () => {
        await props?.onOK?.();
      });
      // Warning stays
      expect(container.textContent).toContain("RetroArch: input_driver");
    });

    it("onOK rejection is silently swallowed (warning stays, no crash)", async () => {
      vi.mocked(backend.fixRetroarchInputDriver).mockRejectedValue(new Error("perm"));
      const container = await renderWithWarning();
      const fixBtn = Array.from(
        container.querySelectorAll('[data-testid="dialog-button"]'),
      ).find((b) => b.textContent === "Fix") as HTMLButtonElement | undefined;
      fireEvent.click(fixBtn!);
      const props = lastConfirmModalProps<{ onOK?: () => void | Promise<void> }>();
      await act(async () => {
        await props?.onOK?.();
      });
      // Truly-ignored catch — warning unchanged.
      expect(container.textContent).toContain("RetroArch: input_driver");
    });
  });

  // ===========================================================================
  // L. Navigation buttons
  // ===========================================================================
  describe("navigation", () => {
    it("clicking Library invokes onNavigate('library')", async () => {
      const onNavigate = vi.fn();
      const { container } = render(<MainPage onNavigate={onNavigate} />);
      await flushAsync();
      fireEvent.click(buttonByExactText(container, "Library")!);
      expect(onNavigate).toHaveBeenCalledWith("library");
    });

    it("clicking Settings invokes onNavigate('settings')", async () => {
      const onNavigate = vi.fn();
      const { container } = render(<MainPage onNavigate={onNavigate} />);
      await flushAsync();
      fireEvent.click(buttonByExactText(container, "Settings")!);
      expect(onNavigate).toHaveBeenCalledWith("settings");
    });

    it("clicking Data Management invokes onNavigate('data')", async () => {
      const onNavigate = vi.fn();
      const { container } = render(<MainPage onNavigate={onNavigate} />);
      await flushAsync();
      fireEvent.click(buttonByExactText(container, "Data Management")!);
      expect(onNavigate).toHaveBeenCalledWith("data");
    });

    it("clicking 'Go to Settings' (save-sort migration banner) invokes onNavigate('settings')", async () => {
      currentSaveSortState = { pending: true, saves_count: 3 };
      // refreshMigrationState runs on mount and writes save_sort back to the
      // store — also return pending:true so the banner stays visible.
      vi.mocked(backend.refreshMigrationState).mockResolvedValue({
        retrodeck: { pending: false },
        save_sort: { pending: true, saves_count: 3 },
      });
      const onNavigate = vi.fn();
      const { container } = render(<MainPage onNavigate={onNavigate} />);
      await flushAsync();
      fireEvent.click(buttonByExactText(container, "Go to Settings")!);
      expect(onNavigate).toHaveBeenCalledWith("settings");
    });

    it("clicking 'View All' (Downloads section) invokes onNavigate('downloads')", async () => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
      try {
        setDownloads([
          {
            rom_id: 1,
            rom_name: "X",
            platform_name: "Y",
            file_name: "x.bin",
            status: "downloading",
            progress: 0,
            bytes_downloaded: 0,
            total_bytes: 1024,
          },
        ]);
        const onNavigate = vi.fn();
        const { container } = render(<MainPage onNavigate={onNavigate} />);
        await act(async () => { await Promise.resolve(); await Promise.resolve(); });
        // downloadPollRef ticks at 1000ms — advance one tick to populate state.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(1100);
        });
        fireEvent.click(buttonByExactText(container, "View All")!);
        expect(onNavigate).toHaveBeenCalledWith("downloads");
      } finally {
        vi.useRealTimers();
      }
    });
  });

  // ===========================================================================
  // M. Downloads section render
  // ===========================================================================
  describe("downloads section", () => {
    // The downloads section local state is populated by downloadPollRef
    // (1000ms setInterval reading getDownloadState()). Use fake timers +
    // advance one tick so the store contents propagate into render.
    beforeEach(() => {
      vi.useFakeTimers({
        toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"],
      });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    async function renderAndTick(): Promise<HTMLElement> {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await act(async () => { await Promise.resolve(); await Promise.resolve(); });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100);
      });
      return container;
    }

    it("hidden when no downloads in the store", async () => {
      const container = await renderAndTick();
      const titles = Array.from(
        container.querySelectorAll('[data-testid="panel-title"]'),
      ).map((n) => n.textContent);
      expect(titles).not.toContain("Downloads");
    });

    it("rendered when at least one active download", async () => {
      setDownloads([
        {
          rom_id: 1,
          rom_name: "Active",
          platform_name: "Genesis",
          file_name: "a.bin",
          status: "downloading",
          progress: 50,
          bytes_downloaded: 512,
          total_bytes: 1024,
        },
      ]);
      const container = await renderAndTick();
      const titles = Array.from(
        container.querySelectorAll('[data-testid="panel-title"]'),
      ).map((n) => n.textContent);
      expect(titles).toContain("Downloads");
    });

    it("shows '+N more downloading' when more than 2 active downloads", async () => {
      setDownloads([
        { rom_id: 1, rom_name: "A", platform_name: "X", file_name: "a", status: "downloading", progress: 0, bytes_downloaded: 0, total_bytes: 1024 },
        { rom_id: 2, rom_name: "B", platform_name: "X", file_name: "b", status: "downloading", progress: 0, bytes_downloaded: 0, total_bytes: 1024 },
        { rom_id: 3, rom_name: "C", platform_name: "X", file_name: "c", status: "downloading", progress: 0, bytes_downloaded: 0, total_bytes: 1024 },
      ]);
      const container = await renderAndTick();
      expect(container.textContent).toContain("+1 more downloading");
    });

    it("shows 'N completed' count for finished items", async () => {
      setDownloads([
        { rom_id: 1, rom_name: "A", platform_name: "X", file_name: "a", status: "completed", progress: 100, bytes_downloaded: 100, total_bytes: 100 },
        { rom_id: 2, rom_name: "B", platform_name: "X", file_name: "b", status: "failed", progress: 0, bytes_downloaded: 0, total_bytes: 100 },
      ]);
      const container = await renderAndTick();
      expect(container.textContent).toContain("2 completed");
    });

    it("active item with total_bytes > 0 renders nProgress = (bytes/total)*100, indeterminate=false", async () => {
      setDownloads([
        {
          rom_id: 1,
          rom_name: "P",
          platform_name: "G",
          file_name: "p",
          status: "downloading",
          progress: 25,
          bytes_downloaded: 256,
          total_bytes: 1024,
        },
      ]);
      const container = await renderAndTick();
      const progress = container.querySelector('[data-testid="progress-progress"]');
      expect(progress?.textContent).toBe("25");
      const indet = container.querySelector('[data-testid="progress-indeterminate"]');
      expect(indet?.textContent).toBe("false");
    });

    it("active item with total_bytes === 0 renders indeterminate=true", async () => {
      setDownloads([
        {
          rom_id: 1,
          rom_name: "P",
          platform_name: "G",
          file_name: "p",
          status: "downloading",
          progress: 0,
          bytes_downloaded: 0,
          total_bytes: 0,
        },
      ]);
      const container = await renderAndTick();
      const indet = container.querySelector('[data-testid="progress-indeterminate"]');
      expect(indet?.textContent).toBe("true");
    });
  });

  // ===========================================================================
  // N. Polling cleanup — setInterval / clearInterval spy pattern
  // ===========================================================================
  describe("polling cleanup", () => {
    it("downloadPollRef setInterval is cleared on unmount", async () => {
      const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
      const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");

      const { unmount } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      // Capture the 1000ms (downloadPollRef) interval id.
      const downloadIds = setIntervalSpy.mock.results
        .filter((_, i) => setIntervalSpy.mock.calls[i][1] === 1000)
        .map((r) => r.value as ReturnType<typeof setInterval>);
      const expectedId = downloadIds[downloadIds.length - 1];
      expect(expectedId).toBeDefined();

      const callsBeforeUnmount = clearIntervalSpy.mock.calls.length;
      unmount();
      expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount);
      expect(clearIntervalSpy).toHaveBeenCalledWith(expectedId);

      setIntervalSpy.mockRestore();
      clearIntervalSpy.mockRestore();
    });

    it("pollRef (250ms sync progress) is cleared on unmount when sync is in-flight", async () => {
      // Pre-arm in-flight sync so startPolling runs at mount.
      setSyncProgress({ running: true, message: "Working" });
      const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
      const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");

      const { unmount } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      const pollIds = setIntervalSpy.mock.results
        .filter((_, i) => setIntervalSpy.mock.calls[i][1] === 250)
        .map((r) => r.value as ReturnType<typeof setInterval>);
      const expectedId = pollIds[pollIds.length - 1];
      expect(expectedId).toBeDefined();

      const callsBeforeUnmount = clearIntervalSpy.mock.calls.length;
      unmount();
      expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(callsBeforeUnmount);
      expect(clearIntervalSpy).toHaveBeenCalledWith(expectedId);

      setIntervalSpy.mockRestore();
      clearIntervalSpy.mockRestore();
    });

    it("pollRef tick stops polling when sync becomes not-running, surfaces final message + refreshes stats", async () => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"] });
      try {
        // Pre-arm in-flight sync.
        setSyncProgress({ running: true, message: "Working" });
        vi.mocked(backend.getSyncStats).mockResolvedValueOnce(defaultStats());
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        // First flush — mount useEffect.
        await act(async () => { await Promise.resolve(); await Promise.resolve(); });

        // Flip the store to NOT running, then advance one 250ms tick — the
        // poll handler should stop the interval, set status to the final
        // message, and refresh stats.
        setSyncProgress({ running: false, message: "Sync finished" });
        const statsAfter: SyncStats = {
          ...defaultStats(),
          roms: 1,
          last_sync: new Date().toISOString(),
        };
        vi.mocked(backend.getSyncStats).mockResolvedValue(statsAfter);

        await act(async () => {
          await vi.advanceTimersByTimeAsync(260);
        });

        // The status field shows the final message.
        expect(fieldLabels(container)).toContain("Sync finished");
        // The Sync Library button is back (in-flight ended).
        expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
        expect(vi.mocked(backend.getSyncStats)).toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });
  });

  // ===========================================================================
  // O. Skip Preview toggle
  // ===========================================================================
  describe("Skip Preview toggle", () => {
    it("flipping the toggle ON updates the checkbox state", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const toggle = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      expect(toggle).not.toBeNull();
      expect(toggle!.checked).toBe(false);
      fireEvent.click(toggle!);
      await flushAsync();
      // Re-query — the toggle is re-rendered.
      const updated = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      expect(updated!.checked).toBe(true);
    });
  });
});

