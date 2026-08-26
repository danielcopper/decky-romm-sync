import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { expectStableSubscribe } from "../test-utils/store-hook-subscription";
import {
  clearMigration,
  getMigrationState,
  onMigrationChange,
  setMigrationStatus,
  useMigrationStatus,
} from "./migrationStore";

// Fakes nothing — the real useSyncExternalStore runs. The wrapper only records
// what the hook passes it, which is the one way to see whether the subscribe
// reference is stable; expectStableSubscribe's docstring explains why the
// property is unreachable from the store's side. The vi.mock is hoisted, so it
// has to live here rather than in the helper.
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useSyncExternalStore: vi.fn(actual.useSyncExternalStore) };
});

describe("migrationStore", () => {
  beforeEach(() => {
    clearMigration();
  });

  describe("snapshot identity", () => {
    it("returns the same object reference while nothing changes", () => {
      setMigrationStatus({ pending: true, old_path: "/old" });
      expect(getMigrationState()).toBe(getMigrationState());
    });

    it("returns a different object reference after a real change", () => {
      setMigrationStatus({ pending: true, old_path: "/old" });
      const before = getMigrationState();
      setMigrationStatus({ pending: true, old_path: "/new" });
      expect(getMigrationState()).not.toBe(before);
      // The old snapshot is untouched — the write did not go in place.
      expect(before.old_path).toBe("/old");
    });

    it("clearMigration installs a fresh not-pending status", () => {
      setMigrationStatus({ pending: true, roms_count: 9 });
      const before = getMigrationState();
      clearMigration();
      expect(getMigrationState()).not.toBe(before);
      expect(getMigrationState()).toEqual({ pending: false });
    });
  });

  describe("onMigrationChange", () => {
    it("notifies subscribers and stops after the unsubscribe", () => {
      let calls = 0;
      const unsub = onMigrationChange(() => {
        calls += 1;
      });
      setMigrationStatus({ pending: true });
      expect(calls).toBe(1);
      unsub();
      setMigrationStatus({ pending: false });
      expect(calls).toBe(1);
    });
  });

  describe("useMigrationStatus", () => {
    it("renders the current status and re-renders on a real change", () => {
      setMigrationStatus({ pending: true, old_path: "/x" });
      const { result, unmount } = renderHook(() => useMigrationStatus());
      expect(result.current).toEqual({ pending: true, old_path: "/x" });

      act(() => {
        setMigrationStatus({ pending: true, roms_count: 9 });
      });
      expect(result.current).toEqual({ pending: true, roms_count: 9 });

      act(() => {
        clearMigration();
      });
      expect(result.current).toEqual({ pending: false });
      unmount();
    });

    it("does not re-render when a notification carries the same status object", () => {
      const status = { pending: true, roms_count: 3 };
      setMigrationStatus(status);
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useMigrationStatus();
      });
      const settled = renders;

      // Re-installing the very object already stored notifies, but the snapshot
      // is unchanged by identity — React bails out rather than re-rendering.
      act(() => {
        setMigrationStatus(status);
      });
      expect(renders).toBe(settled);

      // A real change still gets through — the snapshot is not simply frozen.
      act(() => {
        setMigrationStatus({ pending: true, roms_count: 4 });
      });
      expect(renders).toBeGreaterThan(settled);
      unmount();
    });

    it("stops re-rendering after unmount", () => {
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useMigrationStatus();
      });
      unmount();
      const afterUnmount = renders;
      act(() => {
        setMigrationStatus({ pending: true });
      });
      expect(renders).toBe(afterUnmount);
    });

    it("subscribes with the store's own seam, so a re-render does not re-subscribe", () => {
      expectStableSubscribe(useMigrationStatus, onMigrationChange);
    });
  });
});
