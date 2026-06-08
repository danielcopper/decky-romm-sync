"""ROM file format logic — pure decision/content functions.

These functions contain no I/O. File discovery and writing remain
in the calling service. The functions operate on file lists passed
as parameters.
"""

from __future__ import annotations

import os
from typing import Any

_DISC_EXTENSIONS = (".cue", ".chd", ".iso")


def is_multi_file_download(rom_detail: dict[str, Any]) -> bool:
    """Decide whether RomM will serve this ROM as a ZIP that must be extracted.

    This is the single multi-vs-single gate for the download path. It must
    mirror RomM's own download gate rather than RomM's ``has_multiple_files``
    flag, because the two are computed from different file counts:

    - RomM sets ``has_multiple_files = len(top_level_files) > 1`` — it counts
      only files at the ROM root.
    - RomM's download/content endpoint zips whenever ``len(rom.files) != 1`` —
      it counts *all* files, including ones in subfolders.

    A canonical Switch game is a folder with the base file at the root plus
    ``update/`` and ``dlc/`` in subfolders: exactly one top-level file, so
    ``has_multiple_files`` is ``False`` and ``has_nested_single_file`` is
    ``True``, yet ``len(files) > 1`` so RomM streams a ZIP. Keying on the flag
    alone takes the single-file path and writes the ZIP bytes verbatim into one
    file the emulator cannot read.

    Returning ``len(files) > 1 OR has_multiple_files`` keys on the total file
    count (matching the zip decision) while keeping the flag as a defensive
    fallback for payloads that omit ``files``. Genuine nested-single ROMs have
    ``len(files) == 1`` and correctly stay on the single-file path.
    """
    files = rom_detail.get("files") or []
    return len(files) > 1 or bool(rom_detail.get("has_multiple_files", False))


def needs_m3u(disc_files: list[str]) -> bool:
    """Return True if an M3U playlist should be generated.

    An M3U is generated for **multi-disc** ROMs (2 or more disc files of any
    kind — cue/chd/iso — so the emulator can switch discs) and for
    **single-disc bin/cue** ROMs (exactly one ``.cue`` — so the extract dir can
    be named after a game-named playlist for ES-DE collapse; bin/cue systems
    are M3U-friendly, unlike iso-based GameCube/Wii). Single-disc chd/iso get no
    M3U: they arrive as single-file downloads that never reach this path, and
    iso-based titles do not reliably launch from a single-entry M3U.

    Parameters
    ----------
    disc_files:
        Relative paths of disc files (.cue, .chd, .iso) found in the
        extraction directory. Must already exclude any existing .m3u files.
    """
    return len(disc_files) >= 2 or (len(disc_files) == 1 and disc_files[0].lower().endswith(".cue"))


def build_m3u_content(disc_files: list[str]) -> str:
    """Build M3U playlist content string for the given disc files.

    Parameters
    ----------
    disc_files:
        Relative paths to disc files, sorted in playlist order.

    Returns
    -------
    str
        M3U playlist content with newline-separated entries and a
        trailing newline.
    """
    sorted_files = sorted(disc_files)
    return "\n".join(sorted_files) + "\n"


