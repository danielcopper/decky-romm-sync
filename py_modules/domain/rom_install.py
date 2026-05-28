"""RomInstall — the on-disk install record for a downloaded ROM.

Exists only while a ROM is downloaded: created on download-complete, removed on
uninstall. References its Rom by ``rom_id``. Distinguishes the install directory
(``install_path``) from the specific launch file (``file_path``); the
denormalized ``platform_slug``/``system`` let migration and save-sort read this
record without joining the registry.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class RomInstall:
    """Where a downloaded ROM lives on disk and which file launches it."""

    rom_id: int
    file_path: str
    install_path: str
    platform_slug: str
    system: str
    installed_at: str

    @classmethod
    def mark_installed(
        cls,
        *,
        rom_id: int,
        file_path: str,
        install_path: str,
        platform_slug: str,
        system: str,
        installed_at: str,
    ) -> RomInstall:
        """Record a freshly downloaded ROM installed at ISO timestamp ``installed_at``."""
        if rom_id <= 0:
            raise ValueError("rom_id must be positive")
        return cls(
            rom_id=rom_id,
            file_path=file_path,
            install_path=install_path,
            platform_slug=platform_slug,
            system=system,
            installed_at=installed_at,
        )

    def relocate(self, new_install_path: str, new_file_path: str) -> None:
        """Move the install to a new directory and launch file (RetroDECK home migration)."""
        self.install_path = new_install_path
        self.file_path = new_file_path
