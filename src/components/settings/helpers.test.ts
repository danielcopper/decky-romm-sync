import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  formatRelativeTime,
  isSharedAccount,
  SHARED_ACCOUNT_NAMES,
  sortLabel,
} from "./helpers";

describe("formatRelativeTime", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("returns 'never' for null", () => {
    expect(formatRelativeTime(null)).toBe("never");
  });

  it("returns 'never' for empty string", () => {
    expect(formatRelativeTime("")).toBe("never");
  });

  it("returns 'unknown' for an unparseable timestamp", () => {
    expect(formatRelativeTime("not-a-date")).toBe("unknown");
  });

  it("returns 'just now' for under one minute", () => {
    expect(formatRelativeTime("2025-06-15T11:59:30Z")).toBe("just now");
  });

  it("returns 'Nm ago' for minute granularity", () => {
    expect(formatRelativeTime("2025-06-15T11:30:00Z")).toBe("30m ago");
  });

  it("returns 'Nh ago' for hour granularity", () => {
    expect(formatRelativeTime("2025-06-15T08:00:00Z")).toBe("4h ago");
  });

  it("returns 'D Mon' for older dates", () => {
    // 10 days back puts us in early June; only the day + month tokens matter.
    const out = formatRelativeTime("2025-06-05T12:00:00Z");
    expect(out).toBe("5 Jun");
  });
});

describe("sortLabel", () => {
  it("formats both ON", () => {
    expect(sortLabel({ sort_by_content: true, sort_by_core: true })).toBe(
      "Sort by content: ON, Sort by core: ON",
    );
  });

  it("formats both OFF", () => {
    expect(sortLabel({ sort_by_content: false, sort_by_core: false })).toBe(
      "Sort by content: OFF, Sort by core: OFF",
    );
  });

  it("formats content ON, core OFF (RetroDECK default)", () => {
    expect(sortLabel({ sort_by_content: true, sort_by_core: false })).toBe(
      "Sort by content: ON, Sort by core: OFF",
    );
  });

  it("formats content OFF, core ON", () => {
    expect(sortLabel({ sort_by_content: false, sort_by_core: true })).toBe(
      "Sort by content: OFF, Sort by core: ON",
    );
  });
});

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
