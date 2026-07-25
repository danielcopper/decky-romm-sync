import { beforeEach, describe, expect, it } from "vitest";
import {
  admitPruneFrame,
  beginPrunePreview,
  beginPruneRun,
  getPruneState,
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
