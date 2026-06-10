"""Tests for domain.save_layout value objects."""

from __future__ import annotations

import pytest

from domain.save_layout import ContentDir, InSaveDir, SaveLayout


class TestInSaveDir:
    def test_construction_exposes_flags(self):
        layout = InSaveDir(sort_by_content=True, sort_by_core=False)
        assert layout.sort_by_content is True
        assert layout.sort_by_core is False

    def test_equality_by_flags(self):
        assert InSaveDir(sort_by_content=True, sort_by_core=True) == InSaveDir(sort_by_content=True, sort_by_core=True)

    def test_inequality_on_differing_flags(self):
        assert InSaveDir(sort_by_content=True, sort_by_core=False) != InSaveDir(
            sort_by_content=False, sort_by_core=False
        )

    def test_frozen_rejects_mutation(self):
        layout = InSaveDir(sort_by_content=True, sort_by_core=False)
        with pytest.raises((AttributeError, TypeError)):
            layout.sort_by_content = False  # type: ignore[misc]


class TestContentDir:
    def test_construction(self):
        # No fields — purely a tag in the union.
        assert ContentDir() == ContentDir()

    def test_not_equal_to_in_save_dir(self):
        assert ContentDir() != InSaveDir(sort_by_content=True, sort_by_core=False)

    def test_frozen_rejects_attribute_set(self):
        layout = ContentDir()
        with pytest.raises((AttributeError, TypeError)):
            layout.foo = 1  # type: ignore[attr-defined]


class TestSaveLayoutUnion:
    def test_isinstance_discriminates_the_union(self):
        in_dir: SaveLayout = InSaveDir(sort_by_content=True, sort_by_core=False)
        content: SaveLayout = ContentDir()
        assert isinstance(in_dir, InSaveDir)
        assert not isinstance(in_dir, ContentDir)
        assert isinstance(content, ContentDir)
        assert not isinstance(content, InSaveDir)
