"""Unit tests for the ``FirmwareCacheEntry`` aggregate."""

from __future__ import annotations

from domain.firmware_cache import FirmwareCacheEntry


class TestCached:
    def test_with_int_id_sets_all_fields(self):
        entry = FirmwareCacheEntry.cached(
            id=5,
            name="scph5501.bin",
            platform_slug="ps",
            file_size_bytes=524288,
            cached_at=1000.0,
        )
        assert entry.id == 5
        assert entry.name == "scph5501.bin"
        assert entry.platform_slug == "ps"
        assert entry.file_size_bytes == 524288
        assert entry.cached_at == 1000.0

    def test_none_id_is_preserved(self):
        entry = FirmwareCacheEntry.cached(
            id=None,
            name="scph5501.bin",
            platform_slug="ps",
            file_size_bytes=524288,
            cached_at=1000.0,
        )
        assert entry.id is None
