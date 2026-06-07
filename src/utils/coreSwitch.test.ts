import { describe, it, expect } from "vitest";
import { coreSwitchMayBeIgnored } from "./coreSwitch";

describe("coreSwitchMayBeIgnored", () => {
  it("returns false for a clean filename", () => {
    expect(coreSwitchMayBeIgnored("Tetris.gb")).toBe(false);
  });

  it("returns false for a clean filename with spaces and dashes", () => {
    expect(coreSwitchMayBeIgnored("Mario Golf - Advance Tour.zip")).toBe(false);
  });

  it("returns true for a filename with parentheses", () => {
    expect(coreSwitchMayBeIgnored("Mario Golf - Advance Tour (USA).zip")).toBe(true);
  });

  it("returns true for a filename with square brackets", () => {
    expect(coreSwitchMayBeIgnored("Sonic [!].md")).toBe(true);
  });

  it.each([
    ["paren open", "Game (.gb"],
    ["paren close", "Game ).gb"],
    ["bracket open", "Game [.gb"],
    ["bracket close", "Game ].gb"],
    ["brace open", "Game {.gb"],
    ["brace close", "Game }.gb"],
    ["plus", "Game+.gb"],
    ["star", "Game*.gb"],
    ["question", "Game?.gb"],
    ["pipe", "Game|.gb"],
    ["caret", "Game^.gb"],
    ["dollar", "Game$.gb"],
    ["backslash", "Game\\.gb"],
  ])("returns true for a filename containing a %s metacharacter", (_label, name) => {
    expect(coreSwitchMayBeIgnored(name)).toBe(true);
  });

  it("returns false when only a dot is present (regex dot matches literal dot)", () => {
    expect(coreSwitchMayBeIgnored("Pokemon.Red.gba")).toBe(false);
  });

  it("returns false for an empty string", () => {
    expect(coreSwitchMayBeIgnored("")).toBe(false);
  });
});
