import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  logError,
  releaseOrphanedPruneLeases,
  releasePruneConflictLease,
  renewPruneConflictLease,
} from "../api/backend";
import {
  capturePruneLeaseAdmission,
  isPruneLeaseCancellation,
  maintainPruneLease,
  mountPruneLeaseOwner,
  mountPruneLeasePlugin,
  PruneLeaseAdmissionCancelled,
  releaseAllPruneLeases,
  releasePruneLease,
  releasePruneLeasesByOwner,
  withPruneLease,
} from "./pruneLease";

vi.mock("../api/backend", () => ({
  logError: vi.fn(),
  releaseOrphanedPruneLeases: vi.fn(() => Promise.resolve({ success: true, released: 0 })),
  releasePruneConflictLease: vi.fn(),
  renewPruneConflictLease: vi.fn(),
}));

beforeEach(() => {
  mountPruneLeasePlugin();
});

afterEach(async () => {
  await releaseAllPruneLeases();
  vi.useRealTimers();
  vi.clearAllMocks();
});

it("reads an ordinary failure on a live owner as a real failure, not a cancellation", () => {
  mountPruneLeaseOwner("game-detail:1");
  const admission = capturePruneLeaseAdmission("game-detail:1");

  // The single predicate every surfacing catch shares: while the owner is alive,
  // an error means the operation failed and MUST reach the user.
  expect(isPruneLeaseCancellation(new Error("io"), admission)).toBe(false);
  // The explicit refusal is a cancellation even before any teardown is observed.
  expect(isPruneLeaseCancellation(new PruneLeaseAdmissionCancelled("refused"), admission)).toBe(true);
});

it("reads any failure on a torn-down owner as a cancellation", async () => {
  mountPruneLeaseOwner("game-detail:2");
  const admission = capturePruneLeaseAdmission("game-detail:2");

  await releasePruneLeasesByOwner("game-detail:2");

  // The backend rejects its own in-flight callables on teardown; that rejection
  // describes the teardown, not the work.
  expect(isPruneLeaseCancellation(new Error("io"), admission)).toBe(true);
});

it("reads any failure as a cancellation once the plugin generation rolls", async () => {
  const admission = capturePruneLeaseAdmission();

  await releaseAllPruneLeases();
  mountPruneLeasePlugin();

  expect(isPruneLeaseCancellation(new Error("io"), admission)).toBe(true);
});

it("rejects and releases a lease-bearing response that arrives after owner teardown", async () => {
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  mountPruneLeaseOwner("danger-zone");
  const admission = capturePruneLeaseAdmission("danger-zone");
  const operation = vi.fn().mockResolvedValue(undefined);

  await releasePruneLeasesByOwner("danger-zone");
  await expect(withPruneLease("late-owner", "Late owner", operation, "danger-zone", admission)).rejects.toThrow(
    "cancelled before lease registration",
  );

  expect(operation).not.toHaveBeenCalled();
  expect(releasePruneConflictLease).toHaveBeenCalledWith("late-owner");
});

it("an old plugin generation stays stale after a genuine remount", async () => {
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  const oldAdmission = capturePruneLeaseAdmission();
  const operation = vi.fn().mockResolvedValue(undefined);

  await releaseAllPruneLeases();
  mountPruneLeasePlugin();
  await expect(withPruneLease("late-plugin", "Late plugin", operation, "root", oldAdmission)).rejects.toThrow(
    "cancelled before lease registration",
  );

  expect(operation).not.toHaveBeenCalled();
  expect(releasePruneConflictLease).toHaveBeenCalledWith("late-plugin");
});

it("admits work only after the owner is genuinely mounted again", async () => {
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  mountPruneLeaseOwner("version-picker:42");
  const staleAdmission = capturePruneLeaseAdmission("version-picker:42");
  await releasePruneLeasesByOwner("version-picker:42");
  mountPruneLeaseOwner("version-picker:42");
  const currentAdmission = capturePruneLeaseAdmission("version-picker:42");
  const operation = vi.fn().mockResolvedValue("applied");

  await expect(
    withPruneLease("stale", "Stale version", operation, "version-picker:42", staleAdmission),
  ).rejects.toThrow("cancelled before lease registration");
  await expect(
    withPruneLease("current", "Current version", operation, "version-picker:42", currentAdmission),
  ).resolves.toBe("applied");

  expect(operation).toHaveBeenCalledTimes(1);
});

