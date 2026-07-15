"""Cover-art filename logic for the Steam grid and the per-ROM cover cache.

Pure naming logic for the filename conventions ArtworkService reads and
writes: the per-ROM ``{rom_id}.png`` cache name (in the plugin-owned cover
cache, keyed by RomM ID so every version of a sibling group keeps its own
cover), the final ``{app_id}p.png`` name Steam reads as the active shortcut's
portrait cover, the ``.tmp`` sidecar every write lands in before its atomic
rename, the legacy per-ROM staging name that predates the cache, and the
inverse parse of a grid entry back into its appId (plus the shortcut-appId
range check) for the orphaned grid-image cleanup. Filesystem I/O lives in
adapters; this module is import- and state-free.
"""

from __future__ import annotations

import re

TMP_SUFFIX = ".tmp"

# Steam's grid-image naming: ``{app_id}`` + an optional asset-type suffix +
# a raster extension. The bare ``{app_id}.png`` form is the wide/horizontal
# grid. Anchored and exhaustive — anything else in the grid dir (staging
# files, ``.tmp`` sidecars, non-numeric names, uppercase extensions) is not
# a grid image and never a cleanup candidate.
_GRID_IMAGE_SUFFIXES = ("p", "_hero", "_logo", "_icon", "")
_GRID_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg")
_GRID_IMAGE_RE = re.compile(r"^(\d+)(?:p|_hero|_logo|_icon)?\.(?:png|jpg|jpeg)$", re.ASCII)

# Steam assigns non-Steam shortcut appIds with the high bit set: on-device
# inspection of 68 live plugin-created shortcuts found them uniformly spread
# across [0x80000000, 0xFFFFFFFF] (random assignment at creation — see
# docs/architecture/steam-non-steam-shortcuts.md §App IDs and Artwork), and
# the signed-int32 form shortcuts.vdf records is negative for exactly this
# range. Store appIds (real Steam games, whose custom art also lives in the
# grid dir) are small and never reach the high bit.
SHORTCUT_APP_ID_MIN = 0x8000_0000
SHORTCUT_APP_ID_MAX = 0xFFFF_FFFF


def is_shortcut_app_id(app_id: int) -> bool:
    """Return True when *app_id* sits in the non-Steam-shortcut appId range.

    The range check is the cleanup's first safety gate: only appIds in
    ``[0x80000000, 0xFFFFFFFF]`` (the high-bit-set uint32 range Steam assigns
    to non-Steam shortcuts) can ever be deletion candidates, so custom art a
    user saved for a regular Steam game (a small store appId like ``570``)
    is never touched regardless of the submitted live set.
    """
    return SHORTCUT_APP_ID_MIN <= app_id <= SHORTCUT_APP_ID_MAX


def parse_grid_image_app_id(filename: str) -> int | None:
    """Parse a grid-dir entry into its appId, or ``None`` for a non-grid-image name.

    Matches exactly the five grid-image forms (portrait ``{app_id}p``, wide
    ``{app_id}``, ``_hero``, ``_logo``, ``_icon``) with a ``png``/``jpg``/
    ``jpeg`` extension. Strict by design: a partial or decorated name (a
    ``.tmp`` sidecar, a ``romm_*`` staging file, ``123abc.png``) returns
    ``None`` so it can never become a deletion candidate.
    """
    match = _GRID_IMAGE_RE.match(filename)
    if match is None:
        return None
    return int(match.group(1))


def grid_image_filenames(app_id: int | str) -> list[str]:
    """Return every grid-image filename form for *app_id*.

    The full suffix-by-extension product (portrait/wide/hero/logo/icon across
    png/jpg/jpeg) — the sweep set a shortcut removal deletes so no sibling
    grid file outlives its shortcut.
    """
    return [f"{app_id}{suffix}.{ext}" for suffix in _GRID_IMAGE_SUFFIXES for ext in _GRID_IMAGE_EXTENSIONS]


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
