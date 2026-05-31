/**
 * Pure helpers for the `download_failed` event listeners.
 *
 * Two surfaces consume the event:
 *   - Global (src/index.tsx): updates the download store and surfaces a toast.
 *   - Per-button (src/components/CustomPlayButton.tsx): resets local play-button
 *     state back to "download" when the failed event matches the button's rom.
 *
 * Extracting the listener bodies into pure functions keeps the @decky/api
 * listener registration as a one-line delegation and makes the behavior
 * testable without an addEventListener mock harness.
 */

import type { DownloadFailedEvent, DownloadItem } from "../types";

export interface ToasterLike {
  toast: (msg: { title: string; body: string }) => void;
}

export interface DownloadStoreLike {
  getDownloadState: () => DownloadItem[];
  updateDownload: (item: DownloadItem) => void;
}

/**
 * Apply a `download_failed` event globally: flip the matching entry in the
 * download store to `status: "failed"` (carrying `error_message` as `error`)
 * and surface a toast. Missing prior entries are tolerated — the store gains
 * a synthetic entry with zeroed progress fields.
 */
export function handleGlobalDownloadFailure(
  event: DownloadFailedEvent,
  store: DownloadStoreLike,
  toast: ToasterLike,
): void {
  const prev = store.getDownloadState().find((d) => d.rom_id === event.rom_id);
  store.updateDownload({
    rom_id: event.rom_id,
    rom_name: event.rom_name,
    platform_name: event.platform_name,
    file_name: prev?.file_name ?? "",
    status: "failed",
    progress: prev?.progress ?? 0,
    bytes_downloaded: prev?.bytes_downloaded ?? 0,
    total_bytes: prev?.total_bytes ?? 0,
    error: event.error_message,
  });
  toast.toast({
    title: "RomM Sync",
    body: `Download failed: ${event.rom_name} — ${event.error_message}`,
  });
}

/**
 * Apply a `download_failed` event for a specific play button: if the event
 * targets `romId`, reset the button back to the "download" state so the user
 * can retry. No-op for events targeting other roms or when the button has no
 * associated rom yet (`romId === null`). The toast is owned by the global
 * handler — this helper intentionally does not toast.
 */
export function handleButtonDownloadFailure(event: DownloadFailedEvent, romId: number | null, reset: () => void): void {
  if (romId === null || event.rom_id !== romId) return;
  reset();
}
