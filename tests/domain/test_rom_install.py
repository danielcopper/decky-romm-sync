"""Unit tests for the ``RomInstall`` aggregate."""

from __future__ import annotations

import pytest

from domain.rom_install import RomInstall


class TestMarkInstalled:
    def test_sets_all_fields_multi_file(self):
        install = RomInstall.mark_installed(
            rom_id=42,
            file_path="/roms/psx/FF7/FF7.m3u",
            rom_dir="/roms/psx/FF7",
            platform_slug="psx",
            system="psx",
            installed_at="2026-05-28T10:00:00",
        )
        assert install.rom_id == 42
        assert install.file_path == "/roms/psx/FF7/FF7.m3u"
        assert install.rom_dir == "/roms/psx/FF7"
        assert install.platform_slug == "psx"
        assert install.system == "psx"
        assert install.installed_at == "2026-05-28T10:00:00"

    def test_single_file_has_no_rom_dir(self):
        """A single-file ROM owns no folder — ``rom_dir`` is ``None``."""
        install = RomInstall.mark_installed(
            rom_id=7,
            file_path="/roms/snes/Super Metroid.sfc",
            rom_dir=None,
            platform_slug="snes",
            system="snes",
            installed_at="2026-05-28T10:00:00",
        )
        assert install.file_path == "/roms/snes/Super Metroid.sfc"
        assert install.rom_dir is None

    def test_non_positive_rom_id_raises(self):
        with pytest.raises(ValueError, match="rom_id must be positive"):
            RomInstall.mark_installed(
                rom_id=0,
                file_path="/x",
                rom_dir=None,
                platform_slug="snes",
                system="snes",
                installed_at="2026-05-28T10:00:00",
            )


class TestRelocate:
    def test_updates_both_rom_dir_and_file_path(self):
        """A folder-backed ROM relocates its dedicated directory and launch file."""
        install = RomInstall.mark_installed(
            rom_id=1,
            file_path="/old/dir/game.iso",
            rom_dir="/old/dir",
            platform_slug="ps",
            system="psx",
            installed_at="2026-05-28T10:00:00",
        )
        install.relocate("/new/dir", "/new/dir/game.iso")
        assert install.rom_dir == "/new/dir"
        assert install.file_path == "/new/dir/game.iso"

    def test_single_file_relocates_with_none_rom_dir(self):
        """A single-file ROM relocates its launch file and keeps ``rom_dir`` ``None``."""
        install = RomInstall.mark_installed(
            rom_id=2,
            file_path="/old/roms/n64/zelda.z64",
            rom_dir=None,
            platform_slug="n64",
            system="n64",
            installed_at="2026-05-28T10:00:00",
        )
        install.relocate(None, "/new/roms/n64/zelda.z64")
        assert install.rom_dir is None
        assert install.file_path == "/new/roms/n64/zelda.z64"
