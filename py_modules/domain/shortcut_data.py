"""Pure functions for building shortcut data dicts.

No I/O, no imports from services, adapters, or lib.
"""

from __future__ import annotations

import os


def build_shortcuts_data(roms: list[dict], plugin_dir: str) -> list[dict]:
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
            "cover_url": rom.get("path_cover_large") or rom.get("path_cover_small") or "",
        }
        for rom in roms
    ]


def build_registry_entry(pending: dict, app_id: int, cover_path: str) -> dict:
    """Build a shortcut registry entry from pending sync data."""
    entry = {
        "app_id": app_id,
        "name": pending.get("name", ""),
        "fs_name": pending.get("fs_name", ""),
        "platform_name": pending.get("platform_name", ""),
        "platform_slug": pending.get("platform_slug", ""),
        "cover_path": cover_path,
    }
    for meta_key in ("igdb_id", "sgdb_id", "ra_id"):
        if pending.get(meta_key):
            entry[meta_key] = pending[meta_key]
    return entry
