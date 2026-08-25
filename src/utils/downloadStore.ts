/**
 * Module-level download state store — single source of truth.
 *
 * Updated by:
 *   - download_progress events from the backend (persistent listener in index.tsx)
 *   - download_complete events from the backend (persistent listener in index.tsx)
 *   - download_failed events from the backend (persistent listener in index.tsx)
 *   - DownloadQueue.tsx, which seeds the store from the backend queue on mount
 *     and drops the terminal entries after a successful "Clear Completed"
 *
 * Read by:
 *   - MainPage.tsx and DownloadQueue.tsx, both through {@link useDownloads}
 *   - the download_progress / download_failed handlers themselves, which read
 *     the current entry through {@link getDownloadState} to carry forward the
 *     fields their event frame does not carry
 *
 * The state is held immutably: every mutation that changes something installs a
 * NEW array and notifies, and one that changes nothing does neither. That is
 * what lets {@link getDownloadState} serve as a `useSyncExternalStore` snapshot
 * — React compares snapshots by identity, so a getter handing back a fresh copy
 * per call would re-render forever, and an in-place write would leave a
 * subscriber unable to tell that a download's progress moved.
 *
 * "Changed something" is decided by entry identity, never by comparing fields:
 * every store write is fed by an event frame or a fetch that builds fresh
 * objects, so a re-used object reference is the only case in which the caller
 * genuinely carries no news.
 */

import { useSyncExternalStore } from "react";
import type { DownloadItem } from "../types";

let _downloads: DownloadItem[] = [];
const _listeners = new Set<() => void>();

function notify(): void {
  _listeners.forEach((fn) => fn());
}

function sameEntries(a: readonly DownloadItem[], b: readonly DownloadItem[]): boolean {
  return a.length === b.length && a.every((item, i) => item === b[i]);
}

/** Replace the whole queue. Stores a copy, so a caller that keeps mutating the
 *  array it passed in cannot change store state behind the subscribers' backs. */
export function setDownloads(items: readonly DownloadItem[]): void {
  if (sameEntries(_downloads, items)) return;
  _downloads = items.slice();
  notify();
}

export function updateDownload(item: DownloadItem): void {
  const idx = _downloads.findIndex((d) => d.rom_id === item.rom_id);
  if (idx >= 0) {
    if (_downloads[idx] === item) return;
    const next = _downloads.slice();
    next[idx] = item;
    _downloads = next;
  } else {
    _downloads = [..._downloads, item];
  }
  notify();
}

// Drop a single entry by rom_id. The download_progress cancelled listener calls
// this so a cancelled download — an explicit user discard — leaves no residue in
// the queue view or QAM summary (#149 downloads-round). Idempotent: an unknown
// rom_id keeps the array and stays silent.
export function removeDownload(romId: number): void {
  const next = _downloads.filter((d) => d.rom_id !== romId);
  if (next.length === _downloads.length) return;
  _downloads = next;
  notify();
}

export function getDownloadState(): readonly DownloadItem[] {
  return _downloads;
}

// Drop every terminal (completed/failed/cancelled) entry from the store,
// mirroring the backend's "Clear Completed" eviction (#149). Active, queued,
// paused, and extracting entries stay. Called after clear_completed_downloads
// succeeds so the store matches the freshly-evicted backend queue immediately,
// rather than waiting for the next mount fetch.
export function removeTerminalDownloads(): void {
  const next = _downloads.filter((d) => d.status !== "completed" && d.status !== "failed" && d.status !== "cancelled");
  if (next.length === _downloads.length) return;
  _downloads = next;
  notify();
}

export function onDownloadsChange(fn: () => void): () => void {
  _listeners.add(fn);
  return () => {
    _listeners.delete(fn);
  };
}

/** Subscribe to the download queue from a component. Re-renders the caller
 *  whenever the queue changes and drops its subscription on unmount. */
export function useDownloads(): readonly DownloadItem[] {
  return useSyncExternalStore(onDownloadsChange, getDownloadState);
}
