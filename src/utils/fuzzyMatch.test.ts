import { describe, it, expect } from "vitest";
import { fuzzyMatch } from "./fuzzyMatch";

describe("fuzzyMatch", () => {
  it("matches an in-order subsequence", () => {
    expect(fuzzyMatch("abc", "aXbXc")).toBe(true);
    expect(fuzzyMatch("rpg", "Retro Playable Games")).toBe(true);
  });

  it("matches a contiguous substring", () => {
    expect(fuzzyMatch("game", "My Game List")).toBe(true);
  });

  it("does not match when characters are out of order", () => {
    expect(fuzzyMatch("cba", "abc")).toBe(false);
  });

  it("does not match when a query character is absent", () => {
    expect(fuzzyMatch("xyz", "abc")).toBe(false);
    expect(fuzzyMatch("abcd", "abc")).toBe(false);
  });

  it("is case-insensitive in both directions", () => {
    expect(fuzzyMatch("ABC", "aXbXc")).toBe(true);
    expect(fuzzyMatch("abc", "AXBXC")).toBe(true);
  });

  it("matches everything on an empty query", () => {
    expect(fuzzyMatch("", "anything")).toBe(true);
    expect(fuzzyMatch("", "")).toBe(true);
  });

  it("never matches a non-empty query against an empty target", () => {
    expect(fuzzyMatch("a", "")).toBe(false);
  });
});
