"""Unit tests for the ``BiosFile`` aggregate."""

from __future__ import annotations

import pytest

from domain.bios_file import BiosFile


class TestMarkDownloaded:
    def test_with_firmware_id_sets_all_fields(self):
        bios = BiosFile.mark_downloaded(
            platform_slug="ps",
            file_name="scph5501.bin",
            file_path="/bios/scph5501.bin",
            downloaded_at="2026-05-28T10:00:00",
            firmware_id=99,
        )
        assert bios.platform_slug == "ps"
        assert bios.file_name == "scph5501.bin"
        assert bios.file_path == "/bios/scph5501.bin"
        assert bios.downloaded_at == "2026-05-28T10:00:00"
        assert bios.firmware_id == 99

    def test_without_firmware_id_defaults_none(self):
        bios = BiosFile.mark_downloaded(
            platform_slug="ps",
            file_name="scph5501.bin",
            file_path="/bios/scph5501.bin",
            downloaded_at="2026-05-28T10:00:00",
        )
        assert bios.firmware_id is None

    def test_empty_platform_slug_raises(self):
        with pytest.raises(ValueError, match="platform_slug and file_name are required"):
            BiosFile.mark_downloaded(
                platform_slug="",
                file_name="scph5501.bin",
                file_path="/bios/scph5501.bin",
                downloaded_at="2026-05-28T10:00:00",
            )

    def test_empty_file_name_raises(self):
        with pytest.raises(ValueError, match="platform_slug and file_name are required"):
            BiosFile.mark_downloaded(
                platform_slug="ps",
                file_name="",
                file_path="/bios/scph5501.bin",
                downloaded_at="2026-05-28T10:00:00",
            )


class TestRelocate:
    def test_updates_file_path(self):
        bios = BiosFile.mark_downloaded(
            platform_slug="ps",
            file_name="scph5501.bin",
            file_path="/old/scph5501.bin",
            downloaded_at="2026-05-28T10:00:00",
        )
        bios.relocate("/new/scph5501.bin")
        assert bios.file_path == "/new/scph5501.bin"
