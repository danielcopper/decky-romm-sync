/**
 * Exercises the per-run sync delta store: record/get/reset, dedup of repeated
 * appIds, and independence of the created vs removed sets. The store is
 * module-level state, so each test resets it first.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { resetSyncDelta, recordSyncCreated, recordSyncRemoved, getSyncDelta } from "./syncDeltaStore";

describe("syncDeltaStore", () => {
  beforeEach(() => {
    resetSyncDelta();
  });

  it("starts empty", () => {
    expect(getSyncDelta()).toEqual({ added: 0, removed: 0 });
  });

  it("counts each distinct created appId once", () => {
    recordSyncCreated(100);
    recordSyncCreated(200);
    recordSyncCreated(300);
    expect(getSyncDelta()).toEqual({ added: 3, removed: 0 });
  });

  it("counts each distinct removed appId once", () => {
    recordSyncRemoved(900);
    recordSyncRemoved(800);
    expect(getSyncDelta()).toEqual({ added: 0, removed: 2 });
  });

  it("dedups a created appId recorded twice (multi-unit overlap counts once)", () => {
    recordSyncCreated(100);
    recordSyncCreated(100);
    recordSyncCreated(200);
    expect(getSyncDelta()).toEqual({ added: 2, removed: 0 });
  });

  it("dedups a removed appId recorded twice across per-unit sync_stale emits", () => {
    recordSyncRemoved(900);
    recordSyncRemoved(900);
    expect(getSyncDelta()).toEqual({ added: 0, removed: 1 });
  });

  it("tracks created and removed independently", () => {
    recordSyncCreated(100);
    recordSyncCreated(200);
    recordSyncRemoved(900);
    expect(getSyncDelta()).toEqual({ added: 2, removed: 1 });
  });

  it("clears both sets on reset", () => {
    recordSyncCreated(100);
    recordSyncRemoved(900);
    resetSyncDelta();
    expect(getSyncDelta()).toEqual({ added: 0, removed: 0 });
  });
});
