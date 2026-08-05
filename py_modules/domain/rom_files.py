"""ROM file format logic — pure decision/content functions.

These functions contain no I/O. File discovery and writing remain
in the calling service. The functions operate on file lists passed
as parameters.
"""

from __future__ import annotations

import os
from typing import Any


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


def needs_m3u(disc_files: list[str], m3u_supported: bool) -> bool:
    """Return True if an M3U playlist should be generated.

    Gated first on *m3u_supported* — whether the platform's emulator can read a
    playlist at all (per ES-DE's own ``es_systems.xml`` extension list). When the
    platform does not support ``.m3u`` this returns ``False`` unconditionally, so
    no playlist is created for a system whose launch would break on one.

    When supported, an M3U is generated for **multi-disc** ROMs (2 or more disc
    files of any kind — cue/chd/iso — so the emulator can switch discs) and for
    **single-disc bin/cue** ROMs (exactly one ``.cue`` — so the extract dir can
    be named after a game-named playlist for ES-DE collapse). Single-disc chd/iso
    get no M3U: they arrive as single-file downloads that never reach this path.

    Parameters
    ----------
    disc_files:
        Relative paths of disc files (.cue, .chd, .iso) found in the
        extraction directory. Must already exclude any existing .m3u files.
    m3u_supported:
        Whether the target platform supports ``.m3u`` (ES-DE lists it as a
        supported extension for the system).
    """
    if not m3u_supported:
        return False
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


def detect_launch_file(files: list[tuple[str, int]], m3u_supported: bool) -> str | None:
    """Pick the best launch file from a list of (path, size) tuples.

    Priority order:
    1. M3U playlist — only when *m3u_supported* (platform-gated; otherwise a
       RomM-bundled ``.m3u`` is skipped so selection falls through to the real
       game file)
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
    m3u_supported:
        Whether the target platform supports ``.m3u`` (ES-DE lists it as a
        supported extension for the system). When ``False``, a ``.m3u`` is never
        chosen as the launch file.

    Returns
    -------
    str | None
        Absolute path to the best launch file, or None if ``files`` is empty.
    """
    if not files:
        return None

    paths = [path for path, _size in files]

    # Prefer M3U > CUE — but only honor .m3u on platforms that support it.
    for ext in (".m3u", ".cue") if m3u_supported else (".cue",):
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


# Folder-boot systems launch the game *directory*, not the nested launch file
# ``detect_launch_file`` picks. Each marker is the trailing component run that
# identifies such a layout; it is matched case-sensitively (the on-disk layout
# is standardised uppercase) and stripped to yield the game root. A future
# folder-boot system is a data-only addition here.
FOLDER_BOOT_MARKERS: tuple[tuple[str, ...], ...] = (("PS3_GAME", "USRDIR", "EBOOT.BIN"),)


def folder_boot_root(launch_path: str, rom_dir: str | None) -> str | None:
    """Return the game-root directory to bake for a folder-boot ROM, or ``None``.

    Some emulators boot a game **directory** rather than the nested launch file
    ``detect_launch_file`` selects: RPCS3 rejects ``…/PS3_GAME/USRDIR/EBOOT.BIN``
    and wants the folder that contains ``PS3_GAME``. When *launch_path*'s trailing
    components match a folder-boot marker (:data:`FOLDER_BOOT_MARKERS`), those
    components are stripped and the remaining game root is returned. This also
    collapses a one-level-deeper extract
    (``rom_dir/<Game>/PS3_GAME/USRDIR/EBOOT.BIN`` → ``rom_dir/<Game>``).

    The root is returned only when *rom_dir* is set — a single-file ROM owns no
    folder and is never a folder-boot game — **and** the derived root is
    inside-or-equal *rom_dir*, so a bare ``<roms>/<system>/PS3_GAME/USRDIR/EBOOT.BIN``
    sitting directly in the shared system directory never bakes that shared
    directory as the launch target. Returns ``None`` when no marker matches or the
    containment guard fails; the caller then keeps the launch file unchanged.

    Pure path algebra, stdlib only.
    """
    if rom_dir is None:
        return None
    rom_dir_norm = os.path.normpath(rom_dir)
    for marker in FOLDER_BOOT_MARKERS:
        root = _strip_marker_components(launch_path, marker)
        if root is None:
            continue
        root_norm = os.path.normpath(root)
        if root_norm == rom_dir_norm or root_norm.startswith(rom_dir_norm + os.sep):
            return root
    return None


