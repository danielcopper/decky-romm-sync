"""Unit tests for the ``Rom`` aggregate."""

from __future__ import annotations

import pytest

from domain.rom import Rom


class TestSynced:
    def test_all_required_plus_igdb_sets_fields(self):
        rom = Rom.synced(
            rom_id=42,
            platform_slug="snes",
            name="Super Metroid",
            fs_name="Super Metroid.sfc",
            shortcut_app_id=123456789,
            synced_at="2026-05-28T10:00:00",
            igdb_id=1234,
        )
        assert rom.rom_id == 42
        assert rom.platform_slug == "snes"
        assert rom.name == "Super Metroid"
        assert rom.fs_name == "Super Metroid.sfc"
        assert rom.shortcut_app_id == 123456789
        assert rom.last_synced_at == "2026-05-28T10:00:00"
        assert rom.igdb_id == 1234

    def test_without_igdb_leaves_optional_ids_none(self):
        rom = Rom.synced(
            rom_id=7,
            platform_slug="gba",
            name="Metroid Fusion",
            fs_name="Metroid Fusion.gba",
            shortcut_app_id=987654321,
            synced_at="2026-05-28T11:00:00",
        )
        assert rom.igdb_id is None
        assert rom.cover_path is None
        assert rom.sgdb_id is None
        assert rom.ra_id is None

    def test_non_positive_rom_id_raises(self):
        with pytest.raises(ValueError, match="rom_id must be positive"):
            Rom.synced(
                rom_id=0,
                platform_slug="snes",
                name="x",
                fs_name="x.sfc",
                shortcut_app_id=1,
                synced_at="2026-05-28T10:00:00",
            )

    def test_empty_platform_slug_raises(self):
        with pytest.raises(ValueError, match="platform_slug is required"):
            Rom.synced(
                rom_id=1,
                platform_slug="",
                name="x",
                fs_name="x.sfc",
                shortcut_app_id=1,
                synced_at="2026-05-28T10:00:00",
            )


def _make_rom() -> Rom:
    return Rom.synced(
        rom_id=1,
        platform_slug="snes",
        name="x",
        fs_name="x.sfc",
        shortcut_app_id=1,
        synced_at="2026-05-28T10:00:00",
    )


class TestUpdateCoverPath:
    def test_sets_cover_path(self):
        rom = _make_rom()
        rom.update_cover_path("/covers/1.png")
        assert rom.cover_path == "/covers/1.png"


class TestUnbindShortcut:
    def test_clears_app_id_and_keeps_row(self):
        rom = Rom.synced(
            rom_id=1,
            platform_slug="snes",
            name="Super Metroid",
            fs_name="Super Metroid.sfc",
            shortcut_app_id=123456789,
            synced_at="2026-05-28T10:00:00",
        )
        rom.update_cover_path("/covers/1.png")
        rom.assign_sgdb_id(7)

        rom.unbind_shortcut()

        assert rom.shortcut_app_id is None
        assert rom.rom_id == 1
        assert rom.platform_slug == "snes"
        assert rom.name == "Super Metroid"
        assert rom.cover_path == "/covers/1.png"
        assert rom.sgdb_id == 7


class TestAssignSgdbId:
    def test_sets_sgdb_id(self):
        rom = _make_rom()
        rom.assign_sgdb_id(7)
        assert rom.sgdb_id == 7


class TestAssignRaId:
    def test_sets_ra_id(self):
        rom = _make_rom()
        rom.assign_ra_id(9)
        assert rom.ra_id == 9
