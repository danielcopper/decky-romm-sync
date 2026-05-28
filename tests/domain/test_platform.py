"""Unit tests for the ``Platform`` aggregate."""

from __future__ import annotations

from domain.platform import Platform


class TestConstruction:
    def test_default_excluded_from_sync_is_false(self):
        platform = Platform(slug="snes", display_name="Super Nintendo")
        assert platform.slug == "snes"
        assert platform.display_name == "Super Nintendo"
        assert platform.excluded_from_sync is False


class TestUpdateDisplayName:
    def test_sets_display_name(self):
        platform = Platform(slug="snes", display_name="Super Nintendo")
        platform.update_display_name("Super Famicom")
        assert platform.display_name == "Super Famicom"


class TestExcludeFromSync:
    def test_marks_excluded(self):
        platform = Platform(slug="snes", display_name="Super Nintendo")
        platform.exclude_from_sync()
        assert platform.excluded_from_sync is True


class TestIncludeInSync:
    def test_marks_included(self):
        platform = Platform(slug="snes", display_name="Super Nintendo", excluded_from_sync=True)
        platform.include_in_sync()
        assert platform.excluded_from_sync is False

    def test_exclude_then_include_round_trip(self):
        platform = Platform(slug="snes", display_name="Super Nintendo")
        platform.exclude_from_sync()
        assert platform.excluded_from_sync is True
        platform.include_in_sync()
        assert platform.excluded_from_sync is False
