/**
 * Module-level download state store — single source of truth.
 *
 * Updated by:
 *   - download_progress events from the backend (persistent listener in index.tsx)
 *   - download_complete events from the backend (persistent listener in index.tsx)
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

export function getDownloadState(): DownloadItem[] {
  return _downloads;
}
