"""Tests for domain.version."""

from __future__ import annotations

from domain.version import meets_min_version

MIN = (4, 8, 1)


class TestMeetsMinVersion:
    def test_exact_minimum(self):
        assert meets_min_version("4.8.1", MIN) is True

    def test_patch_above(self):
        assert meets_min_version("4.8.2", MIN) is True

    def test_minor_above(self):
        assert meets_min_version("4.9.0", MIN) is True

    def test_major_above(self):
        assert meets_min_version("5.0.0", MIN) is True

    def test_patch_below(self):
        assert meets_min_version("4.8.0", MIN) is False

    def test_minor_below(self):
        assert meets_min_version("4.7.0", MIN) is False

    def test_major_below(self):
        assert meets_min_version("3.99.99", MIN) is False

    def test_partial_two_part_version_below(self):
        # (4, 8) < (4, 8, 1) in tuple comparison
        assert meets_min_version("4.8", MIN) is False

    def test_four_part_version_above(self):
        # (4, 8, 1, 1) >= (4, 8, 1)
        assert meets_min_version("4.8.1.1", MIN) is True

    def test_garbage_returns_false(self):
        assert meets_min_version("abc", MIN) is False

    def test_empty_string_returns_false(self):
        assert meets_min_version("", MIN) is False

    def test_none_returns_false(self):
        # int() on None raises TypeError; .split on None raises AttributeError.
        # The function must return False for a None input rather than raising.
        assert meets_min_version(None, MIN) is False  # type: ignore[arg-type]

    def test_development_returns_false(self):
        assert meets_min_version("development", MIN) is False

    def test_partly_numeric_returns_false(self):
        assert meets_min_version("4.8.x", MIN) is False
