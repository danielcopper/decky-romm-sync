"""Tests for domain.artwork_paths — pure cover-art filename builders."""

from __future__ import annotations

from domain.artwork_paths import final_filename, staging_filename


class TestStagingFilename:
    def test_int_rom_id(self):
        assert staging_filename(42) == "romm_42_cover.png"

    def test_str_rom_id(self):
        assert staging_filename("42") == "romm_42_cover.png"

    def test_zero(self):
        assert staging_filename(0) == "romm_0_cover.png"

    def test_large_id(self):
        assert staging_filename(999_999_999) == "romm_999999999_cover.png"


class TestFinalFilename:
    def test_app_id(self):
        assert final_filename(100001) == "100001p.png"

    def test_str_id(self):
        assert final_filename("12345") == "12345p.png"

    def test_zero(self):
        assert final_filename(0) == "0p.png"
