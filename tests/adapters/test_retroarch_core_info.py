"""Tests for adapters.retroarch_core_info.RetroArchCoreInfoAdapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from adapters.retroarch_core_info import RetroArchCoreInfoAdapter

if TYPE_CHECKING:
    from pathlib import Path

_SNES9X_INFO = (
    "# Software Information\n"
    'display_name = "Nintendo - SNES / SFC (Snes9x)"\n'
    'corename = "Snes9x"\n'
    'supported_extensions = "smc|sfc|swc|fig|bs|st"\n'
)


_CORES_SUFFIX_PARTS = (
    "retrodeck",
    "components",
    "retroarch",
    "rd_extras",
    "cores",
)


def _user_cores_dir(user_home: Path) -> Path:
    return (
        user_home / ".local" / "share" / "flatpak" / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files"
    ).joinpath(*_CORES_SUFFIX_PARTS)


def _system_cores_dir(system_root: Path) -> Path:
    """The system-root cores dir derived from a fabricated ``SYSTEM_FLATPAK_ROOT``."""
    return (system_root / "app" / "net.retrodeck.retrodeck" / "current" / "active" / "files").joinpath(
        *_CORES_SUFFIX_PARTS
    )


def _make_adapter(user_home: Path) -> RetroArchCoreInfoAdapter:
    return RetroArchCoreInfoAdapter(user_home=str(user_home), logger=logging.getLogger("test"))


@pytest.fixture(autouse=True)
def _isolate_system_dir():
    """Point the shared system flatpak root at a non-existent location so
    tests only see files placed under ``tmp_path``.

    The real ``SYSTEM_FLATPAK_ROOT`` is the Flatpak system install which may or
    may not exist on the developer machine. Pinning it to a non-existent path
    per test keeps results deterministic. Tests that exercise the system
    candidate repoint it at a fabricated tmp tree.
    """
    with patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", "/nonexistent/system/root"):
        yield


class TestGetCorename:
    def test_happy_path_user_path(self, tmp_path):
        """File in the per-user Flatpak dir is found and parsed."""
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        (cores / "snes9x_libretro.info").write_text(_SNES9X_INFO)

        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("snes9x_libretro") == "Snes9x"

    def test_file_not_found_returns_none(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("missing_libretro") is None

    def test_missing_corename_field_returns_none(self, tmp_path):
        """File exists but has no ``corename`` key — returns None."""
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        (cores / "oddcore_libretro.info").write_text('display_name = "Oddcore"\n')

        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("oddcore_libretro") is None

    def test_empty_corename_returns_none(self, tmp_path):
        """File exists with ``corename = ""`` — returns None (empty is not a name)."""
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        (cores / "blank_libretro.info").write_text('corename = ""\n')

        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("blank_libretro") is None


class TestGetCoreInfo:
    def test_returns_full_dict(self, tmp_path):
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        (cores / "snes9x_libretro.info").write_text(_SNES9X_INFO)

        adapter = _make_adapter(tmp_path)
        info = adapter.get_core_info("snes9x_libretro")
        assert info is not None
        assert info["corename"] == "Snes9x"
        assert info["display_name"] == "Nintendo - SNES / SFC (Snes9x)"
        assert info["supported_extensions"] == "smc|sfc|swc|fig|bs|st"

    def test_missing_file_returns_none(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.get_core_info("missing_libretro") is None


class TestCaching:
    def test_cache_hit_avoids_second_read(self, tmp_path):
        """After a successful read the file can be deleted — cache returns the same result."""
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        info_file = cores / "snes9x_libretro.info"
        info_file.write_text(_SNES9X_INFO)

        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("snes9x_libretro") == "Snes9x"

        # Delete the file — cached value should still be returned.
        info_file.unlink()
        assert adapter.get_corename("snes9x_libretro") == "Snes9x"

    def test_cache_negative(self, tmp_path):
        """After returning None once, subsequent calls return None even if
        a real file appears later."""
        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("snes9x_libretro") is None

        # Now create the file — cache should still return None.
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        (cores / "snes9x_libretro.info").write_text(_SNES9X_INFO)
        assert adapter.get_corename("snes9x_libretro") is None


class TestCandidatePathFallback:
    def test_falls_back_to_user_candidate(self, tmp_path):
        """First candidate (system) missing, second (user) present — still resolves."""
        cores = _user_cores_dir(tmp_path)
        cores.mkdir(parents=True)
        (cores / "mgba_libretro.info").write_text('corename = "mGBA"\n')

        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("mgba_libretro") == "mGBA"

    def test_first_candidate_used_when_present(self, tmp_path, monkeypatch):
        """If the first candidate dir has the file, the second isn't consulted."""
        # Fabricate a system-root flatpak tree we can populate.
        system_root = tmp_path / "system_root"
        system_cores = _system_cores_dir(system_root)
        system_cores.mkdir(parents=True)
        (system_cores / "snes9x_libretro.info").write_text('corename = "FromSystem"\n')

        # Put a different corename at the user-level path so we can tell them apart.
        user_cores = _user_cores_dir(tmp_path)
        user_cores.mkdir(parents=True)
        (user_cores / "snes9x_libretro.info").write_text('corename = "FromUser"\n')

        monkeypatch.setattr("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(system_root))
        adapter = _make_adapter(tmp_path)
        assert adapter.get_corename("snes9x_libretro") == "FromSystem"


class TestOsErrorHandling:
    def test_permission_error_continues_to_next_candidate(self, tmp_path, monkeypatch, caplog):
        """PermissionError on first candidate is logged and skipped; adapter
        tries the next candidate."""
        system_root = tmp_path / "system_root"
        system_cores = _system_cores_dir(system_root)
        system_cores.mkdir(parents=True)
        (system_cores / "snes9x_libretro.info").write_text('corename = "FromSystem"\n')

        user_cores = _user_cores_dir(tmp_path)
        user_cores.mkdir(parents=True)
        (user_cores / "snes9x_libretro.info").write_text('corename = "FromUser"\n')

        monkeypatch.setattr("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(system_root))

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).startswith(str(system_cores)):
                raise PermissionError(f"denied: {path}")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fake_open), caplog.at_level(logging.WARNING):
            adapter = _make_adapter(tmp_path)
            assert adapter.get_corename("snes9x_libretro") == "FromUser"
        assert any("Failed to read" in rec.message for rec in caplog.records)

    def test_permission_error_on_info_file_in_all_candidate_dirs_returns_none(self, tmp_path, caplog):
        """Every candidate raises OSError — returns None and logs warnings."""
        real_open = open

        def fake_open(path, *args, **kwargs):
            if "snes9x_libretro.info" in str(path):
                raise PermissionError(f"denied: {path}")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fake_open), caplog.at_level(logging.WARNING):
            adapter = _make_adapter(tmp_path)
            assert adapter.get_corename("snes9x_libretro") is None

    def test_unicode_decode_error_logs_and_tries_next_candidate(self, tmp_path, monkeypatch, caplog):
        """First candidate has non-UTF-8 bytes — adapter logs a warning and
        falls through to the second candidate."""
        # Fabricate the system candidate tree we can populate
        system_root = tmp_path / "system_root"
        system_cores = _system_cores_dir(system_root)
        system_cores.mkdir(parents=True)
        # Non-UTF-8 bytes — reading with encoding="utf-8" must raise UnicodeDecodeError
        (system_cores / "snes9x_libretro.info").write_bytes(b"\xff\xfe\x00corename")

        # Second (per-user) candidate is well-formed
        user_cores = _user_cores_dir(tmp_path)
        user_cores.mkdir(parents=True)
        (user_cores / "snes9x_libretro.info").write_text('corename = "FromUser"\n')

        monkeypatch.setattr("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(system_root))

        with caplog.at_level(logging.WARNING):
            adapter = _make_adapter(tmp_path)
            assert adapter.get_corename("snes9x_libretro") == "FromUser"

        assert any("Failed to read" in rec.message for rec in caplog.records)
