"""RomInstall — the on-disk install record for a downloaded ROM.

Exists only while a ROM is downloaded: created on download-complete, removed on
uninstall. References its Rom by ``rom_id``. Always carries the specific launch
``file_path``; ``rom_dir`` names the dedicated per-ROM directory and is set only
for folder-backed (multi-file) ROMs — it is ``None`` for single-file ROMs, which
live as a bare file in the shared system directory and own no folder. The
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
    rom_dir: str | None
    platform_slug: str
    system: str
    installed_at: str

    @classmethod
    def mark_installed(
        cls,
        *,
        rom_id: int,
        file_path: str,
        rom_dir: str | None,
        platform_slug: str,
        system: str,
        installed_at: str,
    ) -> RomInstall:
        """Record a freshly downloaded ROM installed at ISO timestamp ``installed_at``.

        ``rom_dir`` is the dedicated per-ROM directory for a folder-backed
        (multi-file) ROM, or ``None`` for a single-file ROM that owns no folder.
        """
        if rom_id <= 0:
            raise ValueError("rom_id must be positive")
        return cls(
            rom_id=rom_id,
            file_path=file_path,
            rom_dir=rom_dir,
            platform_slug=platform_slug,
            system=system,
            installed_at=installed_at,
        )

    def relocate(self, new_rom_dir: str | None, new_file_path: str) -> None:
        """Move the install to a new launch file and per-ROM directory (RetroDECK home migration).

        ``new_rom_dir`` stays ``None`` for a single-file ROM; a folder-backed
        ROM carries its relocated dedicated directory.
        """
        self.rom_dir = new_rom_dir
        self.file_path = new_file_path
