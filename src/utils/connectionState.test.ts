import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  getRommConnectionState,
  setRommConnectionState,
  reportServerReachable,
  onRommConnectionChange,
  getServerRetryProgress,
  setServerRetryProgress,
  onServerRetryProgressChange,
} from "./connectionState";

describe("connectionState store (#1345)", () => {
  beforeEach(() => {
    // Reset the module-level store to the neutral state between tests.
    setRommConnectionState("checking");
  });

  it("notifies subscribers on a real change", () => {
    const cb = vi.fn();
    onRommConnectionChange(cb);
    setRommConnectionState("offline");
    expect(cb).toHaveBeenCalledExactlyOnceWith("offline");
    expect(getRommConnectionState()).toBe("offline");
  });

  it("does NOT notify when the state is unchanged", () => {
    setRommConnectionState("connected");
    const cb = vi.fn();
    onRommConnectionChange(cb);
    setRommConnectionState("connected"); // same value — no-op
    expect(cb).not.toHaveBeenCalled();
  });

  it("unsubscribe stops further notifications", () => {
    const cb = vi.fn();
    const unsubscribe = onRommConnectionChange(cb);
    setRommConnectionState("offline");
    expect(cb).toHaveBeenCalledTimes(1);
    unsubscribe();
    setRommConnectionState("connected");
    expect(cb).toHaveBeenCalledTimes(1); // no second call after unsubscribe
  });

  it("reportServerReachable(true) → connected, (false) → offline", () => {
    reportServerReachable(false);
    expect(getRommConnectionState()).toBe("offline");
    reportServerReachable(true);
    expect(getRommConnectionState()).toBe("connected");
  });

  it("reportServerReachable notifies every subscriber", () => {
    const a = vi.fn();
    const b = vi.fn();
    onRommConnectionChange(a);
    onRommConnectionChange(b);
    reportServerReachable(false);
    expect(a).toHaveBeenCalledExactlyOnceWith("offline");
    expect(b).toHaveBeenCalledExactlyOnceWith("offline");
  });
});

describe("serverRetryProgress store (#1345)", () => {
  beforeEach(() => {
    setServerRetryProgress(null);
  });

  it("starts empty (null)", () => {
    expect(getServerRetryProgress()).toBeNull();
  });

  it("sets and reads back a progress value, notifying subscribers", () => {
    const cb = vi.fn();
    onServerRetryProgressChange(cb);
    setServerRetryProgress({ attempt: 2, maxAttempts: 3 });
    expect(getServerRetryProgress()).toEqual({ attempt: 2, maxAttempts: 3 });
    expect(cb).toHaveBeenCalledExactlyOnceWith({ attempt: 2, maxAttempts: 3 });
  });

  it("clearing to null notifies and resets", () => {
    setServerRetryProgress({ attempt: 3, maxAttempts: 3 });
    const cb = vi.fn();
    onServerRetryProgressChange(cb);
    setServerRetryProgress(null);
    expect(getServerRetryProgress()).toBeNull();
    expect(cb).toHaveBeenCalledExactlyOnceWith(null);
  });

  it("does NOT notify when clearing an already-clear store", () => {
    const cb = vi.fn();
    onServerRetryProgressChange(cb);
    setServerRetryProgress(null); // already null — no-op
    expect(cb).not.toHaveBeenCalled();
  });

  it("unsubscribe stops further notifications", () => {
    const cb = vi.fn();
    const unsubscribe = onServerRetryProgressChange(cb);
    setServerRetryProgress({ attempt: 2, maxAttempts: 3 });
    expect(cb).toHaveBeenCalledTimes(1);
    unsubscribe();
    setServerRetryProgress({ attempt: 3, maxAttempts: 3 });
    expect(cb).toHaveBeenCalledTimes(1);
  });
});
