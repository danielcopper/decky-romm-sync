import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import {
  formatLastPlayed,
  formatPlaytime,
  formatTimestamp,
  formatTimeAgo,
} from "./formatters";

describe("formatTimestamp", () => {
  it("returns 'unknown' for null", () => {
    expect(formatTimestamp(null)).toBe("unknown");
  });

  it("formats a valid ISO timestamp as a locale string", () => {
    const out = formatTimestamp("2025-06-15T12:34:56Z");
    expect(out).toContain("Jun");
    expect(out).not.toBe("unknown");
  });

  it("returns the original string when Date construction succeeds but produces Invalid Date", () => {
    // toLocaleString on an Invalid Date returns "Invalid Date" — no throw, no
    // fallback. This documents that branch; if behavior changes we want to know.
    const out = formatTimestamp("not-a-date");
    expect(out).toBe("Invalid Date");
  });
});

describe("formatTimeAgo", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns null for an unparseable string", () => {
    expect(formatTimeAgo("nope")).toBeNull();
  });

  it("returns 'Just now' for timestamps less than a minute old", () => {
    expect(formatTimeAgo("2025-06-15T11:59:30Z")).toBe("Just now");
  });

  it("returns Xm ago for minutes", () => {
    expect(formatTimeAgo("2025-06-15T11:45:00Z")).toBe("15m ago");
  });

  it("returns Xh ago for hours", () => {
    expect(formatTimeAgo("2025-06-15T08:00:00Z")).toBe("4h ago");
  });

  it("returns Xd ago for days", () => {
    expect(formatTimeAgo("2025-06-12T12:00:00Z")).toBe("3d ago");
  });
});

describe("formatLastPlayed", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("returns 'Never' for 0 or negative", () => {
    expect(formatLastPlayed(0)).toBe("Never");
    expect(formatLastPlayed(-1)).toBe("Never");
  });

  it("returns 'Today' for today's timestamp", () => {
    const todayMidday = Math.floor(new Date("2025-06-15T10:00:00Z").getTime() / 1000);
    expect(formatLastPlayed(todayMidday)).toBe("Today");
  });

  it("returns 'Yesterday' for one day ago", () => {
    const yesterday = Math.floor(new Date("2025-06-14T10:00:00Z").getTime() / 1000);
    expect(formatLastPlayed(yesterday)).toBe("Yesterday");
  });

  it("returns 'N days ago' for under a week", () => {
    const threeDaysAgo = Math.floor(new Date("2025-06-12T10:00:00Z").getTime() / 1000);
    expect(formatLastPlayed(threeDaysAgo)).toBe("3 days ago");
  });

  it("returns 'DD. Mon.' for same-year dates older than a week", () => {
    const twoMonthsAgo = Math.floor(new Date("2025-04-10T10:00:00Z").getTime() / 1000);
    expect(formatLastPlayed(twoMonthsAgo)).toBe("10. Apr.");
  });

  it("returns 'DD. Mon. YYYY' for prior-year dates", () => {
    const lastYear = Math.floor(new Date("2024-08-20T10:00:00Z").getTime() / 1000);
    expect(formatLastPlayed(lastYear)).toBe("20. Aug. 2024");
  });
});

describe("formatPlaytime", () => {
  it("returns 'None' for 0 or negative", () => {
    expect(formatPlaytime(0)).toBe("None");
    expect(formatPlaytime(-5)).toBe("None");
  });

  it("returns 'N Min' under an hour", () => {
    expect(formatPlaytime(42)).toBe("42 Min");
  });

  it("returns '1 Hour' singular at exactly 60 minutes", () => {
    expect(formatPlaytime(60)).toBe("1 Hour");
  });

  it("returns 'N Hours' plural for whole multiples", () => {
    expect(formatPlaytime(120)).toBe("2 Hours");
  });

  it("returns 'Nh Mm' for non-whole hours", () => {
    expect(formatPlaytime(125)).toBe("2h 5m");
  });
});
