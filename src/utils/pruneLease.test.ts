import { afterEach, expect, it, vi } from "vitest";
import { logError, releasePruneConflictLease, renewPruneConflictLease } from "../api/backend";
import {
  maintainPruneLease,
  releaseAllPruneLeases,
  releasePruneLease,
  releasePruneLeasesByOwner,
  withPruneLease,
} from "./pruneLease";

vi.mock("../api/backend", () => ({
  logError: vi.fn(),
  releasePruneConflictLease: vi.fn(),
  renewPruneConflictLease: vi.fn(),
}));

afterEach(async () => {
  await releaseAllPruneLeases();
  vi.useRealTimers();
  vi.clearAllMocks();
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
    "Core selection: failed to release prune lease: Error: callable timed out after 5000ms",
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

  await releasePruneLeasesByOwner("version-picker:42");
  resume();
  await continuation;

  expect(wroteAfterAwait).toBe(false);
  expect(releasePruneConflictLease).toHaveBeenCalledWith("lease-delayed");
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