it("renews active ownership and stops heartbeats before release", async () => {
  vi.useFakeTimers();
  vi.mocked(renewPruneConflictLease).mockResolvedValue({ success: true, message: "renewed" });
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  const release = maintainPruneLease("lease-1", "Long rebake");

  await vi.advanceTimersByTimeAsync(180_000);
  expect(renewPruneConflictLease).toHaveBeenCalledTimes(3);

  await release();
  await vi.advanceTimersByTimeAsync(120_000);
  expect(renewPruneConflictLease).toHaveBeenCalledTimes(3);
  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-1");
});

it("bounds a lost release acknowledgement and logs the failed release", async () => {
  vi.useFakeTimers();
  vi.mocked(releasePruneConflictLease).mockImplementation(() => new Promise(() => {}));

  const release = releasePruneLease("lease-1", "Core selection");
  await vi.advanceTimersByTimeAsync(5000);
  await release;

  expect(logError).toHaveBeenCalledWith(
    "Core selection: failed to release prune lease: TimeoutError: callable timed out after 5000ms",
  );
});

it("releases every lease owned by an unmounted component and stops its heartbeats", async () => {
  vi.useFakeTimers();
  vi.mocked(renewPruneConflictLease).mockResolvedValue({ success: true, message: "renewed" });
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  maintainPruneLease("lease-1", "Artwork", "game-detail:42");
  maintainPruneLease("lease-2", "Core", "game-detail:42");

  await releasePruneLeasesByOwner("game-detail:42");
  await vi.advanceTimersByTimeAsync(120_000);

  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-1");
  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-2");
  expect(renewPruneConflictLease).not.toHaveBeenCalled();
});

it("owner teardown aborts a delayed continuation before its next write", async () => {
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  let resume!: () => void;
  let wroteAfterAwait = false;
  const continuation = withPruneLease(
    "lease-delayed",
    "Version switch",
    async (signal) => {
      await new Promise<void>((resolve) => {
        resume = resolve;
      });
      if (!signal.aborted) wroteAfterAwait = true;
    },
    "version-picker:42",
  );
  await Promise.resolve();

  const teardown = releasePruneLeasesByOwner("version-picker:42");
  await Promise.resolve();
  expect(releasePruneConflictLease).not.toHaveBeenCalledWith("lease-delayed");
  resume();
  await continuation;
  await teardown;

  expect(wroteAfterAwait).toBe(false);
  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-delayed");
});

it("owner teardown retains backend exclusion until a started non-cancellable operation settles", async () => {
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  let settle!: () => void;
  const continuation = withPruneLease(
    "lease-started",
    "Artwork",
    () =>
      new Promise<void>((resolve) => {
        settle = resolve;
      }),
    "artwork:42",
  );
  await Promise.resolve();

  const teardown = releasePruneLeasesByOwner("artwork:42");
  await Promise.resolve();
  expect(releasePruneConflictLease).not.toHaveBeenCalledWith("lease-started");

  settle();
  await continuation;
  await teardown;
  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-started");
});

it("plugin teardown releases all registered frontend leases", async () => {
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  maintainPruneLease("lease-1", "Artwork", "artwork:42");
  maintainPruneLease("lease-2", "Sync", "root");

  await releaseAllPruneLeases();

  expect(
    vi
      .mocked(releasePruneConflictLease)
      .mock.calls.map(([token]) => token)
      .sort(),
  ).toEqual(["lease-1", "lease-2"]);
});