def detect_launch_file(files: list[tuple[str, int]]) -> str | None:
    """Pick the best launch file from a list of (path, size) tuples.

    Priority order:
    1. M3U playlist
    2. CUE sheet
    3. WiiU: .rpx (loadiine format in code/ subdirectory)
    4. WiiU disc images: .wud, .wux, .wua
    5. PS3: EBOOT.BIN
    6. 3DS: .3ds > .cia > .cxi
    7. Largest file by size

    Parameters
    ----------
    files:
        List of (absolute_path, size_in_bytes) tuples to consider.
        If empty, returns None.

    Returns
    -------
    str | None
        Absolute path to the best launch file, or None if ``files`` is empty.
    """
    if not files:
        return None

    paths = [path for path, _size in files]

    # Prefer M3U > CUE
    for ext in (".m3u", ".cue"):
        matches = [p for p in paths if p.lower().endswith(ext)]
        if matches:
            return matches[0]

    # WiiU: loadiine format has .rpx in code/ subdirectory
    rpx_files = [p for p in paths if p.lower().endswith(".rpx")]
    if rpx_files:
        return rpx_files[0]

    # WiiU disc images
    for ext in (".wud", ".wux", ".wua"):
        matches = [p for p in paths if p.lower().endswith(ext)]
        if matches:
            return matches[0]

    # PS3: EBOOT.BIN in PS3_GAME/USRDIR/
    eboot_files = [p for p in paths if p.endswith("EBOOT.BIN")]
    if eboot_files:
        return eboot_files[0]

    # 3DS: prefer .3ds > .cia > .cxi
    for ext in (".3ds", ".cia", ".cxi"):
        matches = [p for p in paths if p.lower().endswith(ext)]
        if matches:
            return matches[0]

    # Largest file by pre-computed size
    return max(files, key=lambda t: t[1])[0]


def es_de_collapse_rename(rom_dir: str, launch_file: str) -> tuple[str, str] | None:
    """Return ``(new_rom_dir, new_launch_file)`` renaming *rom_dir* after the launch file.

    ES-DE collapses a multi-file ROM directory into a single game entry only
    when the directory is named with the launch file's full name *including*
    the extension (e.g. ``Final Fantasy VII (USA).m3u/`` containing
    ``Final Fantasy VII (USA).m3u``). The download path extracts into a dir
    named without the extension, so this computes the rename target.

    Pure path algebra only — the caller performs the filesystem move.

    Parameters
    ----------
    rom_dir:
        Absolute path of the extracted ROM directory.
    launch_file:
        Absolute path of the detected launch file (``detect_launch_file``).

    Returns
    -------
    tuple[str, str] | None
        ``(new_rom_dir, new_launch_file)`` when a rename applies, or ``None``
        when no rename is needed or possible:

        - *launch_file* is falsy or equals *rom_dir* (the detect-fallback
          case: no real launch file inside the directory).
        - *launch_file* is nested in a subdirectory of *rom_dir* — ES-DE would
          not collapse it anyway.
        - *rom_dir* is already named after the launch file (idempotent).
    """
    if not launch_file or launch_file == rom_dir:
        return None
    if os.path.dirname(launch_file) != rom_dir:
        return None
    launch_basename = os.path.basename(launch_file)
    if os.path.basename(rom_dir) == launch_basename:
        return None
    new_rom_dir = os.path.join(os.path.dirname(rom_dir), launch_basename)
    new_launch_file = os.path.join(new_rom_dir, launch_basename)
    return (new_rom_dir, new_launch_file)


def resolve_local_file_name(rom_detail: dict[str, Any]) -> tuple[str, bool]:
    """Resolve the on-disk filename for a ROM.

    For nested-single-file ROMs RomM reports ``fs_name`` as the parent
    folder, so the actual filename (with extension) lives in
    ``files[0].file_name``. For all other layouts ``fs_name`` is already
    the correct filename. When ``fs_name`` is missing the synthetic
    ``rom_<id>`` (or ``rom_unknown`` if ``id`` is also missing) is used.

    Returns
    -------
    tuple[str, bool]
        ``(filename, has_inconsistency)`` where ``has_inconsistency`` is
        ``True`` when ``has_nested_single_file=True`` but the ``files``
        list is empty — the caller may want to log a warning. In that
        inconsistent state the resolved name still falls back to
        ``fs_name``.
    """
    fs_name = rom_detail.get("fs_name", f"rom_{rom_detail.get('id', 'unknown')}")
    if not rom_detail.get("has_nested_single_file"):
        return (fs_name, False)
    files = rom_detail.get("files") or []
    if not files:
        return (fs_name, True)
    return (files[0].get("file_name") or fs_name, False)
