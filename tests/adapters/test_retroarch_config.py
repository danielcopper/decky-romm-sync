"""Tests for adapters.retroarch_config.RetroArchConfigAdapter."""

from __future__ import annotations

import logging
from unittest.mock import patch

from adapters.retroarch_config import RetroArchConfigAdapter
from domain.save_layout import ContentDir, InSaveDir


def _make_adapter(tmp_path) -> RetroArchConfigAdapter:
    return RetroArchConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))


def _write_cfg(tmp_path, text: str) -> None:
    cfg_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "retroarch.cfg").write_text(text)


class TestSaveLayoutInSaveDir:
    def test_defaults_when_no_cfg(self, tmp_path):
        """No cfg file found — returns the RetroDECK-default InSaveDir(True, False)."""
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=True, sort_by_core=False)

    def test_reads_sort_by_content_false(self, tmp_path):
        _write_cfg(tmp_path, 'sort_savefiles_by_content_enable = "false"\nsort_savefiles_enable = "false"\n')
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=False, sort_by_core=False)

    def test_reads_sort_by_core_true(self, tmp_path):
        _write_cfg(tmp_path, 'sort_savefiles_by_content_enable = "true"\nsort_savefiles_enable = "true"\n')
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=True, sort_by_core=True)

    def test_mixed_settings(self, tmp_path):
        _write_cfg(tmp_path, 'sort_savefiles_by_content_enable = "false"\nsort_savefiles_enable = "true"\n')
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=False, sort_by_core=True)

    def test_standalone_retroarch_flatpak_path(self, tmp_path):
        """Falls back to the standalone RetroArch Flatpak path if RetroDECK's is missing."""
        cfg_dir = tmp_path / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text('sort_savefiles_enable = "true"\n')
        adapter = _make_adapter(tmp_path)
        layout = adapter.get_save_layout()
        assert isinstance(layout, InSaveDir)
        assert layout.sort_by_core is True

    def test_native_retroarch_path(self, tmp_path):
        """Falls back to ~/.config/retroarch/retroarch.cfg as last resort."""
        cfg_dir = tmp_path / ".config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text('sort_savefiles_by_content_enable = "false"\n')
        adapter = _make_adapter(tmp_path)
        layout = adapter.get_save_layout()
        assert isinstance(layout, InSaveDir)
        assert layout.sort_by_content is False

    def test_ignores_unrelated_cfg_lines(self, tmp_path):
        """Cfg lines that don't match either sort key are skipped cleanly."""
        _write_cfg(
            tmp_path,
            "# RetroArch configuration\n"
            'video_driver = "glcore"\n'
            'audio_driver = "alsa"\n'
            'sort_savefiles_enable = "true"\n'
            'some_other_option = "false"\n',
        )
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=True, sort_by_core=True)

    def test_cfg_line_order_does_not_matter(self, tmp_path):
        """``sort_savefiles_enable`` appears before ``sort_savefiles_by_content_enable``
        — parsing order does not change the result."""
        _write_cfg(tmp_path, 'sort_savefiles_enable = "true"\nsort_savefiles_by_content_enable = "false"\n')
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=False, sort_by_core=True)


class TestSaveLayoutContentDir:
    """``savefiles_in_content_dir=true`` wins over any sort flags — saves are
    written next to the ROM, so plugin save sync is unsupported (#239)."""

    def test_content_dir_true_returns_content_dir(self, tmp_path):
        _write_cfg(tmp_path, 'savefiles_in_content_dir = "true"\n')
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == ContentDir()

    def test_content_dir_true_overrides_sort_flags(self, tmp_path):
        """When in-content-dir is on, the sort flags are irrelevant — ContentDir wins."""
        _write_cfg(
            tmp_path,
            'savefiles_in_content_dir = "true"\n'
            'sort_savefiles_by_content_enable = "true"\n'
            'sort_savefiles_enable = "true"\n',
        )
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == ContentDir()

    def test_content_dir_false_falls_through_to_in_save_dir(self, tmp_path):
        """Explicit ``savefiles_in_content_dir = "false"`` reads the sort flags."""
        _write_cfg(
            tmp_path,
            'savefiles_in_content_dir = "false"\n'
            'sort_savefiles_by_content_enable = "false"\n'
            'sort_savefiles_enable = "true"\n',
        )
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=False, sort_by_core=True)

    def test_content_dir_missing_defaults_to_in_save_dir(self, tmp_path):
        """No ``savefiles_in_content_dir`` line — defaults to the InSaveDir branch."""
        _write_cfg(tmp_path, 'sort_savefiles_enable = "true"\n')
        adapter = _make_adapter(tmp_path)
        assert adapter.get_save_layout() == InSaveDir(sort_by_content=True, sort_by_core=True)


class TestOsErrorHandling:
    def test_permission_error_logs_and_returns_defaults(self, tmp_path, caplog):
        """The only candidate file raises PermissionError — adapter logs a
        warning and returns the RetroDECK-default InSaveDir instead of crashing."""
        cfg_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = cfg_dir / "retroarch.cfg"
        cfg_file.write_text('sort_savefiles_enable = "true"\n')

        real_open = open

        def fake_open(path, *args, **kwargs):
            if "retroarch.cfg" in str(path):
                raise PermissionError(f"denied: {path}")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fake_open), caplog.at_level(logging.WARNING):
            adapter = _make_adapter(tmp_path)
            layout = adapter.get_save_layout()

        assert layout == InSaveDir(sort_by_content=True, sort_by_core=False)
        assert any("Failed to read" in rec.message for rec in caplog.records)

    def test_permission_error_on_first_candidate_tries_second(self, tmp_path, caplog):
        """First candidate raises PermissionError; second candidate exists with
        readable content — adapter falls through and reads the second file."""
        # First (RetroDECK) candidate is the one we'll deny
        first_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
        first_dir.mkdir(parents=True, exist_ok=True)
        first_file = first_dir / "retroarch.cfg"
        first_file.write_text('sort_savefiles_enable = "false"\n')

        # Second (standalone Flatpak) candidate with the value we expect to read
        second_dir = tmp_path / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch"
        second_dir.mkdir(parents=True, exist_ok=True)
        (second_dir / "retroarch.cfg").write_text('sort_savefiles_enable = "true"\n')

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path) == str(first_file):
                raise PermissionError(f"denied: {path}")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=fake_open), caplog.at_level(logging.WARNING):
            adapter = _make_adapter(tmp_path)
            layout = adapter.get_save_layout()

        assert isinstance(layout, InSaveDir)
        assert layout.sort_by_core is True
        assert any("Failed to read" in rec.message for rec in caplog.records)