def is_launchable_target(file_path: str, rom_dir: str | None, supported_extensions: frozenset[str]) -> bool:
    """Whether the system can actually launch *file_path* — the download's recorded launch file.

    :func:`detect_launch_file` ends in "largest file by size", so when no
    format-specific rule matches, whatever happens to be biggest becomes the
    launch target. A PS3 title distributed as ``.pkg`` + ``.rap`` has no
    ``EBOOT.BIN`` at all: the PKG is an installer, the game is still sealed
    inside it, and baking it produces a shortcut the emulator cannot act on.
    This is the check that catches that before the launch command is written.

    *supported_extensions* is the target system's live accept-list (ES-DE's
    per-system ``<extension>`` set, lowercased and dot-prefixed). Two cases pass
    without consulting it:

    - **An empty accept-list** — the source could not answer (unknown system, no
      ES-DE installation). Default-safe: a missing answer must never turn a
      working install into an unlaunchable one, so it accepts.
    - **A folder-boot layout** (:data:`FOLDER_BOOT_MARKERS`) — the baked target
      is the game *directory*, not the nested ``EBOOT.BIN`` that
      ``file_path`` records (ES-DE spells the directory case ``.ps3dir``). The
      marker match is positive evidence that the plugin recognised the layout,
      so no extension is examined.

    Everything else is decided by the recorded launch file's extension. A
    ``.pkg`` is absent from ps3's accept-list; a bare track ``.bin`` is absent
    from dreamcast's — both are caught here.

    Pure path algebra plus a set membership, stdlib only.
    """
    if not supported_extensions:
        return True
    if folder_boot_root(file_path, rom_dir) is not None:
        return True
    return os.path.splitext(file_path)[1].lower() in supported_extensions


def folder_boot_layout_root(files: list[str]) -> str | None:
    """Return the game root of a folder-boot layout among *files*, or ``None``.

    Scans *files* for one whose trailing components match a folder-boot marker
    (:data:`FOLDER_BOOT_MARKERS`, e.g. ``…/PS3_GAME/USRDIR/EBOOT.BIN``) and, on
    the first match, returns the marker-stripped game root — the directory that
    *contains* the marker run (where ``PS3_DISC.SFB`` and ``PS3_GAME`` sit).

    Used by the download path to recognise a folder-boot dump — to suppress the
    M3U playlist and heal the disc ``PS3_DISC.SFB`` — while it still holds only
    the freshly-extracted file list, before the install's ``rom_dir`` is
    recorded. Unlike :func:`folder_boot_root` it takes no ``rom_dir`` and applies
    no containment guard: the caller already scoped *files* to one extract
    directory, so the marker match alone identifies the layout. Case-sensitive,
    stdlib-only.
    """
    for path in files:
        for marker in FOLDER_BOOT_MARKERS:
            root = _strip_marker_components(path, marker)
            if root is not None:
                return root
    return None


def _strip_marker_components(path: str, marker: tuple[str, ...]) -> str | None:
    """Strip a trailing *marker* component run from *path*, or ``None`` if it does not match.

    The marker components are compared against *path*'s trailing basenames in
    order, case-sensitively. Returns the surviving prefix (the game root) when
    every component matches, ``None`` on the first mismatch.
    """
    root = path
    for expected in reversed(marker):
        if os.path.basename(root) != expected:
            return None
        root = os.path.dirname(root)
    return root


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


def synthetic_rom_name(rom_detail: dict[str, Any]) -> str:
    """Last-resort identity name for a ROM whose ``fs_name`` is missing or unusable.

    ``rom_<id>`` (or ``rom_unknown`` when ``id`` is also absent). Shared by the
    local-filename and extract-dir resolvers as the fallback so a ROM without a
    usable server-supplied name still lands under a single, stable identity — and
    used again by the download service as the substitute when a server-supplied
    name coerces to a degenerate path component.
    """
    return f"rom_{rom_detail.get('id', 'unknown')}"


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
    fs_name = rom_detail.get("fs_name", synthetic_rom_name(rom_detail))
    if not rom_detail.get("has_nested_single_file"):
        return (fs_name, False)
    files = rom_detail.get("files") or []
    if not files:
        return (fs_name, True)
    return (files[0].get("file_name") or fs_name, False)


def resolve_extract_dir_name(rom_detail: dict[str, Any]) -> str:
    """Resolve the directory name for an extracted multi-file ROM.

    The extract directory must carry the ROM's own identity, because
    everything downstream inherits it — the launch ``file_path``, the
    ``rom_dir`` install record, and any folder-boot launch bake. RomM reports
    the extensionless ROM name as ``fs_name_no_ext``; when that is absent the
    extension is stripped from ``fs_name``, and when ``fs_name`` is also
    missing the synthetic ``rom_<id>`` (or ``rom_unknown``) mirrors
    ``resolve_local_file_name``.

    Deliberately never reads ``files[0]``: for a ``has_nested_single_file``
    ROM that RomM serves as a ZIP (a folder game such as a PS3 title),
    ``files[0]`` is an arbitrary inner asset, so naming the directory after it
    would misname the whole install.
    """
    fs_name_no_ext = rom_detail.get("fs_name_no_ext")
    if fs_name_no_ext:
        return fs_name_no_ext
    fs_name = rom_detail.get("fs_name", synthetic_rom_name(rom_detail))
    return os.path.splitext(fs_name)[0]
