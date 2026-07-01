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

    def test_development_returns_false(self):
        assert meets_min_version("development", MIN) is False

    def test_partly_numeric_returns_false(self):
        assert meets_min_version("4.8.x", MIN) is False

    def test_alpha_with_number_above_minimum(self):
        assert meets_min_version("5.0.0-alpha.1", MIN) is True

    def test_alpha_without_number_above_minimum(self):
        assert meets_min_version("5.0.0-alpha", MIN) is True

    def test_beta_with_number_above_minimum(self):
        assert meets_min_version("4.9.0-beta.3", MIN) is True

    def test_beta_without_number_at_exact_minimum(self):
        assert meets_min_version("4.8.1-beta", MIN) is True

    def test_alpha_below_minimum_fails(self):
        assert meets_min_version("4.8.0-alpha.99", MIN) is False

    def test_prerelease_missing_tag_number_only(self):
        assert meets_min_version("5.0.0-alpha.", MIN) is False
