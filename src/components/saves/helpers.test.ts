import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  displaySlot,
  formatBytes,
  formatRelativeTime,
  pickLastSyncer,
  attributionLabel,
  formatAttributionSegment,
  statusLabel,
} from "./helpers";
import type { DeviceSyncInfo } from "../../types";

describe("displaySlot", () => {
  it("returns '(no slot)' for null", () => {
    expect(displaySlot(null)).toBe("(no slot)");
  });

  it("returns '(no slot)' for undefined", () => {
    expect(displaySlot(undefined)).toBe("(no slot)");
  });

  it("returns '(no slot)' for empty string", () => {
    expect(displaySlot("")).toBe("(no slot)");
  });

  it("returns the slot name as-is for non-empty input", () => {
    expect(displaySlot("speedrun")).toBe("speedrun");
  });
});

describe("formatBytes", () => {
  it("returns empty string for null", () => {
    expect(formatBytes(null)).toBe("");
  });

  it("formats values under 1 KB as 'N B'", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });

  it("formats values under 1 MB as 'N.N KB'", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(1024 * 1024 - 1)).toBe("1024.0 KB");
  });

  it("formats values 1 MB and up as 'N.N MB'", () => {
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("formatRelativeTime", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-06-15T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("returns empty string for null", () => {
    expect(formatRelativeTime(null)).toBe("");
  });

  it("returns empty string for empty input", () => {
    expect(formatRelativeTime("")).toBe("");
  });

  it("returns empty string for an unparseable timestamp", () => {
    expect(formatRelativeTime("not-a-date")).toBe("");
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

describe("pickLastSyncer", () => {
  it("returns null for undefined", () => {
    expect(pickLastSyncer(undefined)).toBeNull();
  });

  it("returns null for empty array", () => {
    expect(pickLastSyncer([])).toBeNull();
  });

  it("returns the only entry when array has one element", () => {
    const ds: DeviceSyncInfo = { device_name: "deck", last_synced_at: "2025-06-15T10:00:00Z" } as DeviceSyncInfo;
    expect(pickLastSyncer([ds])).toBe(ds);
  });

  it("returns the entry with the latest last_synced_at", () => {
    const a: DeviceSyncInfo = { device_name: "older", last_synced_at: "2025-06-10T10:00:00Z" } as DeviceSyncInfo;
    const b: DeviceSyncInfo = { device_name: "newer", last_synced_at: "2025-06-15T10:00:00Z" } as DeviceSyncInfo;
    expect(pickLastSyncer([a, b])).toBe(b);
    expect(pickLastSyncer([b, a])).toBe(b);
  });

  it("ignores entries with null last_synced_at when picking the latest", () => {
    const withTime: DeviceSyncInfo = { device_name: "real", last_synced_at: "2025-06-15T10:00:00Z" } as DeviceSyncInfo;
    const noTime: DeviceSyncInfo = { device_name: "ghost", last_synced_at: null } as DeviceSyncInfo;
    expect(pickLastSyncer([withTime, noTime])).toBe(withTime);
    expect(pickLastSyncer([noTime, withTime])).toBe(withTime);
  });

  it("falls back to first entry when all entries have null last_synced_at", () => {
    const a: DeviceSyncInfo = { device_name: "a", last_synced_at: null } as DeviceSyncInfo;
    const b: DeviceSyncInfo = { device_name: "b", last_synced_at: null } as DeviceSyncInfo;
    expect(pickLastSyncer([a, b])).toBe(a);
  });
});

describe("attributionLabel", () => {
  it("returns '(this device)' for true", () => {
    expect(attributionLabel(true)).toBe("(this device)");
  });

  it("returns '(not this device)' for false", () => {
    expect(attributionLabel(false)).toBe("(not this device)");
  });

  it("returns null for null", () => {
    expect(attributionLabel(null)).toBeNull();
  });

  it("returns null for undefined", () => {
    expect(attributionLabel(undefined)).toBeNull();
  });
});

describe("formatAttributionSegment", () => {
  it("combines device name and '(this device)' label when uploadedByUs is true", () => {
    expect(formatAttributionSegment(true, "deck")).toBe("deck (this device) ✓");
  });

  it("returns the '(this device)' label alone when uploadedByUs is true and no device name", () => {
    expect(formatAttributionSegment(true, null)).toBe("(this device) ✓");
  });

  it("returns '(not this device)' alone when uploadedByUs is false, even when a device name is provided", () => {
    // The function intentionally drops the device name in the "false" branch:
    // lastSyncer is our own sync record, not the actual uploader.
    expect(formatAttributionSegment(false, "deck")).toBe("(not this device) ✓");
    expect(formatAttributionSegment(false, null)).toBe("(not this device) ✓");
  });

  it("returns 'device ✓' when uploadedByUs is unknown and a device name is provided", () => {
    expect(formatAttributionSegment(null, "deck")).toBe("deck ✓");
    expect(formatAttributionSegment(undefined, "deck")).toBe("deck ✓");
  });

  it("returns just '✓' when uploadedByUs is unknown and no device name is provided", () => {
    // Documents current behavior: the lone checkmark falls through the
    // label === null branch, so callers always get a non-null string —
    // never null — when uploadedByUs is unknown.
    expect(formatAttributionSegment(null, null)).toBe("✓");
    expect(formatAttributionSegment(undefined, undefined)).toBe("✓");
  });
});

describe("statusLabel", () => {
  it("returns green 'Synced' for status 'synced'", () => {
    expect(statusLabel("synced", null)).toEqual({ color: "#5ba32b", label: "Synced" });
  });

  it("returns green 'Synced' for status 'skip'", () => {
    expect(statusLabel("skip", null)).toEqual({ color: "#5ba32b", label: "Synced" });
  });

  it("returns yellow 'Local changes' for status 'upload'", () => {
    expect(statusLabel("upload", null)).toEqual({ color: "#d4a72c", label: "Local changes" });
  });

  it("returns blue 'Server newer' for status 'download'", () => {
    expect(statusLabel("download", null)).toEqual({ color: "#1a9fff", label: "Server newer" });
  });

  it("returns red 'Conflict' for status 'conflict'", () => {
    expect(statusLabel("conflict", null)).toEqual({ color: "#d94126", label: "Conflict" });
  });

  it("defaults to green 'Synced' when status is unknown but lastSyncAt is set", () => {
    expect(statusLabel("weird", "2025-06-15T10:00:00Z")).toEqual({ color: "#5ba32b", label: "Synced" });
  });

  it("defaults to grey 'Not synced' when status is unknown and lastSyncAt is null", () => {
    expect(statusLabel("weird", null)).toEqual({ color: "#8f98a0", label: "Not synced" });
  });
});
