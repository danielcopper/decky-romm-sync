"""BiosFile — one downloaded BIOS/firmware file the plugin tracks on disk.

Identified by ``(platform_slug, file_name)`` — a bare filename is unsafe because
two platforms can ship same-named BIOS. ``firmware_id`` is nullable metadata
from RomM, not part of the identity. Tracked so migrations can move the file
when the RetroDECK home path changes.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class BiosFile:
    """One downloaded BIOS file, keyed by (platform_slug, file_name)."""

    platform_slug: str
    file_name: str
    file_path: str
    downloaded_at: str
    firmware_id: int | None = None

    @classmethod
    def mark_downloaded(
        cls,
        *,
        platform_slug: str,
        file_name: str,
        file_path: str,
        downloaded_at: str,
        firmware_id: int | None = None,
    ) -> BiosFile:
        """Record a freshly downloaded BIOS file at ISO timestamp ``downloaded_at``."""
        if not platform_slug or not file_name:
            raise ValueError("platform_slug and file_name are required")
        return cls(
            platform_slug=platform_slug,
            file_name=file_name,
            file_path=file_path,
            downloaded_at=downloaded_at,
            firmware_id=firmware_id,
        )

    def relocate(self, new_path: str) -> None:
        """Move the BIOS file to a new path (RetroDECK home migration)."""
        self.file_path = new_path
