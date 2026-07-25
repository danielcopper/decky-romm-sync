import { afterEach, expect, it, vi } from "vitest";
import { logError, releasePruneConflictLease, renewPruneConflictLease } from "../api/backend";
import { maintainPruneLease, releasePruneLease } from "./pruneLease";

vi.mock("../api/backend", () => ({
  logError: vi.fn(),
  releasePruneConflictLease: vi.fn(),
  renewPruneConflictLease: vi.fn(),
}));

afterEach(() => {
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
