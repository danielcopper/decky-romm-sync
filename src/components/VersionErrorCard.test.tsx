import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, renderHook, act } from "@testing-library/react";
import { createElement, type ComponentProps } from "react";
import { VersionErrorCard, useVersionError } from "./VersionErrorCard";
import { setVersionError } from "../utils/connectionState";
import * as connectionState from "../utils/connectionState";
import type { WarningCard } from "./WarningCard";

// Capture the props passed to WarningCard so the FC tests can assert the
// delegation contract without rendering WarningCard's own DOM (covered in
// WarningCard.test.tsx).
type CapturedWarningCardProps = ComponentProps<typeof WarningCard>;
const capturedWarningCard: CapturedWarningCardProps[] = [];

vi.mock("./WarningCard", () => ({
  WarningCard: (props: CapturedWarningCardProps) => {
    capturedWarningCard.push(props);
    return createElement("div", { "data-testid": "warning-card" });
  },
}));

describe("VersionErrorCard component", () => {
  beforeEach(() => {
    capturedWarningCard.length = 0;
  });

  it("delegates to WarningCard with the version-error title + message (default compact=false)", () => {
    const { queryByTestId } = render(
      <VersionErrorCard message="RomM 4.7.0 too old" />,
    );
    expect(queryByTestId("warning-card")).not.toBeNull();
    expect(capturedWarningCard).toHaveLength(1);
    expect(capturedWarningCard[0]).toEqual({
      title: "RomM Server Update Required",
      message: "RomM 4.7.0 too old",
      compact: false,
    });
  });

  it("forwards compact=true", () => {
    render(<VersionErrorCard message="m" compact />);
    expect(capturedWarningCard[0]?.compact).toBe(true);
  });
});

describe("useVersionError hook", () => {
  // The hook subscribes to the real in-memory connectionState store. Drive it
  // via setVersionError(...) and reset to null in afterEach so tests don't
  // leak state into siblings (the store is module-singleton).
  afterEach(() => {
    setVersionError(null);
  });

  it("returns the current error from getVersionError() on mount", () => {
    setVersionError("initial err");
    const { result } = renderHook(() => useVersionError());
    expect(result.current).toBe("initial err");
  });

  it("returns null when no error is set", () => {
    setVersionError(null);
    const { result } = renderHook(() => useVersionError());
    expect(result.current).toBeNull();
  });

  it("updates when setVersionError fires after mount", () => {
    setVersionError(null);
    const { result } = renderHook(() => useVersionError());
    expect(result.current).toBeNull();
    act(() => {
      setVersionError("new err");
    });
    expect(result.current).toBe("new err");
  });

  it("unsubscribes on unmount — useEffect cleanup invokes the returned unsubscribe", () => {
    // Spy on onVersionErrorChange so the test owns the unsubscribe callback.
    // The "updates after mount" test above covers the subscribe-then-callback
    // path; this test isolates the cleanup invocation. Asserting unsubSpy
    // was called proves the hook returns its unsubscribe from useEffect — a
    // mutation that drops the return (e.g. `useEffect(() => { onX(setErr); }, [])`)
    // fails this test, where the prior `not.toThrow()` pattern passed
    // vacuously under React 19's silent no-op-on-unmounted-setState.
    const unsubSpy = vi.fn();
    vi.spyOn(connectionState, "onVersionErrorChange").mockImplementation((cb) => {
      void cb;
      return unsubSpy;
    });

    const { unmount } = renderHook(() => useVersionError());
    expect(unsubSpy).not.toHaveBeenCalled();
    unmount();
    expect(unsubSpy).toHaveBeenCalledTimes(1);
  });
});
