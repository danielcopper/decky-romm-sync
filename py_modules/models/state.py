"""TypedDicts describing dict-shaped records crossing service boundaries.

The plugin's relational state lives in SQLite after the cutover (#784);
nothing here is loaded from on-disk JSON. These TypedDicts are checked
shapes still consumed by services that read/return those records
(``ShortcutRegistryEntry``, ``InstalledRomEntry``, ``MetadataCacheEntry``,
``SaveSortSettings``) — they describe the dict contract at a service
boundary without changing the dict's runtime identity.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ShortcutRegistryEntry(TypedDict):
    """One ROM's Steam-shortcut binding record.

    Keyed by ``rom_id`` (string). Optional ID fields are filled on demand
    by SteamGridService and on-the-fly RomM lookups.
    """

    app_id: int
    name: str
    fs_name: str
    platform_name: str
    platform_slug: str
    cover_path: str
    igdb_id: NotRequired[int]
    sgdb_id: NotRequired[int]
    ra_id: NotRequired[int]


class InstalledRomEntry(TypedDict):
    """One installed ROM record inside ``installed_roms``.

    Keyed by ``rom_id`` (string). ``rom_dir`` is set only for ROMs
    extracted from a multi-file archive (otherwise the parent directory
    is inferred from ``file_path``).
    """

    rom_id: int
    file_name: str
    file_path: str
    system: str
    platform_slug: str
    installed_at: str
    rom_dir: NotRequired[str]


class SaveSortSettings(TypedDict):
    """RetroArch save-sorting settings snapshot used by save migrations."""

    sort_by_content: bool
    sort_by_core: bool


class MetadataCacheEntry(TypedDict):
    """One ROM's cached metadata as the frontend ``RomMetadata`` wire shape.

    The list-shaped projection of the ``rom_metadata`` aggregate handed to
    the frontend (``get_rom_metadata`` / ``get_all_metadata_cache`` and the
    game-detail payload): tuple fields on the aggregate flatten to ``list``
    arrays here, and ``first_release_date`` / ``average_rating`` stay
    nullable.
    """

    summary: str
    genres: list[str]
    companies: list[str]
    first_release_date: int | None
    average_rating: float | None
    game_modes: list[str]
    player_count: str
    cached_at: float
    steam_categories: list[int]
