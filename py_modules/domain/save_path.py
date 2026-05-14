"""Save file path and filename resolution for RetroArch save directory layouts.

Handles the various save directory structures created by RetroArch's
sort_savefiles_by_content_enable and sort_savefiles_enable settings,
plus deriving the canonical local filename for a server-supplied save
record.

No I/O, no service/adapter/lib imports. Pure functions only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def resolve_save_dir(
    rom_path: str,
    saves_base: str,
    system: str,
    *,
    roms_base: str | None = None,
    sort_by_content: bool = True,
    sort_by_core: bool = False,
    core_name: str | None = None,
) -> str:
    """Resolve the save directory for a ROM.

    Parameters
    ----------
    rom_path:
        ROM file path — absolute or relative (e.g. "gba/Game.gba",
        "/home/deck/retrodeck/roms/gba/Game.gba", or
        "psx/Game (USA)/Game.m3u").
    saves_base:
        Absolute path to the RetroArch saves root directory.
    system:
        Platform/system slug (e.g. "gba", "psx"). Used as fallback subdir.
    roms_base:
        If provided, strip this absolute prefix from rom_path before
        computing the content directory. Allows callers to pass absolute
        ROM paths directly without pre-stripping.
    sort_by_content:
        If True, save subdir = last folder component of rom_path.
        RetroDECK default: True.
    sort_by_core:
        If True, adds a core name subfolder inside the content dir.
        RetroDECK default: False. Requires core_name.
    core_name:
        RetroArch core name (e.g. "mgba_libretro"). Required when sort_by_core=True.

    Returns
    -------
    str
        Absolute path to the directory where save files should be found/placed.
    """
    # Strip roms_base prefix if provided to get a relative path
    effective_path = rom_path
    if roms_base is not None:
        norm_rom = os.path.normpath(rom_path)
        norm_base = os.path.normpath(roms_base)
        if norm_rom.startswith(norm_base + os.sep):
            effective_path = norm_rom[len(norm_base) + 1 :]
        elif norm_rom.startswith(norm_base):
            effective_path = norm_rom[len(norm_base) :]

    parts: list[str] = [saves_base]

    if sort_by_content:
        # Last folder component of the effective_path (i.e. the directory containing the ROM file)
        rom_dir = os.path.dirname(effective_path)
        content_dir = os.path.basename(rom_dir) if rom_dir else system
        parts.append(content_dir)

    if sort_by_core and core_name:
        parts.append(core_name)

    return os.path.join(*parts)


def resolve_save_filename(rom_path: str, ext: str = ".srm") -> str:
    """Derive the save filename from a ROM path.

    Takes the ROM's basename, strips extension, appends save extension.
    E.g. "gba/Pokemon.gba" -> "Pokemon.srm"
    """
    basename = os.path.basename(rom_path)
    name, _ = os.path.splitext(basename)
    return name + ext


def sanitize_save_filename(name: str) -> str:
    """Reduce *name* to a safe filename component for joining onto ``saves_dir``.

    Defends path joins against compromised-server data (e.g. a malicious
    ``file_extension``) and frontend-supplied filenames (e.g. the
    ``resolve_sync_conflict`` callable parameter). Pure: no I/O, stdlib only.

    Returns the basename of *name* unchanged when it is already a single
    safe component. Raises :class:`ValueError` for inputs that cannot be
    coerced into a usable filename:

    - empty string
    - ``"."`` or ``".."``
    - any string containing a NUL byte
    - inputs whose basename is empty (e.g. trailing path separator)
    """
    if "\x00" in name:
        raise ValueError("filename contains a NUL byte")
    if name in ("", ".", ".."):
        raise ValueError(f"filename is not a valid path component: {name!r}")
    base = os.path.basename(name)
    if base in ("", ".", ".."):
        raise ValueError(f"filename has no valid basename: {name!r}")
    return base


@dataclass(frozen=True)
class LocalSaveTarget:
    """Resolved local filename for a server save with sanitization diagnostics.

    ``filename`` is the canonical on-disk name.

    ``fallback_extension`` is set to the offending value when the
    server-supplied ``file_extension`` produced an unusable filename and
    the function fell back to ``"srm"``. Service callers should log a
    warning when this is non-None.

    ``sanitized_from`` is set to the pre-sanitization filename when
    path-traversal characters were stripped (e.g. ``../etc/passwd``).
    Service callers should log a warning when this is non-None.

    Both flags are mutually exclusive: an unusable extension triggers
    fallback; a sanitizable one triggers strip-and-keep.
    """

    filename: str
    fallback_extension: str | None = None
    sanitized_from: str | None = None


def compute_local_save_target(server_save: dict, rom_name: str) -> LocalSaveTarget:
    """The canonical local filename for a server save: ``<rom_name>.<ext>``.

    ``rom_name`` is the deterministic identity from RetroArch's
    perspective — the ROM file's basename without extension, the same
    string RetroArch uses to look up SRAM. Callers must have already
    resolved the ROM via an "installed?" check; there is no fallback to
    server-derived names because those can mismatch RetroArch's lookup
    path and silently break the sync.
    """
    ext = server_save.get("file_extension", "srm")
    target = f"{rom_name}.{ext}"
    try:
        sanitized = sanitize_save_filename(target)
    except ValueError:
        return LocalSaveTarget(f"{rom_name}.srm", fallback_extension=ext)
    if sanitized != target:
        return LocalSaveTarget(sanitized, sanitized_from=target)
    return LocalSaveTarget(sanitized)


def detect_path_change(stored_path: str | None, resolved_path: str) -> bool:
    """Detect if the save path has changed since last sync.

    Useful for warning users when RetroArch settings change (e.g.
    sort_by_content toggled) which would move where saves are expected.

    Returns True if paths differ (or stored_path is None -- first sync).
    """
    if stored_path is None:
        return True
    return stored_path != resolved_path
