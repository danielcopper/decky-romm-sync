import { describe, it, expect } from "vitest";
import { biosColorForLevel } from "./biosColor";

describe("biosColorForLevel", () => {
  it("maps 'ok' to green", () => {
    expect(biosColorForLevel("ok")).toBe("#5ba32b");
  });

  it("maps 'partial' to amber", () => {
    expect(biosColorForLevel("partial")).toBe("#d4a72c");
  });

  it("maps 'missing' to red", () => {
    expect(biosColorForLevel("missing")).toBe("#d94126");
  });

  it("maps null (no level data) to neutral grey", () => {
    expect(biosColorForLevel(null)).toBe("#8f98a0");
  });
});
