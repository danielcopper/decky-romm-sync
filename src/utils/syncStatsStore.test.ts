// The store's contract is the two properties it exists for:
//   - answers apply in ISSUE order, never in arrival order (the defect: an
//     older poll landing after Force Full Sync's post-clear read overwrote it);
//   - concurrent refreshes collapse into one request, EXCEPT the after-change
//     one, which must issue its own so it cannot be handed pre-change data.
// Plus the `useSyncExternalStore` obligation every store here carries: a getter
// that hands back a fresh object per call renders forever. Those assertions are
// `toBe`, not `toEqual`, on purpose — `toEqual` cannot see the difference.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { getSessionBudgetStatus, getSyncStats } from "../api/backend";
import type { SessionBudgetStatus, SyncStats } from "../types";
import {
  getSessionBudgetSnapshot,
  getSyncStatsSnapshot,
  onSyncStatsStoreChange,
  refreshSessionBudget,
  refreshSessionBudgetAfterChange,
  refreshSyncStats,
  refreshSyncStatsAfterChange,
  resetSyncStatsStoreForTests,
  useSessionBudget,
  useSyncStats,
} from "./syncStatsStore";

vi.mock("../api/backend", () => ({
  getSyncStats: vi.fn(),
  getSessionBudgetStatus: vi.fn(),
  logError: vi.fn(),
}));

function stats(overrides: Partial<SyncStats> = {}): SyncStats {
  return {
    last_sync: "2026-08-01T10:00:00",
    platforms: 3,
    roms: 42,
    total_shortcuts: 42,
    ...overrides,
  };
}

function budget(rssKb: number | null): SessionBudgetStatus {
  return {
    success: true,
    rss_kb: rssKb,
    warn_kb: 1_800_000,
    ceiling_kb: 2_200_000,
    cliff_kb: 2_450_000,
    memory_delta_kb: null,
    resume_ready: null,
    run_done_items: null,
    run_total_items: null,
  };
}

