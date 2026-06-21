"""Tests for adapters.flatpak_install — flatpak install-root resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from adapters import flatpak_install
from adapters.flatpak_install import flatpak_app_files_dirs

if TYPE_CHECKING:
    from pathlib import Path

_APP_ID = "net.retrodeck.retrodeck"


def _system_files_dir(system_root: Path) -> Path:
    return system_root / "app" / _APP_ID / "current" / "active" / "files"


def _user_files_dir(user_home: Path) -> Path:
    return user_home / ".local" / "share" / "flatpak" / "app" / _APP_ID / "current" / "active" / "files"


class TestFlatpakAppFilesDirs:
    def test_system_root_only(self, tmp_path):
        system_root = tmp_path / "system"
        user_home = tmp_path / "home"
        system_files = _system_files_dir(system_root)
        system_files.mkdir(parents=True)

        with mock.patch.object(flatpak_install, "SYSTEM_FLATPAK_ROOT", str(system_root)):
            result = flatpak_app_files_dirs(str(user_home))
        assert result == [str(system_files)]

    def test_user_root_only(self, tmp_path):
        system_root = tmp_path / "nonexistent_system"
        user_home = tmp_path / "home"
        user_files = _user_files_dir(user_home)
        user_files.mkdir(parents=True)

        with mock.patch.object(flatpak_install, "SYSTEM_FLATPAK_ROOT", str(system_root)):
            result = flatpak_app_files_dirs(str(user_home))
        assert result == [str(user_files)]

    def test_both_in_priority_order_system_first(self, tmp_path):
        system_root = tmp_path / "system"
        user_home = tmp_path / "home"
        system_files = _system_files_dir(system_root)
        user_files = _user_files_dir(user_home)
        system_files.mkdir(parents=True)
        user_files.mkdir(parents=True)

        with mock.patch.object(flatpak_install, "SYSTEM_FLATPAK_ROOT", str(system_root)):
            result = flatpak_app_files_dirs(str(user_home))
        assert result == [str(system_files), str(user_files)]

    def test_empty_when_neither_exists(self, tmp_path):
        system_root = tmp_path / "nonexistent_system"
        user_home = tmp_path / "home"

        with mock.patch.object(flatpak_install, "SYSTEM_FLATPAK_ROOT", str(system_root)):
            result = flatpak_app_files_dirs(str(user_home))
        assert result == []

    def test_custom_app_id(self, tmp_path):
        """A non-default app_id resolves against the same root layout."""
        system_root = tmp_path / "system"
        user_home = tmp_path / "home"
        custom_files = system_root / "app" / "org.example.App" / "current" / "active" / "files"
        custom_files.mkdir(parents=True)

        with mock.patch.object(flatpak_install, "SYSTEM_FLATPAK_ROOT", str(system_root)):
            result = flatpak_app_files_dirs(str(user_home), app_id="org.example.App")
        assert result == [str(custom_files)]
