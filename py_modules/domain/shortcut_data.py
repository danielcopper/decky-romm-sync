"""Pure functions for building shortcut data dicts and launch commands.

No I/O, no imports from services, adapters, or lib.
"""

from __future__ import annotations

import os
from typing import Any

# The emulator invocation prefix the launch command wraps the resolved ROM
# path with. RetroDECK's flatpak command is the only value today.
RETRODECK_INVOCATION = "flatpak run net.retrodeck.retrodeck"

# RetroArch cores dir as seen INSIDE the RetroDECK flatpak sandbox. Baked
# literally into the -e override; %EMULATOR_RETROARCH% and %ROM% stay as ES-DE
# placeholders (run_game.sh resolves and quotes them at launch).
_RETROARCH_CORES_DIR = "/var/config/retroarch/cores"


def resolve_emulator_invocation(rom: dict[str, Any], active_core_so: str | None = None) -> str:
    """Return the emulator invocation prefix for *rom*.

    With ``active_core_so`` unset (``None``) the ROM follows the RetroDECK/ES-DE
    default and resolves to the plain RetroDECK flatpak command. With a bare core
    name it returns the RetroDECK ``-e`` override that forces that RetroArch core:
    ``flatpak run … -e "%EMULATOR_RETROARCH% -L <cores>/<so>.so %ROM%"``. The
    cores dir is baked literally; ``%EMULATOR_RETROARCH%`` and ``%ROM%`` remain
    ES-DE placeholders. *rom* is the per-emulator-branch seam (#129) and is
    ignored today.
    """
    del rom  # reserved for the future per-emulator branch
    # B4: branch on None explicitly so None never reaches the f-string (no
    # "None.so"); an unresolvable override degrades to the plain launch upstream.
    if active_core_so is None:
        return RETRODECK_INVOCATION
    # active_core_so is the BARE core name (no extension) — the es_systems parser
    # captures the name without ".so" (regex group excludes the suffix), and both
    # core_defaults.json and the bios registry key on bare names too. Append the
    # ".so" here for the on-disk RetroArch core path that retroarch's -L expects.
    return f'{RETRODECK_INVOCATION} -e "%EMULATOR_RETROARCH% -L {_RETROARCH_CORES_DIR}/{active_core_so}.so %ROM%"'


def label_to_core_so(available_cores: list[dict[str, Any]], label: str) -> str | None:
    """Resolve a core *label* to its ``.so`` filename from *available_cores*.

    *available_cores* is the already-parsed list the core-info reader returns:
    ``[{"core_so": str, "label": str, "is_default": bool}, ...]``. Returns the
    matching ``core_so`` or ``None`` when no entry carries *label* (a blank or
    stale label resolves to ``None``, never to a bogus filename).
    """
    for core in available_cores:
        if core.get("label") == label:
            return core.get("core_so")
    return None


def build_launch_options(invocation: str, path: str) -> str:
    """Compose the Steam shortcut launch command from *invocation* and ROM *path*.

    The path is quoted so paths with spaces survive the launcher's ``exec "$@"``.
    """
    return f'{invocation} "{path}"'


def build_shortcuts_data(
    roms: list[dict[str, Any]],
    plugin_dir: str,
    installed_paths: dict[int, str],
    core_overrides: dict[int, str],
) -> list[dict[str, Any]]:
    """Transform ROM list into shortcut data dicts for frontend AddShortcut calls.

    *installed_paths* maps ``rom_id`` to the resolved on-disk launch path. An
    installed ROM gets a full launch command in ``launch_options``; a ROM absent
    from the map gets ``""`` (empty placeholder) until it is downloaded.

    *core_overrides* maps ``rom_id`` to the **already-resolved** ``.so`` filename
    of a per-game emulator override — only ROMs whose ``emulator_override`` LABEL
    resolved to a real core appear (the caller omits stale ones with a WARNING).
    A ROM absent from the map follows the RetroDECK/ES-DE default (plain launch,
    no ``-e``); a present ROM bakes the ``-e`` override into ``launch_options``.
    Required so a new bake site can never silently skip the override.
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
                build_launch_options(
                    resolve_emulator_invocation(rom, core_overrides.get(rom["id"])),
                    installed_paths[rom["id"]],
                )
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
