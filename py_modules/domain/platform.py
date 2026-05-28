"""Platform — per-platform state the plugin owns locally.

Keyed by ``slug`` (the RomM platform slug). Carries the cached display name
(survives RomM downtime) and the user's exclude-from-sync toggle. RetroDECK-
managed per-platform state (es_systems.xml, gamelist.xml) and bundled reference
data stay outside this aggregate — only state the plugin itself owns lives here.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class Platform:
    """Per-platform local state owned by the plugin (one per RomM slug)."""

    slug: str
    display_name: str
    excluded_from_sync: bool = False

    def update_display_name(self, name: str) -> None:
        """Refresh the cached display name from RomM."""
        self.display_name = name

    def exclude_from_sync(self) -> None:
        """Mark this platform as excluded from library sync."""
        self.excluded_from_sync = True

    def include_in_sync(self) -> None:
        """Re-include this platform in library sync."""
        self.excluded_from_sync = False
