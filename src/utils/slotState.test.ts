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
  activeSlotKnown: false,
  availableSlots: [],
  lastKnownSlots: null,
  unrelated: 0,
  ...overrides,
});

const loadState = (overrides: Partial<LoadState> = {}): LoadState => ({
  activeSlot: null,
  activeSlotKnown: false,
  availableSlots: [],
  lastKnownSlots: null,
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
const lastUpdater = <S>(setter: Dispatch<SetStateAction<S>>): ((prev: S) => S) => {
  const calls = (setter as unknown as { mock: { calls: unknown[][] } }).mock.calls;
  return calls[calls.length - 1]![0] as (prev: S) => S;
};

const callCount = <S>(setter: Dispatch<SetStateAction<S>>): number =>
  (setter as unknown as { mock: { calls: unknown[][] } }).mock.calls.length;

describe("applyRefreshSlotResult", () => {
  it("skips the setter when success=false (preserves persisted state)", () => {
    const setter = makeSetter<RefreshState>();
    const result: SlotsResponse = { success: false, slots: [], message: "boom" };
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
      activeSlotKnown: true,
      availableSlots: [slot("a"), slot("b")],
      lastKnownSlots: null,
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

  it("leaves the active slot unanswered when a success carries no active_slot (#1747)", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true, slots: [slot("x")] }, setter);
    const next = lastUpdater(setter)(refreshState({ activeSlot: "placeholder" }));
    expect(next.activeSlotKnown).toBe(false);
  });

  it("keeps an already-answered active slot answered when a success carries no active_slot (#1747)", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true, slots: [slot("x")] }, setter);
    const next = lastUpdater(setter)(refreshState({ activeSlot: "a", activeSlotKnown: true }));
    expect(next.activeSlotKnown).toBe(true);
  });

  it("takes the last-known snapshot off a failure that carries one (#1755)", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>(
      {
        success: false,
        slots: [],
        reason: "server_unreachable",
        last_known: { slots: [slot("a")], active_slot: "a" },
      },
      setter,
    );
    const next = lastUpdater(setter)(refreshState({ activeSlot: "placeholder" }));
    expect(next.lastKnownSlots).toEqual({
      slots: [slot("a")],
      activeSlot: "a",
    });
    // A snapshot is not an answer — the live fields stay exactly as they were.
    expect(next.activeSlot).toBe("placeholder");
    expect(next.activeSlotKnown).toBe(false);
    expect(next.availableSlots).toEqual([]);
  });

  it("drops a stored snapshot once a success answers (#1755)", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true, slots: [slot("a")], active_slot: "a" }, setter);
    const next = lastUpdater(setter)(refreshState({ lastKnownSlots: { slots: [slot("old")], activeSlot: "old" } }));
    expect(next.lastKnownSlots).toBeNull();
  });

  it("treats explicit null active_slot as the new value (does not preserve prev)", () => {
    const setter = makeSetter<RefreshState>();
    applyRefreshSlotResult<RefreshState>({ success: true, slots: [], active_slot: null }, setter);
    const next = lastUpdater(setter)(refreshState({ activeSlot: "previous" }));
    expect(next.activeSlot).toBeNull();
    // The legacy bucket is an answer like any other name (#1747).
    expect(next.activeSlotKnown).toBe(true);
  });
});

describe("applyLoadSlotsResult", () => {
  it("on failure: logs the error, resets the loaded-once ref, clears spinner only", () => {
    const setter = makeSetter<LoadState>();
    const loadedRef: MutableRefObject<boolean> = { current: true };
    const logError = vi.fn();
    const result: SlotsResponse = { success: false, slots: [], message: "boom" };
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
      // A failed load answered nothing — the shown active slot stays as
      // unanswered as it was (#1747).
      activeSlotKnown: false,
      availableSlots: [slot("keep")],
      lastKnownSlots: null,
      slotsLoading: false,
      unrelated: "keep",
    });
  });

  it("on failure: takes the last-known snapshot when the response carries one (#1755)", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>(
      {
        success: false,
        slots: [],
        reason: "server_unreachable",
        last_known: { slots: [slot("a"), slot("b")], active_slot: "b" },
      },
      setter,
      { current: true },
      vi.fn(),
    );
    const next = lastUpdater(setter)(loadState({ activeSlot: "placeholder", slotsLoading: true }));
    expect(next.lastKnownSlots).toEqual({
      slots: [slot("a"), slot("b")],
      activeSlot: "b",
    });
    expect(next.activeSlotKnown).toBe(false);
    expect(next.availableSlots).toEqual([]);
  });

  it("on failure with no snapshot: keeps the one already held (#1755)", () => {
    const setter = makeSetter<LoadState>();
    const held = { slots: [slot("a")], activeSlot: "a" };
    applyLoadSlotsResult<LoadState>({ success: false, slots: [] }, setter, { current: true }, vi.fn());
    const next = lastUpdater(setter)(loadState({ lastKnownSlots: held }));
    expect(next.lastKnownSlots).toBe(held);
  });

  it("on success: drops a stored snapshot (#1755)", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>(
      { success: true, slots: [slot("a")], active_slot: "a" },
      setter,
      { current: true },
      vi.fn(),
    );
    const next = lastUpdater(setter)(loadState({ lastKnownSlots: { slots: [slot("old")], activeSlot: "old" } }));
    expect(next.lastKnownSlots).toBeNull();
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
      activeSlotKnown: true,
      availableSlots: [slot("a")],
      lastKnownSlots: null,
      slotsLoading: false,
      unrelated: "x",
    });
  });

  it("on success: preserves prev.activeSlot when active_slot is undefined", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>({ success: true, slots: [slot("x")] }, setter, { current: true }, vi.fn());
    const next = lastUpdater(setter)(loadState({ activeSlot: "previous" }));
    expect(next.activeSlot).toBe("previous");
  });

  it("on success with no active_slot: leaves the active slot unanswered (#1747)", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>({ success: true, slots: [slot("x")] }, setter, { current: true }, vi.fn());
    const next = lastUpdater(setter)(loadState({ activeSlot: "placeholder" }));
    expect(next.activeSlotKnown).toBe(false);
  });

  it("on success with no active_slot: keeps an already-answered active slot answered (#1747)", () => {
    const setter = makeSetter<LoadState>();
    applyLoadSlotsResult<LoadState>({ success: true, slots: [slot("x")] }, setter, { current: true }, vi.fn());
    const next = lastUpdater(setter)(loadState({ activeSlot: "a", activeSlotKnown: true }));
    expect(next.activeSlotKnown).toBe(true);
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
});
