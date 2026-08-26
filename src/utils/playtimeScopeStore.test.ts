import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { expectStableSubscribe } from "../test-utils/store-hook-subscription";
import { getPlaytimeScopeNotice } from "../api/backend";
import {
  getPlaytimeScopeState,
  setPlaytimeScopeState,
  onPlaytimeScopeChange,
  fetchPlaytimeScopeState,
  usePlaytimeScopeState,
} from "./playtimeScopeStore";

// Fakes nothing — the real useSyncExternalStore runs. The wrapper only records
// what the hook passes it, which is the one way to see whether the subscribe
// reference is stable; expectStableSubscribe's docstring explains why the
// property is unreachable from the store's side. The vi.mock is hoisted, so it
// has to live here rather than in the helper.
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useSyncExternalStore: vi.fn(actual.useSyncExternalStore) };
});

describe("playtimeScopeStore", () => {
  beforeEach(() => {
    setPlaytimeScopeState({ pending: false });
    vi.mocked(getPlaytimeScopeNotice).mockReset();
  });

  it("starts not-pending", () => {
    expect(getPlaytimeScopeState()).toEqual({ pending: false });
  });

  it("setPlaytimeScopeState updates the state and notifies subscribers", () => {
    const fn = vi.fn();
    onPlaytimeScopeChange(fn);
    setPlaytimeScopeState({ pending: true });
    expect(getPlaytimeScopeState()).toEqual({ pending: true });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("onPlaytimeScopeChange returns an unsubscribe that stops notifications", () => {
    const fn = vi.fn();
    const unsub = onPlaytimeScopeChange(fn);
    unsub();
    setPlaytimeScopeState({ pending: true });
    expect(fn).not.toHaveBeenCalled();
  });

  it("fetchPlaytimeScopeState maps the backend shape and updates the store", async () => {
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: true });
    const result = await fetchPlaytimeScopeState();
    expect(result).toEqual({ pending: true });
    expect(getPlaytimeScopeState()).toEqual({ pending: true });
  });

  it("fetchPlaytimeScopeState clears the store when the backend reports not-pending", async () => {
    setPlaytimeScopeState({ pending: true });
    vi.mocked(getPlaytimeScopeNotice).mockResolvedValue({ pending: false });
    const result = await fetchPlaytimeScopeState();
    expect(result).toEqual({ pending: false });
    expect(getPlaytimeScopeState()).toEqual({ pending: false });
  });

  describe("snapshot identity", () => {
    it("returns the same object reference while nothing changes", () => {
      setPlaytimeScopeState({ pending: true });
      expect(getPlaytimeScopeState()).toBe(getPlaytimeScopeState());
    });

    it("returns a different object reference after a real change", () => {
      setPlaytimeScopeState({ pending: true });
      const before = getPlaytimeScopeState();
      setPlaytimeScopeState({ pending: false });
      expect(getPlaytimeScopeState()).not.toBe(before);
      // The old snapshot is untouched — the write did not go in place.
      expect(before.pending).toBe(true);
    });
  });

  describe("usePlaytimeScopeState", () => {
    it("renders the current notice and re-renders on a real change", () => {
      setPlaytimeScopeState({ pending: true });
      const { result, unmount } = renderHook(() => usePlaytimeScopeState());
      expect(result.current).toEqual({ pending: true });

      act(() => {
        setPlaytimeScopeState({ pending: false });
      });
      expect(result.current).toEqual({ pending: false });
      unmount();
    });

    it("does not re-render when a notification carries the same state object", () => {
      const state = { pending: true };
      setPlaytimeScopeState(state);
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return usePlaytimeScopeState();
      });
      const settled = renders;

      // Re-installing the very object already stored notifies, but the snapshot
      // is unchanged by identity — React bails out rather than re-rendering.
      act(() => {
        setPlaytimeScopeState(state);
      });
      expect(renders).toBe(settled);

      // A real change still gets through — the snapshot is not simply frozen.
      act(() => {
        setPlaytimeScopeState({ pending: false });
      });
      expect(renders).toBeGreaterThan(settled);
      unmount();
    });

    it("stops re-rendering after unmount", () => {
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return usePlaytimeScopeState();
      });
      unmount();
      const afterUnmount = renders;
      act(() => {
        setPlaytimeScopeState({ pending: true });
      });
      expect(renders).toBe(afterUnmount);
    });

    it("subscribes with the store's own seam, so a re-render does not re-subscribe", () => {
      expectStableSubscribe(usePlaytimeScopeState, onPlaytimeScopeChange);
    });
  });
});
