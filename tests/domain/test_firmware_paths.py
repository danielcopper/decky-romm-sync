"""Tests for domain.firmware_paths — pure firmware slug parsing and platform mapping."""

from domain.firmware_paths import parse_firmware_slug, resolve_firmware_slugs


class TestParseFirmwareSlug:
    def test_bios_prefix_returns_second_segment(self):
        assert parse_firmware_slug("bios/ps/scph.bin") == "ps"

    def test_single_part_returns_empty(self):
        assert parse_firmware_slug("bios") == ""

    def test_non_bios_prefix(self):
        assert parse_firmware_slug("firmware/dc/boot.bin") == "firmware"

    def test_empty_path(self):
        assert parse_firmware_slug("") == ""

    def test_leading_and_trailing_slashes_stripped(self):
        assert parse_firmware_slug("/bios/ps/") == "ps"


class TestResolveFirmwareSlugs:
    def test_psx_maps_to_psx_and_ps(self):
        assert resolve_firmware_slugs("psx") == ["psx", "ps"]

    def test_ps2_maps_to_ps2(self):
        assert resolve_firmware_slugs("ps2") == ["ps2"]

    def test_unknown_platform_returns_identity(self):
        assert resolve_firmware_slugs("snes") == ["snes"]

    def test_empty_string_returns_identity(self):
        assert resolve_firmware_slugs("") == [""]
