import { describe, it, expect, beforeEach, vi } from "vitest";
import type { MutableRefObject } from "react";
import { refreshSlotState, takeReadTicket, type PanelReadSeqs, type PanelState, type RomBinding } from "./panelState";
import * as backend from "../api/backend";
import type { SaveSlotSummary } from "../types";

/** A promise the test resolves by hand, so two reads of the same callable can be
 *  made to answer in the opposite order to the one they were issued in. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

const seqsRef = (): MutableRefObject<PanelReadSeqs> => ({
  current: { detail: 0, saveStatus: 0, slots: 0, slotTracking: 0 },
});

const slot = (name: string): SaveSlotSummary => ({
  slot: name,
  source: "server",
  count: 1,
  latest_updated_at: null,
});

function basePanelState(): PanelState {
  return {
    loading: false,
    romId: 7,
    romName: "Game",
    platformName: "SNES",
    installed: false,
    installedRom: null,
    metadata: null,
    coverBase64: null,
    biosStatus: null,
    biosLevel: null,
    coreInfo: null,
    saveSyncEnabled: true,
    saveStatus: null,
    conflicts: [],
    error: false,
    activeTab: "saves",
    raId: null,
    slotConfirmed: false,
    // The pair a freshly-mounted panel holds: the placeholder slot name, and
    // nothing having answered what the active slot actually is.
    activeSlot: "default",
    activeSlotKnown: false,
    availableSlots: [],
    lastKnownSlots: null,
    slotsLoading: false,
    regions: [],
    languages: [],
  };
}

describe("takeReadTicket", () => {
  it("reports a read overtaken once a later read of the same kind takes a ticket", () => {
    const seqs = seqsRef();
    const first = takeReadTicket(seqs, "slots");
    expect(first()).toBe(false);
    const second = takeReadTicket(seqs, "slots");
    expect(first()).toBe(true);
    expect(second()).toBe(false);
  });

  it("keeps the kinds apart, so a read is not fenced by another kind's ticket", () => {
    const seqs = seqsRef();
    const slots = takeReadTicket(seqs, "slots");
    takeReadTicket(seqs, "slotTracking");
    takeReadTicket(seqs, "saveStatus");
    takeReadTicket(seqs, "detail");
    expect(slots()).toBe(false);
  });
});

describe("refreshSlotState", () => {
  let state: PanelState;
  let binding: RomBinding;

  beforeEach(() => {
    vi.resetAllMocks();
    state = basePanelState();
    binding = {
      romId: 7,
      write: (update) => {
        state = typeof update === "function" ? update(state) : update;
      },
    };
  });

  it("folds a slot list and a tracking answer into the panel state", async () => {
    vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [slot("main")], active_slot: "main" });
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: true, active_slot: "main" });

    refreshSlotState(binding, seqsRef());
    await flush();

    expect(state.activeSlot).toBe("main");
    // The name arrived with an answer, so it is one — the placeholder it
    // replaced was not (#1747).
    expect(state.activeSlotKnown).toBe(true);
    expect(state.availableSlots.map((s) => s.slot)).toEqual(["main"]);
    expect(state.slotConfirmed).toBe(true);
  });

  it("keeps the newer refresh's slot list when the earlier refresh answers last", async () => {
    const earlier = deferred<Awaited<ReturnType<typeof backend.getSaveSlots>>>();
    const later = deferred<Awaited<ReturnType<typeof backend.getSaveSlots>>>();
    vi.mocked(backend.getSaveSlots).mockReturnValueOnce(earlier.promise).mockReturnValueOnce(later.promise);
    vi.mocked(backend.isSaveTrackingConfigured).mockResolvedValue({ configured: false, active_slot: null });
    const seqs = seqsRef();

    refreshSlotState(binding, seqs);
    refreshSlotState(binding, seqs);

    later.resolve({ success: true, slots: [slot("newer")], active_slot: "newer" });
    await flush();
    earlier.resolve({ success: true, slots: [slot("older")], active_slot: "older" });
    await flush();

    expect(state.activeSlot).toBe("newer");
    expect(state.availableSlots.map((s) => s.slot)).toEqual(["newer"]);
  });

  it("keeps the newer refresh's tracking answer when the earlier refresh answers last", async () => {
    const earlier = deferred<Awaited<ReturnType<typeof backend.isSaveTrackingConfigured>>>();
    const later = deferred<Awaited<ReturnType<typeof backend.isSaveTrackingConfigured>>>();
    vi.mocked(backend.isSaveTrackingConfigured).mockReturnValueOnce(earlier.promise).mockReturnValueOnce(later.promise);
    vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [], active_slot: null });
    const seqs = seqsRef();

    refreshSlotState(binding, seqs);
    refreshSlotState(binding, seqs);

    later.resolve({ configured: false, active_slot: null });
    await flush();
    earlier.resolve({ configured: true, active_slot: "main" });
    await flush();

    // The wizard-vs-tab gate: the earlier "configured" answer would put the
    // SAVES tab in front of a version whose slots were never set up.
    expect(state.slotConfirmed).toBe(false);
  });

  it("applies a tracking answer a later slot-list read cannot replace", async () => {
    // The two reads have their own sequences because only one of them is also
    // issued by the lazy SAVES load: a slot-list read taken after this refresh
    // must not drop its tracking answer, which nothing else re-issues.
    const tracking = deferred<Awaited<ReturnType<typeof backend.isSaveTrackingConfigured>>>();
    vi.mocked(backend.isSaveTrackingConfigured).mockReturnValue(tracking.promise);
    vi.mocked(backend.getSaveSlots).mockResolvedValue({ success: true, slots: [], active_slot: null });
    const seqs = seqsRef();

    refreshSlotState(binding, seqs);
    takeReadTicket(seqs, "slots");
    tracking.resolve({ configured: true, active_slot: "main" });
    await flush();

    expect(state.slotConfirmed).toBe(true);
  });
});
