import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { expectStableSubscribe } from "../test-utils/store-hook-subscription";
import {
  clearSaveSortMigration,
  getSaveSortMigrationState,
  onSaveSortMigrationChange,
  setSaveSortMigrationStatus,
  useSaveSortMigrationState,
} from "./saveSortMigrationStore";

// Fakes nothing — the real useSyncExternalStore runs. The wrapper only records
// what the hook passes it, which is the one way to see whether the subscribe
// reference is stable; expectStableSubscribe's docstring explains why the
// property is unreachable from the store's side. The vi.mock is hoisted, so it
// has to live here rather than in the helper.
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useSyncExternalStore: vi.fn(actual.useSyncExternalStore) };
});

describe("saveSortMigrationStore", () => {
  beforeEach(() => {
    clearSaveSortMigration();
  });

  describe("snapshot identity", () => {
    it("returns the same object reference while nothing changes", () => {
      setSaveSortMigrationStatus({ pending: true, saves_count: 2 });
      expect(getSaveSortMigrationState()).toBe(getSaveSortMigrationState());
    });

    it("returns a different object reference after a real change", () => {
      setSaveSortMigrationStatus({ pending: true, saves_count: 2 });
      const before = getSaveSortMigrationState();
      setSaveSortMigrationStatus({ pending: true, saves_count: 5 });
      expect(getSaveSortMigrationState()).not.toBe(before);
      // The old snapshot is untouched — the write did not go in place.
      expect(before.saves_count).toBe(2);
    });

    it("clearSaveSortMigration installs a fresh not-pending status", () => {
      setSaveSortMigrationStatus({ pending: true, saves_count: 2 });
      const before = getSaveSortMigrationState();
      clearSaveSortMigration();
      expect(getSaveSortMigrationState()).not.toBe(before);
      expect(getSaveSortMigrationState()).toEqual({ pending: false });
    });
  });

  describe("onSaveSortMigrationChange", () => {
    it("notifies subscribers and stops after the unsubscribe", () => {
      let calls = 0;
      const unsub = onSaveSortMigrationChange(() => {
        calls += 1;
      });
      setSaveSortMigrationStatus({ pending: true });
      expect(calls).toBe(1);
      unsub();
      setSaveSortMigrationStatus({ pending: false });
      expect(calls).toBe(1);
    });
  });

  describe("useSaveSortMigrationState", () => {
    it("renders the current status and re-renders on a real change", () => {
      setSaveSortMigrationStatus({ pending: true, saves_count: 2 });
      const { result, unmount } = renderHook(() => useSaveSortMigrationState());
      expect(result.current).toEqual({ pending: true, saves_count: 2 });

      act(() => {
        setSaveSortMigrationStatus({ pending: true, saves_count: 7 });
      });
      expect(result.current).toEqual({ pending: true, saves_count: 7 });

      act(() => {
        clearSaveSortMigration();
      });
      expect(result.current).toEqual({ pending: false });
      unmount();
    });

    it("does not re-render when a notification carries the same status object", () => {
      const status = { pending: true, saves_count: 2 };
      setSaveSortMigrationStatus(status);
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useSaveSortMigrationState();
      });
      const settled = renders;

      // Re-installing the very object already stored notifies, but the snapshot
      // is unchanged by identity — React bails out rather than re-rendering.
      act(() => {
        setSaveSortMigrationStatus(status);
      });
      expect(renders).toBe(settled);

      // A real change still gets through — the snapshot is not simply frozen.
      act(() => {
        setSaveSortMigrationStatus({ pending: true, saves_count: 3 });
      });
      expect(renders).toBeGreaterThan(settled);
      unmount();
    });

    it("stops re-rendering after unmount", () => {
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useSaveSortMigrationState();
      });
      unmount();
      const afterUnmount = renders;
      act(() => {
        setSaveSortMigrationStatus({ pending: true });
      });
      expect(renders).toBe(afterUnmount);
    });

    it("subscribes with the store's own seam, so a re-render does not re-subscribe", () => {
      expectStableSubscribe(useSaveSortMigrationState, onSaveSortMigrationChange);
    });
  });
});
