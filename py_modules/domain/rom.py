"""Rom — the library entry for one ROM the plugin tracks locally.

Identity, the Steam-shortcut binding, and the external-service ids the plugin
resolves for a ROM. Created/updated atomically when a ROM is synced from RomM.
References its Platform by ``platform_slug`` (FK) — the platform's display name
is resolved through Platform, not carried here.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class Rom:
    """One ROM as the plugin tracks it locally (identity + shortcut binding)."""

    rom_id: int
    platform_slug: str
    name: str
    fs_name: str
    shortcut_app_id: int
    last_synced_at: str
    cover_path: str | None = None
    igdb_id: int | None = None
    sgdb_id: int | None = None
    ra_id: int | None = None

    @classmethod
    def synced(
        cls,
        *,
        rom_id: int,
        platform_slug: str,
        name: str,
        fs_name: str,
        shortcut_app_id: int,
        synced_at: str,
        igdb_id: int | None = None,
    ) -> Rom:
        """Build a Rom synced from RomM at ISO timestamp ``synced_at``."""
        if rom_id <= 0:
            raise ValueError("rom_id must be positive")
        if not platform_slug:
            raise ValueError("platform_slug is required")
        return cls(
            rom_id=rom_id,
            platform_slug=platform_slug,
            name=name,
            fs_name=fs_name,
            shortcut_app_id=shortcut_app_id,
            last_synced_at=synced_at,
            igdb_id=igdb_id,
        )

    def update_cover_path(self, path: str) -> None:
        """Record the local cover-art path once artwork has been written."""
        self.cover_path = path

    def assign_sgdb_id(self, sgdb_id: int) -> None:
        """Stamp the resolved SteamGridDB id."""
        self.sgdb_id = sgdb_id

    def assign_ra_id(self, ra_id: int) -> None:
        """Stamp the resolved RetroAchievements id."""
        self.ra_id = ra_id
