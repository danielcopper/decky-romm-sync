"""Pure functions for building shortcut data dicts and launch commands.

No I/O, no imports from services, adapters, or lib.
"""

from __future__ import annotations

import os
from typing import Any

# The emulator invocation prefix the launch command wraps the resolved ROM
# path with. RetroDECK's flatpak command is the only value today.
RETRODECK_INVOCATION = "flatpak run net.retrodeck.retrodeck"


def resolve_emulator_invocation(rom: dict[str, Any]) -> str:
    """Return the emulator invocation prefix for *rom*.

    The seam where multi-emulator support (#129) will branch per ROM; today
    every ROM resolves to the RetroDECK flatpak command and *rom* is ignored.
    """
    del rom  # reserved for the future per-emulator branch
    return RETRODECK_INVOCATION


def build_launch_options(invocation: str, path: str) -> str:
    """Compose the Steam shortcut launch command from *invocation* and ROM *path*.

    The path is quoted so paths with spaces survive the launcher's ``exec "$@"``.
    """
    return f'{invocation} "{path}"'


def build_shortcuts_data(
    roms: list[dict[str, Any]], plugin_dir: str, installed_paths: dict[int, str]
) -> list[dict[str, Any]]:
    """Transform ROM list into shortcut data dicts for frontend AddShortcut calls.

    *installed_paths* maps ``rom_id`` to the resolved on-disk launch path. An
    installed ROM gets a full launch command in ``launch_options``; a ROM absent
    from the map gets ``""`` (empty placeholder) until it is downloaded.
    """
    exe = os.path.join(plugin_dir, "bin", "rom-launcher")
    start_dir = os.path.join(plugin_dir, "bin")
    return [
        {
            "rom_id": rom["id"],
            "name": rom["name"],
            "fs_name": rom.get("fs_name", ""),
            "exe": exe,
            "start_dir": start_dir,
            "launch_options": (
                build_launch_options(resolve_emulator_invocation(rom), installed_paths[rom["id"]])
                if rom["id"] in installed_paths
                else ""
            ),
            "platform_name": rom.get("platform_name", "Unknown"),
            "platform_slug": rom.get("platform_slug", ""),
            "igdb_id": rom.get("igdb_id"),
            "sgdb_id": rom.get("sgdb_id"),
            "ra_id": rom.get("ra_id"),
            "cover_path": "",
        }
        for rom in roms
    ]
