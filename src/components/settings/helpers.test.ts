import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { formatRelativeTime, sortLabel } from "./helpers";

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
    expect(sortLabel({ sort_by_content: true, sort_by_core: true })).toBe("Sort by content: ON, Sort by core: ON");
  });

  it("formats both OFF", () => {
    expect(sortLabel({ sort_by_content: false, sort_by_core: false })).toBe("Sort by content: OFF, Sort by core: OFF");
  });

  it("formats content ON, core OFF (RetroDECK default)", () => {
    expect(sortLabel({ sort_by_content: true, sort_by_core: false })).toBe("Sort by content: ON, Sort by core: OFF");
  });

  it("formats content OFF, core ON", () => {
    expect(sortLabel({ sort_by_content: false, sort_by_core: true })).toBe("Sort by content: OFF, Sort by core: ON");
  });
});
