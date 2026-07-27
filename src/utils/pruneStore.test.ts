import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  admitPruneFrame,
  beginPrunePreview,
  beginPruneRun,
  getPruneState,
  isPruneResultLost,
  onPruneStateChange,
  resetPruneState,
  setPruneComplete,
  setPruneProgress,
} from "./pruneStore";

describe("pruneStore", () => {
  beforeEach(resetPruneState);

  function begin(runId: string, previewId = "preview-1"): void {
    beginPrunePreview(previewId);
    beginPruneRun(runId, previewId);
  }

  it("ignores late frames from an older run", () => {
    begin("new");
    setPruneProgress({
      run_id: "old",
      preview_id: "preview-1",
      current: 1,
      total: 1,
      stage: "removed",
      rom_ids: [1],
      name: "old",
    });
    expect(getPruneState().progress).toBeNull();
  });

  it("a fresh preview adopts only a matching backend run before the start response", () => {
    beginPrunePreview("preview-new");

    expect(
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "old",
        preview_id: "preview-old",
        removed_rom_ids: [1],
        affected_app_ids: [101],
        results: [],
      }),
    ).toBeNull();
    expect(getPruneState()).toEqual({ runId: null, progress: null, complete: null });

    setPruneProgress({
      run_id: "new",
      preview_id: "preview-new",
      current: 1,
      total: 1,
      stage: "checking",
      rom_ids: [2],
      name: "new",
    });
    expect(getPruneState().runId).toBe("new");
  });

  it("assembles bounded completion chunks and ignores duplicate chunks", () => {
    begin("run-1");
    const first = {
      success: true,
      partial: false,
      run_id: "run-1",
      preview_id: "preview-1",
      chunk_index: 0,
      final: false,
      removed_count: 2,
      problem_count: 0,
      removed_rom_ids: [1],
      affected_app_ids: [101],
      removed_app_ids: [],
      results: [{ group_id: "one", rom_ids: [1], status: "removed" as const, message: "removed" }],
    };
    expect(setPruneComplete(first)).toBeNull();
    expect(setPruneComplete(first)).toBeNull();
    const complete = setPruneComplete({
      ...first,
      chunk_index: 1,
      final: true,
      removed_rom_ids: [2],
      affected_app_ids: [102],
      results: [{ group_id: "two", rom_ids: [2], status: "removed", message: "removed" }],
    });
    expect(complete?.removed_rom_ids).toEqual([1, 2]);
    expect(complete?.affected_app_ids).toEqual([101, 102]);
    expect(complete?.results).toHaveLength(2);
  });

  it("a new run clears a previous completion", () => {
    begin("old", "preview-old");
    setPruneComplete({
      success: true,
      partial: false,
      run_id: "old",
      preview_id: "preview-old",
      removed_rom_ids: [],
      affected_app_ids: [],
      results: [],
    });
    begin("new", "preview-new");
    expect(getPruneState()).toEqual({ runId: "new", progress: null, complete: null });
  });

  it("waits for every earlier chunk when the final chunk arrives first", () => {
    begin("run-2");
    const final = {
      success: true,
      partial: false,
      run_id: "run-2",
      preview_id: "preview-1",
      chunk_index: 1,
      final: true,
      removed_rom_ids: [2],
      affected_app_ids: [102],
      results: [{ group_id: "two", rom_ids: [2], status: "removed" as const, message: "removed" }],
    };

    expect(setPruneComplete(final)).toBeNull();
    expect(getPruneState().complete).toBeNull();
    const complete = setPruneComplete({
      ...final,
      chunk_index: 0,
      final: false,
      removed_rom_ids: [1],
      affected_app_ids: [101],
      results: [{ group_id: "one", rom_ids: [1], status: "removed", message: "removed" }],
    });

    expect(complete?.removed_rom_ids).toEqual([1, 2]);
    expect(complete?.results.map((item) => item.group_id)).toEqual(["one", "two"]);
  });

  it("keeps a completed run terminal when delayed same-run frames arrive", () => {
    begin("run-terminal");
    const terminal = setPruneComplete({
      success: true,
      partial: false,
      run_id: "run-terminal",
      preview_id: "preview-1",
      removed_rom_ids: [7],
      affected_app_ids: [70],
      results: [],
    });

    setPruneProgress({
      run_id: "run-terminal",
      preview_id: "preview-1",
      current: 1,
      total: 1,
      stage: "late",
      rom_ids: [7],
      name: "late",
    });
    expect(
      setPruneComplete({
        success: false,
        partial: true,
        run_id: "run-terminal",
        preview_id: "preview-1",
        chunk_index: 1,
        removed_rom_ids: [],
        affected_app_ids: [],
        results: [],
      }),
    ).toBeNull();
    expect(admitPruneFrame("preview-1", "run-terminal")).toBe(false);

    expect(getPruneState()).toEqual({ runId: "run-terminal", progress: null, complete: terminal });
  });
});

