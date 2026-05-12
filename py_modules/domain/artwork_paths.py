"""Cover-art filename builders for the Steam grid directory.

Pure naming logic for the two filename conventions ArtworkService writes
into the grid dir: the per-ROM staging name used before a Steam app_id is
known, and the final ``{app_id}p.png`` name Steam reads as the portrait
cover. Filesystem I/O lives in adapters; this module is import- and
state-free.
"""

from __future__ import annotations


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
