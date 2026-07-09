"""Cover-art filename builders for the Steam grid and the per-ROM cover cache.

Pure naming logic for the filename conventions ArtworkService writes: the
per-ROM ``{rom_id}.png`` cache name (in the plugin-owned cover cache, keyed by
RomM ID so every version of a sibling group keeps its own cover), the final
``{app_id}p.png`` name Steam reads as the active shortcut's portrait cover, the
``.tmp`` sidecar every write lands in before its atomic rename, and the legacy
per-ROM staging name that predates the cache. Filesystem I/O lives in adapters;
this module is import- and state-free.
"""

from __future__ import annotations

TMP_SUFFIX = ".tmp"


def with_tmp_suffix(name: str) -> str:
    """Return *name* with the write-sidecar ``.tmp`` suffix appended.

    Cover writes stream/copy into this sidecar first, then an atomic rename
    publishes it over the real name — so a concurrent reader (the picker fetch,
    or Steam reading the grid tile) never observes a half-written file. Accepts
    a bare filename or a full path; it is pure string concatenation.
    """
    return name + TMP_SUFFIX


def cache_filename(rom_id: int | str) -> str:
    """Return the per-ROM cover cache filename, keyed by RomM ID.

    The cache is the source of truth for a ROM's cover: it lives in the
    plugin-owned cover-cache directory (never the shared Steam grid dir), so
    every version of a sibling group keeps its own file rather than overwriting
    the group's single ``{app_id}p.png``.
    """
    return f"{rom_id}.png"


def staging_filename(rom_id: int | str) -> str:
    """Return the staging filename for a downloaded cover keyed by RomM ID.

    Used before the shortcut's Steam ``app_id`` is known. Renamed to
    :func:`final_filename` once the shortcut has been created. Accepts
    either an ``int`` ID (the canonical RomM payload type) or its string
    form (used in registry keys and removal callers).
    """
    return f"romm_{rom_id}_cover.png"


def final_filename(app_id: int | str) -> str:
    """Return the Steam grid filename for a finalised portrait cover.

    Accepts either ``int`` (Steam shortcut app_id) or ``str`` (legacy
    ``artwork_id`` payloads observed in some registry entries).
    """
    return f"{app_id}p.png"
