import { afterEach, expect, it, vi } from "vitest";
import { logError, releasePruneConflictLease } from "../api/backend";
import { releasePruneLease } from "./pruneLease";

vi.mock("../api/backend", () => ({
  logError: vi.fn(),
  releasePruneConflictLease: vi.fn(),
}));

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
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
