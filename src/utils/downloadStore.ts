/**
 * Module-level download state store — single source of truth.
 *
 * Updated by:
 *   - download_progress events from the backend (persistent listener in index.tsx)
 *   - download_complete events from the backend (persistent listener in index.tsx)
 *   - download_failed events from the backend (persistent listener in index.tsx)
 *
 * Read by:
 *   - DownloadQueue.tsx via a cheap setInterval (no callable round-trips)
 */

import type { DownloadItem } from "../types";

let _downloads: DownloadItem[] = [];

export function setDownloads(items: DownloadItem[]): void {
  _downloads = items;
}

export function updateDownload(item: DownloadItem): void {
  const idx = _downloads.findIndex((d) => d.rom_id === item.rom_id);
  if (idx >= 0) _downloads[idx] = item;
  else _downloads.push(item);
}

// Drop a single entry by rom_id. The download_progress cancelled listener calls
// this so a cancelled download — an explicit user discard — leaves no residue in
// the queue view or QAM summary (#149 downloads-round). Idempotent.
export function removeDownload(romId: number): void {
  _downloads = _downloads.filter((d) => d.rom_id !== romId);
}

export function getDownloadState(): DownloadItem[] {
  return _downloads;
}

// Drop every terminal (completed/failed/cancelled) entry from the store,
// mirroring the backend's "Clear Completed" eviction (#149). Active, queued,
// paused, and extracting entries stay. Called after clear_completed_downloads
// succeeds so the store matches the freshly-evicted backend queue immediately,
// rather than waiting for the next mount fetch.
export function removeTerminalDownloads(): void {
  _downloads = _downloads.filter((d) => d.status !== "completed" && d.status !== "failed" && d.status !== "cancelled");
}
