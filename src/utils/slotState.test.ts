import { describe, it, expect, vi } from "vitest";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import {
  applyLoadSlotsResult,
  applyRefreshSlotResult,
  type LoadSlotsFields,
  type RefreshSlotFields,
  type SlotsResponse,
} from "./slotState";
import type { SaveSlotSummary } from "../types";

const slot = (name: string): SaveSlotSummary => ({
  slot: name,
  source: "server",
  count: 1,
  latest_updated_at: null,
});

interface RefreshState extends RefreshSlotFields {
  unrelated: number;
}

interface LoadState extends LoadSlotsFields {
  unrelated: string;
}

const refreshState = (overrides: Partial<RefreshState> = {}): RefreshState => ({
  activeSlot: null,
  availableSlots: [],
  unrelated: 0,
  ...overrides,
});

const loadState = (overrides: Partial<LoadState> = {}): LoadState => ({
  activeSlot: null,
  availableSlots: [],
  slotsLoading: false,
  unrelated: "",
  ...overrides,
});

/** Build a typed setState mock that matches `Dispatch<SetStateAction<S>>` —
 *  vi.fn's default mock type narrows to the first invocation and trips TS
 *  when the helper accepts `S | (prev: S) => S`. */
const makeSetter = <S>() => vi.fn() as unknown as Dispatch<SetStateAction<S>>;

/** Read the updater function passed to a `makeSetter` mock — typed to keep
 *  the test assertions clean even though the underlying mock is `unknown`. */
const lastUpdater = <S>(setter: Dispatch<SetStateAction<S>>): (prev: S) => S => {
  const calls = (setter as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  return calls[calls.length - 1][0] as (prev: S) => S;
};

const callCount = <S>(setter: Dispatch<SetStateAction<S>>): number =>
  (setter as unknown as { mock: { calls: unknown[][] } }).mock.calls.length;

describe("applyRefreshSlotResult", () => {
  it("skips the setter when success=false (preserves persisted state)", () => {
    const setter = makeSetter<RefreshState>();
    const result: SlotsResponse = { success: false, slots: [], error: "boom" };
    applyRefreshSlotResult<RefreshState>(result, setter);
    expect(callCount(setter)).toBe(0);
  });

  it("merges slots and active_slot on success", () => {
    const setter = makeSetter<RefreshState>();
    const result: SlotsResponse = {
      success: true,
      slots: [slot("a"), slot("b")],
      active_slot: "a",
    };
    applyRefreshSlotResult<RefreshState>(result, setter);
    expect(callCount(setter)).toBe(1);
    const next = lastUpdater(setter)(refreshState({ unrelated: 7 }));
    expect(next).toEqual({
      activeSlot: "a",
      availableSlots: [slot("a"), slot("b")],
      unrelated: 7,
    });
  });

  it("preserves prev.activeSlot when active_slot is undefined", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true, slots: [slot("x")] }, setter);
    const next = lastUpdater(setter)(refreshState({ activeSlot: "previous" }));
    expect(next.activeSlot).toBe("previous");
    expect(next.availableSlots).toEqual([slot("x")]);
  });

  it("treats explicit null active_slot as the new value (does not preserve prev)", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true, slots: [], active_slot: null }, setter);
    const next = lastUpdater(setter)(refreshState({ activeSlot: "previous" }));
    expect(next.activeSlot).toBeNull();
  });

  it("defaults a missing slots field to []", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true } as unknown as SlotsResponse, setter);
    const next = lastUpdater(setter)(refreshState({ availableSlots: [slot("stale")] }));
    expect(next.availableSlots).toEqual([]);
  });
});

describe("applyLoadSlotsResult", () => {
  it("on failure: logs the error, resets the loaded-once ref, clears spinner only", () => {
    const setter = makeSetter<LoadState>();
    const loadedRef: MutableRefObject<boolean> = { current: true };
    const logError = vi.fn();
    const result: SlotsResponse = { success: false, slots: [], error: "boom" };
    applyLoadSlotsResult<LoadState>(result, setter, loadedRef, logError);

    expect(logError).toHaveBeenCalledWith("Failed to load save slots: boom");
    expect(loadedRef.current).toBe(false);
    expect(callCount(setter)).toBe(1);
    const prev = loadState({
      activeSlot: "keep",
      availableSlots: [slot("keep")],
      slotsLoading: true,
      unrelated: "keep",
    });
    const next = lastUpdater(setter)(prev);
    expect(next).toEqual({
      activeSlot: "keep",
      availableSlots: [slot("keep")],
      slotsLoading: false,
      unrelated: "keep",
    });
  });

  it("on failure with no error field: logs 'unknown'", () => {
    const logError = vi.fn();
    applyLoadSlotsResult<LoadState>(
      { success: false, slots: [] },
      makeSetter<LoadState>(),
      { current: true },
      logError,
    );
    expect(logError).toHaveBeenCalledWith("Failed to load save slots: unknown");
  });

  it("on success: merges slots + active_slot, clears spinner, does not log or touch ref", () => {
    const setter = makeSetter<LoadState>();
    const loadedRef: MutableRefObject<boolean> = { current: true };
    const logError = vi.fn();
    const result: SlotsResponse = {
      success: true,
      slots: [slot("a")],
      active_slot: "a",
    };
    applyLoadSlotsResult<LoadState>(result, setter, loadedRef, logError);

    expect(logError).not.toHaveBeenCalled();
    expect(loadedRef.current).toBe(true);
    const next = lastUpdater(setter)(loadState({ slotsLoading: true, unrelated: "x" }));
    expect(next).toEqual({
      activeSlot: "a",
      availableSlots: [slot("a")],
      slotsLoading: false,
      unrelated: "x",
    });
  });

  it("on success: preserves prev.activeSlot when active_slot is undefined", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>(
      { success: true, slots: [slot("x")] },
      setter,
      { current: true },
      vi.fn(),
    );
    const next = lastUpdater(setter)(loadState({ activeSlot: "previous" }));
    expect(next.activeSlot).toBe("previous");
  });

  it("on success: treats explicit null active_slot as the new value", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>(
      { success: true, slots: [], active_slot: null },
      setter,
      { current: true },
      vi.fn(),
    );
    const next = lastUpdater(setter)(loadState({ activeSlot: "previous" }));
    expect(next.activeSlot).toBeNull();
  });

  it("on success: defaults a missing slots field to []", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>(
      { success: true } as unknown as SlotsResponse,
      setter,
      { current: true },
      vi.fn(),
    );
    const next = lastUpdater(setter)(loadState({ availableSlots: [slot("stale")] }));
    expect(next.availableSlots).toEqual([]);
  });
});
