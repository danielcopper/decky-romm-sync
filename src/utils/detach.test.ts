import { describe, it, expect, vi, afterEach } from "vitest";
import { detach } from "./detach";

/** Resolve once the microtask + macrotask queues have drained. */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

describe("detach", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("attaches a rejection handler so a rejecting promise stays handled", async () => {
    const rejecting = Promise.reject(new Error("boom"));
    // Spy on the actual promise — detach must call .catch on it, which is what
    // prevents the rejection from surfacing as unhandled. (Promise.resolve(p)
    // returns the same native promise, so the handler lands on `rejecting`.)
    const catchSpy = vi.spyOn(rejecting, "catch");

    expect(() => {
      detach(rejecting);
    }).not.toThrow();

    expect(catchSpy).toHaveBeenCalledTimes(1);

    // Drain queues — the spied .catch already swallows the rejection, so this
    // test would fail with an unhandled rejection if detach had skipped it.
    await flush();
  });

  it("returns undefined synchronously (does not block on the promise)", () => {
    const result = detach(Promise.resolve(123));
    expect(result).toBeUndefined();
  });

  it("is a no-op for a resolving promise — the value is dropped, no throw", async () => {
    const onResolve = vi.fn();
    const onReject = vi.fn();
    const p = Promise.resolve("value");
    // Observe the underlying promise still settles normally after detach().
    p.then(onResolve, onReject).catch(() => undefined);

    detach(p);
    await flush();

    expect(onResolve).toHaveBeenCalledWith("value");
    expect(onReject).not.toHaveBeenCalled();
  });

  it("does not throw when handed a non-thenable (e.g. an undefined-returning mock)", async () => {
    // Production always passes a real Promise, but a test mock without a
    // mockResolvedValue returns undefined. A bare promise.catch(...) would
    // throw "Cannot read properties of undefined". detach must stay silent.
    expect(() => {
      detach(undefined as unknown as Promise<unknown>);
    }).not.toThrow();
    await flush();
  });

  it("swallows a rejection that settles after detach was called", async () => {
    let reject!: (reason: unknown) => void;
    const pending = new Promise<unknown>((_, rej) => {
      reject = rej;
    });
    const catchSpy = vi.spyOn(pending, "catch");

    detach(pending);
    expect(catchSpy).toHaveBeenCalledTimes(1);

    // Reject after the handler is attached — must not surface as unhandled.
    reject(new Error("late failure"));
    await flush();
  });
});
