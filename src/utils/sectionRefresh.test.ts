import { describe, it, expect, vi, beforeEach } from "vitest";
import type { Dispatch, SetStateAction } from "react";
import {
  refreshActiveSlotInBackground,
  refreshBiosInBackground,
  refreshAchievementsInBackground,
} from "./sectionRefresh";
import * as backend from "../api/backend";

interface ActiveSlotState {
  activeSlot: string | null;
  unrelated: number;
}

interface BiosState {
  biosNeeded: boolean;
  biosStatus: "ok" | "partial" | "missing" | null;
  biosLabel: string;
  activeCoreLabel: string | null;
  activeCoreIsDefault: boolean;
  availableCores: Array<{ core_so: string; label: string; is_default: boolean }>;
  unrelated: string;
}

interface AchievementState {
  achievementEarned: number;
  achievementTotal: number;
  unrelated: boolean;
}

const flushMicrotasks = () => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Build a promise that resolves only when the returned `resolve` is called.
 * Lets a test simulate the "long-running fetch" window (e.g. the 5s
 * `timeoutMs` race inside `getBiosStatus`) and flip a `cancelled` flag
 * mid-await — proving the helper re-reads the closure after each await
 * instead of capturing a stale boolean snapshot.
 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (e: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("refreshActiveSlotInBackground", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("applies active_slot to the setter when not cancelled", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValueOnce({
      active_slot: "slot-2",
    } as unknown as Awaited<ReturnType<typeof backend.getSaveStatus>>);

    const setter = vi.fn<(updater: (prev: ActiveSlotState) => ActiveSlotState) => void>();
    refreshActiveSlotInBackground(1, () => false, setter as unknown as Dispatch<SetStateAction<ActiveSlotState>>);
    await flushMicrotasks();

    expect(setter).toHaveBeenCalledOnce();
    const updater = setter.mock.calls[0][0];
    expect(updater({ activeSlot: null, unrelated: 7 })).toEqual({
      activeSlot: "slot-2",
      unrelated: 7,
    });
  });

  it("skips the setter when cancelled", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValueOnce({
      active_slot: "slot-2",
    } as unknown as Awaited<ReturnType<typeof backend.getSaveStatus>>);

    const setter = vi.fn();
    refreshActiveSlotInBackground(1, () => true, setter);
    await flushMicrotasks();

    expect(setter).not.toHaveBeenCalled();
  });

  it("re-reads the cancelled closure after the await (cancelled mid-await)", async () => {
    const d = deferred<{ active_slot: string }>();
    vi.mocked(backend.getSaveStatus).mockReturnValueOnce(
      d.promise as unknown as ReturnType<typeof backend.getSaveStatus>,
    );

    let cancelled = false;
    const setter = vi.fn();
    refreshActiveSlotInBackground(1, () => cancelled, setter);

    // Cancel before the backend call resolves — proves we re-read the closure.
    cancelled = true;
    d.resolve({ active_slot: "slot-2" });
    await flushMicrotasks();

    expect(setter).not.toHaveBeenCalled();
  });

  it("swallows backend errors without invoking the setter", async () => {
    vi.mocked(backend.getSaveStatus).mockRejectedValueOnce(new Error("network"));
    const setter = vi.fn();
    refreshActiveSlotInBackground(1, () => false, setter);
    await flushMicrotasks();
    // active-slot's `.catch` has no observable side effect beyond skipping the
    // setter; asserting the setter never fires is the post-catch state.
    expect(setter).not.toHaveBeenCalled();
  });

  it("falls back to null when active_slot is missing", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValueOnce({
      active_slot: null,
    } as unknown as Awaited<ReturnType<typeof backend.getSaveStatus>>);

    const setter = vi.fn<(updater: (prev: ActiveSlotState) => ActiveSlotState) => void>();
    refreshActiveSlotInBackground(1, () => false, setter as unknown as Dispatch<SetStateAction<ActiveSlotState>>);
    await flushMicrotasks();

    const updater = setter.mock.calls[0][0];
    expect(updater({ activeSlot: "x", unrelated: 1 })).toEqual({
      activeSlot: null,
      unrelated: 1,
    });
  });
});

