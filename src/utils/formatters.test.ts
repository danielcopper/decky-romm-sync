import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { formatTimestamp, formatTimeAgo } from "./formatters";

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
