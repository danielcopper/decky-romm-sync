"""Tests for ``domain.installed_roms.is_pending_migration_path``."""

from __future__ import annotations

from domain.installed_roms import is_pending_migration_path


class TestIsPendingMigrationPath:
    def test_empty_pending_home_returns_false(self):
        """No pending migration marker → no entry is preserved."""
        assert is_pending_migration_path("/old/retrodeck/roms/n64/a.z64", "", "") is False

    def test_file_path_under_pending_home(self):
        """file_path under pending_home → True."""
        assert (
            is_pending_migration_path(
                "/old/retrodeck/roms/n64/zelda.z64",
                "",
                "/old/retrodeck",
            )
            is True
        )

    def test_rom_dir_under_pending_home(self):
        """rom_dir under pending_home → True (fallback for multi-file ROMs)."""
        assert (
            is_pending_migration_path(
                "",
                "/old/retrodeck/roms/psx/FF7",
                "/old/retrodeck",
            )
            is True
        )

    def test_both_unrelated_returns_false(self):
        """Neither file_path nor rom_dir under pending_home → False."""
        assert (
            is_pending_migration_path(
                "/new/retrodeck/roms/n64/a.z64",
                "/new/retrodeck/roms/psx/FF7",
                "/old/retrodeck",
            )
            is False
        )

    def test_both_empty_returns_false(self):
        """Empty file_path and rom_dir → False even when pending_home set."""
        assert is_pending_migration_path("", "", "/old/retrodeck") is False

    def test_none_rom_dir_falls_back_to_file_path(self):
        """Single-file ROM (``rom_dir`` is ``None``) → decided by file_path alone."""
        assert is_pending_migration_path("/old/retrodeck/roms/n64/zelda.z64", None, "/old/retrodeck") is True
        assert is_pending_migration_path("/new/retrodeck/roms/n64/a.z64", None, "/old/retrodeck") is False

    def test_prefix_false_match_rejected(self):
        """``/foo`` does NOT preserve ``/foobar/x`` — the separator must follow."""
        assert is_pending_migration_path("/foobar/x.z64", "", "/foo") is False

    def test_exact_match_without_separator_rejected(self):
        """``/old/retrodeck`` exactly matching the pending_home root (no trailing
        separator) is not "under" the pending home and is rejected."""
        assert is_pending_migration_path("/old/retrodeck", "", "/old/retrodeck") is False
