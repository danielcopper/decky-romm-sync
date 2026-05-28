"""Unit tests for the ``RomInstall`` aggregate."""

from __future__ import annotations

import pytest

from domain.rom_install import RomInstall


class TestMarkInstalled:
    def test_sets_all_fields(self):
        install = RomInstall.mark_installed(
            rom_id=42,
            file_path="/roms/snes/Super Metroid.sfc",
            install_path="/roms/snes",
            platform_slug="snes",
            system="snes",
            installed_at="2026-05-28T10:00:00",
        )
        assert install.rom_id == 42
        assert install.file_path == "/roms/snes/Super Metroid.sfc"
        assert install.install_path == "/roms/snes"
        assert install.platform_slug == "snes"
        assert install.system == "snes"
        assert install.installed_at == "2026-05-28T10:00:00"

    def test_non_positive_rom_id_raises(self):
        with pytest.raises(ValueError, match="rom_id must be positive"):
            RomInstall.mark_installed(
                rom_id=0,
                file_path="/x",
                install_path="/",
                platform_slug="snes",
                system="snes",
                installed_at="2026-05-28T10:00:00",
            )


class TestRelocate:
    def test_updates_both_install_and_file_path(self):
        install = RomInstall.mark_installed(
            rom_id=1,
            file_path="/old/dir/game.iso",
            install_path="/old/dir",
            platform_slug="ps",
            system="psx",
            installed_at="2026-05-28T10:00:00",
        )
        install.relocate("/new/dir", "/new/dir/game.iso")
        assert install.install_path == "/new/dir"
        assert install.file_path == "/new/dir/game.iso"
