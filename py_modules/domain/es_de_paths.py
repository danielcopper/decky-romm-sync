"""ES-DE gamelist ``<path>`` identity for a ROM — canonical form and comparison.

ES-DE records each game's identity in ``gamelist.xml`` as a ``<path>`` relative
to the system's ROM directory, not as a bare filename. A single-file ROM lives
directly in the shared system directory, so its entry is just ``FF7.cue``; a
folder-backed (multi-file) ROM owns a dedicated per-ROM directory, so its entry
is ``FF7/FF7.m3u`` — the dedicated dir's basename joined with the launch file's
path inside it. This module is the single place that derives that identity from
a ``RomInstall``'s ``file_path``/``rom_dir`` pair and the single place that
normalizes two gamelist ``<path>`` values for comparison. Anything that resolves
a per-game core override, matches a gamelist entry, or writes one must route
through here so single-file and folder-backed ROMs share one identity rule. Pure
path algebra — no I/O, no service/adapter imports, stdlib only.
"""

from __future__ import annotations

import os


def gamelist_entry_path(file_path: str, rom_dir: str | None) -> str:
    """Return the ES-DE gamelist ``<path>`` identity for a ROM's launch file.

    ``file_path`` is the always-set launch target (e.g.
    ``/roms/ps1/FF7/FF7.m3u``); ``rom_dir`` is the dedicated per-ROM directory,
    set only for folder-backed ROMs and ``None`` for single-file ROMs
    (ADR-0008). The returned value matches what ES-DE stores in ``gamelist.xml``
    relative to the system ROM directory:

    - single-file (``rom_dir`` falsy) → ``basename(file_path)`` (e.g.
      ``FF7.cue``).
    - folder-backed → ``<dedicated-dir-name>/<launch file inside it>`` (e.g.
      ``FF7/FF7.m3u``, or ``FF7/disc1/FF7.cue`` for a nested launch file).

    ``rom_dir`` is tolerant of a trailing separator: the dedicated-dir segment
    is derived from the normalized directory name, so ``/roms/ps1/FF7/`` and
    ``/roms/ps1/FF7`` both yield the ``FF7`` segment.

    An empty ``file_path`` yields ``""`` (the function is total: ``os.path.relpath``
    raises ``ValueError`` on an empty first argument, so this early guard keeps
    the call safe for any ``rom_dir``).

    Defensive fallback to ``basename(file_path)`` when the dedicated-dir
    identity can't be formed: ``file_path`` resolving outside ``rom_dir`` (a
    data-inconsistency that would make the relative path escape with ``..``), or
    ``file_path`` being the directory itself (relative path ``"."``). The escape
    fallback is silent — it never raises or logs.
    """
    if not file_path:
        return ""
    if not rom_dir:
        return os.path.basename(file_path)
    normalized_dir = os.path.normpath(rom_dir)
    rel = os.path.relpath(file_path, normalized_dir)
    if rel == "." or rel == ".." or rel.startswith(".." + os.sep):
        return os.path.basename(file_path)
    return os.path.join(os.path.basename(normalized_dir), rel)


def normalize_gamelist_path(path: str) -> str:
    """Normalize a gamelist ``<path>`` value for identity comparison.

    Strips a single leading ``"./"`` prefix (the relative-path marker ES-DE
    writes) and surrounding whitespace, leaving the rest of the path intact.
    This is a prefix strip, not a character-set strip: ``"./.hidden"`` becomes
    ``".hidden"`` (the dot of the hidden file is preserved), and ``"./sub/x"``
    becomes ``"sub/x"``. Two gamelist paths denote the same game iff their
    normalized forms are equal.
    """
    p = path[2:] if path.startswith("./") else path
    return p.strip()
