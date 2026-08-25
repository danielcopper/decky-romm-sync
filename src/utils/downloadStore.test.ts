// The store's contract is what replaced the two pollers (#1181), so these tests
// pin both halves of it:
//   - a real change installs a NEW array and notifies;
//   - a change that changes nothing does neither, and a subscriber does not
//     re-render.
// The reference assertions are `toBe`, not `toEqual`, on purpose: a
// `useSyncExternalStore` snapshot that returns a fresh array per call renders
// forever, and `toEqual` cannot see the difference.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  getDownloadState,
  onDownloadsChange,
  removeDownload,
  removeTerminalDownloads,
  setDownloads,
  updateDownload,
  useDownloads,
} from "./downloadStore";
import type { DownloadItem } from "../types";

function makeItem(overrides: Partial<DownloadItem> = {}): DownloadItem {
  return {
    rom_id: 1,
    rom_name: "Sonic",
    platform_name: "Genesis",
    file_name: "sonic.bin",
    status: "downloading",
    progress: 25,
    bytes_downloaded: 256,
    total_bytes: 1024,
    resumable: false,
    ...overrides,
  };
}

describe("downloadStore", () => {
  beforeEach(() => {
    setDownloads([]);
  });

  describe("snapshot identity", () => {
    it("returns the same array reference while nothing changes", () => {
      setDownloads([makeItem()]);
      expect(getDownloadState()).toBe(getDownloadState());
    });

    it("returns a different array reference after a real change", () => {
      setDownloads([makeItem()]);
      const before = getDownloadState();
      updateDownload(makeItem({ progress: 80 }));
      expect(getDownloadState()).not.toBe(before);
      // The old snapshot is untouched — the update did not write in place.
      expect(before[0]!.progress).toBe(25);
    });

    it("keeps the same array reference when a mutation changes nothing", () => {
      setDownloads([makeItem()]);
      const before = getDownloadState();
      removeDownload(999);
      removeTerminalDownloads();
      setDownloads(before);
      expect(getDownloadState()).toBe(before);
    });
  });

  describe("setDownloads", () => {
    it("replaces the queue and notifies", () => {
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      const item = makeItem({ rom_id: 7 });
      setDownloads([item]);
      expect(getDownloadState()).toEqual([item]);
      expect(listener).toHaveBeenCalledTimes(1);
      unsub();
    });

    it("stores a copy — mutating the caller's array afterwards does not reach the store", () => {
      const items = [makeItem({ rom_id: 1 })];
      setDownloads(items);
      const stored = getDownloadState();
      items.push(makeItem({ rom_id: 2 }));
      expect(getDownloadState()).toBe(stored);
      expect(getDownloadState().map((d) => d.rom_id)).toEqual([1]);
    });

    it("is silent when handed the same entries in the same order", () => {
      const item = makeItem();
      setDownloads([item]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      setDownloads([item]);
      expect(listener).not.toHaveBeenCalled();
      unsub();
    });
  });

  describe("updateDownload", () => {
    it("replaces the matching entry by rom_id and notifies", () => {
      setDownloads([makeItem({ rom_id: 1 }), makeItem({ rom_id: 2, rom_name: "Mario" })]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      updateDownload(makeItem({ rom_id: 2, rom_name: "Mario", status: "completed" }));
      expect(getDownloadState().map((d) => d.status)).toEqual(["downloading", "completed"]);
      expect(listener).toHaveBeenCalledTimes(1);
      unsub();
    });

    it("appends an entry whose rom_id is not in the queue yet", () => {
      setDownloads([makeItem({ rom_id: 1 })]);
      updateDownload(makeItem({ rom_id: 5, rom_name: "New" }));
      expect(getDownloadState().map((d) => d.rom_id)).toEqual([1, 5]);
    });

    it("is silent when handed the entry already stored", () => {
      const item = makeItem();
      setDownloads([item]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      updateDownload(item);
      expect(listener).not.toHaveBeenCalled();
      unsub();
    });
  });

  describe("removeDownload", () => {
    it("drops the entry and notifies", () => {
      setDownloads([makeItem({ rom_id: 1 }), makeItem({ rom_id: 2 })]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      removeDownload(1);
      expect(getDownloadState().map((d) => d.rom_id)).toEqual([2]);
      expect(listener).toHaveBeenCalledTimes(1);
      unsub();
    });

    it("is silent for a rom_id the queue does not hold", () => {
      setDownloads([makeItem({ rom_id: 1 })]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      removeDownload(42);
      expect(getDownloadState().map((d) => d.rom_id)).toEqual([1]);
      expect(listener).not.toHaveBeenCalled();
      unsub();
    });
  });

  describe("removeTerminalDownloads", () => {
    it("drops completed / failed / cancelled entries, keeps the rest, and notifies", () => {
      setDownloads([
        makeItem({ rom_id: 1, status: "downloading" }),
        makeItem({ rom_id: 2, status: "completed" }),
        makeItem({ rom_id: 3, status: "failed" }),
        makeItem({ rom_id: 4, status: "cancelled" }),
        makeItem({ rom_id: 5, status: "paused" }),
        makeItem({ rom_id: 6, status: "queued" }),
        makeItem({ rom_id: 7, status: "extracting" }),
      ]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      removeTerminalDownloads();
      expect(getDownloadState().map((d) => d.rom_id)).toEqual([1, 5, 6, 7]);
      expect(listener).toHaveBeenCalledTimes(1);
      unsub();
    });

    it("is silent when no entry is terminal", () => {
      setDownloads([makeItem({ rom_id: 1, status: "downloading" })]);
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      removeTerminalDownloads();
      expect(listener).not.toHaveBeenCalled();
      unsub();
    });
  });

  describe("onDownloadsChange", () => {
    it("stops notifying after the returned unsubscribe runs", () => {
      const listener = vi.fn();
      const unsub = onDownloadsChange(listener);
      setDownloads([makeItem({ rom_id: 1 })]);
      expect(listener).toHaveBeenCalledTimes(1);
      unsub();
      setDownloads([makeItem({ rom_id: 2 })]);
      expect(listener).toHaveBeenCalledTimes(1);
    });
  });

  describe("useDownloads", () => {
    it("renders the current queue and re-renders on a real change", () => {
      setDownloads([makeItem({ rom_id: 1 })]);
      const { result, unmount } = renderHook(() => useDownloads());
      expect(result.current.map((d) => d.rom_id)).toEqual([1]);

      act(() => {
        updateDownload(makeItem({ rom_id: 2, rom_name: "Mario" }));
      });
      expect(result.current.map((d) => d.rom_id)).toEqual([1, 2]);
      unmount();
    });

    it("does not re-render a subscriber when a mutation changes nothing (#1181)", () => {
      setDownloads([makeItem({ rom_id: 1 })]);
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useDownloads();
      });
      const settled = renders;

      // Each of these leaves the queue exactly as it was. Before the store
      // notified, both callers polled and spread a fresh array every tick, so
      // the equivalent of this produced a render each time.
      act(() => {
        removeDownload(999);
        removeTerminalDownloads();
      });
      expect(renders).toBe(settled);

      // A real change still gets through — the guard is not simply mute.
      act(() => {
        removeDownload(1);
      });
      expect(renders).toBeGreaterThan(settled);
      unmount();
    });

    it("stops re-rendering after unmount", () => {
      let renders = 0;
      const { unmount } = renderHook(() => {
        renders += 1;
        return useDownloads();
      });
      unmount();
      const afterUnmount = renders;
      act(() => {
        setDownloads([makeItem({ rom_id: 3 })]);
      });
      expect(renders).toBe(afterUnmount);
    });
  });
});
