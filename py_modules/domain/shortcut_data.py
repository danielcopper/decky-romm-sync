"""Pure functions for building shortcut data dicts and launch commands.

No I/O, no imports from services, adapters, or lib.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from domain.sibling_group import compute_sibling_group_key

# RetroDECK's flatpak application id. Its plain ``flatpak run <app>`` form is the
# emulator invocation prefix the launch command wraps the resolved ROM path with;
# the folder-boot ``direct`` form threads a ``--command=<launcher>`` between the
# ``flatpak run`` verb and the app id (see :func:`resolve_emulator_invocation`).
_RETRODECK_APP_ID = "net.retrodeck.retrodeck"
RETRODECK_INVOCATION = f"flatpak run {_RETRODECK_APP_ID}"

# The leading ``%EMULATOR_<NAME>%`` binary token and the trailing ``%ROM%`` target
# of an ES-DE ``<command>`` — stripped from a standalone command to recover the
# middle launcher args (e.g. ``--no-gui``) for the folder-boot ``direct`` bake.
_EMULATOR_TOKEN_RE = re.compile(r"%EMULATOR_[A-Z0-9_-]+%")

# RetroArch cores dir as seen INSIDE the RetroDECK flatpak sandbox. Baked
# literally into the -e override; %EMULATOR_RETROARCH% and %ROM% stay as ES-DE
# placeholders (run_game.sh resolves and quotes them at launch).
_RETROARCH_CORES_DIR = "/var/config/retroarch/cores"


@dataclass(frozen=True)
class EmulatorInvocation:
    """What a ROM launches with — a libretro core, a standalone emulator, or a direct sandbox launch.

    The plugin resolves one of these per ROM and bakes it into the shortcut's
    ``launch_options`` via :func:`resolve_emulator_invocation`. The payload
    carried depends on ``kind``:

    - ``kind == "libretro"`` → ``core_so`` is the BARE core name (no ``.so``); the
      renderer emits the RetroArch ``-L <coresdir>/<so>.so %ROM%`` form (the cores
      dir is baked literally because RetroDECK does not expand ``%CORE_RETROARCH%``
      through ``-e``).
    - ``kind == "standalone"`` → ``command`` is the full ES-DE ``<command>`` text
      (already ending in ``%ROM%``, e.g. ``%EMULATOR_RPCS3% --no-gui %ROM%``),
      baked verbatim into ``-e``. RetroDECK's ``run_game.sh`` resolves
      ``%EMULATOR_*%`` and substitutes ``%ROM%`` with the trailing rom path.
    - ``kind == "direct"`` → the folder-boot form (ADR-0019): ``command`` is the
      same full ES-DE standalone command AND ``launcher`` is the emulator's
      sandbox launcher path (e.g.
      ``/app/retrodeck/components/rpcs3/component_launcher.sh``). The renderer
      emits ``flatpak run --command=<launcher> <app> <args>`` — running the
      emulator launcher directly INSIDE the sandbox, bypassing ``run_game.sh``,
      because ``run_game.sh`` reinterprets any directory ``%ROM%`` as an ES-DE
      "directory as a file" and can never launch a bare game folder. The ``<args>``
      are the standalone command's middle (``%EMULATOR_*%`` and ``%ROM%``
      stripped, e.g. ``--no-gui``); the game folder is appended by
      :func:`build_launch_options`.

    ``label`` is the ES-DE display label (diagnostics only). This is the
    standalone-emulator seam (#129); read-path consumers that only understand
    libretro keep reading ``core_so`` (``None`` for a standalone or direct
    emulator) and degrade exactly as they do for a ``(None, None)`` resolution.
    """

    kind: str  # "libretro" | "standalone" | "direct"
    label: str | None = None
    core_so: str | None = None
    command: str | None = None
    launcher: str | None = None

    @classmethod
    def libretro(cls, core_so: str, label: str | None = None) -> EmulatorInvocation:
        """A RetroArch libretro core, identified by its bare ``.so`` name."""
        return cls(kind="libretro", label=label, core_so=core_so)

    @classmethod
    def standalone(cls, command: str, label: str | None = None) -> EmulatorInvocation:
        """A standalone emulator, identified by its full ES-DE ``<command>`` text."""
        return cls(kind="standalone", label=label, command=command)

    @classmethod
    def direct(cls, command: str, launcher: str, label: str | None = None) -> EmulatorInvocation:
        """A standalone emulator launched directly via its sandbox *launcher* (folder-boot form).

        *command* is the full ES-DE standalone ``<command>`` (its middle args are
        recovered at render time); *launcher* is the emulator's sandbox launcher
        path handed to ``flatpak run --command=``.
        """
        return cls(kind="direct", label=label, command=command, launcher=launcher)


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
    if emulator.kind == "direct" and emulator.launcher and emulator.command:
        # Run the emulator's sandbox launcher directly, bypassing run_game.sh's
        # directory-as-a-file reinterpretation (ADR-0019). The game folder is
        # appended by build_launch_options; only the middle args ride here.
        args = _direct_launch_args(emulator.command)
        base = f"flatpak run --command={emulator.launcher} {_RETRODECK_APP_ID}"
        return f"{base} {args}" if args else base
    if emulator.kind == "standalone" and emulator.command:
        return f'{RETRODECK_INVOCATION} -e "{emulator.command}"'
    if emulator.kind == "libretro" and emulator.core_so:
        # The bare core name + ".so" forms the on-disk RetroArch core path -L expects.
        return f'{RETRODECK_INVOCATION} -e "%EMULATOR_RETROARCH% -L {_RETROARCH_CORES_DIR}/{emulator.core_so}.so %ROM%"'
    return RETRODECK_INVOCATION


def _direct_launch_args(command: str) -> str:
    """Recover a standalone command's middle launcher args for the ``direct`` bake.

    Strips the leading ``%EMULATOR_<NAME>%`` binary token(s) and the ``%ROM%``
    target from an ES-DE ``<command>``, collapsing surrounding whitespace, so
    ``%EMULATOR_RPCS3% --no-gui %ROM%`` yields ``--no-gui`` and
    ``%EMULATOR_RPCS3% %ROM%`` yields ``""``. Pure text.
    """
    stripped = _EMULATOR_TOKEN_RE.sub("", command).replace("%ROM%", "")
    return " ".join(stripped.split())


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

    The sibling-group key (ADR-0021) and RomM's version dimensions (``regions`` /
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
