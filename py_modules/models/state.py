"""TypedDicts for the plugin's on-disk JSON state and metadata cache.

These dicts back the live wiring carried through every ``*ServiceConfig``
(``state``, ``metadata_cache``). They are not dataclasses because the JSON
shape is the source of truth: persisted shape, runtime shape, and service
contract must all be the same dict. TypedDicts give that dict a checked
key-set without changing its runtime identity, so call sites that mutate
``state["shortcut_registry"]`` in place keep working unchanged.

Keys flagged ``NotRequired`` are the ones that are written transiently
(e.g. ``retrodeck_home_path_previous`` only exists while a home migration
is pending) or only after a particular event has occurred (e.g.
``last_synced_collections`` is written by the sync reporter).

The metadata cache mixes a persistence-layer ``version`` integer with
per-ROM entries keyed by ``rom_id`` string. Only the persistence adapter
reads/writes ``version``; services exclusively ``.get(rom_id_str)`` /
``[rom_id_str] = entry``. The type alias narrows the service-facing
contract to ``dict[str, MetadataCacheEntry]`` to keep the per-entry shape
checked; the persistence adapter keeps a raw ``dict`` view to stamp the
version field.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class ShortcutRegistryEntry(TypedDict):
    """One ROM's Steam-shortcut binding inside ``shortcut_registry``.

    Keyed by ``rom_id`` (string). Required fields are written through
    :class:`models.registry_patches.RegistrySyncApplyPatch` during sync;
    optional ID fields are written on demand by SteamGridService and
    on-the-fly RomM lookups.
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


class SyncStats(TypedDict):
    """Aggregated counts surfaced by ``get_sync_stats`` callable."""

    platforms: int
    roms: int


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
    """Top-level on-disk plugin state dict (``state.json``).

    The seven canonical keys mirror :func:`bootstrap._default_state` —
    production wiring always initialises them, so they are required at
    the type level and direct-access (``state["shortcut_registry"]``) is
    safe. Transient keys (only present while a particular event is in
    flight) are flagged ``NotRequired``.

    Transient keys:

    - ``retrodeck_home_path_previous`` — populated while a RetroDECK
      home migration is awaiting user confirmation.
    - ``save_sort_settings_previous`` — populated while a RetroArch
      save-sort change is awaiting user confirmation.
    - ``last_synced_collections`` / ``last_synced_platforms`` — written
      after the first successful sync; absent until then.

    ``save_sort_settings`` is ``None`` before RetroArch save-sort has
    been observed for the first time.
    """

    shortcut_registry: dict[str, ShortcutRegistryEntry]
    installed_roms: dict[str, InstalledRomEntry]
    last_sync: str | None
    sync_stats: SyncStats
    downloaded_bios: dict[str, DownloadedBiosEntry]
    retrodeck_home_path: str
    save_sort_settings: SaveSortSettings | None
    retrodeck_home_path_previous: NotRequired[str]
    save_sort_settings_previous: NotRequired[SaveSortSettings]
    last_synced_collections: NotRequired[list[str]]
    last_synced_platforms: NotRequired[list[str]]


def make_default_plugin_state() -> PluginState:
    """Return a fresh default ``PluginState`` dict.

    Provides a single source of truth for the canonical key set that
    services and tests can reuse. The shape mirrors
    :func:`bootstrap._default_state` (which delegates to this factory).
    """
    return {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {"platforms": 0, "roms": 0},
        "downloaded_bios": {},
        "retrodeck_home_path": "",
        "save_sort_settings": None,
    }


class MetadataCacheEntry(TypedDict):
    """One ROM's cached metadata inside ``metadata_cache``.

    Mirrors :class:`models.metadata.RomMetadata` after ``asdict``: the
    cached value is built by :meth:`services.metadata.MetadataService.extract_metadata`
    via ``asdict(RomMetadata(...))``, so ``tuple`` fields on
    ``RomMetadata`` flatten to ``list`` here.
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


# Service-facing metadata cache contract. The on-disk JSON also carries a
# persistence-layer ``version: int`` key; only adapters/persistence.py
# reads/writes that field, so a homogeneous ``dict[str, MetadataCacheEntry]``
# is the honest contract at the service boundary.
MetadataCache = dict[str, MetadataCacheEntry]
