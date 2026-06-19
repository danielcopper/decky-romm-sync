import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  computeSyncSummary,
  displaySlot,
  formatRelativeTime,
  pickLastSyncer,
  attributionLabel,
  formatAttributionSegment,
  slotDeleteFailureToast,
  statusLabel,
} from "./helpers";
import type { DeviceSyncInfo, SaveStatus, SyncConflict, SlotDeleteInfo } from "../../types";

describe("displaySlot", () => {
  it("returns '(no slot)' for null", () => {
    expect(displaySlot(null)).toBe("Legacy (no slot)");
  });

  it("returns '(no slot)' for undefined", () => {
    expect(displaySlot(undefined)).toBe("Legacy (no slot)");
  });

  it("returns '(no slot)' for empty string", () => {
    expect(displaySlot("")).toBe("Legacy (no slot)");
  });

  it("returns the slot name as-is for non-empty input", () => {
    expect(displaySlot("speedrun")).toBe("speedrun");
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

  it("returns grey 'Status unknown' for status 'unknown'", () => {
    expect(statusLabel("unknown", null)).toEqual({ color: "#8f98a0", label: "Status unknown" });
  });

  it("returns grey 'Status unknown' for status 'unknown' even when lastSyncAt is set", () => {
    expect(statusLabel("unknown", "2025-06-15T10:00:00Z")).toEqual({ color: "#8f98a0", label: "Status unknown" });
  });

  it("defaults to green 'Synced' when status is unknown but lastSyncAt is set", () => {
    expect(statusLabel("weird", "2025-06-15T10:00:00Z")).toEqual({ color: "#5ba32b", label: "Synced" });
  });

  it("defaults to grey 'Not synced' when status is unknown and lastSyncAt is null", () => {
    expect(statusLabel("weird", null)).toEqual({ color: "#8f98a0", label: "Not synced" });
  });
});

describe("slotDeleteFailureToast", () => {
  it("explains the active-slot guard when reason='active_slot'", () => {
    const info: SlotDeleteInfo = { success: false, reason: "active_slot" };
    expect(slotDeleteFailureToast(info)).toBe("Cannot delete the active slot. Switch to a different slot first.");
  });

  it("explains the active-slot guard when is_active flag is set", () => {
    const info: SlotDeleteInfo = { success: false, is_active: true };
    expect(slotDeleteFailureToast(info)).toBe("Cannot delete the active slot. Switch to a different slot first.");
  });

  it("surfaces the server-unreachable warning when reason='server_unreachable'", () => {
    // Regression for #626: without this branch the modal opens and the user
    // confirms a destructive delete based on stale/empty data.
    const info: SlotDeleteInfo = {
      success: false,
      reason: "server_unreachable",
      message: "Cannot inspect slot — server unreachable",
    };
    expect(slotDeleteFailureToast(info)).toBe("Cannot inspect slot — server unreachable");
  });

  it("falls back to a generic server-unreachable message when no message is provided", () => {
    const info: SlotDeleteInfo = { success: false, reason: "server_unreachable" };
    expect(slotDeleteFailureToast(info)).toBe("Cannot inspect slot — RomM server is not reachable");
  });

  it("surfaces the backend message for unrecognised failure reasons", () => {
    const info: SlotDeleteInfo = { success: false, reason: "weird", message: "Custom failure" };
    expect(slotDeleteFailureToast(info)).toBe("Custom failure");
  });

  it("falls back to a generic message when no message and no special reason", () => {
    const info: SlotDeleteInfo = { success: false };
    expect(slotDeleteFailureToast(info)).toBe("Cannot delete this slot");
  });
});

describe("computeSyncSummary", () => {
  const makeStatus = (overrides: Partial<SaveStatus> = {}): SaveStatus => ({
    rom_id: 1,
    files: [],
    playtime: {
      total_seconds: 0,
      session_count: 0,
      last_session_start: null,
      last_session_duration_sec: null,
    },
    device_id: "dev",
    last_sync_check_at: null,
    ...overrides,
  });

  it("returns null text for inactive slot", () => {
    expect(computeSyncSummary(false, makeStatus(), [])).toEqual({
      syncSummaryText: null,
      syncSummaryColor: "#8f98a0",
    });
  });

  it("returns null text when saveStatus is null", () => {
    expect(computeSyncSummary(true, null, [])).toEqual({
      syncSummaryText: null,
      syncSummaryColor: "#8f98a0",
    });
  });

  it("short-circuits to 'Server unreachable' when server_query_failed is true", () => {
    const status = makeStatus({
      server_query_failed: true,
      files: [
        {
          filename: "save.srm",
          local_path: "/local/save.srm",
          local_hash: "abc",
          local_mtime: "2025-06-15T10:00:00Z",
          local_size: 100,
          server_save_id: null,
          server_file_name: null,
          server_emulator: null,
          server_updated_at: null,
          server_size: null,
          last_sync_at: null,
          status: "unknown",
        },
      ],
    });
    expect(computeSyncSummary(true, status, [])).toEqual({
      syncSummaryText: "Server unreachable",
      syncSummaryColor: "#8f98a0",
    });
  });

  it("server_query_failed wins over conflicts", () => {
    const status = makeStatus({ server_query_failed: true });
    const conflicts: SyncConflict[] = [
      {
        type: "sync_conflict",
        rom_id: 1,
        filename: "save.srm",
        server_save_id: 1,
        server_updated_at: "2025-06-15T10:00:00Z",
        server_size: 100,
        local_path: null,
        local_hash: null,
        local_mtime: null,
        local_size: null,
        created_at: "2025-06-15T10:00:00Z",
      },
    ];
    expect(computeSyncSummary(true, status, conflicts)).toEqual({
      syncSummaryText: "Server unreachable",
      syncSummaryColor: "#8f98a0",
    });
  });

  it("returns 'Conflict detected' (red) when conflicts present", () => {
    const status = makeStatus({ files: [] });
    const conflicts: SyncConflict[] = [
      {
        type: "sync_conflict",
        rom_id: 1,
        filename: "save.srm",
        server_save_id: 1,
        server_updated_at: "2025-06-15T10:00:00Z",
        server_size: 100,
        local_path: null,
        local_hash: null,
        local_mtime: null,
        local_size: null,
        created_at: "2025-06-15T10:00:00Z",
      },
    ];
    expect(computeSyncSummary(true, status, conflicts)).toEqual({
      syncSummaryText: "Conflict detected",
      syncSummaryColor: "#d94126",
    });
  });

  it("returns 'No saves found' when fileCount is 0 and no conflicts", () => {
    expect(computeSyncSummary(true, makeStatus(), [])).toEqual({
      syncSummaryText: "No saves found",
      syncSummaryColor: "#8f98a0",
    });
  });

  it("returns 'Not synced' when files exist but last_sync_check_at is null", () => {
    const status = makeStatus({
      files: [
        {
          filename: "save.srm",
          local_path: "/local/save.srm",
          local_hash: "abc",
          local_mtime: "2025-06-15T10:00:00Z",
          local_size: 100,
          server_save_id: null,
          server_file_name: null,
          server_emulator: null,
          server_updated_at: null,
          server_size: null,
          last_sync_at: null,
          status: "upload",
        },
      ],
    });
    expect(computeSyncSummary(true, status, [])).toEqual({
      syncSummaryText: "Not synced",
      syncSummaryColor: "#8f98a0",
    });
  });

  it("returns 'Synced just now' when relative time is 'just now'", () => {
    const nowIso = new Date().toISOString();
    const status = makeStatus({
      last_sync_check_at: nowIso,
      files: [
        {
          filename: "save.srm",
          local_path: "/local/save.srm",
          local_hash: "abc",
          local_mtime: nowIso,
          local_size: 100,
          server_save_id: null,
          server_file_name: null,
          server_emulator: null,
          server_updated_at: null,
          server_size: null,
          last_sync_at: null,
          status: "synced",
        },
      ],
    });
    expect(computeSyncSummary(true, status, [])).toEqual({
      syncSummaryText: "Synced just now",
      syncSummaryColor: "#5ba32b",
    });
  });

  it("returns 'Synced <rel>' with relative time when last_sync_check_at is older", () => {
    const oldIso = new Date(Date.now() - 30 * 60 * 1000).toISOString(); // 30 min ago
    const status = makeStatus({
      last_sync_check_at: oldIso,
      files: [
        {
          filename: "save.srm",
          local_path: "/local/save.srm",
          local_hash: "abc",
          local_mtime: oldIso,
          local_size: 100,
          server_save_id: null,
          server_file_name: null,
          server_emulator: null,
          server_updated_at: null,
          server_size: null,
          last_sync_at: null,
          status: "synced",
        },
      ],
    });
    const result = computeSyncSummary(true, status, []);
    expect(result.syncSummaryColor).toBe("#5ba32b");
    expect(result.syncSummaryText).toMatch(/^Synced \d+m ago$/);
  });
});
