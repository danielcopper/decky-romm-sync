"""Pure functions for building shortcut data dicts.

No I/O, no imports from services, adapters, or lib.
"""

from __future__ import annotations

import os
from typing import Any


def build_shortcuts_data(roms: list[dict[str, Any]], plugin_dir: str) -> list[dict[str, Any]]:
    """Transform ROM list into shortcut data dicts for frontend AddShortcut calls."""
    exe = os.path.join(plugin_dir, "bin", "romm-launcher")
    start_dir = os.path.join(plugin_dir, "bin")
    return [
        {
            "rom_id": rom["id"],
            "name": rom["name"],
            "fs_name": rom.get("fs_name", ""),
            "exe": exe,
            "start_dir": start_dir,
            "launch_options": f"romm:{rom['id']}",
            "platform_name": rom.get("platform_name", "Unknown"),
            "platform_slug": rom.get("platform_slug", ""),
            "igdb_id": rom.get("igdb_id"),
            "sgdb_id": rom.get("sgdb_id"),
            "ra_id": rom.get("ra_id"),
            "cover_path": "",
        }
        for rom in roms
    ]
