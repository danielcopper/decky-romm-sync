import { describe, it, expect, beforeEach, vi } from "vitest";
import { runLaunchGate, markLaunchSkipped, consumeLaunchSkip } from "./launchGate";
import type { LaunchGateOps, PreLaunchSyncOutcome } from "./launchGate";
import type { SyncConflict } from "../types";

function conflict(overrides: Partial<SyncConflict> = {}): SyncConflict {
  return {
    type: "sync_conflict",
    rom_id: 42,
    filename: "save.srm",
    server_save_id: 7,
    server_updated_at: "2026-01-01T00:00:00Z",
    server_size: 1024,
    local_path: "/local/save.srm",
    local_hash: "abc",
    local_mtime: "2026-01-01T00:00:00Z",
    local_size: 1024,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

// All-pass ops: migration not pending, tracking proceeds, core OK, online,
// sync succeeds with no conflicts. Each test overrides only the step it drives.
function makeOps(overrides: Partial<LaunchGateOps> = {}): LaunchGateOps {
  const okSync: PreLaunchSyncOutcome = { success: true, message: "" };
  return {
    migrationPending: vi.fn(() => false),
    ensureTrackingConfigured: vi.fn(async (): Promise<"proceed" | "abort"> => "proceed"),
    checkCoreChange: vi.fn(async () => true),
    checkReachability: vi.fn(async () => true),
    preLaunchSync: vi.fn(async () => okSync),
    checkLocalDrift: vi.fn(async () => false),
    ...overrides,
  };
}

describe("runLaunchGate — verdict branches", () => {
  it("blocks with migration_pending when a migration is pending", async () => {
    const ops = makeOps({ migrationPending: () => true });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({
      decision: "block",
      reason: "migration_pending",
    });
    // Later steps must not run once migration blocks.
    expect(ops.ensureTrackingConfigured).not.toHaveBeenCalled();
    expect(ops.checkReachability).not.toHaveBeenCalled();
  });

  it("aborts when tracking setup returns abort", async () => {
    const ops = makeOps({ ensureTrackingConfigured: vi.fn(async (): Promise<"proceed" | "abort"> => "abort") });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "abort" });
    expect(ops.checkCoreChange).not.toHaveBeenCalled();
    expect(ops.checkReachability).not.toHaveBeenCalled();
  });

  it("aborts when the core-change confirm is cancelled", async () => {
    const ops = makeOps({ checkCoreChange: vi.fn(async () => false) });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "abort" });
    expect(ops.checkReachability).not.toHaveBeenCalled();
  });

  it("returns conflict when online pre-launch sync surfaces conflicts", async () => {
    const conflicts = [conflict()];
    const ops = makeOps({
      checkReachability: vi.fn(async () => true),
      preLaunchSync: vi.fn(async () => ({ success: false, message: "conflict", conflicts })),
    });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "conflict", conflicts });
    // Online branch: drift check is never consulted.
    expect(ops.checkLocalDrift).not.toHaveBeenCalled();
  });

  it("returns sync_failed (with message) when online sync fails without conflicts", async () => {
    const ops = makeOps({
      checkReachability: vi.fn(async () => true),
      preLaunchSync: vi.fn(async () => ({ success: false, message: "device not registered" })),
    });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({
      decision: "sync_failed",
      message: "device not registered",
    });
  });

  it("allows when online sync succeeds with no conflicts", async () => {
    const ops = makeOps({
      checkReachability: vi.fn(async () => true),
      preLaunchSync: vi.fn(async () => ({ success: true, message: "synced" })),
    });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "allow" });
    expect(ops.checkLocalDrift).not.toHaveBeenCalled();
  });

  it("returns offline_drift when offline and the local save has drifted", async () => {
    const ops = makeOps({
      checkReachability: vi.fn(async () => false),
      checkLocalDrift: vi.fn(async () => true),
    });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "offline_drift" });
    // Offline branch: pre-launch sync is never attempted.
    expect(ops.preLaunchSync).not.toHaveBeenCalled();
  });

  it("allows when offline and the local save has not drifted", async () => {
    const ops = makeOps({
      checkReachability: vi.fn(async () => false),
      checkLocalDrift: vi.fn(async () => false),
    });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "allow" });
    expect(ops.preLaunchSync).not.toHaveBeenCalled();
  });

  it("never throws — an injected callback that throws resolves to allow", async () => {
    const ops = makeOps({
      // A bug in a gate step must not trap the user's game.
      checkReachability: vi.fn(async () => {
        throw new Error("probe blew up");
      }),
    });
    // Observable allow (not just "didn't throw") — non-vacuous.
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "allow" });
  });

  it("never throws — a synchronous migrationPending throw resolves to allow", async () => {
    const ops = makeOps({
      migrationPending: () => {
        throw new Error("migration state read blew up");
      },
    });
    await expect(runLaunchGate(100, 42, ops)).resolves.toEqual({ decision: "allow" });
  });
});

describe("skip-set — markLaunchSkipped / consumeLaunchSkip", () => {
  // Module-level set: clear any residue between tests by consuming the ids used.
  beforeEach(() => {
    consumeLaunchSkip(555);
    consumeLaunchSkip(777);
  });

  it("consumeLaunchSkip returns true once after marking, then false (one-shot)", () => {
    markLaunchSkipped(555);
    expect(consumeLaunchSkip(555)).toBe(true);
    // Mark is consumed — a second read is false.
    expect(consumeLaunchSkip(555)).toBe(false);
  });

  it("consumeLaunchSkip returns false for an unmarked id", () => {
    expect(consumeLaunchSkip(777)).toBe(false);
  });
});
