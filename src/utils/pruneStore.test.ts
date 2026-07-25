import { beforeEach, describe, expect, it } from "vitest";
import { beginPruneRun, getPruneState, resetPruneState, setPruneComplete, setPruneProgress } from "./pruneStore";

describe("pruneStore", () => {
  beforeEach(resetPruneState);

  it("ignores late frames from an older run", () => {
    beginPruneRun("new");
    setPruneProgress({ run_id: "old", current: 1, total: 1, stage: "removed", rom_ids: [1], name: "old" });
    expect(getPruneState().progress).toBeNull();
  });

  it("assembles bounded completion chunks and ignores duplicate chunks", () => {
    beginPruneRun("run-1");
    const first = {
      success: true,
      partial: false,
      run_id: "run-1",
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
    setPruneComplete({
      success: true,
      partial: false,
      run_id: "old",
      removed_rom_ids: [],
      affected_app_ids: [],
      results: [],
    });
    beginPruneRun("new");
    expect(getPruneState()).toEqual({ runId: "new", progress: null, complete: null });
  });
});
