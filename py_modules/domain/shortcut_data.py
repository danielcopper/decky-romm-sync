"""Pure functions for building shortcut data dicts and launch commands.

No I/O, no imports from services, adapters, or lib.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from domain.sibling_group import compute_sibling_group_key

# The emulator invocation prefix the launch command wraps the resolved ROM
# path with. RetroDECK's flatpak command is the only value today.
RETRODECK_INVOCATION = "flatpak run net.retrodeck.retrodeck"

# RetroArch cores dir as seen INSIDE the RetroDECK flatpak sandbox. Baked
# literally into the -e override; %EMULATOR_RETROARCH% and %ROM% stay as ES-DE
# placeholders (run_game.sh resolves and quotes them at launch).
_RETROARCH_CORES_DIR = "/var/config/retroarch/cores"


@dataclass(frozen=True)
class EmulatorInvocation:
    """What a ROM launches with — a RetroArch libretro core OR a standalone emulator.

    The plugin resolves one of these per ROM and bakes it into the shortcut's
    ``launch_options`` via :func:`resolve_emulator_invocation`. Exactly one of
    ``core_so`` / ``command`` carries the payload:

    - ``kind == "libretro"`` → ``core_so`` is the BARE core name (no ``.so``); the
      renderer emits the RetroArch ``-L <coresdir>/<so>.so %ROM%`` form (the cores
      dir is baked literally because RetroDECK does not expand ``%CORE_RETROARCH%``
      through ``-e``).
    - ``kind == "standalone"`` → ``command`` is the full ES-DE ``<command>`` text
      (already ending in ``%ROM%``, e.g. ``%EMULATOR_RPCS3% --no-gui %ROM%``),
      baked verbatim into ``-e``. RetroDECK resolves ``%EMULATOR_*%`` and
      substitutes ``%ROM%`` with the trailing rom path at launch — the same path
      the libretro form relies on.

    ``label`` is the ES-DE display label (diagnostics only). This is the
    standalone-emulator seam (#129); read-path consumers that only understand
    libretro keep reading ``core_so`` (``None`` for a standalone emulator) and
    degrade exactly as they do for a ``(None, None)`` resolution.
    """

    kind: str  # "libretro" | "standalone"
    label: str | None = None
    core_so: str | None = None
    command: str | None = None

    @classmethod
    def libretro(cls, core_so: str, label: str | None = None) -> EmulatorInvocation:
        """A RetroArch libretro core, identified by its bare ``.so`` name."""
        return cls(kind="libretro", label=label, core_so=core_so)

    @classmethod
    def standalone(cls, command: str, label: str | None = None) -> EmulatorInvocation:
        """A standalone emulator, identified by its full ES-DE ``<command>`` text."""
        return cls(kind="standalone", label=label, command=command)


def resolve_emulator_invocation(rom: dict[str, Any], emulator: EmulatorInvocation | None = None) -> str:
    """Return the emulator invocation prefix for *rom*.

    With *emulator* unset (``None``) the ROM follows the plain RetroDECK flatpak
    command (the single genuine fallback for a platform with no resolvable
    emulator). A **libretro** invocation renders the RetroDECK ``-e`` override that
    forces that RetroArch core:
    ``flatpak run … -e "%EMULATOR_RETROARCH% -L <cores>/<so>.so %ROM%"`` (cores dir
    literal; ``%EMULATOR_RETROARCH%`` / ``%ROM%`` stay ES-DE placeholders). A
    **standalone** invocation bakes the emulator's full ES-DE command verbatim:
    ``flatpak run … -e "<command … %ROM%>"`` (e.g. ``%EMULATOR_RPCS3% --no-gui
    %ROM%``) — RetroDECK resolves ``%EMULATOR_*%`` and substitutes ``%ROM%`` at
    launch. *rom* is the per-emulator-branch seam and is ignored today.
    """
    del rom  # reserved for the future per-emulator branch
    # Branch explicitly so a half-resolved invocation never reaches the f-string
    # (no "None.so" / empty -e); anything unrenderable degrades to the plain launch.
    if emulator is None:
        return RETRODECK_INVOCATION
    if emulator.kind == "standalone" and emulator.command:
        return f'{RETRODECK_INVOCATION} -e "{emulator.command}"'
    if emulator.kind == "libretro" and emulator.core_so:
        # The bare core name + ".so" forms the on-disk RetroArch core path -L expects.
        return f'{RETRODECK_INVOCATION} -e "%EMULATOR_RETROARCH% -L {_RETROARCH_CORES_DIR}/{emulator.core_so}.so %ROM%"'
    return RETRODECK_INVOCATION


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

    The path is double-quoted so paths with spaces survive the launcher's
    ``exec "$@"``. Embedded ``\\`` and ``"`` in the path are backslash-escaped
    (backslash first, then quote) so a server-controlled ROM filename cannot
    break out of the quoted token and inject extra argv elements into the
    emulator invocation. Only the path is escaped — *invocation* is trusted
    build-time text whose own ``-e "..."`` quoting must survive verbatim.
    """
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'{invocation} "{escaped}"'


def build_shortcuts_data(
    roms: list[dict[str, Any]],
    plugin_dir: str,
    installed_paths: dict[int, str],
    core_overrides: dict[int, EmulatorInvocation],
) -> list[dict[str, Any]]:
    """Transform ROM list into shortcut data dicts for frontend AddShortcut calls.

    *installed_paths* maps ``rom_id`` to the resolved on-disk launch path. An
    installed ROM gets a full launch command in ``launch_options``; a ROM absent
    from the map gets ``""`` (empty placeholder) until it is downloaded.

    *core_overrides* maps ``rom_id`` to the **already-resolved**
    :class:`EmulatorInvocation` the ROM launches with (its full active emulator —
    libretro core or standalone — folding the per-game/per-platform override over
    the es_systems default). Only ROMs that resolved to an emulator appear (the
    caller omits the ``(None, None)`` fallback); a ROM absent from the map follows
    the plain RetroDECK launch, a present ROM bakes its ``-e`` form into
    ``launch_options``. Required so a new bake site can never silently skip the
    override.

    The sibling-group key (ADR-0019) and RomM's version dimensions (``regions`` /
    ``languages`` / ``revision`` / ``tags`` / ``is_main_sibling``) are derived
    from each raw ROM dict here and carried through so the commit persists them
    on the ``Rom`` aggregate. ``is_main_sibling`` sits under ``rom_user``; the
    lookup is guarded so a missing or ``null`` ``rom_user`` degrades to ``False``.
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
            "sibling_group_key": compute_sibling_group_key(rom),
            "regions": list(rom.get("regions") or []),
            "languages": list(rom.get("languages") or []),
            "revision": rom.get("revision") or "",
            "tags": list(rom.get("tags") or []),
            "is_main_sibling": bool((rom.get("rom_user") or {}).get("is_main_sibling", False)),
        }
        for rom in roms
    ]
