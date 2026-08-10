import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { resolveSaveSyncLabel, applySaveSyncDisplay, extractBiosInfo, extractCoreInfo, timeoutMs } from "./playSection";
import { libretroEmu } from "../test-utils/coreFixtures";
import type { CoreInfo, SaveStatus, SaveSyncDisplay } from "../types";

describe("resolveSaveSyncLabel", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("returns the static label when one is provided", () => {
    const display: SaveSyncDisplay = {
      status: "synced",
      label: "All caught up",
      last_sync_check_at: null,
    };
    expect(resolveSaveSyncLabel(display)).toBe("All caught up");
  });

  it("derives an Xm-ago label from last_sync_check_at when label is null", () => {
    const display: SaveSyncDisplay = {
      status: "synced",
      label: null,
      last_sync_check_at: "2025-06-15T11:45:00Z",
    };
    expect(resolveSaveSyncLabel(display)).toBe("15m ago");
  });

  it("falls back to 'Not synced' when both label and last_sync_check_at are absent", () => {
    const display: SaveSyncDisplay = {
      status: "none",
      label: null,
      last_sync_check_at: null,
    };
    expect(resolveSaveSyncLabel(display)).toBe("Not synced");
  });

  it("falls back to 'Not synced' when last_sync_check_at is unparseable", () => {
    const display: SaveSyncDisplay = {
      status: "synced",
      label: null,
      last_sync_check_at: "not-a-date",
    };
    expect(resolveSaveSyncLabel(display)).toBe("Not synced");
  });
});

describe("applySaveSyncDisplay", () => {
  it("uses the typed display payload when provided", () => {
    const display: SaveSyncDisplay = {
      status: "synced",
      label: "Synced 2h ago",
      last_sync_check_at: null,
    };
    expect(applySaveSyncDisplay(display, null)).toEqual({
      status: "synced",
      label: "Synced 2h ago",
    });
  });

  it("falls back to conflict when no display but SaveStatus has conflicts", () => {
    const saveStatus = {
      conflicts: [{ filename: "foo.sav" }],
    } as unknown as SaveStatus;
    expect(applySaveSyncDisplay(undefined, saveStatus)).toEqual({
      status: "conflict",
      label: "Conflict",
    });
  });

  it("falls back to 'No saves' when no display and no conflicts", () => {
    expect(applySaveSyncDisplay(undefined, null)).toEqual({
      status: "none",
      label: "No saves",
    });
  });
});

describe("extractBiosInfo", () => {
  // Only the PRESENCE of this object is read, never its contents — but it is
  // typed as the real payload, so it has to be one.
  const requirement = { platform_slug: "snes", server_count: 3, local_count: 3, all_downloaded: true };

  it("projects the pre-computed level/label into the BIOS-only play-section fields (no core fields, #923)", () => {
    const result = extractBiosInfo({ bios_status: requirement, bios_level: "ok", bios_label: "BIOS OK" });
    expect(result).not.toBeNull();
    expect(result!.biosNeeded).toBe(true);
    expect(result!.biosStatus).toBe("ok");
    expect(result!.biosLabel).toBe("BIOS OK");
    // Core fields no longer ride the BIOS payload — they come from extractCoreInfo.
    expect(result).not.toHaveProperty("activeCoreLabel");
    expect(result).not.toHaveProperty("availableCores");
  });

  it("coerces null label to empty string", () => {
    const result = extractBiosInfo({ bios_status: requirement, bios_level: null, bios_label: null });
    expect(result!.biosLabel).toBe("");
    expect(result!.biosStatus).toBeNull();
  });

  it("reports the cleared shape when the answer carries no requirement (#1690)", () => {
    expect(extractBiosInfo({ bios_status: null, bios_level: null, bios_label: null })).toEqual({
      biosNeeded: false,
      biosStatus: null,
      biosLabel: "",
    });
  });

  it("ignores a level and label left over next to an absent requirement (#1690)", () => {
    // The three fields move together — a cleared requirement never leaves a
    // level or label behind for the row to render against nothing.
    expect(extractBiosInfo({ bios_level: "missing", bios_label: "0/3" })).toEqual({
      biosNeeded: false,
      biosStatus: null,
      biosLabel: "",
    });
  });

  it("returns null when the payload carries no BIOS answer (#1693)", () => {
    // The same absent requirement as the clear above — the flag is the only
    // thing separating them, and it is what stops a check that could not answer
    // from taking a shown requirement off the page.
    expect(
      extractBiosInfo({ bios_status: null, bios_level: null, bios_label: null, bios_status_unknown: true }),
    ).toBeNull();
  });

  it("refuses an unknown payload whatever level and label ride along (#1693)", () => {
    expect(
      extractBiosInfo({ bios_status: null, bios_level: "missing", bios_label: "0/3", bios_status_unknown: true }),
    ).toBeNull();
  });

  it("treats an explicit bios_status_unknown: false as the answer it is", () => {
    expect(
      extractBiosInfo({ bios_status: null, bios_level: null, bios_label: null, bios_status_unknown: false }),
    ).toEqual({ biosNeeded: false, biosStatus: null, biosLabel: "" });
  });
});

