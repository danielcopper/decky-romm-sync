"""ROM file format logic — pure decision/content functions.

These functions contain no I/O. File discovery and writing remain
in the calling service. The functions operate on file lists passed
as parameters.
"""

from __future__ import annotations

_DISC_EXTENSIONS = (".cue", ".chd", ".iso")


def needs_m3u(disc_files: list[str]) -> bool:
    """Return True if an M3U playlist should be generated.

    Decides based on the number of disc files found. An M3U is needed
    when there are 2 or more disc files (multi-disc ROM).

    Parameters
    ----------
    disc_files:
        Relative paths of disc files (.cue, .chd, .iso) found in the
        extraction directory. Must already exclude any existing .m3u files.
    """
    return len(disc_files) >= 2


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


def resolve_local_file_name(rom_detail: dict) -> tuple[str, bool]:
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
