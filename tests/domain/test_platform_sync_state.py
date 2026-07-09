"""Unit tests for the ``PlatformSyncState`` aggregate."""

from __future__ import annotations

import pytest

from domain.platform_sync_state import PlatformSyncState


class TestStamp:
    def test_sets_all_fields(self):
        stamp = PlatformSyncState.stamp(
            platform_slug="n64",
            at="2026-05-28T10:00:00+00:00",
            rom_count=2091,
        )
        assert stamp.platform_slug == "n64"
        assert stamp.completed_at == "2026-05-28T10:00:00+00:00"
        assert stamp.rom_count == 2091

    def test_zero_rom_count_allowed(self):
        stamp = PlatformSyncState.stamp(platform_slug="n64", at="2026-05-28T10:00:00+00:00", rom_count=0)
        assert stamp.rom_count == 0

    def test_empty_platform_slug_raises(self):
        with pytest.raises(ValueError, match="platform_slug is required"):
            PlatformSyncState.stamp(platform_slug="", at="2026-05-28T10:00:00+00:00", rom_count=1)

    def test_negative_rom_count_raises(self):
        with pytest.raises(ValueError, match="rom_count must be non-negative"):
            PlatformSyncState.stamp(platform_slug="n64", at="2026-05-28T10:00:00+00:00", rom_count=-1)
