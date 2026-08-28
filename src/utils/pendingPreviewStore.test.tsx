import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, act } from "@testing-library/react";
import { createElement } from "react";
import {
  usePendingPreview,
  getPendingPreviewSnapshot,
  onPendingPreviewChange,
  adoptPreview,
  clearPendingPreview,
  refreshPendingPreview,
  resetPendingPreviewStoreForTests,
} from "./pendingPreviewStore";
import * as backend from "../api/backend";
import type { SyncPreview } from "../types";

vi.mock("../api/backend", () => ({
  getPendingPreview: vi.fn(),
  logError: vi.fn(),
}));

function preview(id: string): SyncPreview {
  return {
    success: true,
    summary: {
      new_count: 1,
      changed_count: 0,
      unchanged_count: 0,
      remove_count: 0,
      disabled_platform_remove_count: 0,
    },
    new_names: ["a"],
    changed_names: [],
    preview_id: id,
  };
}

/** A promise plus the handle to settle it, so a test can hold a read open
 *  across a user answer and decide when it lands. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (e: unknown) => void } {
  let resolve!: (value: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("pendingPreviewStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    resetPendingPreviewStoreForTests();
    vi.mocked(backend.getPendingPreview).mockResolvedValue({ success: true, preview: null });
  });

  it("starts empty and hands the same snapshot back until it changes", () => {
    // The useSyncExternalStore contract: React compares snapshots by identity,
    // so a getter that allocates per call re-renders forever.
    expect(getPendingPreviewSnapshot()).toBeNull();
    const held = preview("p1");
    adoptPreview(held);
    expect(getPendingPreviewSnapshot()).toBe(held);
    expect(getPendingPreviewSnapshot()).toBe(getPendingPreviewSnapshot());
  });

  it("notifies subscribers on a change and stops on unsubscribe", () => {
    const seen = vi.fn();
    const unsubscribe = onPendingPreviewChange(seen);
    adoptPreview(preview("p1"));
    expect(seen).toHaveBeenCalledTimes(1);
    clearPendingPreview();
    expect(seen).toHaveBeenCalledTimes(2);
    unsubscribe();
    adoptPreview(preview("p2"));
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("stays silent when a write installs the value already stored", () => {
    const held = preview("p1");
    adoptPreview(held);
    const seen = vi.fn();
    onPendingPreviewChange(seen);
    adoptPreview(held);
    expect(seen).not.toHaveBeenCalled();
    clearPendingPreview();
    clearPendingPreview();
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it("fills from the backend's staged snapshot", async () => {
    vi.mocked(backend.getPendingPreview).mockResolvedValue({ success: true, preview: preview("staged") });
    await refreshPendingPreview();
    expect(getPendingPreviewSnapshot()?.preview_id).toBe("staged");
  });

  it("a read cannot restore a card the user answered while it was open", async () => {
    // The ordering rule: the read describes a world in which the dismiss had not
    // happened, so the dismiss — issued later — outranks it.
    const read = deferred<{ success: boolean; preview: SyncPreview | null }>();
    vi.mocked(backend.getPendingPreview).mockReturnValue(read.promise);
    const inFlight = refreshPendingPreview();

    clearPendingPreview();
    read.resolve({ success: true, preview: preview("dismissed") });
    await inFlight;

    expect(getPendingPreviewSnapshot()).toBeNull();
  });

  it("a read cannot overwrite a preview produced while it was open", async () => {
    const read = deferred<{ success: boolean; preview: SyncPreview | null }>();
    vi.mocked(backend.getPendingPreview).mockReturnValue(read.promise);
    const inFlight = refreshPendingPreview();

    adoptPreview(preview("fresh"));
    read.resolve({ success: true, preview: preview("older") });
    await inFlight;

    expect(getPendingPreviewSnapshot()?.preview_id).toBe("fresh");
  });

  it("between two reads the one asked for last wins, whichever answers first", async () => {
    const first = deferred<{ success: boolean; preview: SyncPreview | null }>();
    const second = deferred<{ success: boolean; preview: SyncPreview | null }>();
    vi.mocked(backend.getPendingPreview).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const a = refreshPendingPreview();
    const b = refreshPendingPreview();

    second.resolve({ success: true, preview: preview("newer") });
    await b;
    first.resolve({ success: true, preview: preview("older") });
    await a;

    expect(getPendingPreviewSnapshot()?.preview_id).toBe("newer");
  });

  it("a null answer never drops a preview the store holds", async () => {
    // `preview: null` conflates nothing-staged, aged-out and withheld-while-a-
    // run-is-in-flight, so it is not evidence that this card is gone.
    adoptPreview(preview("held"));
    vi.mocked(backend.getPendingPreview).mockResolvedValue({ success: true, preview: null });

    await refreshPendingPreview();

    expect(getPendingPreviewSnapshot()?.preview_id).toBe("held");
  });

  it("an unsuccessful answer never drops a preview the store holds", async () => {
    adoptPreview(preview("held"));
    vi.mocked(backend.getPendingPreview).mockResolvedValue({ success: false, preview: preview("ignored") });

    await refreshPendingPreview();

    expect(getPendingPreviewSnapshot()?.preview_id).toBe("held");
  });

  it("logs a rejected read and leaves the store as it stands", async () => {
    adoptPreview(preview("held"));
    vi.mocked(backend.getPendingPreview).mockRejectedValue(new Error("boom"));

    await expect(refreshPendingPreview()).resolves.toBeUndefined();

    expect(vi.mocked(backend.logError)).toHaveBeenCalledWith(
      expect.stringContaining("Failed to query pending preview"),
    );
    expect(getPendingPreviewSnapshot()?.preview_id).toBe("held");
  });

  it("a failed read is not an answer — a later read still applies", async () => {
    const failing = deferred<{ success: boolean; preview: SyncPreview | null }>();
    vi.mocked(backend.getPendingPreview).mockReturnValueOnce(failing.promise);
    const rejected = refreshPendingPreview();
    failing.reject(new Error("boom"));
    await rejected;

    vi.mocked(backend.getPendingPreview).mockResolvedValue({ success: true, preview: preview("after") });
    await refreshPendingPreview();

    expect(getPendingPreviewSnapshot()?.preview_id).toBe("after");
  });

  it("re-renders a subscribed component once per change, and never loops", () => {
    // The snapshot-identity discipline, observed from React's side: a getter that
    // allocated per call would re-render without end here rather than twice.
    let renders = 0;
    const Probe = () => {
      renders += 1;
      return createElement("div", null, usePendingPreview()?.preview_id ?? "none");
    };
    const { container, unmount } = render(createElement(Probe));
    expect(container.textContent).toBe("none");
    const atStart = renders;

    act(() => adoptPreview(preview("p1")));
    expect(container.textContent).toBe("p1");
    act(() => clearPendingPreview());
    expect(container.textContent).toBe("none");
    expect(renders).toBe(atStart + 2);

    unmount();
    adoptPreview(preview("p2"));
    expect(renders).toBe(atStart + 2);
  });
});