describe("refreshBiosInBackground", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("merges the projected BIOS fields when not cancelled and payload present", async () => {
    vi.mocked(backend.getBiosStatus).mockResolvedValueOnce({
      bios_status: {
        active_core_label: "Mupen64Plus-Next",
        available_cores: [
          { core_so: "x.so", label: "Mupen64Plus-Next", is_default: true },
        ],
      },
      bios_level: "ok",
      bios_label: "BIOS OK",
    } as unknown as Awaited<ReturnType<typeof backend.getBiosStatus>>);

    const setter = vi.fn<(updater: (prev: BiosState) => BiosState) => void>();
    refreshBiosInBackground(1, () => false, setter as unknown as Dispatch<SetStateAction<BiosState>>);
    await flushMicrotasks();

    expect(setter).toHaveBeenCalledOnce();
    const next = setter.mock.calls[0][0]({
      biosNeeded: false,
      biosStatus: null,
      biosLabel: "",
      activeCoreLabel: null,
      activeCoreIsDefault: true,
      availableCores: [],
      unrelated: "keep",
    });
    expect(next.biosNeeded).toBe(true);
    expect(next.biosLabel).toBe("BIOS OK");
    expect(next.unrelated).toBe("keep");
  });

  it("skips the setter when cancelled at call time", async () => {
    vi.mocked(backend.getBiosStatus).mockResolvedValueOnce({
      bios_status: { available_cores: [] },
      bios_level: "ok",
      bios_label: "ok",
    } as unknown as Awaited<ReturnType<typeof backend.getBiosStatus>>);
    const setter = vi.fn();
    refreshBiosInBackground(1, () => true, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });

  it("re-reads the cancelled closure after the await (regression — was boolean snapshot)", async () => {
    // Regression test for #725: a `boolean` snapshot was captured at call
    // time, so flipping `cancelled` during the 5s `timeoutMs` race in
    // `getBiosStatus` did not prevent a setter call on an unmounted component.
    const d = deferred<unknown>();
    vi.mocked(backend.getBiosStatus).mockReturnValueOnce(
      d.promise as unknown as ReturnType<typeof backend.getBiosStatus>,
    );

    let cancelled = false;
    const setter = vi.fn();
    refreshBiosInBackground(1, () => cancelled, setter);

    cancelled = true;
    d.resolve({
      bios_status: { available_cores: [] },
      bios_level: "ok",
      bios_label: "ok",
    });
    await flushMicrotasks();

    expect(setter).not.toHaveBeenCalled();
  });

  it("skips the setter when bios_status is null", async () => {
    vi.mocked(backend.getBiosStatus).mockResolvedValueOnce({
      bios_status: null,
      bios_level: null,
      bios_label: null,
    } as unknown as Awaited<ReturnType<typeof backend.getBiosStatus>>);
    const setter = vi.fn();
    refreshBiosInBackground(1, () => false, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });

  it("logs the error and skips the setter when the fetch rejects", async () => {
    vi.mocked(backend.getBiosStatus).mockRejectedValueOnce(new Error("network"));
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
    const setter = vi.fn();
    refreshBiosInBackground(1, () => false, setter);
    await flushMicrotasks();

    expect(setter).not.toHaveBeenCalled();
    // Non-vacuous catch assertion: the `.catch` calls debugLog with the error.
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("Background BIOS status fetch error"),
    );
  });
});

describe("refreshAchievementsInBackground", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("applies earned/total when success=true", async () => {
    vi.mocked(backend.getAchievementProgress).mockResolvedValueOnce({
      success: true,
      earned: 12,
      total: 30,
    } as unknown as Awaited<ReturnType<typeof backend.getAchievementProgress>>);

    const setter = vi.fn<(updater: (prev: AchievementState) => AchievementState) => void>();
    refreshAchievementsInBackground(1, () => false, setter as unknown as Dispatch<SetStateAction<AchievementState>>);
    await flushMicrotasks();

    expect(setter).toHaveBeenCalledOnce();
    const next = setter.mock.calls[0][0]({
      achievementEarned: 0,
      achievementTotal: 0,
      unrelated: true,
    });
    expect(next).toEqual({ achievementEarned: 12, achievementTotal: 30, unrelated: true });
  });

  it("skips the setter when success=false", async () => {
    vi.mocked(backend.getAchievementProgress).mockResolvedValueOnce({
      success: false,
      earned: 0,
      total: 0,
    } as unknown as Awaited<ReturnType<typeof backend.getAchievementProgress>>);
    const setter = vi.fn();
    refreshAchievementsInBackground(1, () => false, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });

  it("skips the setter when cancelled at call time", async () => {
    vi.mocked(backend.getAchievementProgress).mockResolvedValueOnce({
      success: true,
      earned: 1,
      total: 2,
    } as unknown as Awaited<ReturnType<typeof backend.getAchievementProgress>>);
    const setter = vi.fn();
    refreshAchievementsInBackground(1, () => true, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });

  it("re-reads the cancelled closure after the await (cancelled mid-await)", async () => {
    const d = deferred<unknown>();
    vi.mocked(backend.getAchievementProgress).mockReturnValueOnce(
      d.promise as unknown as ReturnType<typeof backend.getAchievementProgress>,
    );

    let cancelled = false;
    const setter = vi.fn();
    refreshAchievementsInBackground(1, () => cancelled, setter);

    cancelled = true;
    d.resolve({ success: true, earned: 1, total: 2 });
    await flushMicrotasks();

    expect(setter).not.toHaveBeenCalled();
  });

  it("logs the error and skips the setter when the fetch rejects", async () => {
    vi.mocked(backend.getAchievementProgress).mockRejectedValueOnce(new Error("network"));
    vi.mocked(backend.debugLog).mockResolvedValue(undefined);
    const setter = vi.fn();
    refreshAchievementsInBackground(1, () => false, setter);
    await flushMicrotasks();

    expect(setter).not.toHaveBeenCalled();
    // Non-vacuous catch assertion: the `.catch` calls debugLog with the error.
    expect(vi.mocked(backend.debugLog)).toHaveBeenCalledWith(
      expect.stringContaining("Background achievement progress fetch error"),
    );
  });
});
