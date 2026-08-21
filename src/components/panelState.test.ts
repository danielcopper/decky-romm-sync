import { describe, it, expect, beforeEach, vi } from "vitest";
import type { MutableRefObject, SetStateAction } from "react";
import {
  bindRomInState,
  refreshPanelCoreInfo,
  refreshSlotState,
  takeReadTicket,
  type PanelReadSeqs,
  type PanelState,
  type RomBinding,
} from "./panelState";
import * as backend from "../api/backend";
import { _resetSharedReadsForTests } from "../api/sharedReads";
import { refreshCoreInfoInBackground } from "../utils/sectionRefresh";
import type { CoreInfoFields } from "../utils/playSection";
import type { CoreInfo, SaveSlotSummary } from "../types";

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
  current: { detail: 0, saveStatus: 0, slots: 0, slotTracking: 0, bios: 0 },
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

describe("bindRomInState", () => {
  /** A `useState` setter over a plain value, so a binding's writes can be
   *  applied and read back without mounting the panel. */
  function stateSetter(initial: PanelState) {
    let state = initial;
    return {
      setter: (update: SetStateAction<PanelState>) => {
        state = typeof update === "function" ? update(state) : update;
      },
      get current(): PanelState {
        return state;
      },
    };
  }

  it("folds an answer in while the state still names the bound rom", () => {
    const store = stateSetter(basePanelState());
    bindRomInState(7, store.setter).write((prev) => ({ ...prev, activeSlot: "main", activeSlotKnown: true }));
    expect(store.current.activeSlot).toBe("main");
    expect(store.current.activeSlotKnown).toBe(true);
  });

  it("drops an answer for a rom the state has moved off", () => {
    const store = stateSetter({ ...basePanelState(), romId: 8 });
    bindRomInState(7, store.setter).write((prev) => ({ ...prev, activeSlot: "main", activeSlotKnown: true }));
    expect(store.current.activeSlot).toBe("default");
    expect(store.current.activeSlotKnown).toBe(false);
  });

  it("takes a whole-state action as well as an updater — the writer's published shape", () => {
    const store = stateSetter(basePanelState());
    bindRomInState(7, store.setter).write({ ...basePanelState(), activeSlot: "main" });
    expect(store.current.activeSlot).toBe("main");
  });
});

// The panel and the play row each read this ROM's core info on every page open,
// microtasks apart, and neither knows about the other (#1758).
describe("core info shared with the play row's load", () => {
  const coreInfo: CoreInfo = {
    active_core: "snes9x.so",
    active_core_label: "Snes9x",
    platform_core_label: null,
    has_game_override: false,
    emulator_data_available: true,
    emulators: [],
  };

  beforeEach(() => {
    vi.resetAllMocks();
    _resetSharedReadsForTests();
  });

  it("costs ONE backend call when both lanes read the same ROM at once", async () => {
    const open = deferred<CoreInfo>();
    vi.mocked(backend.getPlatformCoreInfo).mockReturnValue(open.promise);
    let panelState = basePanelState();
    let playRowState = {} as CoreInfoFields;

    const panelRead = refreshPanelCoreInfo({
      romId: 7,
      write: (update) => {
        panelState = typeof update === "function" ? update(panelState) : update;
      },
    });
    refreshCoreInfoInBackground<CoreInfoFields>(
      7,
      () => false,
      (update) => {
        playRowState = typeof update === "function" ? update(playRowState) : update;
      },
    );

    open.resolve(coreInfo);
    await panelRead;
    await flush();

    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledExactlyOnceWith(7);
    // Both lanes folded the SAME answer — sharing a request is only correct if
    // every caller is handed the same one.
    expect(panelState.coreInfo).toBe(coreInfo);
    expect(playRowState.activeCoreLabel).toBe("Snes9x");
  });

  it("reads again once the shared request has settled", async () => {
    vi.mocked(backend.getPlatformCoreInfo).mockResolvedValue(coreInfo);
    let panelState = basePanelState();
    const binding: RomBinding = {
      romId: 7,
      write: (update) => {
        panelState = typeof update === "function" ? update(panelState) : update;
      },
    };

    await refreshPanelCoreInfo(binding);
    await refreshPanelCoreInfo(binding);

    expect(vi.mocked(backend.getPlatformCoreInfo)).toHaveBeenCalledTimes(2);
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
