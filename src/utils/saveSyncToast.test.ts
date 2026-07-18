import { describe, it, expect } from "vitest";
import { saveSyncToastBody } from "./saveSyncToast";

describe("saveSyncToastBody", () => {
  it("names uploads when saves only moved up", () => {
    expect(saveSyncToastBody(2, 0)).toBe("Saves uploaded to RomM");
  });

  it("names downloads when saves only moved down", () => {
    expect(saveSyncToastBody(0, 3)).toBe("Saves downloaded from RomM");
  });

  it("names both counts when a run went both ways", () => {
    expect(saveSyncToastBody(1, 2)).toBe("Saves synced with RomM (1 up, 2 down)");
  });

  it("returns null for a no-op run (nothing transferred)", () => {
    expect(saveSyncToastBody(0, 0)).toBeNull();
  });

  it("treats absent counts as zero → null", () => {
    expect(saveSyncToastBody(undefined, undefined)).toBeNull();
  });

  it("treats a missing upload count as zero", () => {
    expect(saveSyncToastBody(undefined, 1)).toBe("Saves downloaded from RomM");
  });
});