it("stops renewing an unresolved continuation at the five-minute bound", async () => {
  vi.useFakeTimers();
  vi.mocked(renewPruneConflictLease).mockResolvedValue({ success: true, message: "renewed" });
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });

  const operation = withPruneLease("lease-hung", "Hung continuation", () => new Promise<never>(() => {}));
  const rejection = expect(operation).rejects.toThrow("callable timed out after 300000ms");
  await vi.advanceTimersByTimeAsync(300_001);
  await rejection;
  const renewalsAtTimeout = vi.mocked(renewPruneConflictLease).mock.calls.length;
  await vi.advanceTimersByTimeAsync(120_000);

  expect(renewalsAtTimeout).toBeGreaterThan(0);
  expect(renewPruneConflictLease).toHaveBeenCalledTimes(renewalsAtTimeout);
  expect(releasePruneConflictLease).not.toHaveBeenCalledWith("lease-hung");
});

it("timeout aborts future work and releases only after the underlying operation settles", async () => {
  vi.useFakeTimers();
  vi.mocked(renewPruneConflictLease).mockResolvedValue({ success: true, message: "renewed" });
  vi.mocked(releasePruneConflictLease).mockResolvedValue({ success: true, message: "released" });
  let resume!: () => void;
  let wroteAfterAwait = false;
  const operation = withPruneLease("lease-timeout", "Timed continuation", async (signal) => {
    await new Promise<void>((resolve) => {
      resume = resolve;
    });
    if (!signal.aborted) wroteAfterAwait = true;
  });
  const rejection = expect(operation).rejects.toThrow("callable timed out after 300000ms");

  await vi.advanceTimersByTimeAsync(300_001);
  await rejection;
  expect(releasePruneConflictLease).not.toHaveBeenCalledWith("lease-timeout");

  resume();
  await vi.advanceTimersByTimeAsync(0);
  expect(wroteAfterAwait).toBe(false);
  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-timeout");
});

it("a refused renewal aborts future writes and abandons the refused token", async () => {
  vi.useFakeTimers();
  vi.mocked(renewPruneConflictLease).mockResolvedValue({ success: false, message: "expired" });
  let resume!: () => void;
  let wroteAfterAwait = false;
  const operation = withPruneLease("lease-refused", "Refused continuation", async (signal) => {
    await new Promise<void>((resolve) => {
      resume = resolve;
    });
    if (!signal.aborted) wroteAfterAwait = true;
  });

  await vi.advanceTimersByTimeAsync(60_000);
  resume();
  await operation;

  expect(wroteAfterAwait).toBe(false);
  expect(releasePruneConflictLease).not.toHaveBeenCalledWith("lease-refused");
});

it("disowns leases stranded by a previous frontend context on mount", async () => {
  vi.mocked(releaseOrphanedPruneLeases).mockResolvedValueOnce({ success: true, released: 1 });

  mountPruneLeasePlugin();
  await Promise.resolve();
  await Promise.resolve();

  // A context torn down mid-call never released its lease and never renews it,
  // so nothing but a fresh mount can free the gate before the TTL.
  expect(releaseOrphanedPruneLeases).toHaveBeenCalled();
  expect(logError).toHaveBeenCalledWith(expect.stringContaining("disowned 1 lease(s) stranded"));
});

it("says nothing on a mount that had nothing to disown", async () => {
  vi.mocked(releaseOrphanedPruneLeases).mockResolvedValueOnce({ success: true, released: 0 });

  mountPruneLeasePlugin();
  await Promise.resolve();
  await Promise.resolve();

  expect(logError).not.toHaveBeenCalled();
});

it("keeps mounting when the disown call fails", async () => {
  vi.mocked(releaseOrphanedPruneLeases).mockRejectedValueOnce(new Error("bridge offline"));

  mountPruneLeasePlugin();
  await Promise.resolve();
  await Promise.resolve();

  // Mount must not be blocked by a best-effort cleanup; the TTL still backs it.
  expect(logError).toHaveBeenCalledWith(expect.stringContaining("could not disown stranded leases"));
  expect(capturePruneLeaseAdmission().pluginGeneration).toBeGreaterThan(0);
});
