import { describe, it, expect, vi, afterEach } from "vitest";
import { withTimeout, TimeoutError } from "./withTimeout";

describe("withTimeout", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("resolves with the promise's value when it settles before the deadline", async () => {
    await expect(withTimeout(Promise.resolve("ok"), 1000)).resolves.toBe("ok");
  });

  it("propagates the promise's rejection when it rejects before the deadline", async () => {
    await expect(withTimeout(Promise.reject(new Error("boom")), 1000)).rejects.toThrow("boom");
  });

  it("rejects with a timeout error once the deadline elapses first", async () => {
    vi.useFakeTimers();
    const pending = new Promise<string>(() => {
      /* never settles */
    });
    const assertion = expect(withTimeout(pending, 5000)).rejects.toThrow("callable timed out after 5000ms");
    await vi.advanceTimersByTimeAsync(5000);
    await assertion;
  });

  it("rejects with a TimeoutError on the deadline, and passes a raced rejection through untouched", async () => {
    vi.useFakeTimers();
    const pending = new Promise<string>(() => {
      /* never settles */
    });
    const deadline = expect(withTimeout(pending, 5000)).rejects.toBeInstanceOf(TimeoutError);
    await vi.advanceTimersByTimeAsync(5000);
    await deadline;
    // The promise's own failure must stay distinguishable from the deadline's.
    await expect(withTimeout(Promise.reject(new Error("boom")), 5000)).rejects.not.toBeInstanceOf(TimeoutError);
  });

  it("clears the deadline timer once the promise settles — no leaked timer", async () => {
    vi.useFakeTimers();
    const clearSpy = vi.spyOn(globalThis, "clearTimeout");
    await withTimeout(Promise.resolve("done"), 5000);
    // The finally-cleanup cleared the deadline, leaving nothing pending.
    expect(clearSpy).toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    clearSpy.mockRestore();
  });
});
