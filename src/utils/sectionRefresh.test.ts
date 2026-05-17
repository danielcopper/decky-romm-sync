import { describe, it, expect, vi, beforeEach } from "vitest";
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

describe("refreshActiveSlotInBackground", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("applies active_slot to the setter when not cancelled", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValueOnce({
      active_slot: "slot-2",
    } as unknown as Awaited<ReturnType<typeof backend.getSaveStatus>>);

    const setter = vi.fn<(updater: (prev: ActiveSlotState) => ActiveSlotState) => void>();
    refreshActiveSlotInBackground(1, () => false, setter);
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

  it("swallows backend errors", async () => {
    vi.mocked(backend.getSaveStatus).mockRejectedValueOnce(new Error("network"));
    const setter = vi.fn();
    refreshActiveSlotInBackground(1, () => false, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });

  it("falls back to null when active_slot is missing", async () => {
    vi.mocked(backend.getSaveStatus).mockResolvedValueOnce({
      active_slot: null,
    } as unknown as Awaited<ReturnType<typeof backend.getSaveStatus>>);

    const setter = vi.fn<(updater: (prev: ActiveSlotState) => ActiveSlotState) => void>();
    refreshActiveSlotInBackground(1, () => false, setter);
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
    refreshBiosInBackground(1, false, setter);
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

  it("skips the setter when cancelled", async () => {
    vi.mocked(backend.getBiosStatus).mockResolvedValueOnce({
      bios_status: { available_cores: [] },
      bios_level: "ok",
      bios_label: "ok",
    } as unknown as Awaited<ReturnType<typeof backend.getBiosStatus>>);
    const setter = vi.fn();
    refreshBiosInBackground(1, true, setter);
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
    refreshBiosInBackground(1, false, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });

  it("logs but swallows errors", async () => {
    vi.mocked(backend.getBiosStatus).mockRejectedValueOnce(new Error("network"));
    const setter = vi.fn();
    refreshBiosInBackground(1, false, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
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
    refreshAchievementsInBackground(1, () => false, setter);
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

  it("skips the setter when cancelled", async () => {
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

  it("logs but swallows errors", async () => {
    vi.mocked(backend.getAchievementProgress).mockRejectedValueOnce(new Error("network"));
    const setter = vi.fn();
    refreshAchievementsInBackground(1, () => false, setter);
    await flushMicrotasks();
    expect(setter).not.toHaveBeenCalled();
  });
});
