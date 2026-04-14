"""Tests for adapters.retroarch_config.RetroArchConfigAdapter."""

from __future__ import annotations

import logging

from adapters.retroarch_config import RetroArchConfigAdapter


def _make_adapter(tmp_path) -> RetroArchConfigAdapter:
    return RetroArchConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))


class TestRetroArchSaveSorting:
    def test_defaults_when_no_cfg(self, tmp_path):
        """No cfg file found — returns RetroDECK defaults (True, False)."""
        adapter = _make_adapter(tmp_path)
        sort_by_content, sort_by_core = adapter.get_retroarch_save_sorting()
        assert sort_by_content is True
        assert sort_by_core is False

    def test_reads_sort_by_content_false(self, tmp_path):
        cfg_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text(
            'sort_savefiles_by_content_enable = "false"\nsort_savefiles_enable = "false"\n'
        )
        adapter = _make_adapter(tmp_path)
        sort_by_content, sort_by_core = adapter.get_retroarch_save_sorting()
        assert sort_by_content is False
        assert sort_by_core is False

    def test_reads_sort_by_core_true(self, tmp_path):
        cfg_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text(
            'sort_savefiles_by_content_enable = "true"\nsort_savefiles_enable = "true"\n'
        )
        adapter = _make_adapter(tmp_path)
        sort_by_content, sort_by_core = adapter.get_retroarch_save_sorting()
        assert sort_by_content is True
        assert sort_by_core is True

    def test_mixed_settings(self, tmp_path):
        cfg_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text(
            'sort_savefiles_by_content_enable = "false"\nsort_savefiles_enable = "true"\n'
        )
        adapter = _make_adapter(tmp_path)
        sort_by_content, sort_by_core = adapter.get_retroarch_save_sorting()
        assert sort_by_content is False
        assert sort_by_core is True

    def test_standalone_retroarch_flatpak_path(self, tmp_path):
        """Falls back to the standalone RetroArch Flatpak path if RetroDECK's is missing."""
        cfg_dir = tmp_path / ".var" / "app" / "org.libretro.RetroArch" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text('sort_savefiles_enable = "true"\n')
        adapter = _make_adapter(tmp_path)
        _, sort_by_core = adapter.get_retroarch_save_sorting()
        assert sort_by_core is True

    def test_native_retroarch_path(self, tmp_path):
        """Falls back to ~/.config/retroarch/retroarch.cfg as last resort."""
        cfg_dir = tmp_path / ".config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text('sort_savefiles_by_content_enable = "false"\n')
        adapter = _make_adapter(tmp_path)
        sort_by_content, _ = adapter.get_retroarch_save_sorting()
        assert sort_by_content is False

    def test_ignores_unrelated_cfg_lines(self, tmp_path):
        """Cfg lines that don't match either sort key are skipped cleanly."""
        cfg_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retroarch"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "retroarch.cfg").write_text(
            "# RetroArch configuration\n"
            'video_driver = "glcore"\n'
            'audio_driver = "alsa"\n'
            'sort_savefiles_enable = "true"\n'
            'some_other_option = "false"\n'
        )
        adapter = _make_adapter(tmp_path)
        sort_by_content, sort_by_core = adapter.get_retroarch_save_sorting()
        assert sort_by_content is True  # default
        assert sort_by_core is True
