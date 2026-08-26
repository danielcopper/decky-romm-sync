import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { expectStableSubscribe } from "../test-utils/store-hook-subscription";
import { getSettingsResetNotice } from "../api/backend";
import {
  getSettingsResetState,
  setSettingsResetState,
  onSettingsResetChange,
  fetchSettingsResetState,
  useSettingsResetState,
} from "./settingsResetStore";

// Fakes nothing — the real useSyncExternalStore runs. The wrapper only records
// what the hook passes it, which is the one way to see whether the subscribe
// reference is stable; expectStableSubscribe's docstring explains why the
// property is unreachable from the store's side. The vi.mock is hoisted, so it
// has to live here rather than in the helper.
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useSyncExternalStore: vi.fn(actual.useSyncExternalStore) };
});

describe("settingsResetStore", () => {
  beforeEach(() => {
    setSettingsResetState({ pending: false, backedUpTo: null });
    vi.mocked(getSettingsResetNotice).mockReset();
  });

  it("starts not-pending", () => {
    expect(getSettingsResetState()).toEqual({ pending: false, backedUpTo: null });
  });

  it("setSettingsResetState updates the state and notifies subscribers", () => {
    const fn = vi.fn();
    onSettingsResetChange(fn);
    setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-7" });
    expect(getSettingsResetState()).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-7" });
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("onSettingsResetChange returns an unsubscribe that stops notifications", () => {
    const fn = vi.fn();
    const unsub = onSettingsResetChange(fn);
    unsub();
    setSettingsResetState({ pending: true, backedUpTo: null });
    expect(fn).not.toHaveBeenCalled();
  });

  it("fetchSettingsResetState maps the backend shape and updates the store", async () => {
    vi.mocked(getSettingsResetNotice).mockResolvedValue({
      pending: true,
      backed_up_to: "settings.json.corrupt-42",
    });
    const result = await fetchSettingsResetState();
    expect(result).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-42" });
    expect(getSettingsResetState()).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-42" });
  });

  it("fetchSettingsResetState clears the store when the backend reports not-pending", async () => {
    setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-old" });
    vi.mocked(getSettingsResetNotice).mockResolvedValue({ pending: false, backed_up_to: null });
    const result = await fetchSettingsResetState();
    expect(result).toEqual({ pending: false, backedUpTo: null });
    expect(getSettingsResetState()).toEqual({ pending: false, backedUpTo: null });
  });

  describe("snapshot identity", () => {
    it("returns the same object reference while nothing changes", () => {
      setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-1" });
      expect(getSettingsResetState()).toBe(getSettingsResetState());
    });

    it("returns a different object reference after a real change", () => {
      setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-1" });
      const before = getSettingsResetState();
      setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-2" });
      expect(getSettingsResetState()).not.toBe(before);
      // The old snapshot is untouched — the write did not go in place.
      expect(before.backedUpTo).toBe("settings.json.corrupt-1");
    });
  });

  describe("useSettingsResetState", () => {
    it("renders the current notice and re-renders on a real change", () => {
      setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-1" });
      const { result, unmount } = renderHook(() => useSettingsResetState());
      expect(result.current).toEqual({ pending: true, backedUpTo: "settings.json.corrupt-1" });

      act(() => {
        setSettingsResetState({ pending: false, backedUpTo: null });
      });
      expect(result.current).toEqual({ pending: false, backedUpTo: null });
      unmount();
    });

    it("does not re-render when a notification carries the same state object", () => {
      const state = { pending: true, backedUpTo: "settings.json.corrupt-1" };
      setSettingsResetState(state);
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useSettingsResetState();
      });
      const settled = renders;

      // Re-installing the very object already stored notifies, but the snapshot
      // is unchanged by identity — React bails out rather than re-rendering.
      act(() => {
        setSettingsResetState(state);
      });
      expect(renders).toBe(settled);

      // A real change still gets through — the snapshot is not simply frozen.
      act(() => {
        setSettingsResetState({ pending: false, backedUpTo: null });
      });
      expect(renders).toBeGreaterThan(settled);
      unmount();
    });

    it("stops re-rendering after unmount", () => {
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useSettingsResetState();
      });
      unmount();
      const afterUnmount = renders;
      act(() => {
        setSettingsResetState({ pending: true, backedUpTo: "settings.json.corrupt-3" });
      });
      expect(renders).toBe(afterUnmount);
    });

    it("subscribes with the store's own seam, so a re-render does not re-subscribe", () => {
      expectStableSubscribe(useSettingsResetState, onSettingsResetChange);
    });
  });
});
