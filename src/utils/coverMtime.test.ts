import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as backend from "../api/backend";
import * as steamState from "./steamState";
import { stampCoverMtimes, healCoverMtimes } from "./coverMtime";

// The stamp writes a MobX observable per appId; a synchronous burst flickers the
// QAM. stampCoverMtimes slices the writes and yields between slices. logInfo/
// logError are plain wrappers (not callables), so spy to observe them.
describe("stampCoverMtimes (micro-batched cover stamp)", () => {
  let overviews: Map<number, { rt_custom_image_mtime?: number }>;
  const getAppOverview = vi.fn((appId: number) => overviews.get(appId) ?? null);
  let logInfoSpy: ReturnType<typeof vi.spyOn>;
  let logErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    overviews = new Map();
    getAppOverview.mockClear();
    vi.stubGlobal("appStore", { GetAppOverviewByAppID: getAppOverview, allApps: [] });
    logInfoSpy = vi.spyOn(backend, "logInfo").mockImplementation(() => {});
    logErrorSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
  });

  afterEach(() => {
    logInfoSpy.mockRestore();
    logErrorSpy.mockRestore();
  });

  it("stamps a 60-id list across ≥3 slices (yielding between), all stamped, one summary log", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      const ids = Array.from({ length: 60 }, (_, i) => 1000 + i);
      for (const id of ids) overviews.set(id, {});

      const p = stampCoverMtimes(ids, "");
      // The first slice (25) ran synchronously before the first yield — proof the
      // burst is broken up rather than applied all at once, and the summary log is
      // withheld until the end.
      const stampedAfterFirstSlice = ids.filter((id) => overviews.get(id)!.rt_custom_image_mtime !== undefined).length;
      expect(stampedAfterFirstSlice).toBe(25);
      expect(logInfoSpy).not.toHaveBeenCalled();

      // Drain the inter-slice setTimeout(0) yields (60 → 25 + 25 + 10 = 3 slices).
      await vi.runAllTimersAsync();
      await p;

      expect(ids.every((id) => typeof overviews.get(id)!.rt_custom_image_mtime === "number")).toBe(true);
      expect(logInfoSpy).toHaveBeenCalledTimes(1);
      expect(logInfoSpy).toHaveBeenCalledWith("[FE] cover mtime nudge: 60 stamped, 0 no overview");
    } finally {
      vi.useRealTimers();
    }
  });

  it("is a no-op for an empty list (no lookup, no log)", async () => {
    await stampCoverMtimes([], "");
    expect(getAppOverview).not.toHaveBeenCalled();
    expect(logInfoSpy).not.toHaveBeenCalled();
  });

  it("counts missing overviews, stamps the rest, and never throws (fail-soft)", async () => {
    overviews.set(1, {}); // appId 2 has no overview
    await stampCoverMtimes([1, 2], " (chunk)");
    expect(typeof overviews.get(1)!.rt_custom_image_mtime).toBe("number");
    expect(logInfoSpy).toHaveBeenCalledWith("[FE] cover mtime nudge (chunk): 1 stamped, 1 no overview");
  });

  it("summarizes to logError (does not reject) when a lookup throws", async () => {
    getAppOverview.mockImplementationOnce(() => {
      throw new Error("boom");
    });
    await expect(stampCoverMtimes([1], " (chunk)")).resolves.toBeUndefined();
    expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("cover mtime nudge (chunk) failed"));
  });

  it("healCoverMtimes re-stamps but emits NO summary log (the poll owns the heal log)", async () => {
    overviews.set(1, {});
    await healCoverMtimes([1]);
    expect(typeof overviews.get(1)!.rt_custom_image_mtime).toBe("number");
    expect(logInfoSpy).not.toHaveBeenCalled();
  });

  it("healCoverMtimes is fail-soft — a throwing lookup resolves and logs the heal failure", async () => {
    getAppOverview.mockImplementationOnce(() => {
      throw new Error("boom");
    });
    await expect(healCoverMtimes([1])).resolves.toBeUndefined();
    expect(logErrorSpy).toHaveBeenCalledWith(expect.stringContaining("cover mtime heal failed"));
  });

  it("routes the observable writes through stateTransaction, and they still land (#M2)", async () => {
    // Spy without a replacement impl so the real transaction runs (a no-op when
    // __mobxGlobals is absent) — proving the write path is wrapped, not that it's
    // stubbed. On a strict-actions build this is what keeps the write from bouncing.
    const txSpy = vi.spyOn(steamState, "stateTransaction");
    try {
      overviews.set(1, {});
      await stampCoverMtimes([1], "");
      expect(txSpy).toHaveBeenCalled();
      expect(typeof overviews.get(1)!.rt_custom_image_mtime).toBe("number");
    } finally {
      txSpy.mockRestore();
    }
  });
});