describe("extractCoreInfo", () => {
  const baseCoreInfo: CoreInfo = {
    active_core: "mupen64plus_next_libretro.so",
    active_core_label: "Mupen64Plus-Next",
    platform_core_label: null,
    has_game_override: false,
    emulator_data_available: true,
    emulators: [
      libretroEmu("mupen64plus_next_libretro.so", "Mupen64Plus-Next", true),
      libretroEmu("parallel_n64_libretro.so", "ParaLLEl N64"),
    ],
  };

  it("projects CoreInfo into the core-selection play-section fields", () => {
    const result = extractCoreInfo(baseCoreInfo);
    expect(result.activeCoreLabel).toBe("Mupen64Plus-Next");
    expect(result.activeCoreIsDefault).toBe(true);
    expect(result.emulators).toHaveLength(2);
    expect(result.emulatorDataAvailable).toBe(true);
    expect(result.platformCoreLabel).toBeNull();
    expect(result.hasGameOverride).toBe(false);
  });

  it("maps has_game_override=true through to hasGameOverride (#211)", () => {
    const result = extractCoreInfo({ ...baseCoreInfo, has_game_override: true });
    expect(result.hasGameOverride).toBe(true);
  });

  it("maps has_game_override=false through to hasGameOverride (#211)", () => {
    const result = extractCoreInfo({ ...baseCoreInfo, has_game_override: false });
    expect(result.hasGameOverride).toBe(false);
  });

  it("marks activeCoreIsDefault=false when active core differs from default", () => {
    const result = extractCoreInfo({ ...baseCoreInfo, active_core_label: "ParaLLEl N64" });
    expect(result.activeCoreIsDefault).toBe(false);
  });

  it("marks activeCoreIsDefault=true when no active core is set", () => {
    const result = extractCoreInfo({ ...baseCoreInfo, active_core: null, active_core_label: null });
    expect(result.activeCoreIsDefault).toBe(true);
    expect(result.activeCoreLabel).toBeNull();
  });

  it("maps a non-null platform_core_label through to platformCoreLabel (#954)", () => {
    const result = extractCoreInfo({ ...baseCoreInfo, platform_core_label: "ParaLLEl N64" });
    expect(result.platformCoreLabel).toBe("ParaLLEl N64");
  });

  it("maps a null platform_core_label through to null platformCoreLabel (#954)", () => {
    const result = extractCoreInfo({ ...baseCoreInfo, platform_core_label: null });
    expect(result.platformCoreLabel).toBeNull();
  });

  it("defaults emulators to [] when the list is empty", () => {
    const result = extractCoreInfo({
      active_core: null,
      active_core_label: null,
      platform_core_label: null,
      has_game_override: false,
      emulator_data_available: false,
      emulators: [],
    });
    expect(result.emulators).toEqual([]);
    expect(result.emulatorDataAvailable).toBe(false);
  });
});

describe("timeoutMs", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("rejects with 'timeout' after the configured delay", async () => {
    const promise = timeoutMs(500);
    const assertion = expect(promise).rejects.toThrow("timeout");
    vi.advanceTimersByTime(500);
    await assertion;
  });

  it("loses Promise.race against a faster resolver", async () => {
    const fast = new Promise<string>((resolve) => setTimeout(() => resolve("ok"), 100));
    const race = Promise.race([fast, timeoutMs(500)]);
    vi.advanceTimersByTime(100);
    await expect(race).resolves.toBe("ok");
  });
});
