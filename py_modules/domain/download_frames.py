"""Terminal ``download_progress`` payloads — pure projection of a queue entry.

A download's terminal frame is the last thing the frontend store hears about a
ROM, so what it carries is a wire contract rather than an implementation detail
of whoever emits it. Anything that projects a queue entry onto a terminal
``download_progress`` payload belongs here; deciding *when* to emit one is the
download service's concern.
"""

from __future__ import annotations

from typing import Any


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
