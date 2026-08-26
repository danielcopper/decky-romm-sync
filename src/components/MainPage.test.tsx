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
//   - handleCancel try/catch → finishCancelWithStatus("Failed to cancel sync")
//     surfaces the message after stopPolling + setSyncing(false) + setLoading(false)
//     un-gate the status field; #733 fix landed — message now visible.
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
import { MainPage, ConnectionIndicator } from "./MainPage";
import * as backend from "../api/backend";
import { useVersionError } from "./VersionErrorCard";
import {
  setSyncProgress,
  updateSyncProgress,
  onSyncProgressChange,
  getSyncProgress,
  FETCH_SHARE,
  COVERS_SHARE,
  APPLY_SHARE,
} from "../utils/syncProgress";
import { beginEtaRun, resetEta, liveEtaSeconds } from "../utils/syncEta";
import * as syncEta from "../utils/syncEta";
import { NEW_ITEM_SEC, UPDATED_ITEM_SEC, COVER_DOWNLOAD_SEC, FETCH_ALLOWANCE_SEC } from "../utils/syncEstimate";
import { setDownloads } from "../utils/downloadStore";
import { resetConnectionProbeForTests } from "../utils/connectionProbe";
import { resetSyncStatsStoreForTests } from "../utils/syncStatsStore";
import { showModal } from "@decky/ui";
import * as syncManager from "../utils/syncManager";
import * as connectionState from "../utils/connectionState";
import type {
  MigrationStatus,
  SaveSortMigrationStatus,
  SyncStats,
  SyncPreview,
  SyncPreviewSummary,
  SessionBudgetStatus,
  DownloadItem,
  PluginSettings,
  SyncProgress,
} from "../types";

// -----------------------------------------------------------------------------
// Module mocks
// -----------------------------------------------------------------------------

vi.mock("./VersionErrorCard", () => ({
  useVersionError: vi.fn(() => null),
  VersionErrorCard: (props: { message: string; compact?: boolean }) =>
    createElement("div", { "data-testid": "version-error-card" }, props.message),
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
  reconcileStaleShortcuts: vi.fn().mockResolvedValue(undefined),
  isCancelRequested: vi.fn().mockReturnValue(false),
  resetSyncCancel: vi.fn(),
}));

