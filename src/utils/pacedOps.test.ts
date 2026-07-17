import { describe, it, expect, vi, afterEach } from "vitest";
import * as backend from "../api/backend";
import { pacedForEach, delay } from "./pacedOps";

describe("pacedOps — delay", () => {
  it("resolves after the given number of milliseconds", async () => {
    vi.useFakeTimers();
    try {
      let resolved = false;
      const p = delay(50).then(() => {
        resolved = true;
      });
      await vi.advanceTimersByTimeAsync(49);
      expect(resolved).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      await p;
      expect(resolved).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("pacedOps — pacedForEach", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("per-item mode: a 50ms breather after every item except the last", async () => {
    vi.useFakeTimers();
    const calls: number[] = [];
    const p = pacedForEach([1, 2, 3], (x) => {
      calls.push(x);
    });
    // The first item runs synchronously at entry, then the loop blocks on the breather.
    expect(calls).toEqual([1]);
    await vi.advanceTimersByTimeAsync(50);
    expect(calls).toEqual([1, 2]);
    await vi.advanceTimersByTimeAsync(50);
    expect(calls).toEqual([1, 2, 3]);
    // No trailing delay: the loop resolves without another timer, and none is pending.
    expect(vi.getTimerCount()).toBe(0);
    await expect(p).resolves.toBe(true);
  });

  it("chunked mode: items run back-to-back within a chunk, one breather between chunks, none trailing", async () => {
    vi.useFakeTimers();
    const calls: number[] = [];
    const p = pacedForEach(
      [1, 2, 3, 4, 5],
      (x) => {
        calls.push(x);
      },
      { chunkSize: 2, delayMs: 50 },
    );
    // Chunk 1 (items 1,2) runs back-to-back with only microtask hops — no timer between them.
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(calls).toEqual([1, 2]);
    // Second chunk is gated behind the single breather.
    await vi.advanceTimersByTimeAsync(50);
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(calls).toEqual([1, 2, 3, 4]);
    // Third chunk (just item 5) after the second breather; no trailing delay follows it.
    await vi.advanceTimersByTimeAsync(50);
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(calls).toEqual([1, 2, 3, 4, 5]);
    expect(vi.getTimerCount()).toBe(0);
    await expect(p).resolves.toBe(true);
  });

  it("empty list: never calls fn, schedules no timer, resolves true", async () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    const done = await pacedForEach([], fn);
    expect(fn).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    expect(done).toBe(true);
  });

  it("a single item takes no breather at all", async () => {
    vi.useFakeTimers();
    const calls: number[] = [];
    const p = pacedForEach([42], (x) => {
      calls.push(x);
    });
    expect(calls).toEqual([42]);
    expect(vi.getTimerCount()).toBe(0);
    await expect(p).resolves.toBe(true);
  });

  it("logs and continues past an item whose fn throws — one failure never aborts the batch", async () => {
    const logSpy = vi.spyOn(backend, "logError").mockImplementation(() => {});
    const seen: number[] = [];
    // delayMs 0 keeps the test fast; the item at index 1 (value 2) throws.
    const done = await pacedForEach(
      [1, 2, 3],
      (x) => {
        if (x === 2) throw new Error("boom");
        seen.push(x);
      },
      { delayMs: 0 },
    );
    // Item 2 threw but 1 and 3 still ran, and the loop reports completion.
    expect(seen).toEqual([1, 3]);
    expect(done).toBe(true);
    expect(logSpy).toHaveBeenCalledWith(expect.stringContaining("item 1 failed"));
    logSpy.mockRestore();
  });

  it("awaits each async fn in sequence (never overlaps the next item)", async () => {
    const order: string[] = [];
    let releaseFirst!: () => void;
    const gate = new Promise<void>((r) => {
      releaseFirst = r;
    });
    const p = pacedForEach(
      [1, 2],
      async (x) => {
        order.push(`start-${x}`);
        if (x === 1) await gate;
        order.push(`end-${x}`);
      },
      { delayMs: 0 },
    );
    await Promise.resolve();
    // Item 1 is mid-flight; item 2 has NOT started while item 1 is unresolved.
    expect(order).toEqual(["start-1"]);
    releaseFirst();
    await p;
    expect(order).toEqual(["start-1", "end-1", "start-2", "end-2"]);
  });

  it("stops before the next item when isCancelled returns true, and returns false", async () => {
    const seen: number[] = [];
    const done = await pacedForEach(
      [1, 2, 3, 4],
      (x) => {
        seen.push(x);
      },
      { delayMs: 0, isCancelled: () => seen.length >= 2 },
    );
    // Cancel observed after item 2 → items 3 and 4 never run; the loop reports early exit.
    expect(seen).toEqual([1, 2]);
    expect(done).toBe(false);
  });

  it("runs the whole list when isCancelled stays false, returning true", async () => {
    const seen: number[] = [];
    const done = await pacedForEach(
      [1, 2, 3],
      (x) => {
        seen.push(x);
      },
      { delayMs: 0, isCancelled: () => false },
    );
    expect(seen).toEqual([1, 2, 3]);
    expect(done).toBe(true);
  });

  it("throttles the heartbeat to at most once per interval, not once per item", async () => {
    vi.useFakeTimers();
    let beats = 0;
    const p = pacedForEach([1, 2, 3, 4], () => {}, {
      delayMs: 50,
      heartbeat: () => {
        beats += 1;
      },
      heartbeatIntervalMs: 100,
    });
    // Four items with 50ms breathers span ~150ms of virtual time; a 100ms throttle
    // window lets the heartbeat fire at least once but far fewer than four times.
    await vi.advanceTimersByTimeAsync(400);
    await p;
    expect(beats).toBeGreaterThanOrEqual(1);
    expect(beats).toBeLessThan(4);
  });

  it("never fires a heartbeat when none is supplied", async () => {
    // No heartbeat option → no heartbeat path taken (removals pass no watchdog).
    const done = await pacedForEach([1, 2, 3], () => {}, { delayMs: 0 });
    expect(done).toBe(true);
  });
});
