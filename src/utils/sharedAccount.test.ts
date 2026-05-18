import { describe, it, expect } from "vitest";
import { isSharedAccount, SHARED_ACCOUNT_NAMES } from "./sharedAccount";

describe("isSharedAccount", () => {
  it("matches every well-known shared-account name", () => {
    for (const name of SHARED_ACCOUNT_NAMES) {
      expect(isSharedAccount(name)).toBe(true);
    }
  });

  it("is case-insensitive", () => {
    expect(isSharedAccount("ADMIN")).toBe(true);
    expect(isSharedAccount("Admin")).toBe(true);
    expect(isSharedAccount("RoMm")).toBe(true);
  });

  it("trims surrounding whitespace before matching", () => {
    expect(isSharedAccount("  admin  ")).toBe(true);
    expect(isSharedAccount("\tguest\n")).toBe(true);
  });

  it("returns false for a non-shared name", () => {
    expect(isSharedAccount("daniel")).toBe(false);
    expect(isSharedAccount("alice")).toBe(false);
  });

  it("returns false for the empty string", () => {
    expect(isSharedAccount("")).toBe(false);
  });
});

describe("SHARED_ACCOUNT_NAMES", () => {
  it("contains the expected canonical shared-account names", () => {
    expect(SHARED_ACCOUNT_NAMES).toEqual(
      new Set(["admin", "romm", "user", "guest", "root"]),
    );
  });
});