// Local @decky/ui re-mock — global stub lacks ProgressBar (used to render sync
// + download progress). Mirror the rest with thin pass-throughs + a vi.fn
// showModal so we can capture ConfirmModal calls.
vi.mock("@decky/ui", async () => {
  type AnyProps = Record<string, unknown> & { children?: unknown };
  const { createElement: ce } = await import("react");
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
    // Focusable wrappers around read-only rows (info fields, banners, progress) —
    // pass children through a plain div so the wrapped content stays queryable.
    Focusable: passthrough("div"),
    ButtonItem: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { onClick, disabled }, children as never),
    Field: (p: AnyProps & { label?: unknown; description?: unknown }) =>
      ce(
        "div",
        { "data-testid": "field" },
        ce("span", { "data-testid": "field-label" }, p.label as never),
        ce("span", { "data-testid": "field-desc" }, p.description as never),
        p.children as never,
      ),
    ToggleField: (
      p: AnyProps & {
        checked?: boolean;
        onChange?: (v: boolean) => void;
        label?: unknown;
      },
    ) =>
      ce(
        "div",
        { "data-testid": "toggle" },
        ce("input", {
          type: "checkbox",
          "data-testid": "toggle-input",
          checked: p.checked ?? false,
          onChange: (e: { target: { checked: boolean } }) => p.onChange?.(e.target.checked),
        }),
        typeof p.label === "string" ? p.label : null,
      ),
    Spinner: () => ce("div", { "data-testid": "spinner" }),
    DialogButton: ({ children, onClick, disabled }: AnyProps & { onClick?: () => void; disabled?: boolean }) =>
      ce("button", { "data-testid": "dialog-button", onClick, disabled }, children as never),
    ConfirmModal: (
      p: AnyProps & {
        strTitle?: string;
        strDescription?: string;
        strOKButtonText?: string;
        strCancelButtonText?: string;
        onOK?: () => void;
        onCancel?: () => void;
      },
    ) => ce("div", { "data-testid": "confirm-modal" }, p.children as never),
    ProgressBar: (p: AnyProps & { nProgress?: number; indeterminate?: boolean }) =>
      ce(
        "div",
        { "data-testid": "progress" },
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

/** A promise plus the handle to settle it, so a test can hold a backend read
 *  open across an event and decide when — and in which order — it answers. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function defaultSettings(): PluginSettings {
  return {
    romm_url: "https://romm.local",
    has_token: true,
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

function buttonByExactText(container: HTMLElement, text: string): HTMLButtonElement | null {
  const btn = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === text);
  return (btn as HTMLButtonElement | undefined) ?? null;
}

function lastConfirmModalProps<T = Record<string, unknown>>(): T | null {
  const calls = vi.mocked(showModal).mock.calls;
  if (calls.length === 0) return null;
  const el = calls[calls.length - 1]?.[0] as ReactElement<T> | undefined;
  return el?.props ?? null;
}

function fieldLabels(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('[data-testid="field-label"]')).map((n) => n.textContent);
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
    // Clear the module-level live-ETA estimator so a prior test's run never bleeds
    // into the next (its state persists across renders like the other stores).
    resetEta();
    // The connection probe outlives the panel by design (#1730), so its verdict
    // survives unmount and would carry a prior test's answer into the next.
    resetConnectionProbeForTests();
    // Same for the sync stats and session budget: both the stored answers and
    // any read still open outlive the render that issued them.
    resetSyncStatsStoreForTests();
    setSyncProgress({
      running: false,
      stage: "",
      current: 0,
      total: 0,
      message: "",
    });

    // Re-stub useVersionError (resetAllMocks wiped it).
    vi.mocked(useVersionError).mockReturnValue(null);

    // Re-stub isCancelRequested (resetAllMocks wiped the module-mock return
    // value). Defaults to false so the normal preview flow runs; the
    // RC-CANCEL-PREVIEW test flips it true (#1202).
    vi.mocked(syncManager.isCancelRequested).mockReturnValue(false);

    // Re-stub migrationStore impls.
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

    // Default backend mocks — tests override per case.
    vi.mocked(backend.refreshMigrationState).mockResolvedValue({
      retrodeck: { pending: false },
      save_sort: { pending: false },
    });
    vi.mocked(backend.getSyncStats).mockResolvedValue(defaultStats());
    vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue({
      success: true,
      rss_kb: null,
      warn_kb: 1_800_000,
      ceiling_kb: 2_200_000,
      cliff_kb: 2_450_000,
      memory_delta_kb: null,
      resume_ready: null,
      run_done_items: null,
      run_total_items: null,
    });
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
    vi.mocked(backend.getSyncStatus).mockResolvedValue({
      running: false,
      stage: "",
      current: 0,
      total: 0,
      message: "",
    });
    vi.mocked(backend.getRetroDeckStatus).mockResolvedValue({
      status: "ok",
      config_path: "/cfg/retrodeck.json",
      resolved_home: "/home/deck/retrodeck",
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

    it("renders the panel without any section headings, blocks divided by rules", async () => {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // No headings anywhere — the thin block separators are the only
      // boundaries between the status, sync, and menu blocks.
      expect(container.querySelectorAll('[data-testid="panel-title"]')).toHaveLength(0);
      expect(container.querySelectorAll('[data-testid="block-separator"]')).toHaveLength(2);
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
      expect(vi.mocked(migrationStore.setMigrationStatus)).toHaveBeenCalledWith(retrodeck);
      expect(vi.mocked(saveSortMigrationStore.setSaveSortMigrationStatus)).toHaveBeenCalledWith(saveSort);
    });

    it("logs the failure when refreshMigrationState rejects", async () => {
      vi.mocked(backend.refreshMigrationState).mockRejectedValue(new Error("boom"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to refresh migration state"));
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
      // Pin the joined one-line library form (order, "·" separators, plural
      // forms). The stat counts bound shortcuts (sibling groups), so the label
      // says games, not ROMs (#1298 audit).
      expect(container.textContent).toContain("42 games · 3 platforms · 2 collections");
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

    it("testConnection reason='version_error' surfaces r.message via setVersionError", async () => {
      vi.mocked(backend.testConnection).mockResolvedValue({
        success: false,
        message: "server out of date",
        reason: "version_error",
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

    it("recovers in-flight sync state from getSyncStatus() on mount", async () => {
      // Backend is authoritative: the mount query returns a live run, so the
      // in-flight UI is shown even though the event-fed store was idle.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        message: "Fetching library...",
        step: 1,
        totalSteps: 5,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // In-flight: Cancel Sync button is rendered (replaces the Sync Library
      // button) and the determinate bar shows the recovered stage label.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
      expect(container.querySelector('[data-testid="sync-stage"]')?.textContent).toContain("Fetching library");
    });

    it("logs the failure when getSyncStatus rejects on mount", async () => {
      vi.mocked(backend.getSyncStatus).mockRejectedValue(new Error("offline"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to query sync status"));
      // Falls back to the idle UI — the Sync Library button stays available.
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      logSpy.mockRestore();
    });

    it("logs the failure when getSyncStats rejects on mount", async () => {
      vi.mocked(backend.getSyncStats).mockRejectedValue(new Error("boom"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to load sync stats"));
      logSpy.mockRestore();
    });

    it("keeps the connection row in 'Checking…' after a single testConnection rejection (retrying, not failed) (#1045)", async () => {
      // A lone rejection is treated as transient — the probe retries rather than
      // declaring the backend dead. Before any backoff elapses (only microtasks
      // flushed) the row must still read "Checking…", never the failure state.
      vi.mocked(backend.testConnection).mockRejectedValue(new Error("net"));
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain("Checking...");
      expect(container.textContent).not.toContain("Backend error");
    });

    it("logs the failure when getSettings rejects on mount", async () => {
      vi.mocked(backend.getSettings).mockRejectedValue(new Error("io"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to load settings"));
      logSpy.mockRestore();
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
  // D. ConnectionIndicator — 4 states (covered via top-level rendering)
  // ===========================================================================
  describe("ConnectionIndicator", () => {
    it("connected=null (testConnection never resolves) renders 'Checking...' + Spinner", async () => {
      vi.mocked(backend.testConnection).mockImplementation(
        () =>
          new Promise(() => {
            /* never */
          }),
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

    // A failed probe carries the backend's {reason, message}; the row shows a
    // specific label instead of a bare "Not connected". version_error is not
    // exercised here — a version failure short-circuits the whole panel to the
    // VersionErrorCard (covered separately); the label mapping is covered by the
    // direct-render cases below.
    it.each([
      ["auth_failed", "401 Unauthorized", "Sign-in rejected"],
      ["server_unreachable", "timed out", "Server unreachable"],
      ["config_error", "No server URL configured", "No server URL"],
      ["config_error", "Not signed in — sign in to RomM first", "Not signed in"],
      ["config_error", "some other config issue", "Not connected"],
    ] as const)("a failed probe with reason=%s renders %s", async (reason, message, label) => {
      vi.mocked(backend.testConnection).mockResolvedValue({ success: false, reason, message });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain(label);
    });
  });

  // ===========================================================================
  // D1. ConnectionIndicator — failure label mapping (direct render)
  // ===========================================================================
  describe("ConnectionIndicator failure labels", () => {
    it.each([
      ["auth_failed", "", "Sign-in rejected"],
      ["server_unreachable", "", "Server unreachable"],
      ["version_error", "server too old", "Unsupported RomM version"],
      ["config_error", "No server URL configured", "No server URL"],
      ["config_error", "Not signed in — sign in to RomM first", "Not signed in"],
      ["config_error", "unclassified config problem", "Not connected"],
      ["unknown", "", "Not connected"],
    ] as const)("reason=%s / message=%s → %s", (reason, message, label) => {
      const { container } = render(<ConnectionIndicator connected={false} failure={{ reason, message }} />);
      expect(container.textContent).toContain(label);
    });

    it("falls back to 'Not connected' when no failure detail is present", () => {
      const { container } = render(<ConnectionIndicator connected={false} failure={null} />);
      expect(container.textContent).toContain("Not connected");
    });

    it("still renders the unchanged Connected / Checking… / Backend error states", () => {
      const connected = render(<ConnectionIndicator connected={true} />);
      expect(connected.container.textContent).toContain("Connected");
      const checking = render(<ConnectionIndicator connected={null} />);
      expect(checking.container.textContent).toContain("Checking...");
      const failed = render(<ConnectionIndicator connected="backend_failed" />);
      expect(failed.container.textContent).toContain("Backend error");
    });
  });

  // ===========================================================================
  // D2. Backend bootstrap failure — the retry-exhausted failure state (#1045)
  // ===========================================================================
  describe("backend bootstrap failure (#1045)", () => {
    it("shows 'Backend error' only when the backend itself is dead (testConnection AND getSettings both fail)", async () => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        // Bootstrap aborted: the whole RPC bridge is dead, so BOTH the
        // test_connection probes AND the get_settings liveness ping fail.
        vi.mocked(backend.testConnection).mockRejectedValue(new Error("backend down"));
        vi.mocked(backend.getSettings).mockRejectedValue(new Error("backend down"));
        const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        // Drive the full retry schedule (2+5+10+15+20s of backoff) + the ping.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(60_000);
        });
        // The row lands on the explicit failure — not an eternal "Checking…"
        // spinner (the #1045 bug), and not the false "Not connected".
        expect(container.textContent).toContain("Backend error");
        expect(container.textContent).toContain("Plugin backend failed to start — check Decky logs.");
        expect(container.textContent).not.toContain("Checking...");
        expect(container.textContent).not.toContain("Not connected");
        // Sync is gated off while the backend is down.
        const sync = buttonByExactText(container, "Sync Library");
        expect(sync).not.toBeNull();
        expect(sync!.disabled).toBe(true);
        // Non-vacuous catch coverage: the dead-backend branch logs the liveness
        // ping failure to console.error (logError itself would hang here).
        expect(errSpy).toHaveBeenCalledWith(
          expect.stringContaining("backend RPC bridge unreachable"),
          expect.anything(),
        );
        errSpy.mockRestore();
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows 'Not connected' (NOT 'Backend error') when the backend is alive but the server is unreachable-by-timeout", async () => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        // Healthy backend, hanging RomM server: test_connection never answers
        // within any per-attempt deadline (the backend heartbeat outlives it),
        // but the get_settings liveness ping resolves — the backend IS alive.
        vi.mocked(backend.testConnection).mockImplementation(
          () =>
            new Promise(() => {
              /* never resolves — server round-trip hangs past every deadline */
            }),
        );
        vi.mocked(backend.getSettings).mockResolvedValue(defaultSettings());
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        // Drive every 5s per-attempt timeout + backoff to exhaustion, then the ping.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(90_000);
        });
        // Truthful state for an unreachable server — the backend didn't fail.
        expect(container.textContent).toContain("Not connected");
        expect(container.textContent).not.toContain("Backend error");
        expect(container.textContent).not.toContain("Checking...");
      } finally {
        vi.useRealTimers();
      }
    });

    it("recovers to 'Connected' when a retry succeeds after a slow start (no false backend-failure)", async () => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
      try {
        // The backend is merely slow: the first two probes reject, the third
        // resolves. The row must recover, never showing the failure state.
        vi.mocked(backend.testConnection)
          .mockRejectedValueOnce(new Error("starting"))
          .mockRejectedValueOnce(new Error("starting"))
          .mockResolvedValue({ success: true, message: "" });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000);
        });
        expect(container.textContent).toContain("Connected");
        expect(container.textContent).not.toContain("Backend error");
      } finally {
        vi.useRealTimers();
      }
    });
  });

  // ===========================================================================
  // E. Module helpers — exercised via rendered output
  // ===========================================================================
  describe("formatBytes (via active download bytes caption)", () => {
    async function renderWithActiveDownload(bytes: number, total: number): Promise<HTMLElement> {
      const item: DownloadItem = {
        rom_id: 1,
        rom_name: "Test ROM",
        platform_name: "Test Platform",
        file_name: "test.bin",
        status: "downloading",
        progress: bytes,
        bytes_downloaded: bytes,
        total_bytes: total,
        resumable: false,
      };
      setDownloads([item]);
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      // The store is seeded before render, so the subscription has it from the
      // first pass — only the mount useEffect's microtasks need flushing.
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
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
      const bytes = c.querySelector('[data-testid="dl-bytes"]');
      expect(bytes?.textContent).toContain("512 B");
      expect(bytes?.textContent).toContain("1.0 KB");
    });

    it("renders bytes in MB range with 1 decimal", async () => {
      const c = await renderWithActiveDownload(2 * 1024 * 1024, 4 * 1024 * 1024);
      const bytes = c.querySelector('[data-testid="dl-bytes"]');
      expect(bytes?.textContent).toContain("2.0 MB");
      expect(bytes?.textContent).toContain("4.0 MB");
    });

    it("renders bytes in GB range with 2 decimals", async () => {
      const c = await renderWithActiveDownload(Math.round(1.5 * 1024 * 1024 * 1024), 2 * 1024 * 1024 * 1024);
      const bytes = c.querySelector('[data-testid="dl-bytes"]');
      expect(bytes?.textContent).toContain("1.50 GB");
      expect(bytes?.textContent).toContain("2.00 GB");
    });

    it("renders only the bytes_downloaded value when total_bytes is 0", async () => {
      const c = await renderWithActiveDownload(700, 0);
      const bytes = c.querySelector('[data-testid="dl-bytes"]');
      expect(bytes?.textContent).toBe("700 B");
    });
  });

  describe("Last sync field", () => {
    function lastSyncText(container: HTMLElement): string | null {
      const labels = Array.from(container.querySelectorAll('[data-testid="field-label"]'));
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

    it("falls back to the raw value when last_sync cannot be parsed", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: "not-a-timestamp",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Neither `new Date` nor `Date.parse` throws on an unparseable string —
      // they yield an Invalid Date and NaN. A formatter that does the arithmetic
      // anyway reaches its last branch with NaN and renders it, so the second
      // assertion is the one that pins the bug rather than the fallback shape.
      expect(lastSyncText(container)).toContain("not-a-timestamp");
      expect(lastSyncText(container)).not.toContain("NaN");
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

    it("renders the attempt time + status when only last_attempt is set (never completed) (#1367)", async () => {
      // A cancelled/crashed run with no completed run ever — must NOT read "Never".
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: null,
        last_attempt: { finished_at: "2026-06-01T17:48:00", status: "cancelled" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const text = lastSyncText(container);
      expect(text).toContain("17:48");
      expect(text).toContain("(cancelled)");
      expect(text).not.toContain("Never");
    });

    it("renders the attempt time + status when the newest attempt was interrupted (crash-resume)", async () => {
      // A crash-resumed run reports the "interrupted" terminal status the backend
      // now also emits — the status string is rendered verbatim.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: null,
        last_attempt: { finished_at: "2026-06-01T17:48:00", status: "interrupted" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const text = lastSyncText(container);
      expect(text).toContain("17:48");
      expect(text).toContain("(interrupted)");
      expect(text).not.toContain("Never");
    });

    it("renders both the last_sync time and a subtle last-attempt line when both exist", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 5 * 60_000).toISOString(),
        last_attempt: { finished_at: "2026-06-01T18:03:00", status: "cancelled" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Primary line: the completed run's relative time in the field value.
      expect(lastSyncText(container)).toContain("5m ago");
      // Secondary line: the newer cancelled attempt as its own full-width row
      // below the field (the in-field description slot floated mid-row).
      expect(container.textContent).toContain("last attempt: 18:03 (cancelled)");
    });

    it("renders 'Never' when neither last_sync nor last_attempt is present", async () => {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: null,
        last_attempt: null,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(lastSyncText(container)).toContain("Never");
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
      const descs = Array.from(c.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
    });

    it("renders the Games category in signed notation (added / updated / removed)", async () => {
      const c = await renderPreview({
        new_count: 3,
        changed_count: 1,
        remove_count: 2,
      });
      const descs = Array.from(c.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      // Every segment spells its word out; " / "-separated; " · " between categories.
      expect(descs.some((d) => d.includes("Games: 3 new / 1 updated / 2 removed"))).toBe(true);
    });

    it("renders the Platforms category from platform_collection_diff", async () => {
      const c = await renderPreview({
        platform_collection_diff: {
          has_changes: true,
          added_count: 2,
          removed_count: 1,
        },
      });
      const descs = Array.from(c.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs.some((d) => d.includes("Platforms: 2 new / 1 removed"))).toBe(true);
    });

    it("renders the Collections category from collection_diff", async () => {
      const c = await renderPreview({
        collection_diff: {
          has_changes: true,
          added: ["A", "B"],
          removed: ["C"],
        },
      });
      const descs = Array.from(c.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs.some((d) => d.includes("Collections: 2 new / 1 removed"))).toBe(true);
    });

    it("joins multiple categories with ' · ' and omits zero segments", async () => {
      const c = await renderPreview({
        new_count: 1001,
        changed_count: 50,
        remove_count: 1200,
        platform_collection_diff: { has_changes: true, added_count: 1, removed_count: 0 },
        collection_diff: { has_changes: true, added: ["A", "B"], removed: [] },
      });
      // Zero segments (platform removed, collection removed) drop out entirely.
      // Words, not sigils — "+"/"~"/"−" were a legend the panel never carried. The
      // delta line has its own node since the coverage line shares the Changes block.
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "Games: 1001 new / 50 updated / 1200 removed · Platforms: 1 new · Collections: 2 new",
      );
    });

    it("omits zero segments within a category (0 removed → not rendered)", async () => {
      const c = await renderPreview({
        new_count: 1,
        changed_count: 0,
        remove_count: 0,
      });
      // Should render "Games: 1 new" — no updated or removed segments.
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe("Games: 1 new");
    });

    it("names cover-only work when the shortcut delta is empty (#1386 flow gap)", async () => {
      const c = await renderPreview({ cover_refresh_count: 3 });
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "No shortcut changes — 3 cover updates.",
      );
    });

    it("singularizes a single cover update", async () => {
      const c = await renderPreview({ cover_refresh_count: 1 });
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "No shortcut changes — 1 cover update.",
      );
    });

    it("keeps 'Everything is up to date.' when covers are explicitly zero (regression pin)", async () => {
      const c = await renderPreview({ cover_refresh_count: 0 });
      const descs = Array.from(c.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
    });

    it("names the re-stamp work when an unstamped platform has an empty delta (#1416)", async () => {
      const c = await renderPreview({ restamp_platform_count: 1 });
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "No changes — finishing a previous sync.",
      );
    });

    it("keeps 'Everything is up to date.' when restamp is explicitly zero (regression pin)", async () => {
      const c = await renderPreview({ restamp_platform_count: 0 });
      const descs = Array.from(c.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
    });

    it("shows the Full re-sync line when every platform is re-fetched with changed games (#1318)", async () => {
      // Force Full Sync: all platforms unstamped AND the whole library counts as
      // changed (recorded launch options cleared) — changed_count > 0 admits the line.
      const c = await renderPreview({
        changed_count: 2843,
        restamp_platform_count: 5,
        sync_platform_count: 5,
      });
      expect(c.querySelector('[data-testid="sync-full-resync"]')?.textContent).toBe(
        "Full re-sync — all platforms re-fetched.",
      );
      // The normal change line still renders below the context line.
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toContain("Games: 2843 updated");
    });

    it("hides the Full re-sync line on a first-ever sync (all-unstamped but pure new games, #1318)", async () => {
      // A fresh install is all-unstamped too (restamp === sync), but its delta is
      // pure new_count — nothing to "re-fetch". The changed_count > 0 leg keeps
      // the Force-only wording off so the odd copy never shows on a first sync.
      const c = await renderPreview({
        new_count: 3000,
        changed_count: 0,
        restamp_platform_count: 5,
        sync_platform_count: 5,
      });
      expect(c.querySelector('[data-testid="sync-full-resync"]')).toBeNull();
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe("Games: 3000 new");
    });

    it("hides the Full re-sync line when only some platforms are unstamped (partial resume)", async () => {
      const c = await renderPreview({
        changed_count: 10,
        restamp_platform_count: 2,
        sync_platform_count: 5,
      });
      expect(c.querySelector('[data-testid="sync-full-resync"]')).toBeNull();
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe("Games: 10 updated");
    });

    it("hides the Full re-sync line when sync_platform_count is 0 (0 === 0 must not trigger)", async () => {
      // An all-collections run, or an older backend that omits the counts: the
      // equality holds at 0 but the `> 0` guard keeps the line off.
      const c = await renderPreview({
        new_count: 3,
        restamp_platform_count: 0,
        sync_platform_count: 0,
      });
      expect(c.querySelector('[data-testid="sync-full-resync"]')).toBeNull();
    });

    it("keeps the restamp-only empty-delta message with no Full re-sync line (#1318 / #1416)", async () => {
      // Empty segments + every platform unstamped: the empty-delta branch owns
      // the copy, and the Full re-sync line (a NON-empty-delta context line)
      // stays off — the two never collide.
      const c = await renderPreview({
        restamp_platform_count: 3,
        sync_platform_count: 3,
      });
      expect(c.querySelector('[data-testid="sync-full-resync"]')).toBeNull();
      expect(c.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "No changes — finishing a previous sync.",
      );
    });
  });

  describe("session-budget advisory (#1383)", () => {
    async function renderPreviewWithPause(pauseLikely: boolean | undefined): Promise<HTMLElement> {
      const preview: SyncPreview = {
        success: true,
        summary: {
          new_count: 2000,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: [],
        changed_names: [],
        preview_id: "p-budget",
      };
      // Omit pause_likely entirely when undefined (exactOptionalPropertyTypes)
      // to model an older backend / unavailable reading that never sends the key.
      if (pauseLikely !== undefined) preview.pause_likely = pauseLikely;
      vi.mocked(backend.syncPreview).mockResolvedValue(preview);
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

    it("shows the BLUE (info) pause advisory when pause_likely is true", async () => {
      const c = await renderPreviewWithPause(true);
      const advisory = c.querySelector('[data-testid="budget-advisory"]') as HTMLElement | null;
      expect(advisory).not.toBeNull();
      expect(advisory?.textContent).toContain("Will likely pause partway to protect Steam");
      expect(advisory?.textContent).toContain("Restart Steam when prompted, then Resume Sync.");
      // Recolored from amber to blue — it announces normal, planned behavior (#1383).
      expect(advisory?.style.color).toBe("#7fbcff");
    });

    it("hides the advisory when pause_likely is false", async () => {
      const c = await renderPreviewWithPause(false);
      expect(c.querySelector('[data-testid="budget-advisory"]')).toBeNull();
    });

    it("hides the advisory when pause_likely is absent (older backend / unavailable reading)", async () => {
      const c = await renderPreviewWithPause(undefined);
      expect(c.querySelector('[data-testid="budget-advisory"]')).toBeNull();
    });
  });

  describe("preview scope line (#29)", () => {
    async function renderPreviewScope(platforms: number, collections: number): Promise<HTMLElement> {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          // One new item: the scope/hint rows describe the run Apply would
          // start, so they only render when the delta is non-empty.
          new_count: 1,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          sync_platform_count: platforms,
          sync_collection_count: collections,
        },
        new_names: [],
        changed_names: [],
        preview_id: "p-scope",
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

    // Coverage and duration each own a label-less line under the "Changes"
    // block ("Scope" and "Preview" as competing labels read as duplicate info).
    // These summaries have one new item, so the estimate is essentially the flat
    // fixed-overhead allowance (45s) and reads "< 1 min".
    it("reads 'Syncing N platforms · M collections' with both counts", async () => {
      const c = await renderPreviewScope(3, 2);
      expect(c.querySelector('[data-testid="sync-scope"]')?.textContent).toBe("Syncing 3 platforms · 2 collections");
      expect(c.querySelector('[data-testid="sync-estimate"]')?.textContent).toBe("Estimated duration: < 1 min");
    });

    it("omits the collections part when the run syncs none, and singularizes", async () => {
      const c = await renderPreviewScope(1, 0);
      expect(c.querySelector('[data-testid="sync-scope"]')?.textContent).toBe("Syncing 1 platform");
    });

    it("omits the platforms part on a collections-only run (LOW-6)", async () => {
      const c = await renderPreviewScope(0, 3);
      expect(c.querySelector('[data-testid="sync-scope"]')?.textContent).toBe("Syncing 3 collections");
    });

    it("shows the duration alone when the backend omits both scope counts (empty scope)", async () => {
      const c = await renderPreviewScope(0, 0);
      expect(c.querySelector('[data-testid="sync-scope"]')).toBeNull();
      expect(c.querySelector('[data-testid="sync-estimate"]')?.textContent).toBe("Estimated duration: < 1 min");
    });

    it("hides the scope line and the progress hint on an empty delta (nothing to apply)", async () => {
      // Scope/estimate and "Progress is saved…" describe the run Apply would
      // start; with 'Everything is up to date.' + Dismiss there is no run.
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          sync_platform_count: 5,
          sync_collection_count: 0,
        },
        new_names: [],
        changed_names: [],
        preview_id: "p-empty",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      const descs = Array.from(container.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
      expect(container.querySelector('[data-testid="sync-scope"]')).toBeNull();
      expect(container.querySelector('[data-testid="sync-estimate"]')).toBeNull();
      expect(container.textContent).not.toContain("Progress is saved");
      expect(buttonByExactText(container, "Dismiss")).not.toBeNull();
    });
  });

  describe("persistent session-budget banners (#1383)", () => {
    async function renderIdle(
      lastAttemptStatus: string | undefined,
      rssKb: number | null,
      resumeReady: boolean | null = null,
      counts: { done: number | null; total: number | null } = { done: null, total: null },
    ): Promise<HTMLElement> {
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 42,
        last_attempt: lastAttemptStatus
          ? { finished_at: "2026-07-11T17:48:00", status: lastAttemptStatus as "paused" }
          : null,
      });
      vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue({
        success: true,
        rss_kb: rssKb,
        warn_kb: 1_800_000,
        ceiling_kb: 2_200_000,
        cliff_kb: 2_450_000,
        memory_delta_kb: null,
        resume_ready: resumeReady,
        run_done_items: counts.done,
        run_total_items: counts.total,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      return container;
    }

    it("shows the blue paused banner with the live number after a paused run", async () => {
      const c = await renderIdle("paused", 2_299_000);
      const banner = c.querySelector('[data-testid="budget-paused-banner"]');
      expect(banner).not.toBeNull();
      expect(banner?.textContent).toContain("Steam memory is full (2.3 GB). Restart Steam, then Resume Sync.");
    });

    it("degrades the paused banner to text-only when the reading is unavailable", async () => {
      const c = await renderIdle("paused", null);
      const banner = c.querySelector('[data-testid="budget-paused-banner"]');
      expect(banner).not.toBeNull();
      expect(banner?.textContent).toContain("Resume Sync");
      expect(banner?.textContent).not.toContain("GB");
    });

    it("shows the yellow high-heap banner after a completed run with a high live heap", async () => {
      const c = await renderIdle(undefined, 1_900_000);
      const banner = c.querySelector('[data-testid="budget-high-heap-banner"]');
      expect(banner).not.toBeNull();
      expect(banner?.textContent).toContain("Steam memory is high: 1.9 GB of 2.4 GB");
      expect(c.querySelector('[data-testid="budget-paused-banner"]')).toBeNull();
    });

    it("feeds the backend's run progress into the paused banner", async () => {
      const c = await renderIdle("paused", 2_299_000, false, { done: 1200, total: 2001 });
      expect(c.querySelector('[data-testid="budget-paused-banner"]')?.textContent).toContain(
        "1200 of 2001 games done.",
      );
    });

    it("omits the progress sentence when the backend doesn't know the counts (post-reload)", async () => {
      const c = await renderIdle("paused", 2_299_000, false, { done: null, total: null });
      expect(c.querySelector('[data-testid="budget-paused-banner"]')?.textContent).not.toContain("games done");
    });

    it("shows no banner when idle with a low live heap and no paused attempt", async () => {
      const c = await renderIdle(undefined, 440_000);
      expect(c.querySelector('[data-testid="budget-paused-banner"]')).toBeNull();
      expect(c.querySelector('[data-testid="budget-high-heap-banner"]')).toBeNull();
    });

    it("flips the paused banner to 'memory is free' once resume_ready is true (#38)", async () => {
      const c = await renderIdle("paused", 500_000, true);
      const banner = c.querySelector('[data-testid="budget-paused-banner"]');
      expect(banner?.textContent).toContain("Steam memory is free again (0.5 GB)");
      expect(banner?.textContent).not.toContain("Restart Steam, then Resume Sync");
    });

    it("flips the last-attempt line + hides the paused banner when a newer terminal supersedes it (#39)", async () => {
      // A paused run is showing.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 42,
        last_attempt: { finished_at: "2026-07-11T14:41:00", status: "paused" },
      });
      vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue({
        success: true,
        rss_kb: 500_000,
        warn_kb: 1_800_000,
        ceiling_kb: 2_200_000,
        cliff_kb: 2_450_000,
        memory_delta_kb: null,
        resume_ready: true,
        run_done_items: null,
        run_total_items: null,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.querySelector('[data-testid="budget-paused-banner"]')).not.toBeNull();
      expect(container.textContent).toContain("(paused)");

      // A newer cancelled attempt supersedes the paused run; the stats refetch on the
      // terminal event returns it.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 42,
        last_attempt: { finished_at: "2026-07-11T14:45:00", status: "cancelled" },
      });
      await act(async () => {
        setSyncProgress({ running: false, stage: "cancelled", message: "Sync cancelled" });
      });
      await flushAsync();

      expect(container.querySelector('[data-testid="budget-paused-banner"]')).toBeNull();
      expect(container.textContent).toContain("(cancelled)");
      expect(container.textContent).not.toContain("(paused)");
    });

    it("recovers via the paused-poll stats backstop if the terminal refetch was missed (#39)", async () => {
      // Belt-and-braces on top of the backend emit-last fix: even if no terminal
      // event reached this mount, the paused poll re-reads stats and flips once the
      // newer terminal appears in the data.
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        vi.mocked(backend.getSyncStats).mockResolvedValue({
          ...defaultStats(),
          roms: 42,
          last_attempt: { finished_at: "2026-07-11T14:41:00", status: "paused" },
        });
        vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue({
          success: true,
          rss_kb: 500_000,
          warn_kb: 1_800_000,
          ceiling_kb: 2_200_000,
          cliff_kb: 2_450_000,
          memory_delta_kb: 0,
          resume_ready: true,
          run_done_items: null,
          run_total_items: null,
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        expect(container.querySelector('[data-testid="budget-paused-banner"]')).not.toBeNull();

        // A newer cancelled attempt now appears in the data (no terminal event fired).
        vi.mocked(backend.getSyncStats).mockResolvedValue({
          ...defaultStats(),
          roms: 42,
          last_attempt: { finished_at: "2026-07-11T14:45:00", status: "cancelled" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000); // one paused-poll tick
        });
        expect(container.querySelector('[data-testid="budget-paused-banner"]')).toBeNull();
        expect(container.textContent).toContain("(cancelled)");
      } finally {
        vi.useRealTimers();
      }
    });

    it("polls while paused and flips the banner when a Steam restart frees memory (#38)", async () => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        vi.mocked(backend.getSyncStats).mockResolvedValue({
          ...defaultStats(),
          roms: 42,
          last_attempt: { finished_at: "2026-07-11T17:48:00", status: "paused" },
        });
        // Before the restart: high RSS, resume would re-pause. After (poll tick):
        // fresh baseline, resume_ready true.
        vi.mocked(backend.getSessionBudgetStatus)
          .mockResolvedValueOnce({
            success: true,
            rss_kb: 2_199_000,
            warn_kb: 1_800_000,
            ceiling_kb: 2_200_000,
            cliff_kb: 2_450_000,
            memory_delta_kb: null,
            resume_ready: false,
            run_done_items: null,
            run_total_items: null,
          })
          .mockResolvedValue({
            success: true,
            rss_kb: 500_000,
            warn_kb: 1_800_000,
            ceiling_kb: 2_200_000,
            cliff_kb: 2_450_000,
            memory_delta_kb: null,
            resume_ready: true,
            run_done_items: null,
            run_total_items: null,
          });

        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        const bannerText = () => container.querySelector('[data-testid="budget-paused-banner"]')?.textContent ?? "";
        expect(bannerText()).toContain("Restart Steam, then Resume Sync");
        expect(buttonByExactText(container, "Restart Steam now")).not.toBeNull();

        // The paused poll runs at 10s — advance one tick → re-fetch → the banner flips.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000);
        });
        expect(bannerText()).toContain("Steam memory is free again (0.5 GB)");
        expect(buttonByExactText(container, "Restart Steam now")).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe("STATUS memory row (#32)", () => {
    async function renderMemoryRow(rssKb: number | null, memoryDeltaKb: number | null): Promise<HTMLElement> {
      vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue({
        success: true,
        rss_kb: rssKb,
        warn_kb: 1_800_000,
        ceiling_kb: 2_200_000,
        cliff_kb: 2_450_000,
        memory_delta_kb: memoryDeltaKb,
        resume_ready: null,
        run_done_items: null,
        run_total_items: null,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      return container;
    }

    it("shows the live memory value and the signed last-run delta on one line", async () => {
      const c = await renderMemoryRow(440_000, 800_000);
      const row = c.querySelector('[data-testid="steam-memory"]');
      // One line "0.4 GB · last run +0.8": the delta drops its GB unit (inline
      // after the GB reading) and reads "last run" (not "last sync"), so a paused
      // run reads honestly as that run's consumption so far (#36).
      expect(row?.textContent).toBe("0.4 GB · last run +0.8");
    });

    it("renders a negative delta with a minus sign", async () => {
      const c = await renderMemoryRow(1_000_000, -300_000);
      expect(c.querySelector('[data-testid="steam-memory"]')?.textContent).toBe("1.0 GB · last run -0.3");
    });

    it("omits the delta part when the delta is unmeasurable (null)", async () => {
      const c = await renderMemoryRow(1_200_000, null);
      const row = c.querySelector('[data-testid="steam-memory"]');
      expect(row?.textContent).toBe("1.2 GB");
      expect(row?.textContent).not.toContain("last run");
    });

    it("omits the whole row when the live reading is unavailable (rss_kb null)", async () => {
      const c = await renderMemoryRow(null, null);
      expect(c.querySelector('[data-testid="steam-memory"]')).toBeNull();
    });

    // Traffic-light colouring of the value text, driven by the payload thresholds
    // (renderMemoryRow supplies warn_kb 1.8 GB, ceiling_kb 2.2 GB). Yellow is
    // strict (above the floor, matching the yellow banner); red is inclusive
    // (at the ceiling the gate pauses).
    const valueColor = (c: HTMLElement) =>
      (c.querySelector('[data-testid="steam-memory-value"]') as HTMLElement).style.color;

    it("colours the value GREEN below the advisory floor", async () => {
      const c = await renderMemoryRow(440_000, null);
      expect(valueColor(c)).toBe("#59bf40");
    });

    it("colours the value YELLOW strictly above the advisory floor", async () => {
      const c = await renderMemoryRow(1_800_001, null);
      expect(valueColor(c)).toBe("#d4a72c");
    });

    it("colours the value RED at/above the pause ceiling", async () => {
      const c = await renderMemoryRow(2_200_000, null);
      expect(valueColor(c)).toBe("#d4343c");
    });

    it("does not colour the label or the delta part", async () => {
      const c = await renderMemoryRow(2_200_000, 800_000);
      // The delta part stays uncoloured (only the value carries the traffic-light colour).
      const deltaPart = c.querySelector('[data-testid="steam-memory-delta"]') as HTMLElement | null;
      expect(deltaPart).not.toBeNull();
      expect(deltaPart!.style.color).toBe("");
    });
  });

  describe("Steam memory row live poll during a sync (#33)", () => {
    const budget = (rssKb: number | null): SessionBudgetStatus => ({
      success: true,
      rss_kb: rssKb,
      warn_kb: 1_800_000,
      ceiling_kb: 2_200_000,
      cliff_kb: 2_450_000,
      memory_delta_kb: null,
      resume_ready: null,
      run_done_items: null,
      run_total_items: null,
    });
    const runningStatus = {
      running: true,
      stage: "applying" as const,
      step: 1,
      totalSteps: 1,
      current: 0,
      total: 10,
      message: "N64: 0/10",
    };
    const memoryRowText = (c: HTMLElement) => c.querySelector('[data-testid="steam-memory"]')?.textContent ?? "";

    it("re-polls the live reading every ~5s while running and updates the row", async () => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        // Mount into a live run (getSyncStatus seeds syncing=true). The mount fetch
        // reads 0.6 GB; every poll thereafter reads the climbed 1.3 GB.
        vi.mocked(backend.getSyncStatus).mockResolvedValue(runningStatus);
        vi.mocked(backend.getSessionBudgetStatus)
          .mockResolvedValueOnce(budget(600_000))
          .mockResolvedValue(budget(1_300_000));

        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        expect(memoryRowText(container)).toContain("0.6 GB"); // stale mount value

        // One 5s poll tick → re-fetch → the row tracks the climbed reading. Non-vacuous:
        // the displayed value actually changes, so a dead interval would fail here.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5000);
        });
        expect(memoryRowText(container)).toContain("1.3 GB");
        expect(memoryRowText(container)).not.toContain("0.6 GB");
      } finally {
        vi.useRealTimers();
      }
    });

    it("stops polling once the run reaches a terminal stage", async () => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        vi.mocked(backend.getSyncStatus).mockResolvedValue(runningStatus);
        vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue(budget(1_000_000));

        render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000); // two poll ticks while running
        });
        expect(vi.mocked(backend.getSessionBudgetStatus).mock.calls.length).toBeGreaterThan(1);

        // Terminal frame flips syncing→false: the subscriber does one final refresh
        // and the poll effect tears its interval down.
        await act(async () => {
          setSyncProgress({ running: false, stage: "done", message: "Sync complete" });
        });
        await flushAsync();
        const callsAfterTerminal = vi.mocked(backend.getSessionBudgetStatus).mock.calls.length;

        // Well past several poll intervals: no further polls fire.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(20_000);
        });
        expect(vi.mocked(backend.getSessionBudgetStatus).mock.calls).toHaveLength(callsAfterTerminal);
      } finally {
        vi.useRealTimers();
      }
    });

    it("tears the poll interval down on unmount", async () => {
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      try {
        vi.mocked(backend.getSyncStatus).mockResolvedValue(runningStatus);
        vi.mocked(backend.getSessionBudgetStatus).mockResolvedValue(budget(1_000_000));

        const { unmount } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        const before = vi.mocked(backend.getSessionBudgetStatus).mock.calls.length;

        unmount();
        await act(async () => {
          await vi.advanceTimersByTimeAsync(15_000);
        });
        expect(vi.mocked(backend.getSessionBudgetStatus).mock.calls).toHaveLength(before);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe("two-level in-flight progress UI", () => {
    it("main bar interpolates within the running unit and shows the stage label", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 5,
        current: 3,
        total: 10,
        message: "N64: 3/10",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const op = container.querySelector('[data-testid="sync-stage"]');
      expect(op?.textContent).toContain("Applying shortcuts");
      // The caption's step span still carries the coarse "step/totalSteps" text.
      expect(container.querySelector('[data-testid="sync-step"]')?.textContent).toContain("2/5");
      // Interpolated: floor (step-1)=1 plus the apply sub-slice fill — fetch and
      // covers already filled their shares, so applying starts at (F+C) and adds
      // A*(3/10) — over 5 steps (#1407).
      const within = FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * (3 / 10);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((1 + within) / 5) * 100, 5);
      expect(container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("false");
    });

    it("main bar interpolates a large unit's within-unit fraction (2091 items at 2/8)", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 450,
        total: 2091,
        message: "PSX: 450/2091",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // (step-1 + apply sub-slice fill) / totalSteps * 100, where applying fills
      // (F+C) + A*(450/2091) of the unit's slice (#1407).
      const within = FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * (450 / 2091);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((1 + within) / 8) * 100, 5);
    });

    it("main bar weights units by the plan's item weights when a plan is measured (#1382)", async () => {
      // Plan weights [10, 2091, 5] — the huge PSX unit owns most of the bar, not
      // an equal 1/3 slice. The run state comes from the real syncEta module,
      // exactly as the sync_plan listener seeds it.
      beginEtaRun("run-1", [10, 2091, 5], 2106);
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 3,
        current: 450,
        total: 2091,
        message: "PSX: 450/2091",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Weighted: unit 1's full weight (10) plus PSX's within-unit fill of its
      // 2091 weight. Applying fills (F+C) + A*(450/2091) of the unit (#1407), so
      // the running unit contributes within*2091 of the 2106 total.
      const within = FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * (450 / 2091);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((10 + within * 2091) / 2106) * 100, 5);
    });

    it("a LEADING predicted-skip unit claims its equal index share (#1506)", async () => {
      // Unit 1 was zero-weighted as a predicted wholesale skip. #1382 gave such
      // a unit no bar width at all, on the premise that a skip is
      // instantaneous, so costing it nothing was free. #1506 is the evidence
      // that the premise is false for a LEADING skip: an empty apply delta
      // still refreshes covers and occupies real wall-clock time, so a
      // zero-width leading unit pinned the whole bar to empty while it worked.
      // It therefore claims an ordinary equal 1/totalUnits slice as a floor.
      beginEtaRun("run-1", [0, 100], 100);
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 2,
        current: 50,
        total: 100,
        message: "GBA: 50/100",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // The leading skip floors the bar at its 1/2 slice; unit 2's weighted
      // fill — applying at (F+C) + A*(50/100) of its 100 weight over the 100
      // total (#1407) — fills the band above that floor rather than stalling on
      // it, so the bar keeps moving through the unit that does the real work.
      const within = FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * (50 / 100);
      const floor = 1 / 2;
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo((floor + (1 - floor) * within) * 100, 5);
    });

    it("a mid-plan zero-weight unit still occupies no bar width (#1382)", async () => {
      // The #1506 floor covers only the plan's LEADING run of zero-weight
      // units. Unit 2 here follows real work, so the weighting's distribution
      // intent is untouched: the bar rests on unit 1's completed share and the
      // skipped unit adds nothing, whatever its within-unit fill reads.
      beginEtaRun("run-1", [100, 0, 100], 200);
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 3,
        current: 50,
        total: 100,
        message: "GBA: 50/100",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Unit 1's 100 of the 200 total, and nothing from the zero-weight unit 2.
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(50, 5);
    });

    it("falls back to index weighting when the plan's unit count mismatches the run (stale plan)", async () => {
      // A leftover plan from another run (2 units) cannot apportion an 8-unit
      // run — the bar falls back to the equal-slice interpolation.
      beginEtaRun("run-other", [5, 5], 10);
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 450,
        total: 2091,
        message: "PSX: 450/2091",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Index fallback: (step-1 + apply sub-slice fill) / totalSteps (#1407).
      const within = FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * (450 / 2091);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((1 + within) / 8) * 100, 5);
    });

    it("main bar rests at the unit floor during fetch when no sub-stage is present (old backend)", async () => {
      // A backend that predates #1407 sends fetch frames with no subStage: they
      // carry current/total (page counters) to drive the fine line, but with no
      // sub-slice to fill the coarse bar rests at (step-1)/totalSteps — the
      // pre-#1407 behaviour, never a backwards jump at the fetch→apply boundary.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        step: 2,
        totalSteps: 8,
        current: 30,
        total: 62,
        message: "Fetching GBA (page 30/62)",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Floor only: (2-1)/8 * 100 = 12.5. The page counter (30/62) does not lift it.
      expect(container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("12.5");
    });

    it("fetch sub-stage fills within the fetch sub-slice (#1407)", async () => {
      // A fetch-phase frame (subStage "fetch") lifts the bar within the fetch
      // share only — page 30/62 → FETCH_SHARE * (30/62) above the unit floor.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        subStage: "fetch",
        step: 2,
        totalSteps: 8,
        current: 30,
        total: 62,
        message: "Fetching GBA (page 30/62)",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const within = FETCH_SHARE * (30 / 62);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((1 + within) / 8) * 100, 5);
    });

    it("covers sub-stage continues above the fetch share (#1407)", async () => {
      // A cover-phase frame (subStage "covers") starts where fetch ended
      // (FETCH_SHARE) and fills the covers share by its own current/total.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        subStage: "covers",
        step: 2,
        totalSteps: 8,
        current: 500,
        total: 2000,
        message: "Preparing covers for GBA (500/2000)",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const within = FETCH_SHARE + COVERS_SHARE * (500 / 2000);
      expect(within).toBeGreaterThan(FETCH_SHARE);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((1 + within) / 8) * 100, 5);
    });

    it("advances monotonically across a fetch → covers → apply frame sequence (#1407)", async () => {
      // Drive the running unit (step 2/8) through its three phases and assert the
      // coarse bar never decreases at any frame — the core #1407 guarantee. The
      // mount seed is a running fetch anchor so the in-flight bar renders.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        step: 2,
        totalSteps: 8,
        current: 0,
        total: 0,
        message: "Fetching GBA",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const barValue = () => Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);

      const frames: SyncProgress[] = [];
      // Fetch anchor (no sub-stage) then paginated fetch frames.
      frames.push({ running: true, stage: "fetching", step: 2, totalSteps: 8, current: 0, total: 0 });
      for (let page = 1; page <= 10; page++) {
        frames.push({
          running: true,
          stage: "fetching",
          subStage: "fetch",
          step: 2,
          totalSteps: 8,
          current: page,
          total: 10,
        });
      }
      // Cover download frames.
      for (let c = 1; c <= 100; c++) {
        frames.push({
          running: true,
          stage: "fetching",
          subStage: "covers",
          step: 2,
          totalSteps: 8,
          current: c,
          total: 100,
        });
      }
      // Frontend apply frames.
      for (let a = 1; a <= 50; a++) {
        frames.push({ running: true, stage: "applying", step: 2, totalSteps: 8, current: a, total: 50 });
      }

      let previous = -1;
      for (const frame of frames) {
        act(() => setSyncProgress(frame));
        const value = barValue();
        expect(value).toBeGreaterThanOrEqual(previous);
        previous = value;
      }
      // The unit ends the apply phase at its full slice ceiling: floor + 1 slice.
      expect(previous).toBeCloseTo((2 / 8) * 100, 5);
    });

    it("main bar reads 100% during finalizing (step == totalSteps, all units done)", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "finalizing",
        step: 8,
        totalSteps: 8,
        message: "Finalizing…",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Terminal-ish stage keeps the full step count → 8/8 * 100 = 100 (a naive
      // (step-1) base would drop it to 87.5, jumping backwards from the last
      // unit's apply which reached 100%).
      expect(container.querySelector('[data-testid="progress-progress"]')?.textContent).toBe("100");
    });

    it("main bar goes indeterminate when totalSteps is 0", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        step: 0,
        totalSteps: 0,
        message: "Fetching platforms...",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.querySelector('[data-testid="progress-indeterminate"]')?.textContent).toBe("true");
    });

    it("detail line renders the bare fine current/total message (no step prefix)", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 1,
        totalSteps: 2,
        current: 4,
        total: 8,
        message: "N64: 4/8",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // The detail line carries the bare message — the coarse "step/totalSteps"
      // is shown once on the bar row (sync-step), not duplicated here.
      expect(container.textContent).toContain("N64: 4/8");
      expect(container.textContent).not.toContain("[1/2]");
    });

    it("renders the full detail message without mid-word truncation (CSS wraps it)", async () => {
      // The longer narrated messages must not be clipped mid-parenthesis with an
      // ellipsis (the old formatProgressText behavior); they wrap in CSS instead.
      const longMsg = "Fetching Game Boy Advance (page 4/62) and a lot more text";
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 5,
        current: 4,
        total: 60,
        message: longMsg,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.textContent).toContain(longMsg);
      expect(container.textContent).not.toContain("…");
      // The shared wrap rule is applied: the line wraps (whiteSpace normal),
      // never single-line nowrap.
      const fine = container.querySelector('[data-testid="sync-fine"]') as HTMLElement | null;
      expect(fine).not.toBeNull();
      expect(fine!.style.whiteSpace).toBe("normal");
      expect(fine!.style.whiteSpace).not.toBe("nowrap");
    });

    it("shows a spinner next to the stage label while running without fine detail", async () => {
      // The initial anchor frame: running, a stage label, but no narrated
      // fine-detail page frame yet (no total/message). Without a spinner here
      // the panel looks hung — the stage label must carry one so a running sync
      // always shows motion.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        step: 0,
        totalSteps: 0,
        // no total / message → hasFineDetail false
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const stage = container.querySelector('[data-testid="sync-stage"]');
      expect(stage).not.toBeNull();
      // The spinner sits inline with the stage label (its wrapping span).
      expect(stage!.parentElement?.querySelector('[data-testid="spinner"]')).not.toBeNull();
      // No second spinner — the fine line is absent, and the connection row is
      // "Connected" (icon, no spinner).
      expect(container.querySelectorAll('[data-testid="spinner"]')).toHaveLength(1);
    });

    it("shows only the fine-line spinner (no stage-label spinner) once fine detail exists", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 5,
        current: 3,
        total: 10,
        message: "N64: 3/10",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const stage = container.querySelector('[data-testid="sync-stage"]');
      // No spinner beside the stage label — the fine line owns the only spinner.
      expect(stage!.parentElement?.querySelector('[data-testid="spinner"]')).toBeNull();
      expect(container.querySelectorAll('[data-testid="spinner"]')).toHaveLength(1);
    });
  });

  describe("fine-detail row stays mounted across unit boundaries (#1415)", () => {
    const fineRow = (c: HTMLElement) => c.querySelector('[data-testid="sync-fine"]');

    it("keeps the fine row mounted (with the carried content) across a boundary anchor frame", async () => {
      // Unit N applying with real fine detail — the row is mounted and narrates.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 50,
        total: 50,
        message: "GBA: 50/50",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(fineRow(container)).not.toBeNull();
      expect(fineRow(container)!.textContent).toContain("GBA: 50/50");

      // Unit boundary: the next unit's FETCHING anchor frame resets current/total
      // to 0 (worst case: also an empty message). Pre-#1415 this flipped
      // hasFineDetail false and unmounted the row for a frame; now the row stays
      // mounted, carrying unit N's last line.
      act(() =>
        setSyncProgress({
          running: true,
          stage: "fetching",
          step: 3,
          totalSteps: 8,
          current: 0,
          total: 0,
          message: "",
        }),
      );
      expect(fineRow(container)).not.toBeNull();
      expect(fineRow(container)!.textContent).toContain("GBA: 50/50");

      // The next unit's first real fetch frame arrives — the row updates to its
      // content, still without ever unmounting.
      act(() =>
        setSyncProgress({
          running: true,
          stage: "fetching",
          subStage: "fetch",
          step: 3,
          totalSteps: 8,
          current: 1,
          total: 5,
          message: "Fetching SNES (page 1/5)",
        }),
      );
      expect(fineRow(container)).not.toBeNull();
      expect(fineRow(container)!.textContent).toContain("Fetching SNES (page 1/5)");
    });

    it("does not flash the stage-label spinner at a boundary — the fine line keeps the only spinner", async () => {
      // The inline stage-label spinner appears only when hasFineDetail is false;
      // a boundary that unmounted the fine row would flash it on for that frame.
      // With the carry, hasFineDetail stays true, so no stage-label spinner ever
      // appears and the count stays at one (the fine line's).
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 50,
        total: 50,
        message: "GBA: 50/50",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.querySelectorAll('[data-testid="spinner"]')).toHaveLength(1);

      act(() =>
        setSyncProgress({
          running: true,
          stage: "fetching",
          step: 3,
          totalSteps: 8,
          current: 0,
          total: 0,
          message: "",
        }),
      );
      const stage = container.querySelector('[data-testid="sync-stage"]');
      expect(stage!.parentElement?.querySelector('[data-testid="spinner"]')).toBeNull();
      expect(container.querySelectorAll('[data-testid="spinner"]')).toHaveLength(1);
    });

    it("clears the carry when the run ends — no stale line on the done state nor the next run's start", async () => {
      // Run A applies with fine detail (row mounted).
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 8,
        totalSteps: 8,
        current: 50,
        total: 50,
        message: "GBA: 50/50",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(fineRow(container)!.textContent).toContain("GBA: 50/50");

      // Run A terminates (running:false) — the carried line is dropped and the
      // in-flight UI tears down, so the done/summary state shows no fine row.
      act(() => setSyncProgress({ running: false, stage: "done", message: "Sync complete: 50 games" }));
      await flushAsync();
      expect(fineRow(container)).toBeNull();

      // Run B starts via Skip Preview → the optimistic coarse fetch anchor carries
      // no fine detail. Because the prior run's carry was cleared, the fine row is
      // absent — no stale "GBA: 50/50" leaks into the new run's start. (Without the
      // reset, hasFineDetail would fall back to the carried line and show it here.)
      const toggle = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      expect(toggle).not.toBeNull();
      fireEvent.click(toggle!);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(fineRow(container)).toBeNull();
    });
  });

  describe("sync boundary: the anchor names the next unit and the row height is reserved", () => {
    const fineRow = (c: HTMLElement) => c.querySelector('[data-testid="sync-fine"]') as HTMLElement | null;

    it("replaces the carried line with the boundary anchor's own message (names the new unit)", async () => {
      // Unit A applying with real fine detail — the row narrates unit A.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 50,
        total: 50,
        message: "GBA: 50/50",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(fineRow(container)!.textContent).toContain("GBA: 50/50");

      // Unit boundary: the next unit's FETCHING anchor carries no fine detail
      // (total 0) but a coarse position (totalSteps 8) and names the new unit in
      // its message. The fine line must snap to the NEW unit immediately, not
      // keep unit A's stale text during the anchor dwell before the first real
      // frame lands.
      act(() =>
        setSyncProgress({
          running: true,
          stage: "fetching",
          step: 3,
          totalSteps: 8,
          current: 0,
          total: 0,
          message: "Fetching SNES... (3/8)",
        }),
      );
      expect(fineRow(container)!.textContent).toContain("Fetching SNES... (3/8)");
      expect(fineRow(container)!.textContent).not.toContain("GBA: 50/50");

      // The new unit's first real fetch frame lands — normal formatted text.
      act(() =>
        setSyncProgress({
          running: true,
          stage: "fetching",
          subStage: "fetch",
          step: 3,
          totalSteps: 8,
          current: 1,
          total: 5,
          message: "Fetching SNES (page 1/5)",
        }),
      );
      expect(fineRow(container)!.textContent).toContain("Fetching SNES (page 1/5)");
    });

    it("reserves the two-line clamp box height on the fine-detail element", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 3,
        total: 10,
        message: "N64: 3/10",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const fine = fineRow(container);
      expect(fine).not.toBeNull();
      // Two 1.4 line-heights reserved as 2.8em — a 1↔2-line wrap change never
      // reflows the ETA row / Cancel button below it (the residual jolt).
      expect(fine!.style.minHeight).toBe("2.8em");
      expect(fine!.style.lineHeight).toBe("1.4");
    });

    it("keeps the prior line when a boundary anchor carries an empty message (never blank)", async () => {
      // The defensive branch: both real apply/preview anchors carry a message,
      // but an empty-message running frame must never clear the carry — the row
      // would blank mid-run. Carry replacement, never carry removal.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 2,
        totalSteps: 8,
        current: 50,
        total: 50,
        message: "GBA: 50/50",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(fineRow(container)!.textContent).toContain("GBA: 50/50");

      act(() =>
        setSyncProgress({
          running: true,
          stage: "fetching",
          step: 3,
          totalSteps: 8,
          current: 0,
          total: 0,
          message: "",
        }),
      );
      expect(fineRow(container)).not.toBeNull();
      expect(fineRow(container)!.textContent).toContain("GBA: 50/50");
    });
  });

  describe("QAM remount mid-run preserves fine progress + ETA", () => {
    it("merges the store's fine fields + etaSeconds over the backend's coarse running snapshot", async () => {
      // Module store holds the in-flight run's FINE state — what a live QAM had
      // (frontend per-item updates + the sync_plan-derived ETA) before it was
      // torn down and remounted.
      setSyncProgress({
        running: true,
        stage: "applying",
        current: 1200,
        total: 3084,
        message: "PSX: 1200/3084",
        step: 2,
        totalSteps: 8,
        runId: "run-live",
        etaSeconds: 480,
      });
      // Backend snapshot for the SAME run is coarse: current/total 0, no ETA.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        current: 0,
        total: 0,
        message: "PSX (2/8)",
        step: 2,
        totalSteps: 8,
        runId: "run-live",
      });

      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      // Coarse step counter is present either way.
      expect(container.querySelector('[data-testid="sync-step"]')?.textContent).toContain("2/8");
      // Fine line survives the remount — it renders only when total && message,
      // both preserved from the store (backend's total was 0).
      expect(container.textContent).toContain("PSX: 1200/3084");
      // ETA row survives — etaSeconds is frontend-only, never in the backend
      // snapshot; a blind replace would drop it. Non-vacuous: the visible
      // "up to X" row proves the merge kept it.
      expect(container.querySelector('[data-testid="estimate-time"]')?.textContent).toContain("up to");
    });

    it("keeps the store's applying stage when the backend's snapshot is still the fetch anchor", async () => {
      // The backend never emits an "applying" frame (its last emit is the fetch
      // anchor), so a remount mid-apply must not let the stale "fetching" stage
      // drop the coarse-bar interpolation or flip the label.
      setSyncProgress({
        running: true,
        stage: "applying",
        current: 1200,
        total: 3084,
        message: "PSX: 1200/3084",
        step: 2,
        totalSteps: 8,
        runId: "run-live",
      });
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        current: 0,
        total: 0,
        message: "Fetching PSX",
        step: 2,
        totalSteps: 8,
        runId: "run-live",
      });

      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      expect(container.querySelector('[data-testid="sync-stage"]')?.textContent).toContain("Applying shortcuts");
      // Interpolation stays live in the apply sub-slice: (1 + (F+C) + A*1200/3084)
      // / 8, not the 12.5% unit floor (#1407).
      const within = FETCH_SHARE + COVERS_SHARE + APPLY_SHARE * (1200 / 3084);
      const nProgress = Number(container.querySelector('[data-testid="progress-progress"]')?.textContent);
      expect(nProgress).toBeCloseTo(((1 + within) / 8) * 100, 5);
    });

    it("replaces (drops stale fine fields + ETA) when the backend reports a different run", async () => {
      setSyncProgress({
        running: true,
        stage: "applying",
        current: 1200,
        total: 3084,
        message: "PSX: 1200/3084",
        step: 2,
        totalSteps: 8,
        runId: "run-old",
        etaSeconds: 480,
      });
      // A different in-flight run — the old run's fine fields + ETA must NOT
      // bleed through into the fresh run's UI.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        current: 0,
        total: 0,
        message: "Fetching library...",
        step: 0,
        totalSteps: 0,
        runId: "run-new",
      });

      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      expect(container.querySelector('[data-testid="sync-stage"]')?.textContent).toContain("Fetching library");
      // Stale ETA from the prior run is gone (replace branch, not merge).
      expect(container.querySelector('[data-testid="estimate-time"]')).toBeNull();
      // Stale fine line from the prior run is gone too.
      expect(container.textContent).not.toContain("PSX: 1200/3084");
    });
  });

  describe("always-on sync estimate (#1025 UX)", () => {
    // ``coverRefreshCount`` is left OFF the summary when zero, so the default
    // fixtures keep exercising the absent-field (older backend) path.
    function previewWithCounts(newCount: number, changedCount: number, coverRefreshCount = 0): SyncPreview {
      return {
        success: true,
        summary: {
          new_count: newCount,
          changed_count: changedCount,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          ...(coverRefreshCount > 0 ? { cover_refresh_count: coverRefreshCount } : {}),
        },
        new_names: [],
        changed_names: [],
        preview_id: "p1",
      };
    }

    async function renderPreviewWithCounts(
      newCount: number,
      changedCount: number,
      coverRefreshCount = 0,
    ): Promise<HTMLElement> {
      vi.mocked(backend.syncPreview).mockResolvedValue(previewWithCounts(newCount, changedCount, coverRefreshCount));
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

    // Applying-state readout — the separate "Estimated time" Field (sync-scope's
    // sibling only while a run is in flight).
    function estimateText(container: HTMLElement): string | undefined {
      return container.querySelector('[data-testid="estimate-time"]')?.textContent ?? undefined;
    }

    // Preview-state readout — the estimate owns its own line under the "Changes"
    // block, below the coverage line.
    function estimateLine(container: HTMLElement): string | undefined {
      return container.querySelector('[data-testid="sync-estimate"]')?.textContent ?? undefined;
    }

    it("renders an estimate on its own line for a small preview", async () => {
      // 3 new * (0.36s walk + 0.15s cover) + 45s allowance = 46.53s → "< 1 min".
      // A handful of creates really is dominated by the run's fixed overhead.
      const c = await renderPreviewWithCounts(3, 0);
      expect(estimateLine(c)).toBe("Estimated duration: < 1 min");
    });

    it("renders an estimate on its own line for a large preview", async () => {
      // 1000 new * (0.36s walk + 0.15s cover) + 45s = 555s → ~9 min.
      const c = await renderPreviewWithCounts(1000, 0);
      expect(estimateLine(c)).toBe("Estimated duration: 9 min");
    });

    it("prices updated items into the preview estimate", async () => {
      // 100 updated * 0.13s + 45s = 58s → "< 1 min". Updates carry no cover
      // download — the apply loop applies artwork only to created shortcuts.
      const c = await renderPreviewWithCounts(0, 100);
      expect(estimateLine(c)).toBe("Estimated duration: < 1 min");
    });

    it("prices cover refreshes, so a cover-only preview no longer reads the flat allowance (#1511)", async () => {
      // No shortcut delta at all, 400 covers changed server-side: 400 * 0.15s +
      // 45s = 105s → ~2 min. Under the old model this read a flat 90s ("2 min")
      // whether one cover or four thousand had changed.
      const c = await renderPreviewWithCounts(0, 0, 400);
      expect(estimateLine(c)).toBe("Estimated duration: 2 min");
    });

    it("shows the compact info copy alongside the preview estimate (short sync → no sleep caveat)", async () => {
      // ~2 min preview (< 10 min threshold): the always-shown line only.
      const c = await renderPreviewWithCounts(1, 0);
      expect(c.textContent).toContain("Progress is saved about every 200 games — cancelling is safe.");
      expect(c.textContent).not.toContain("Long syncs pause during sleep");
    });

    it("appends the sleep-pause caveat only when the estimate is ≥ 10 min", async () => {
      // 1200 new * (0.36s walk + 0.15s cover) + 45s = 657s ≥ 600s (10 min) → the
      // caveat sentence appears. Pricing cover downloads separately is what keeps
      // this fixture above the threshold after the walk rate came down (#1511).
      const c = await renderPreviewWithCounts(1200, 0);
      expect(c.textContent).toContain("Progress is saved about every 200 games — cancelling is safe.");
      expect(c.textContent).toContain("Long syncs pause during sleep; keep the Deck powered.");
    });

    it("prices the preview row from the DELTA (new + changed) — unchanged items are skipped, not walked", async () => {
      // Resume-shaped: 153 real creates and ~3000 content-unchanged items. The
      // delta-restricted apply (#1383) skips the unchanged entirely (no Set* walk,
      // no confirm poll), so they cost nothing and no longer inflate the estimate:
      // 153*(NEW_ITEM_SEC + COVER_DOWNLOAD_SEC) + 45s allowance = 123s → ~2 min.
      // The 3000 unchanged priced the old walk model at ~13 min; that overshoot is gone.
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 153,
          changed_count: 0,
          unchanged_count: 3000,
          remove_count: 0,
          disabled_platform_remove_count: 0,
        },
        new_names: [],
        changed_names: [],
        preview_id: "p-resume",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(estimateLine(container)).toBe("Estimated duration: 2 min");
      // The 3000 unchanged items must NOT be priced — the walk-model overshoot is gone.
      expect(estimateLine(container)).not.toBe("Estimated duration: 13 min");
    });

    it("renders 'up to X min' while applying when etaSeconds is set", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 1,
        totalSteps: 2,
        message: "N64: 1/10",
        etaSeconds: 850,
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(estimateText(container)).toBe("up to 14 min");
    });

    it("omits the applying estimate row when etaSeconds is absent (honest silence)", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 1,
        totalSteps: 2,
        message: "N64: 1/10",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(container.querySelector('[data-testid="estimate-time"]')).toBeNull();
    });

    async function applyPreviewSummary(summary: Partial<SyncPreviewSummary>): Promise<HTMLElement> {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          ...summary,
        },
        new_names: [],
        changed_names: [],
        preview_id: "p-eta",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Sync → preview with changes → Apply Sync appears.
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // Apply → the optimistic store write carries the walk-cost seed.
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Apply Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      return container;
    }

    it("handleApply seeds the apply ETA from the DELTA (new + changed), unchanged priced at zero", async () => {
      // The delta apply touches only new + changed — creates at the new rate plus
      // their cover download, changed at the bare update rate — and skips unchanged
      // entirely (#1383). new=100, changed=200, unchanged=600 → 36 + 26 + 15 + 45 =
      // 122s; the 600 unchanged add nothing.
      const container = await applyPreviewSummary({ new_count: 100, changed_count: 200, unchanged_count: 600 });
      expect(getSyncProgress().etaSeconds).toBeCloseTo(
        100 * NEW_ITEM_SEC + 200 * UPDATED_ITEM_SEC + 100 * COVER_DOWNLOAD_SEC + FETCH_ALLOWANCE_SEC,
      );
      // Surfaced as the "up to ~X" upper bound (122s → ~2 min) until the live
      // countdown takes over.
      expect(container.querySelector('[data-testid="estimate-time"]')?.textContent).toBe("up to 2 min");
    });

    it("prices a resume-shaped preview (few creates, many unchanged) by its DELTA — unchanged skipped", async () => {
      // The #1382-M3 fix: a resume with ~90 real creates and ~3000 content-unchanged
      // items now SKIPS the 3000 (delta-restricted apply), so the seed prices only
      // the 90 creates: 90*(NEW_ITEM_SEC + COVER_DOWNLOAD_SEC) + 45s allowance =
      // 90.9s → ~2 min. The old walk model priced the 3000 unchanged at ~12 min;
      // that overshoot is gone, and the small delta is the true, fast cost.
      const container = await applyPreviewSummary({ new_count: 90, changed_count: 0, unchanged_count: 3000 });
      expect(getSyncProgress().etaSeconds).toBeCloseTo(
        90 * NEW_ITEM_SEC + 90 * COVER_DOWNLOAD_SEC + FETCH_ALLOWANCE_SEC,
      );
      const text = container.querySelector('[data-testid="estimate-time"]')?.textContent;
      expect(text).toBe("up to 2 min");
      // The 3000 unchanged items must NOT be priced — the walk-model overshoot is gone.
      expect(text).not.toBe("up to 12 min");
    });
  });

  describe("live ETA countdown (#1025)", () => {
    function estimateText(container: HTMLElement): string | undefined {
      return container.querySelector('[data-testid="estimate-time"]')?.textContent ?? undefined;
    }

    it("switches from the static 'up to' seed to a measured 'X min left' countdown, then clears on terminal", async () => {
      vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
      try {
        vi.setSystemTime(0);
        // The plan is set by index.tsx's sync_plan listener; one unit of 54700
        // items. The static seed is total * NEW_ITEM_SEC.
        beginEtaRun("run-1", [54700], 54700);
        const seed = 54700 * NEW_ITEM_SEC;
        // The module store already holds the live run (sync_plan set etaSeconds; an
        // applying frame carries the fine counters) — the state a mounted QAM has.
        // getSyncStatus reports the SAME run so the mount seed keeps it (syncing
        // on) and samples one apply frame at t=0.
        setSyncProgress({
          running: true,
          stage: "applying",
          step: 1,
          totalSteps: 1,
          current: 100,
          total: 54700,
          message: "X: 100/54700",
          runId: "run-1",
          etaSeconds: seed,
        });
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "applying",
          step: 1,
          totalSteps: 1,
          current: 100,
          total: 54700,
          message: "X: 100/54700",
          runId: "run-1",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        // One sample so far (the t=0 mount seed) → static upper bound.
        expect(estimateText(container)).toContain("up to");

        // Advance past the readiness span and deliver a second frame → rate is
        // measured (600 items / 6s = 100/s) → live countdown replaces the seed.
        vi.setSystemTime(6000);
        await act(async () => {
          setSyncProgress({
            running: true,
            stage: "applying",
            step: 1,
            totalSteps: 1,
            current: 700,
            total: 54700,
            message: "X: 700/54700",
            runId: "run-1",
            etaSeconds: seed,
          });
        });
        // remaining = (54700 - 700) / 100 = 540s → rounded up to 9 min.
        expect(estimateText(container)).toBe("9 min left");

        // Terminal stage tears the run down; the in-flight body (and its estimate
        // row) is replaced by the idle UI.
        vi.setSystemTime(7000);
        await act(async () => {
          setSyncProgress({ running: false, stage: "done", message: "Sync complete" });
        });
        expect(container.querySelector('[data-testid="estimate-time"]')).toBeNull();
        expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it("does not feed the rate from fetch frames (page counters), so a fetch burst never yields a live ETA", async () => {
      vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
      try {
        vi.setSystemTime(0);
        beginEtaRun("run-1", [54700], 54700);
        const seed = 54700 * NEW_ITEM_SEC;
        // Live run in the fetch phase — syncing on, estimate row shows the seed.
        setSyncProgress({
          running: true,
          stage: "fetching",
          step: 1,
          totalSteps: 1,
          current: 5,
          total: 62,
          message: "Fetching (page 5/62)",
          runId: "run-1",
          etaSeconds: seed,
        });
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "fetching",
          step: 1,
          totalSteps: 1,
          current: 5,
          total: 62,
          message: "Fetching (page 5/62)",
          runId: "run-1",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();

        // A second fetch frame spanning the readiness window with a large
        // page-counter jump — must NOT be sampled as apply progress.
        vi.setSystemTime(8000);
        await act(async () => {
          setSyncProgress({
            running: true,
            stage: "fetching",
            step: 1,
            totalSteps: 1,
            current: 60,
            total: 62,
            message: "Fetching (page 60/62)",
            runId: "run-1",
            etaSeconds: seed,
          });
        });
        // Still the static seed — fetch frames were ignored by the estimator.
        expect(estimateText(container)).toContain("up to");
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps the live countdown across a fetch gap where the estimator re-arms to null (sticky)", async () => {
      // The estimator's READY gate re-arms ~5s after every inter-unit fetch gap,
      // and the run's tail is small units that each apply in <5s and never re-arm
      // it. The countdown must NOT blink back to the static "up to ~X" seed on
      // those null measurements — it holds the last good deadline and keeps
      // counting down. (Fix: MainPage tracks an absolute deadline, not a raw
      // seconds snapshot; a null measurement keeps the prior deadline.)
      vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
      try {
        vi.setSystemTime(0);
        // Single unit of 54700, so cumulativeProcessed == current (step 1).
        beginEtaRun("run-1", [54700], 54700);
        const seed = 54700 * NEW_ITEM_SEC;
        setSyncProgress({
          running: true,
          stage: "applying",
          step: 1,
          totalSteps: 1,
          current: 100,
          total: 54700,
          message: "X: 100/54700",
          runId: "run-1",
          etaSeconds: seed,
        });
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "applying",
          step: 1,
          totalSteps: 1,
          current: 100,
          total: 54700,
          message: "X: 100/54700",
          runId: "run-1",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        // One sample so far → static upper bound.
        expect(estimateText(container)).toContain("up to");

        // Second frame at t=6s → rate 100/s, remaining 540s → "~9 min left".
        vi.setSystemTime(6000);
        await act(async () => {
          setSyncProgress({
            running: true,
            stage: "applying",
            step: 1,
            totalSteps: 1,
            current: 700,
            total: 54700,
            message: "X: 700/54700",
            runId: "run-1",
            etaSeconds: seed,
          });
        });
        expect(estimateText(container)).toBe("9 min left");

        // A fetch gap (no sample), then two applying frames far enough apart that
        // the window ages down to just its two most recent samples spanning <5s —
        // so liveEtaSeconds() re-arms to null exactly as it does at the start of a
        // tail unit's apply.
        vi.setSystemTime(30000);
        await act(async () => {
          setSyncProgress({
            running: true,
            stage: "fetching",
            step: 1,
            totalSteps: 1,
            current: 20,
            total: 62,
            message: "Fetching (page 20/62)",
            runId: "run-1",
            etaSeconds: seed,
          });
        });
        vi.setSystemTime(33000);
        await act(async () => {
          setSyncProgress({
            running: true,
            stage: "applying",
            step: 1,
            totalSteps: 1,
            current: 800,
            total: 54700,
            message: "X: 800/54700",
            runId: "run-1",
            etaSeconds: seed,
          });
        });
        vi.setSystemTime(37000);
        await act(async () => {
          setSyncProgress({
            running: true,
            stage: "applying",
            step: 1,
            totalSteps: 1,
            current: 900,
            total: 54700,
            message: "X: 900/54700",
            runId: "run-1",
            etaSeconds: seed,
          });
        });
        // Precondition: the estimator really is re-armed to null (window span 4s).
        expect(liveEtaSeconds()).toBeNull();
        // Sticky: the display still shows a "left" countdown, NOT the static seed.
        const text = estimateText(container);
        expect(text).toContain("left");
        expect(text).not.toContain("up to");
      } finally {
        vi.useRealTimers();
      }
    });

    it("a throwing earlier listener cannot starve the mounted instance's re-render (freeze contract)", async () => {
      // On-device an instance mounted before run start froze on the optimistic
      // "Applying" frame and stopped re-rendering for the rest of the run — a
      // subscriber throw aborting the store's notify loop before the re-render.
      // Register a THROWING listener BEFORE mounting so it sits earlier in the
      // store's listener array than MainPage's own subscriber: with the store's
      // per-listener try/catch reverted this earlier throw aborts notify() and
      // MainPage never re-renders, so the assertions below genuinely pin the
      // hardening (not just a happy-path re-render smoke test).
      const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});
      const unsubThrower = onSyncProgressChange(() => {
        throw new Error("earlier listener boom");
      });
      try {
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
          preview_id: "p-freeze",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();

        // Sync → preview with changes → Apply Sync appears.
        await act(async () => {
          fireEvent.click(buttonByExactText(container, "Sync Library")!);
          await Promise.resolve();
          await Promise.resolve();
        });
        // Apply → handleApply sets syncing + the optimistic "Applying" frame (the
        // frame the frozen instance was stuck on).
        await act(async () => {
          fireEvent.click(buttonByExactText(container, "Apply Sync")!);
          await Promise.resolve();
          await Promise.resolve();
        });
        expect(container.querySelector('[data-testid="sync-stage"]')?.textContent).toContain("Applying shortcuts");

        // sync_plan listener shape — a partial update carrying only the ETA seed.
        await act(async () => {
          updateSyncProgress({ etaSeconds: 1000 });
        });

        // Per-item applying frames (syncManager processUnitShortcuts shape). Each
        // must drive a fresh render despite the earlier listener throwing on every
        // notify — the frozen instance stopped advancing here.
        await act(async () => {
          updateSyncProgress({
            running: true,
            stage: "applying",
            current: 5,
            total: 200,
            message: "PSX: 5/200",
            step: 2,
            totalSteps: 8,
          });
        });
        expect(container.textContent).toContain("PSX: 5/200");
        expect(container.querySelector('[data-testid="sync-step"]')?.textContent).toContain("2/8");

        await act(async () => {
          updateSyncProgress({ current: 6, total: 200, message: "PSX: 6/200", step: 2, totalSteps: 8 });
        });
        // The mounted instance kept re-rendering — the fine line advanced.
        expect(container.textContent).toContain("PSX: 6/200");
        // Non-vacuous: the earlier listener really did throw on notify (isolated
        // by the store to console.error), so the re-renders above prove isolation.
        expect(consoleSpy).toHaveBeenCalledWith("[RomM] sync-progress listener threw:", expect.any(Error));
      } finally {
        unsubThrower();
        consoleSpy.mockRestore();
      }
    });

    it("logs and keeps advancing the local mirror when the subscriber's derived work throws", async () => {
      // The subscriber's outer try/catch: the local mirror (setSyncProgress) is
      // updated FIRST and unconditionally, then the derived work runs guarded. If
      // the derived work throws, the catch must log AND the mirror must still
      // advance on every later frame — the re-render chain must not break. Inject
      // the throw by making observeApplyProgress() (called in the non-terminal
      // branch) throw on each applying frame.
      const etaSpy = vi.spyOn(syncEta, "observeApplyProgress").mockImplementation(() => {
        throw new Error("derived boom");
      });
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      try {
        // Recover an in-flight applying run on mount so syncing=true (the
        // in-flight body renders) and the subscriber fires with an applying frame.
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "applying",
          step: 2,
          totalSteps: 8,
          current: 5,
          total: 200,
          message: "PSX: 5/200",
          runId: "run-throw",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        // Post-catch state: the mirror advanced to the first frame despite the
        // throw, and the catch surfaced the subscriber-failure log.
        expect(container.textContent).toContain("PSX: 5/200");
        expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("sync-progress subscriber failed"));

        // Subsequent frames keep advancing the local mirror — the throw on each
        // frame never breaks the re-render chain.
        await act(async () => {
          updateSyncProgress({
            running: true,
            stage: "applying",
            step: 2,
            totalSteps: 8,
            current: 6,
            total: 200,
            message: "PSX: 6/200",
          });
        });
        expect(container.textContent).toContain("PSX: 6/200");

        await act(async () => {
          updateSyncProgress({
            running: true,
            stage: "applying",
            step: 2,
            totalSteps: 8,
            current: 7,
            total: 200,
            message: "PSX: 7/200",
          });
        });
        expect(container.textContent).toContain("PSX: 7/200");
      } finally {
        etaSpy.mockRestore();
        logSpy.mockRestore();
      }
    });

    it("does NOT feed the live-rate estimator on a cover-refresh applying frame (#1456)", async () => {
      const etaSpy = vi.spyOn(syncEta, "observeApplyProgress");
      try {
        // Recover an in-flight applying run so the subscriber fires on applying frames.
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "applying",
          step: 2,
          totalSteps: 8,
          current: 5,
          total: 200,
          message: "PSX: 5/200",
          runId: "run-cover-eta",
        });
        render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        etaSpy.mockClear();

        // A normal shortcut-item applying frame DOES feed the estimator.
        await act(async () => {
          updateSyncProgress({ running: true, stage: "applying", step: 2, totalSteps: 8, current: 6, total: 200 });
        });
        expect(etaSpy).toHaveBeenCalledTimes(1);
        etaSpy.mockClear();

        // A cover-refresh applying frame carries a cover counter, not item
        // progress (current is unchanged) — the estimator must NOT be fed, or the
        // cover phase would distort the rate (#1456).
        await act(async () => {
          updateSyncProgress({
            running: true,
            stage: "applying",
            step: 2,
            totalSteps: 8,
            message: "PSX: covers 37/140",
            coverRefresh: true,
          });
        });
        expect(etaSpy).not.toHaveBeenCalled();
      } finally {
        etaSpy.mockRestore();
      }
    });
  });

  describe("sync button label (resume vs fresh)", () => {
    // roms > 0 = partial progress actually exists to resume. Every non-completed,
    // non-errored terminal status resumes: interrupted, cancelled, and the #1383
    // session-budget pause.
    it.each(["interrupted", "cancelled", "paused"] as const)(
      "reads 'Resume Sync' when the newest attempt was %s with bound shortcuts on disk",
      async (status) => {
        vi.mocked(backend.getSyncStats).mockResolvedValue({
          ...defaultStats(),
          roms: 42,
          last_attempt: { finished_at: "2026-06-01T17:48:00", status },
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        expect(buttonByExactText(container, "Resume Sync")).not.toBeNull();
        expect(buttonByExactText(container, "Sync Library")).toBeNull();
      },
    );

    it("reads 'Sync Library' when an interrupted attempt left ZERO bound shortcuts (all removed — nothing to resume)", async () => {
      // The regression: after an interrupted run the user removed every shortcut
      // (DangerZone "remove all"), so roms is 0 — the next run is a full fresh
      // import, and the label must not falsely promise a resume.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 0,
        last_attempt: { finished_at: "2026-06-01T17:48:00", status: "interrupted" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      expect(buttonByExactText(container, "Resume Sync")).toBeNull();
    });

    it("keeps 'Sync Library' when the newest attempt errored (resume isn't the model)", async () => {
      // An errored run often failed before applying anything (config error, etc.),
      // so "resume" would mislead — the fresh label stays.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_attempt: { finished_at: "2026-06-01T17:48:00", status: "errored" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      expect(buttonByExactText(container, "Resume Sync")).toBeNull();
    });

    it("keeps 'Sync Library' when there is no last attempt (clean state)", async () => {
      // defaultStats() has no last_attempt → the fresh label.
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      expect(buttonByExactText(container, "Resume Sync")).toBeNull();
    });
  });

  describe("Force Full Sync button visibility", () => {
    it("shows Force Full Sync with only an interrupted last_attempt (no completed run)", async () => {
      // The resume situation — where a forced fresh start is most likely wanted.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: null,
        last_attempt: { finished_at: "2026-06-01T17:48:00", status: "interrupted" },
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Force Full Sync")).not.toBeNull();
    });

    it("hides Force Full Sync on a pristine install (no last_sync and no last_attempt)", async () => {
      // defaultStats(): last_sync null, no last_attempt → nothing to clear.
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Force Full Sync")).toBeNull();
    });

    it("keeps 'Resume Sync' and the Force button after a force-clear (history preserved, #1318)", async () => {
      // Force Full Sync no longer deletes the run history — the backend preserves
      // it so the Last-sync display stays truthful. Both stats reads (mount +
      // post-clear) therefore return the SAME resume situation.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        roms: 42,
        last_sync: null,
        last_attempt: { finished_at: "2026-06-01T17:48:00", status: "interrupted" },
      });
      vi.mocked(backend.clearSyncCache).mockResolvedValue({ success: true, message: "Cleared" });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Resume situation: label reads "Resume Sync", Force button shown.
      expect(buttonByExactText(container, "Resume Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Force Full Sync")).not.toBeNull();

      // Press Force Full Sync → clearSyncCache succeeds → the stats refresh reads
      // the SAME preserved history.
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Force Full Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      await flushAsync();

      // History preserved: the label stays "Resume Sync" and the Force button
      // stays visible (idempotent — the stamps are already cleared).
      expect(buttonByExactText(container, "Resume Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
      expect(buttonByExactText(container, "Force Full Sync")).not.toBeNull();
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

    it("collection enabled but zero add/remove delta: Dismiss only, no Apply, 'up to date' (#1147)", async () => {
      // collection_diff.has_changes is True whenever any collection is enabled
      // (the backend pins it via an `or current` term so first-sync still
      // applies), yet an empty added/removed delta means there is nothing to
      // apply. The gate must key off the real add/remove delta so the button
      // matches the "Everything is up to date." description.
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 0,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          collection_diff: { has_changes: true, added: [], removed: [] },
        },
        new_names: [],
        changed_names: [],
        preview_id: "preview-1147",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(buttonByExactText(container, "Dismiss")).not.toBeNull();
      expect(buttonByExactText(container, "Apply Sync")).toBeNull();
      const descs = Array.from(container.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
    });

    it("cover-only preview proceeds to Apply with the cover wording (#1386 flow gap)", async () => {
      // Empty shortcut delta but pending cover refreshes: the flow must offer
      // the same Apply/Cancel confirm as a non-empty preview — the cover
      // refresh pass only runs inside the apply, so a "no changes" dead end
      // strands the stale tiles forever (hardware-reproduced).
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 4,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          cover_refresh_count: 2,
        },
        new_names: [],
        changed_names: [],
        preview_id: "preview-covers",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(buttonByExactText(container, "Apply Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Cancel")).not.toBeNull();
      expect(buttonByExactText(container, "Dismiss")).toBeNull();
      expect(container.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "No shortcut changes — 2 cover updates.",
      );
      // Same interaction shape as a non-empty preview: the run description rows render.
      expect(container.querySelector('[data-testid="sync-estimate"]')).not.toBeNull();
      expect(container.textContent).toContain("Progress is saved");
      // Apply drives the same delta callable the shortcut path uses.
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Apply Sync")!);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.syncApplyDelta)).toHaveBeenCalledWith("preview-covers");
    });

    it("unstamped-platform preview proceeds to Apply with the re-stamp wording (#1416)", async () => {
      // Empty shortcut delta but a platform lacking a completion stamp: the
      // apply must still run once to re-stamp it and heal the lingering
      // "interrupted" status, so the flow offers Apply/Cancel rather than the
      // "no changes" dead end.
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 4,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          cover_refresh_count: 0,
          restamp_platform_count: 1,
        },
        new_names: [],
        changed_names: [],
        preview_id: "preview-restamp",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(buttonByExactText(container, "Apply Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Cancel")).not.toBeNull();
      expect(buttonByExactText(container, "Dismiss")).toBeNull();
      expect(container.querySelector('[data-testid="sync-changes"]')?.textContent).toBe(
        "No changes — finishing a previous sync.",
      );
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Apply Sync")!);
        await Promise.resolve();
      });
      expect(vi.mocked(backend.syncApplyDelta)).toHaveBeenCalledWith("preview-restamp");
    });

    it("zero changes with explicit zero covers keeps the exact Dismiss-only shape (regression pin)", async () => {
      vi.mocked(backend.syncPreview).mockResolvedValue({
        success: true,
        summary: {
          new_count: 0,
          changed_count: 0,
          unchanged_count: 4,
          remove_count: 0,
          disabled_platform_remove_count: 0,
          cover_refresh_count: 0,
          sync_platform_count: 5,
        },
        new_names: [],
        changed_names: [],
        preview_id: "preview-zero-covers",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // Byte-identical to today's empty-delta preview: unchanged message,
      // Dismiss only, and none of the run-description rows.
      const descs = Array.from(container.querySelectorAll('[data-testid="field-desc"]')).map((n) => n.textContent);
      expect(descs).toContain("Everything is up to date.");
      expect(buttonByExactText(container, "Dismiss")).not.toBeNull();
      expect(buttonByExactText(container, "Apply Sync")).toBeNull();
      expect(container.querySelector('[data-testid="sync-scope"]')).toBeNull();
      expect(container.querySelector('[data-testid="sync-estimate"]')).toBeNull();
      expect(container.textContent).not.toContain("Progress is saved");
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

    it("aborts to idle (no phantom 'Apply Sync') when a cancel landed during syncPreview (#1202 RC-CANCEL-PREVIEW)", async () => {
      // syncPreview resolves SUCCESS with changes (normally → Apply Sync), but a
      // Cancel landed in-flight so isCancelRequested() is true at the
      // post-resolve re-check. The preview must NOT show; the UI returns to idle
      // and the stale flag is cleared so the next sync isn't pre-cancelled.
      vi.mocked(syncManager.isCancelRequested).mockReturnValue(true);
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
        preview_id: "preview-cancel",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // No phantom preview — Apply Sync absent, back to idle (Sync Library back).
      expect(buttonByExactText(container, "Apply Sync")).toBeNull();
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      // Non-vacuous: the abort surfaced the "Sync cancelled" status — only the
      // RC-CANCEL-PREVIEW branch produces idle + that status after a SUCCESS
      // preview, and the stale cancel flag was cleared for the next run.
      expect(fieldLabels(container)).toContain("Sync cancelled");
      expect(vi.mocked(syncManager.resetSyncCancel)).toHaveBeenCalled();
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

    it("reconciles stale shortcuts BEFORE startSync (skipPreview path) (#1046)", async () => {
      const order: string[] = [];
      vi.mocked(syncManager.reconcileStaleShortcuts).mockImplementation(async () => {
        order.push("reconcile");
      });
      vi.mocked(backend.startSync).mockImplementation(async () => {
        order.push("startSync");
        return { success: true, message: "" };
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
      // Reconcile must unbind dead bindings before the work queue is built.
      expect(order).toEqual(["reconcile", "startSync"]);
    });

    it("awaits reconcile to COMPLETION before startSync so the delta classify trusts a reconciled registry (#1383)", async () => {
      const order: string[] = [];
      // The delta-restricted apply classifies against the bound registry and skips
      // content-unchanged shortcuts. A shortcut the user deleted in Steam leaves a
      // dead binding that would be skipped forever unless reconcile unbinds it
      // FIRST — so reconcile must COMPLETE before the backend fetch/classify starts.
      // The marker is pushed after internal awaits, so a non-awaited reconcile would
      // let startSync run first and fail this ordering (proving completion, not just
      // start-order).
      vi.mocked(syncManager.reconcileStaleShortcuts).mockImplementation(async () => {
        await Promise.resolve();
        await Promise.resolve();
        order.push("reconcile-done");
      });
      vi.mocked(backend.startSync).mockImplementation(async () => {
        order.push("startSync");
        return { success: true, message: "" };
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const toggle = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      fireEvent.click(toggle!);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await flushAsync();
      });
      expect(order).toEqual(["reconcile-done", "startSync"]);
    });

    it("resets the stale cancel flag BEFORE reconcile/startSync (#1198/#1202)", async () => {
      const order: string[] = [];
      vi.mocked(syncManager.resetSyncCancel).mockImplementation(() => {
        order.push("resetSyncCancel");
      });
      vi.mocked(syncManager.reconcileStaleShortcuts).mockImplementation(async () => {
        order.push("reconcile");
      });
      vi.mocked(backend.startSync).mockImplementation(async () => {
        order.push("startSync");
        return { success: true, message: "" };
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
      // The stale cancel flag must be cleared before the run starts, so a fresh
      // sync never begins pre-cancelled.
      expect(order).toEqual(["resetSyncCancel", "reconcile", "startSync"]);
    });

    it("reconciles stale shortcuts BEFORE syncPreview (preview path) (#1046)", async () => {
      const order: string[] = [];
      vi.mocked(syncManager.reconcileStaleShortcuts).mockImplementation(async () => {
        order.push("reconcile");
      });
      vi.mocked(backend.syncPreview).mockImplementation(async () => {
        order.push("syncPreview");
        return {
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
          preview_id: "p-order",
        };
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(order).toEqual(["reconcile", "syncPreview"]);
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
    it("clicking 'Cancel Sync' requests cancel, cancels the active run, and disarms to 'Cancelling…' (#1202)", async () => {
      // Pre-arm an in-flight sync via the backend-authoritative mount query. The
      // run id rides on the sync_progress store (runId), which handleCancel
      // reads to scope the cancel — no separate frontend run-id mirror (#1202).
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
        runId: "run-x",
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
      // Scoped to the active run id sourced from the sync_progress store (#1202).
      expect(vi.mocked(backend.cancelSync)).toHaveBeenCalledWith("run-x");
      // RC-B: disarmed into a disabled "Cancelling…" — NOT re-armed to idle.
      const cancelling = buttonByExactText(container, "Cancelling…");
      expect(cancelling).not.toBeNull();
      expect(cancelling!.disabled).toBe(true);
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
    });

    it("cancel in the pre-progress window (no run id yet) cancels unconditionally (#1202)", async () => {
      // The "Fetching library…" window: the backend hasn't stamped a run id into
      // sync_progress yet, so the store's runId is empty. handleCancel must send
      // "" → the backend's unconditional cancel path, NOT a stale id.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "fetching",
        message: "Fetching library...",
        // no runId — pre-progress window
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
      // Non-vacuous: "" proves the empty/absent run id maps to the unconditional
      // cancel. A regression that fabricated a stale id would send non-empty.
      expect(vi.mocked(backend.cancelSync)).toHaveBeenCalledWith("");
    });

    it("stays disarmed ('Cancelling…') during the drain — no status flash — then re-arms on the terminal CANCELLED stage (#1202 RC-B)", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
        runId: "run-x",
      });
      vi.mocked(backend.cancelSync).mockResolvedValue({
        success: true,
        message: "cancelled-msg",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Cancel Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      // Drain: disarmed to "Cancelling…", Sync Library NOT back, and the
      // cancelSync result message is NOT surfaced — no instant-finish flash.
      expect(buttonByExactText(container, "Cancelling…")).not.toBeNull();
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
      expect(fieldLabels(container)).not.toContain("cancelled-msg");

      // Terminal CANCELLED sync_progress lands via the module store → re-arm.
      await act(async () => {
        setSyncProgress({ running: false, stage: "cancelled", message: "Sync cancelled" });
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      expect(buttonByExactText(container, "Cancelling…")).toBeNull();
      // The terminal stage's message is surfaced (not the cancelSync result's).
      expect(fieldLabels(container)).toContain("Sync cancelled");
    });

    it("the terminal cancel message stays past 8s and auto-clears after 15s", async () => {
      vi.useFakeTimers({
        toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"],
      });
      try {
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "applying",
          message: "Working",
          runId: "run-x",
        });
        vi.mocked(backend.cancelSync).mockResolvedValue({
          success: true,
          message: "cancelled-msg",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await act(async () => {
          await Promise.resolve();
          await Promise.resolve();
        });
        await act(async () => {
          fireEvent.click(buttonByExactText(container, "Cancel Sync")!);
          await Promise.resolve();
          await Promise.resolve();
        });
        // Terminal stage surfaces the message + arms the 15s auto-clear.
        await act(async () => {
          setSyncProgress({ running: false, stage: "cancelled", message: "Sync cancelled" });
          await Promise.resolve();
        });
        expect(fieldLabels(container)).toContain("Sync cancelled");
        // Past the OLD 8s threshold: still visible (the "stays longer" change).
        await act(async () => {
          await vi.advanceTimersByTimeAsync(8000);
        });
        expect(fieldLabels(container)).toContain("Sync cancelled");
        // Crossing 15s total: now cleared.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(7001);
        });
        expect(fieldLabels(container)).not.toContain("Sync cancelled");
      } finally {
        vi.useRealTimers();
      }
    });

    it("cancelSync rejection: re-arms and surfaces 'Failed to cancel sync' (no terminal will arrive)", async () => {
      // The cancel call itself fails — no backend terminal stage will follow, so
      // handleCancel re-arms (out of "Cancelling…") and surfaces the failure so
      // the user can retry.
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
        runId: "run-x",
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
      // Status field un-gated and shows the failure message; re-armed to idle.
      expect(fieldLabels(container)).toContain("Failed to cancel sync");
      expect(buttonByExactText(container, "Cancel Sync")).toBeNull();
      expect(buttonByExactText(container, "Cancelling…")).toBeNull();
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

    it("clearSyncCache rejection surfaces 'Failed to clear sync cache' (neutral, not green)", async () => {
      // Non-vacuous catch coverage: the rejection routes through the catch's
      // showTransientStatus, so both the message AND its neutral tone (no green
      // override — the clear failed) must be observable on the status element.
      vi.mocked(backend.getSyncStats).mockResolvedValue({
        ...defaultStats(),
        last_sync: new Date(Date.now() - 30_000).toISOString(),
      });
      vi.mocked(backend.clearSyncCache).mockRejectedValue(new Error("io"));
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Force Full Sync")!);
        await Promise.resolve();
        await Promise.resolve();
      });
      const el = container.querySelector('[data-testid="sync-status"]') as HTMLElement | null;
      expect(el?.textContent).toBe("Failed to clear sync cache");
      expect(el?.style.color).toBe(""); // neutral tone — no success green
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
      const fixBtn = Array.from(container.querySelectorAll('[data-testid="dialog-button"]')).find(
        (b) => b.textContent === "Fix",
      ) as HTMLButtonElement | undefined;
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
      const fixBtn = Array.from(container.querySelectorAll('[data-testid="dialog-button"]')).find(
        (b) => b.textContent === "Fix",
      ) as HTMLButtonElement | undefined;
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
      const fixBtn = Array.from(container.querySelectorAll('[data-testid="dialog-button"]')).find(
        (b) => b.textContent === "Fix",
      ) as HTMLButtonElement | undefined;
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
      const fixBtn = Array.from(container.querySelectorAll('[data-testid="dialog-button"]')).find(
        (b) => b.textContent === "Fix",
      ) as HTMLButtonElement | undefined;
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

    it("clicking System invokes onNavigate('system')", async () => {
      const onNavigate = vi.fn();
      const { container } = render(<MainPage onNavigate={onNavigate} />);
      await flushAsync();
      fireEvent.click(buttonByExactText(container, "System")!);
      expect(onNavigate).toHaveBeenCalledWith("system");
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
            resumable: false,
          },
        ]);
        const onNavigate = vi.fn();
        const { container } = render(<MainPage onNavigate={onNavigate} />);
        await act(async () => {
          await Promise.resolve();
          await Promise.resolve();
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
    // The downloads section reads the store through useDownloads(), so seeding
    // the store before render is enough. Fake timers stay for MainPage's other
    // timed effects.
    beforeEach(() => {
      vi.useFakeTimers({
        toFake: ["setInterval", "clearInterval", "setTimeout", "clearTimeout"],
      });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    async function renderSection(): Promise<HTMLElement> {
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      return container;
    }

    it("hidden when no downloads in the store", async () => {
      const container = await renderSection();
      // The downloads block is heading-less; its "View All" button is the
      // presence anchor.
      expect(buttonByExactText(container, "View All")).toBeNull();
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
          resumable: false,
        },
      ]);
      const container = await renderSection();
      expect(buttonByExactText(container, "View All")).not.toBeNull();
      // The rom name lands in the full-width caption (not clipped in a Field
      // label column) — see the #751 ProgressBarWithInfo fix.
      expect(container.querySelector('[data-testid="dl-caption"]')?.textContent).toBe("Active");
    });

    it("shows '+N more downloading' when more than 2 active downloads", async () => {
      setDownloads([
        {
          rom_id: 1,
          rom_name: "A",
          platform_name: "X",
          file_name: "a",
          status: "downloading",
          progress: 0,
          bytes_downloaded: 0,
          total_bytes: 1024,
          resumable: false,
        },
        {
          rom_id: 2,
          rom_name: "B",
          platform_name: "X",
          file_name: "b",
          status: "downloading",
          progress: 0,
          bytes_downloaded: 0,
          total_bytes: 1024,
          resumable: false,
        },
        {
          rom_id: 3,
          rom_name: "C",
          platform_name: "X",
          file_name: "c",
          status: "downloading",
          progress: 0,
          bytes_downloaded: 0,
          total_bytes: 1024,
          resumable: false,
        },
      ]);
      const container = await renderSection();
      expect(container.textContent).toContain("+1 more downloading");
    });

    it("shows 'N completed' count for finished items", async () => {
      setDownloads([
        {
          rom_id: 1,
          rom_name: "A",
          platform_name: "X",
          file_name: "a",
          status: "completed",
          progress: 100,
          bytes_downloaded: 100,
          total_bytes: 100,
          resumable: false,
        },
        {
          rom_id: 2,
          rom_name: "B",
          platform_name: "X",
          file_name: "b",
          status: "failed",
          progress: 0,
          bytes_downloaded: 0,
          total_bytes: 100,
          resumable: false,
        },
      ]);
      const container = await renderSection();
      // Self-describing label — the heading-less downloads block gives the row
      // no context of its own.
      expect(container.textContent).toContain("2 downloads completed");
      // The block ends in a rule so it doesn't run into the menu buttons.
      expect(container.querySelectorAll('[data-testid="block-separator"]')).toHaveLength(3);
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
          resumable: false,
        },
      ]);
      const container = await renderSection();
      const progress = container.querySelector('[data-testid="progress-progress"]');
      expect(progress?.textContent).toBe("25");
      const indet = container.querySelector('[data-testid="progress-indeterminate"]');
      expect(indet?.textContent).toBe("false");
      expect(container.querySelector('[data-testid="dl-caption"]')?.textContent).toBe("P");
      expect(container.querySelector('[data-testid="dl-bytes"]')?.textContent).toBe("256 B / 1.0 KB");
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
          resumable: false,
        },
      ]);
      const container = await renderSection();
      const indet = container.querySelector('[data-testid="progress-indeterminate"]');
      expect(indet?.textContent).toBe("true");
      expect(container.querySelector('[data-testid="dl-caption"]')?.textContent).toBe("P");
      expect(container.querySelector('[data-testid="dl-bytes"]')?.textContent).toBe("0 B");
    });
  });

  // ===========================================================================
  // N. Subscription cleanup
  // ===========================================================================
  describe("subscription cleanup", () => {
    it("onSyncProgressChange subscription is removed on unmount", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
      });
      const { container, unmount } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // In-flight UI is up — the subscription is live.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();
      unmount();
      // After unmount, a store update must not throw (listener was removed)
      // and must not resurrect the unmounted tree.
      act(() => {
        setSyncProgress({ running: false, stage: "done", message: "Sync complete" });
      });
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
    });
  });

  // ===========================================================================
  // N2. Backend-authoritative progress — store subscription drives the UI
  // ===========================================================================
  describe("store-driven sync UI (#751)", () => {
    it("terminal stage tears down the in-flight UI and surfaces the final message", async () => {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 1,
        totalSteps: 2,
        message: "Working",
      });
      const statsAfter: SyncStats = {
        ...defaultStats(),
        roms: 7,
        last_sync: new Date().toISOString(),
      };
      vi.mocked(backend.getSyncStats).mockResolvedValue(statsAfter);
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // In-flight initially.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();

      // A terminal sync_progress lands via the module store.
      await act(async () => {
        setSyncProgress({ running: false, stage: "done", message: "Sync complete: 7 games" });
        await Promise.resolve();
        await Promise.resolve();
      });

      // Torn down: Sync Library button back, final message surfaced, stats refreshed.
      expect(buttonByExactText(container, "Sync Library")).not.toBeNull();
      expect(buttonByExactText(container, "Cancel Sync")).toBeNull();
      expect(fieldLabels(container)).toContain("Sync complete: 7 games");
      expect(vi.mocked(backend.getSyncStats)).toHaveBeenCalledTimes(2);
    });

    // The terminal stage rewrites last_sync, last_attempt and the counts, and it
    // re-measures the heap the run consumed — so both re-reads below are issued
    // BECAUSE the facts changed, and neither may join a read issued while the run
    // was still going. Open the panel in a run's last seconds and the mount reads
    // are exactly such reads. Nothing re-reads afterwards for a completed run —
    // the poll interval needs `syncing || lastRunPaused` and stops on the same
    // tick — so a joined pre-run answer is the last one the panel ever shows.
    it("re-reads the stats itself at the terminal stage rather than joining the read still open", async () => {
      const mountRead = deferred<SyncStats>();
      const terminalRead = deferred<SyncStats>();
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
      });
      vi.mocked(backend.getSyncStats).mockReturnValueOnce(mountRead.promise).mockReturnValueOnce(terminalRead.promise);

      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // Still open: the mount read has not answered, so the library line is blank.
      expect(container.textContent).not.toContain("games");

      await act(async () => {
        setSyncProgress({ running: false, stage: "done", message: "Sync complete" });
        await Promise.resolve();
      });
      // Two reads, not one — a joined read would answer for the run that just ended.
      expect(vi.mocked(backend.getSyncStats)).toHaveBeenCalledTimes(2);

      terminalRead.resolve({ ...defaultStats(), roms: 7, platforms: 1, last_sync: new Date().toISOString() });
      await flushAsync();
      // The overtaken pre-run answer lands last and must change nothing.
      mountRead.resolve({ ...defaultStats(), roms: 3, platforms: 1 });
      await flushAsync();

      expect(container.textContent).toContain("7 games");
      expect(container.textContent).not.toContain("3 games");
    });

    it("re-reads the heap itself at the terminal stage rather than joining the read still open", async () => {
      const liveBudget = (rssKb: number): SessionBudgetStatus => ({
        success: true,
        rss_kb: rssKb,
        warn_kb: 1_800_000,
        ceiling_kb: 2_200_000,
        cliff_kb: 2_450_000,
        memory_delta_kb: null,
        resume_ready: null,
        run_done_items: null,
        run_total_items: null,
      });
      const mountRead = deferred<SessionBudgetStatus>();
      const terminalRead = deferred<SessionBudgetStatus>();
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
      });
      vi.mocked(backend.getSessionBudgetStatus)
        .mockReturnValueOnce(mountRead.promise)
        .mockReturnValueOnce(terminalRead.promise);

      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();

      await act(async () => {
        setSyncProgress({ running: false, stage: "done", message: "Sync complete" });
        await Promise.resolve();
      });
      expect(vi.mocked(backend.getSessionBudgetStatus)).toHaveBeenCalledTimes(2);

      terminalRead.resolve(liveBudget(500_000));
      await flushAsync();
      mountRead.resolve(liveBudget(1_300_000));
      await flushAsync();

      const memoryRow = container.querySelector('[data-testid="steam-memory"]')?.textContent ?? "";
      expect(memoryRow).toContain("0.5 GB");
      expect(memoryRow).not.toContain("1.3 GB");
    });

    it("a bare running:false (no terminal stage) does NOT tear down the in-flight UI", async () => {
      // Reproduces the #751 teardown race: a non-terminal running:false must
      // not collapse the syncing UI (it would right after startSync, before
      // events land).
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        message: "Working",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();

      await act(async () => {
        setSyncProgress({ running: false, stage: "", message: "" });
        await Promise.resolve();
      });

      // Still in-flight — only a terminal stage tears down.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
    });

    it("Sync Library button is disabled optimistically on click before the first event", async () => {
      // skipPreview path: startSync resolves but no progress event has landed.
      // The button must be gone (replaced by Cancel Sync) immediately.
      let resolveStart: (v: { success: boolean; message: string }) => void = () => {};
      vi.mocked(backend.startSync).mockImplementation(
        () =>
          new Promise((res) => {
            resolveStart = res;
          }),
      );
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      const toggle = container.querySelector('[data-testid="toggle-input"]') as HTMLInputElement | null;
      fireEvent.click(toggle!);
      await flushAsync();

      await act(async () => {
        fireEvent.click(buttonByExactText(container, "Sync Library")!);
        await Promise.resolve();
      });
      // Optimistic: in-flight UI shown before startSync even resolves.
      expect(buttonByExactText(container, "Sync Library")).toBeNull();
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();

      await act(async () => {
        resolveStart({ success: true, message: "" });
        await Promise.resolve();
      });
      // Still in-flight after the resolve — store subscription owns teardown.
      expect(buttonByExactText(container, "Cancel Sync")).not.toBeNull();
    });
  });

  // ===========================================================================
  // N3. Sync-complete status styling — green + smaller + longer visibility
  // ===========================================================================
  describe("sync-complete status styling", () => {
    const syncStatus = (c: HTMLElement) => c.querySelector('[data-testid="sync-status"]') as HTMLElement | null;

    // Mount into a live run so a terminal sync_progress has an in-flight UI to
    // tear down, then land the terminal frame through the module store exactly
    // as a backend event would.
    async function mountThenTerminal(stage: "done" | "cancelled" | "error", message: string): Promise<HTMLElement> {
      vi.mocked(backend.getSyncStatus).mockResolvedValue({
        running: true,
        stage: "applying",
        step: 1,
        totalSteps: 1,
        message: "Working",
      });
      const { container } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      await act(async () => {
        setSyncProgress({ running: false, stage, message });
        await Promise.resolve();
        await Promise.resolve();
      });
      return container;
    }

    it("renders a clean sync finish smaller and green", async () => {
      const c = await mountThenTerminal("done", "Sync complete: 7 games");
      const el = syncStatus(c);
      expect(el?.textContent).toBe("Sync complete: 7 games");
      // Non-vacuous: both the affirmative green AND the small caption size sit on
      // the element — dropping the success tone (or the size) fails this.
      expect(el?.style.color).toBe("#59bf40");
      expect(el?.style.fontSize).toBe("12px");
    });

    it("renders a cancelled finish smaller but NOT green", async () => {
      const c = await mountThenTerminal("cancelled", "Sync cancelled");
      const el = syncStatus(c);
      expect(el?.textContent).toBe("Sync cancelled");
      // Neutral tone: no colour override, so the panel's default text colour wins.
      expect(el?.style.color).toBe("");
      expect(el?.style.fontSize).toBe("12px");
    });

    it("renders an errored finish smaller but NOT green", async () => {
      const c = await mountThenTerminal("error", "Sync failed: server unreachable");
      const el = syncStatus(c);
      expect(el?.textContent).toBe("Sync failed: server unreachable");
      expect(el?.style.color).toBe("");
      expect(el?.style.fontSize).toBe("12px");
    });

    it("keeps the finish status visible past 8s and clears it only after 15s", async () => {
      vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
      try {
        vi.mocked(backend.getSyncStatus).mockResolvedValue({
          running: true,
          stage: "applying",
          step: 1,
          totalSteps: 1,
          message: "Working",
        });
        const { container } = render(<MainPage onNavigate={vi.fn()} />);
        await flushAsync();
        await act(async () => {
          setSyncProgress({ running: false, stage: "done", message: "Sync complete: 7 games" });
          await Promise.resolve();
          await Promise.resolve();
        });
        expect(syncStatus(container)?.textContent).toBe("Sync complete: 7 games");

        // Past the OLD 8s auto-clear: still on screen — this guards the "stays
        // visible a bit longer" ask (the pre-change 8000ms would have cleared it).
        await act(async () => {
          await vi.advanceTimersByTimeAsync(9000);
        });
        expect(syncStatus(container)).not.toBeNull();

        // Crossing 15s total: the auto-clear fires and the line disappears.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(6001);
        });
        expect(syncStatus(container)).toBeNull();
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

  // ===========================================================================
  // P. RetroDECK config-health banner
  // ===========================================================================
  describe("RetroDECK config-health banner", () => {
    it("shows the unreadable banner when status is 'unreadable'", async () => {
      vi.mocked(backend.getRetroDeckStatus).mockResolvedValue({
        status: "unreadable",
        config_path: "/cfg/retrodeck.json",
        resolved_home: "/home/deck/retrodeck",
      });
      const { findByText } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(await findByText("RetroDECK configuration unreadable")).toBeInTheDocument();
      expect(await findByText(/syncs and downloads may target the wrong location/)).toBeInTheDocument();
      // Probed config path is surfaced.
      expect(await findByText(/\/cfg\/retrodeck\.json/)).toBeInTheDocument();
    });

    it("shows the root-missing banner when status is 'root_missing'", async () => {
      vi.mocked(backend.getRetroDeckStatus).mockResolvedValue({
        status: "root_missing",
        config_path: "/cfg/retrodeck.json",
        resolved_home: "/run/media/sdcard/retrodeck",
      });
      const { findByText } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(await findByText("RetroDECK library not found")).toBeInTheDocument();
      expect(await findByText(/make sure the card is inserted/)).toBeInTheDocument();
      // Resolved home is surfaced.
      expect(await findByText(/\/run\/media\/sdcard\/retrodeck/)).toBeInTheDocument();
    });

    it("renders no banner when status is 'ok'", async () => {
      vi.mocked(backend.getRetroDeckStatus).mockResolvedValue({
        status: "ok",
        config_path: "/cfg/retrodeck.json",
        resolved_home: "/home/deck/retrodeck",
      });
      const { queryByText } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(queryByText("RetroDECK configuration unreadable")).toBeNull();
      expect(queryByText("RetroDECK library not found")).toBeNull();
    });

    it("renders no banner when status is 'absent' (fresh-install case)", async () => {
      vi.mocked(backend.getRetroDeckStatus).mockResolvedValue({
        status: "absent",
        config_path: "/cfg/retrodeck.json",
        resolved_home: "/home/deck/retrodeck",
      });
      const { queryByText } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      expect(queryByText("RetroDECK configuration unreadable")).toBeNull();
      expect(queryByText("RetroDECK library not found")).toBeNull();
    });

    it("leaves the banner cleared when getRetroDeckStatus rejects", async () => {
      vi.mocked(backend.getRetroDeckStatus).mockRejectedValue(new Error("boom"));
      const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
      const { queryByText } = render(<MainPage onNavigate={vi.fn()} />);
      await flushAsync();
      // No banner, and the rejection is logged (non-vacuous .catch assertion).
      expect(queryByText("RetroDECK configuration unreadable")).toBeNull();
      expect(queryByText("RetroDECK library not found")).toBeNull();
      expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("Failed to query RetroDECK status"));
    });
  });
});