/** A promise plus the handle to settle it, so a test can decide the order two
 *  reads resolve in independently of the order they were issued. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (e: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("syncStatsStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    resetSyncStatsStoreForTests();
  });

  describe("issue ordering", () => {
    it("keeps the later-issued answer when an earlier read resolves last", async () => {
      // The reachable case: the paused poll has a stats read open when the user
      // presses Force Full Sync. The post-clear read is issued second and
      // resolves first; the poll's pre-clear answer lands after it and must not
      // be installed.
      const poll = deferred<SyncStats>();
      const afterClear = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValueOnce(poll.promise).mockReturnValueOnce(afterClear.promise);

      const first = refreshSyncStats();
      const second = refreshSyncStatsAfterChange();

      afterClear.resolve(stats({ roms: 0, last_sync: null }));
      await second;
      expect(getSyncStatsSnapshot()?.roms).toBe(0);

      poll.resolve(stats({ roms: 42 }));
      await first;
      expect(getSyncStatsSnapshot()?.roms).toBe(0);
      expect(getSyncStatsSnapshot()?.last_sync).toBeNull();
    });

    it("installs the later-issued answer when it resolves last too", async () => {
      // The same two reads in the other arrival order — the ordering must not
      // depend on which one won the race.
      const poll = deferred<SyncStats>();
      const afterClear = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValueOnce(poll.promise).mockReturnValueOnce(afterClear.promise);

      const first = refreshSyncStats();
      const second = refreshSyncStatsAfterChange();

      poll.resolve(stats({ roms: 42 }));
      await first;
      expect(getSyncStatsSnapshot()?.roms).toBe(42);

      afterClear.resolve(stats({ roms: 0, last_sync: null }));
      await second;
      expect(getSyncStatsSnapshot()?.roms).toBe(0);
    });

    it("lets an older read answer when the newer one failed", async () => {
      // A rejection installs nothing and moves nothing, so the older read still
      // open is free to answer — the display keeps the last SUCCESSFUL answer
      // rather than nothing at all.
      const older = deferred<SyncStats>();
      const newer = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

      const first = refreshSyncStats();
      const second = refreshSyncStatsAfterChange();

      newer.reject(new Error("boom"));
      await second;
      expect(getSyncStatsSnapshot()).toBeNull();

      older.resolve(stats({ roms: 42 }));
      await first;
      expect(getSyncStatsSnapshot()?.roms).toBe(42);
    });

    it("logs a failed read and leaves the last answer standing", async () => {
      vi.mocked(getSyncStats).mockResolvedValueOnce(stats({ roms: 42 }));
      await refreshSyncStats();

      vi.mocked(getSyncStats).mockRejectedValueOnce(new Error("boom"));
      await refreshSyncStats();

      const { logError } = await import("../api/backend");
      expect(vi.mocked(logError)).toHaveBeenCalledWith(expect.stringContaining("Failed to load sync stats"));
      expect(getSyncStatsSnapshot()?.roms).toBe(42);
    });
  });

  describe("request sharing", () => {
    it("collapses two concurrent refreshes into one callable call", async () => {
      const open = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValue(open.promise);

      const a = refreshSyncStats();
      const b = refreshSyncStats();
      expect(vi.mocked(getSyncStats)).toHaveBeenCalledTimes(1);

      open.resolve(stats({ roms: 7 }));
      await Promise.all([a, b]);
      expect(getSyncStatsSnapshot()?.roms).toBe(7);
    });

    it("reads again once the shared request has settled", async () => {
      vi.mocked(getSyncStats).mockResolvedValue(stats());
      await refreshSyncStats();
      await refreshSyncStats();
      expect(vi.mocked(getSyncStats)).toHaveBeenCalledTimes(2);
    });

    it("shares the session-budget read the same way", async () => {
      const open = deferred<SessionBudgetStatus>();
      vi.mocked(getSessionBudgetStatus).mockReturnValue(open.promise);

      const a = refreshSessionBudget();
      const b = refreshSessionBudget();
      expect(vi.mocked(getSessionBudgetStatus)).toHaveBeenCalledTimes(1);

      open.resolve(budget(600_000));
      await Promise.all([a, b]);
      expect(getSessionBudgetSnapshot()?.rss_kb).toBe(600_000);
    });

    it("issues its own read after a change even while one is open, and that answer wins", async () => {
      const preClear = deferred<SyncStats>();
      const postClear = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValueOnce(preClear.promise).mockReturnValueOnce(postClear.promise);

      const poll = refreshSyncStats();
      const forced = refreshSyncStatsAfterChange();
      // Two calls, not one: joining the pre-clear read would hand the post-clear
      // caller the pre-clear counts.
      expect(vi.mocked(getSyncStats)).toHaveBeenCalledTimes(2);

      preClear.resolve(stats({ roms: 42 }));
      postClear.resolve(stats({ roms: 0, last_sync: null }));
      await Promise.all([poll, forced]);
      expect(getSyncStatsSnapshot()?.roms).toBe(0);
    });

    it("issues its own budget read after a change even while one is open, and that answer wins", async () => {
      // The budget lane needs the same twin: the terminal stage re-reads BECAUSE
      // the run ended, and the 5s poll it would otherwise join took its reading
      // while the run was still consuming memory.
      const midRun = deferred<SessionBudgetStatus>();
      const runEnded = deferred<SessionBudgetStatus>();
      vi.mocked(getSessionBudgetStatus).mockReturnValueOnce(midRun.promise).mockReturnValueOnce(runEnded.promise);

      const poll = refreshSessionBudget();
      const terminal = refreshSessionBudgetAfterChange();
      expect(vi.mocked(getSessionBudgetStatus)).toHaveBeenCalledTimes(2);

      runEnded.resolve(budget(500_000));
      midRun.resolve(budget(1_300_000));
      await Promise.all([poll, terminal]);
      expect(getSessionBudgetSnapshot()?.rss_kb).toBe(500_000);
    });

    it("hands a later refresh the post-change read to join, not the superseded one", async () => {
      const preClear = deferred<SyncStats>();
      const postClear = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValueOnce(preClear.promise).mockReturnValueOnce(postClear.promise);

      const poll = refreshSyncStats();
      const forced = refreshSyncStatsAfterChange();
      // The post-change read replaced the pre-change one as the joinable open
      // request, so this joins it instead of opening a third.
      const joiner = refreshSyncStats();
      expect(vi.mocked(getSyncStats)).toHaveBeenCalledTimes(2);

      preClear.resolve(stats({ roms: 42 }));
      postClear.resolve(stats({ roms: 0 }));
      await Promise.all([poll, forced, joiner]);
      expect(getSyncStatsSnapshot()?.roms).toBe(0);
    });
  });

  describe("snapshot identity", () => {
    it("returns the same reference while nothing changes", async () => {
      vi.mocked(getSyncStats).mockResolvedValue(stats());
      vi.mocked(getSessionBudgetStatus).mockResolvedValue(budget(600_000));
      await refreshSyncStats();
      await refreshSessionBudget();

      expect(getSyncStatsSnapshot()).toBe(getSyncStatsSnapshot());
      expect(getSessionBudgetSnapshot()).toBe(getSessionBudgetSnapshot());
    });

    it("returns a different reference after a real change", async () => {
      vi.mocked(getSyncStats).mockResolvedValue(stats({ roms: 1 }));
      await refreshSyncStats();
      const before = getSyncStatsSnapshot();

      vi.mocked(getSyncStats).mockResolvedValue(stats({ roms: 2 }));
      await refreshSyncStats();

      expect(getSyncStatsSnapshot()).not.toBe(before);
      expect(before?.roms).toBe(1);
    });

    it("notifies subscribers on an applied answer and not on an overtaken one", async () => {
      const notified = vi.fn();
      const unsubscribe = onSyncStatsStoreChange(notified);
      const older = deferred<SyncStats>();
      const newer = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

      const first = refreshSyncStats();
      const second = refreshSyncStatsAfterChange();
      newer.resolve(stats({ roms: 0 }));
      await second;
      expect(notified).toHaveBeenCalledTimes(1);

      older.resolve(stats({ roms: 42 }));
      await first;
      expect(notified).toHaveBeenCalledTimes(1);

      unsubscribe();
    });
  });

  describe("component subscription", () => {
    it("re-renders a subscriber when an answer lands", async () => {
      const open = deferred<SyncStats>();
      vi.mocked(getSyncStats).mockReturnValue(open.promise);

      const { result } = renderHook(() => useSyncStats());
      expect(result.current).toBeNull();

      const request = refreshSyncStats();
      open.resolve(stats({ roms: 42 }));
      await act(async () => {
        await request;
      });
      expect(result.current?.roms).toBe(42);
    });

    it("keeps the last answer across an unmount and remount", async () => {
      vi.mocked(getSyncStats).mockResolvedValue(stats({ roms: 42 }));
      vi.mocked(getSessionBudgetStatus).mockResolvedValue(budget(600_000));
      await refreshSyncStats();
      await refreshSessionBudget();

      const first = renderHook(() => ({ stats: useSyncStats(), budget: useSessionBudget() }));
      expect(first.result.current.stats?.roms).toBe(42);
      first.unmount();

      // The store outlives the panel: the remount renders the known answer on
      // its first pass, with no read in flight and nothing blanked.
      const second = renderHook(() => ({ stats: useSyncStats(), budget: useSessionBudget() }));
      expect(second.result.current.stats?.roms).toBe(42);
      expect(second.result.current.budget?.rss_kb).toBe(600_000);
      expect(vi.mocked(getSyncStats)).toHaveBeenCalledTimes(1);
      second.unmount();
    });

    it("stops notifying an unsubscribed listener", async () => {
      vi.mocked(getSyncStats).mockResolvedValue(stats({ roms: 1 }));
      const notified = vi.fn();
      onSyncStatsStoreChange(notified)();

      await refreshSyncStats();
      expect(notified).not.toHaveBeenCalled();
    });
  });
});