describe("pruneStore — lost-result recovery", () => {
  beforeEach(() => {
    resetPruneState();
    vi.useFakeTimers();
  });

  afterEach(() => {
    resetPruneState();
    vi.useRealTimers();
  });

  /** Adopt a run and leave chunk 0 permanently missing, so it can never finalize. */
  function beginIncompleteRun(): void {
    beginPrunePreview("preview-lost");
    beginPruneRun("run-lost", "preview-lost");
    setPruneProgress({
      run_id: "run-lost",
      preview_id: "preview-lost",
      current: 1,
      total: 2,
      stage: "removing_rows",
      rom_ids: [7],
      name: "Removed Game",
    });
    expect(
      setPruneComplete({
        success: true,
        partial: false,
        run_id: "run-lost",
        preview_id: "preview-lost",
        chunk_index: 1,
        final: true,
        removed_rom_ids: [2],
        affected_app_ids: [102],
        results: [],
      }),
    ).toBeNull();
  }

  it("drops a run whose chunk set never completes and raises the lost-result flag", () => {
    const changes = vi.fn();
    const unsubscribe = onPruneStateChange(changes);
    try {
      beginIncompleteRun();
      // Still pinned before the timeout — the entry point stays disabled while a
      // run may genuinely still be working.
      vi.advanceTimersByTime(14 * 60_000);
      expect(getPruneState().progress).not.toBeNull();
      expect(isPruneResultLost()).toBe(false);
      changes.mockClear();

      vi.advanceTimersByTime(60_000);

      expect(getPruneState()).toEqual({ runId: null, progress: null, complete: null });
      expect(isPruneResultLost()).toBe(true);
      // Subscribers are told, so the Danger Zone re-renders its re-enabled entry.
      expect(changes).toHaveBeenCalled();
    } finally {
      unsubscribe();
    }
  });

  it("keeps the countdown alive while frames keep arriving", () => {
    beginIncompleteRun();

    for (let index = 0; index < 6; index++) {
      vi.advanceTimersByTime(14 * 60_000);
      setPruneProgress({
        run_id: "run-lost",
        preview_id: "preview-lost",
        current: index + 1,
        total: 20,
        stage: "removing_rows",
        rom_ids: [7],
        name: "Removed Game",
      });
    }

    expect(isPruneResultLost()).toBe(false);
    expect(getPruneState().runId).toBe("run-lost");
  });

  it("never fires once a contiguous chunk set finalizes", () => {
    beginPrunePreview("preview-ok");
    beginPruneRun("run-ok", "preview-ok");
    const complete = setPruneComplete({
      success: true,
      partial: false,
      run_id: "run-ok",
      preview_id: "preview-ok",
      removed_rom_ids: [7],
      affected_app_ids: [],
      results: [],
    });

    vi.advanceTimersByTime(30 * 60_000);

    expect(getPruneState().complete).toBe(complete);
    expect(isPruneResultLost()).toBe(false);
  });

  it("lets a merely slow run re-adopt and still finalize after the countdown fired", () => {
    beginIncompleteRun();
    vi.advanceTimersByTime(15 * 60_000);
    expect(isPruneResultLost()).toBe(true);

    // The missing chunk finally lands — the run must complete, not be discarded
    // as foreign, so an over-eager countdown never loses a real result.
    const complete = setPruneComplete({
      success: true,
      partial: false,
      run_id: "run-lost",
      preview_id: "preview-lost",
      chunk_index: 0,
      final: false,
      removed_rom_ids: [1],
      affected_app_ids: [101],
      results: [],
    });

    expect(complete?.removed_rom_ids).toEqual([1, 2]);
    expect(getPruneState().complete).toBe(complete);
    expect(isPruneResultLost()).toBe(false);
  });

  it("still refuses a foreign run after the countdown fired", () => {
    beginIncompleteRun();
    vi.advanceTimersByTime(15 * 60_000);

    expect(admitPruneFrame("preview-other", "run-other")).toBe(false);
    expect(getPruneState().runId).toBeNull();
  });

  it("clears the lost-result flag when a fresh preview opens", () => {
    beginIncompleteRun();
    vi.advanceTimersByTime(15 * 60_000);
    expect(isPruneResultLost()).toBe(true);

    beginPrunePreview("preview-next");

    expect(isPruneResultLost()).toBe(false);
    expect(getPruneState()).toEqual({ runId: null, progress: null, complete: null });
  });
});
