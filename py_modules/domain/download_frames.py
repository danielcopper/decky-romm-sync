"""The payloads a download's last word carries — pure projections, no I/O.

A terminal frame is the last thing the frontend store hears about a ROM, so what
it carries is a wire contract rather than an implementation detail of whoever
emits it. Anything that builds one — a terminal ``download_progress`` payload or
the ``download_failed`` payload that replaces it — belongs here; deciding *when*
to emit one is the download service's concern.
"""

from __future__ import annotations

from typing import Any


def failed_frame(rom_id: int, rom_name: str, platform_name: str, error_message: str) -> dict[str, Any]:
    """Build the ``download_failed`` payload for a download that never landed.

    Named rather than assembled at each raise site: every failure the frontend
    is told about carries the same four keys, whether it was refused before a
    byte moved or died mid-transfer.
    """
    return {
        "rom_id": rom_id,
        "rom_name": rom_name,
        "platform_name": platform_name,
        "error_message": error_message,
    }


def cancelled_frame(rom_id: int, entry: dict[str, Any]) -> dict[str, Any]:
    """Build the terminal ``cancelled`` ``download_progress`` payload from a queue entry."""
    return {
        "rom_id": rom_id,
        "rom_name": entry.get("rom_name", ""),
        "platform_name": entry.get("platform_name", ""),
        "file_name": entry.get("file_name", ""),
        "status": "cancelled",
        "progress": entry.get("progress", 0),
        "bytes_downloaded": entry.get("bytes_downloaded", 0),
        "total_bytes": entry.get("total_bytes", 0),
        "resumable": entry.get("resumable", False),
    }
