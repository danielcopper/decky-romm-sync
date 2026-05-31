"""TypedDicts for the plugin's residual on-disk JSON state shapes.

The relational state (shortcut registry, installed ROMs, metadata cache,
sync stats, last-sync timestamp) lives in SQLite after the cutover (#784).
What remains in JSON is the ``downloaded_bios`` index inside ``state.json``,
read once at startup into :class:`PluginState`. The other TypedDicts here
are checked shapes still consumed by services that read/return those
records (``ShortcutRegistryEntry``, ``InstalledRomEntry``,
``MetadataCacheEntry``, ``SaveSortSettings``) — they describe the dict
contract at a service boundary without changing the dict's runtime
identity.
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


class DownloadedBiosEntry(TypedDict):
    """One downloaded BIOS/firmware file record inside ``downloaded_bios``.

    Keyed by the BIOS file name. Tracked so migrations can move BIOS
    files when the RetroDECK home path changes.
    """

    file_path: str
    firmware_id: int
    platform_slug: str
    downloaded_at: str


class SaveSortSettings(TypedDict):
    """RetroArch save-sorting settings snapshot used by save migrations."""

    sort_by_content: bool
    sort_by_core: bool


class PluginState(TypedDict):
    """Residual on-disk plugin state read from ``state.json`` at startup.

    Post-cutover (#784) the relational state moved to SQLite; the only
    key still read from JSON is ``downloaded_bios`` — the BIOS/firmware
    download index MigrationService consults when the RetroDECK home path
    changes.
    """

    downloaded_bios: dict[str, DownloadedBiosEntry]


def make_default_plugin_state() -> PluginState:
    """Return a fresh default ``PluginState`` dict.

    Single source of truth for the residual key set bootstrap initialises
    when ``state.json`` is missing or carries no ``downloaded_bios`` index.
    """
    return {
        "downloaded_bios": {},
    }


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
