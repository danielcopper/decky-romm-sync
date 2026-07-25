import { describe, it, expect, vi, afterEach } from "vitest";

const removeShortcut = vi.fn();
vi.mock("./steamShortcuts", () => ({
  removeShortcut: (...args: unknown[]) => removeShortcut(...args),
}));

import { removeShortcutsPaced } from "./shortcutRemoval";

describe("shortcutRemoval — removeShortcutsPaced", () => {
  afterEach(() => {
    removeShortcut.mockClear();
    vi.useRealTimers();
  });

  it("removes in 25-item chunks with a 50ms breather between them", async () => {
    vi.useFakeTimers();
    const appIds = Array.from({ length: 26 }, (_, i) => i + 1);
    const p = removeShortcutsPaced(appIds);
    for (let i = 0; i < 40; i++) await Promise.resolve();
    // First 25 removed back-to-back; the 26th is gated behind the breather.
    expect(removeShortcut).toHaveBeenCalledTimes(25);
    await vi.advanceTimersByTimeAsync(50);
    expect(removeShortcut).toHaveBeenCalledTimes(26);
    await p;
  });

  it("resolves immediately for an empty list without removing anything", async () => {
    await removeShortcutsPaced([]);
    expect(removeShortcut).not.toHaveBeenCalled();
  });

  it("removes each app_id exactly once, in order", async () => {
    await removeShortcutsPaced([5, 9, 3]);
    expect(removeShortcut.mock.calls.map((c) => c[0])).toEqual([5, 9, 3]);
  });

  it("reports (removed, total) after each removal when a progress callback is given", async () => {
    const progress: [number, number][] = [];
    await removeShortcutsPaced([11, 22, 33], (removed, total) => progress.push([removed, total]));
    expect(progress).toEqual([
      [1, 3],
      [2, 3],
      [3, 3],
    ]);
  });

  it("does not start another Steam removal after its abort signal fires", async () => {
    let settle!: () => void;
    removeShortcut.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const controller = new AbortController();
    const removal = removeShortcutsPaced([11, 22], undefined, controller.signal);
    await Promise.resolve();

    controller.abort();
    settle();
    await removal;

    expect(removeShortcut.mock.calls.map((call) => call[0])).toEqual([11]);
  });
});
