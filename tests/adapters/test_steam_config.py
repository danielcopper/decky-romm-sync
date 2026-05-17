import logging
import os
from unittest.mock import patch

import pytest
import vdf

from adapters.steam_config import SteamConfigAdapter
from lib.errors import SteamGridDirMissingError


@pytest.fixture
def adapter(tmp_path):
    return SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))


# ── find_steam_user_dir ─────────────────────────────────────


class TestFindSteamUserDir:
    def test_single_user_local_share(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata"
        user_dir = userdata / "12345"
        user_dir.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.find_steam_user_dir()
        assert result == str(user_dir)

    def test_single_user_dot_steam(self, tmp_path):
        userdata = tmp_path / ".steam" / "steam" / "userdata"
        user_dir = userdata / "67890"
        user_dir.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.find_steam_user_dir()
        assert result == str(user_dir)

    def test_multiple_users_returns_most_recent(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata"
        user1 = userdata / "111"
        user2 = userdata / "222"
        user1.mkdir(parents=True)
        user2.mkdir(parents=True)
        # Make user2 newer
        os.utime(str(user1), (1000, 1000))
        os.utime(str(user2), (2000, 2000))
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.find_steam_user_dir()
        assert result == str(user2)

    def test_no_steam_dir_returns_none(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        assert adapter.find_steam_user_dir() is None

    def test_no_numeric_dirs_returns_none(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata"
        (userdata / "not_numeric").mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        assert adapter.find_steam_user_dir() is None

    def test_prefers_local_share_over_dot_steam(self, tmp_path):
        # .local/share path is checked first
        path1 = tmp_path / ".local" / "share" / "Steam" / "userdata" / "111"
        path1.mkdir(parents=True)
        path2 = tmp_path / ".steam" / "steam" / "userdata" / "222"
        path2.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.find_steam_user_dir()
        assert result == str(path1)


# ── shortcuts_vdf_path ──────────────────────────────────────


class TestShortcutsVdfPath:
    def test_returns_path_when_user_dir_exists(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.shortcuts_vdf_path()
        assert result == os.path.join(str(userdata), "config", "shortcuts.vdf")

    def test_returns_none_when_no_user_dir(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        assert adapter.shortcuts_vdf_path() is None


# ── grid_dir ────────────────────────────────────────────────


class TestGridDir:
    def test_creates_and_returns_grid_dir(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.grid_dir()
        expected = os.path.join(str(userdata), "config", "grid")
        assert result == expected
        assert os.path.isdir(expected)

    def test_returns_none_when_no_user_dir(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        assert adapter.grid_dir() is None


# ── read_shortcuts / write_shortcuts ────────────────────────


class TestReadShortcuts:
    def test_returns_empty_when_no_path(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.read_shortcuts()
        assert result == {"shortcuts": {}}

    def test_returns_empty_when_file_missing(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.read_shortcuts()
        assert result == {"shortcuts": {}}

    def test_reads_existing_vdf(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        config_dir = userdata / "config"
        config_dir.mkdir(parents=True)
        vdf_data = {"shortcuts": {"0": {"appname": "Test"}}}
        vdf_path = config_dir / "shortcuts.vdf"
        with open(str(vdf_path), "wb") as f:
            f.write(vdf.binary_dumps(vdf_data))
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.read_shortcuts()
        assert result["shortcuts"]["0"]["appname"] == "Test"


class TestWriteShortcuts:
    def test_writes_vdf_file(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        config_dir = userdata / "config"
        config_dir.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        data = {"shortcuts": {"0": {"appname": "Written"}}}
        adapter.write_shortcuts(data)
        vdf_path = config_dir / "shortcuts.vdf"
        assert vdf_path.exists()
        with open(str(vdf_path), "rb") as f:
            loaded = vdf.binary_loads(f.read())
        assert loaded["shortcuts"]["0"]["appname"] == "Written"

    def test_raises_when_no_user_dir(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with pytest.raises(RuntimeError, match="Cannot find"):
            adapter.write_shortcuts({"shortcuts": {}})

    def test_atomic_write_via_replace(self, tmp_path):
        """Write uses tmp file + os.replace for atomicity."""
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        config_dir = userdata / "config"
        config_dir.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        adapter.write_shortcuts({"shortcuts": {}})
        # .tmp should not remain
        tmp_file = config_dir / "shortcuts.vdf.tmp"
        assert not tmp_file.exists()
        assert (config_dir / "shortcuts.vdf").exists()


# ── write_shortcut_icon ─────────────────────────────────────


class TestWriteShortcutIcon:
    def test_writes_png_to_grid_dir(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        result = adapter.write_shortcut_icon(12345, b"png-data")
        grid_dir = userdata / "config" / "grid"
        expected = grid_dir / "12345_icon.png"
        assert result == str(expected)
        assert expected.read_bytes() == b"png-data"

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        adapter.write_shortcut_icon(12345, b"data")
        grid_dir = userdata / "config" / "grid"
        assert not (grid_dir / "12345_icon.png.tmp").exists()

    def test_overwrites_existing(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        adapter.write_shortcut_icon(12345, b"first")
        adapter.write_shortcut_icon(12345, b"second")
        grid_dir = userdata / "config" / "grid"
        assert (grid_dir / "12345_icon.png").read_bytes() == b"second"

    def test_raises_when_no_grid_dir(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with pytest.raises(SteamGridDirMissingError, match="grid directory"):
            adapter.write_shortcut_icon(12345, b"data")

    def test_cleans_tmp_on_failure(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        userdata.mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with (
            patch("adapters.steam_config.os.replace", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            adapter.write_shortcut_icon(12345, b"data")
        grid_dir = userdata / "config" / "grid"
        assert not (grid_dir / "12345_icon.png.tmp").exists()
        assert not (grid_dir / "12345_icon.png").exists()


# ── set_steam_input_config ──────────────────────────────────


class TestSetSteamInputConfig:
    def _make_adapter_with_localconfig(self, tmp_path, localconfig_data):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        config_dir = userdata / "config"
        config_dir.mkdir(parents=True)
        localconfig_path = config_dir / "localconfig.vdf"
        with open(str(localconfig_path), "w", encoding="utf-8") as f:
            vdf.dump(localconfig_data, f, pretty=True)
        return SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))

    def test_no_user_dir_returns_early(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        # Should not raise
        adapter.set_steam_input_config([12345], mode="force_on")

    def test_no_localconfig_returns_early(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        (userdata / "config").mkdir(parents=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        adapter.set_steam_input_config([12345], mode="force_on")

    def test_force_on_sets_value_2(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {}}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        adapter.set_steam_input_config([12345], mode="force_on")
        # Re-read and verify
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        with open(str(userdata / "config" / "localconfig.vdf")) as f:
            result = vdf.load(f)
        assert result["UserLocalConfigStore"]["Apps"]["12345"]["UseSteamControllerConfig"] == "2"

    def test_force_off_sets_value_0(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {}}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        adapter.set_steam_input_config([99], mode="force_off")
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        with open(str(userdata / "config" / "localconfig.vdf")) as f:
            result = vdf.load(f)
        assert result["UserLocalConfigStore"]["Apps"]["99"]["UseSteamControllerConfig"] == "0"

    def test_default_removes_override(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {"42": {"UseSteamControllerConfig": "2", "other": "val"}}}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        adapter.set_steam_input_config([42], mode="default")
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        with open(str(userdata / "config" / "localconfig.vdf")) as f:
            result = vdf.load(f)
        # Key removed, but app entry remains because it has other keys
        assert "UseSteamControllerConfig" not in result["UserLocalConfigStore"]["Apps"]["42"]

    def test_default_removes_empty_app_entry(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {"42": {"UseSteamControllerConfig": "2"}}}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        adapter.set_steam_input_config([42], mode="default")
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        with open(str(userdata / "config" / "localconfig.vdf")) as f:
            result = vdf.load(f)
        assert "42" not in result["UserLocalConfigStore"]["Apps"]

    def test_default_mode_no_apps_key_returns_early(self, tmp_path):
        data = {"UserLocalConfigStore": {}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        # Should not raise, just return
        adapter.set_steam_input_config([42], mode="default")

    def test_force_on_creates_apps_key_if_missing(self, tmp_path):
        data = {"UserLocalConfigStore": {}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        adapter.set_steam_input_config([42], mode="force_on")
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        with open(str(userdata / "config" / "localconfig.vdf")) as f:
            result = vdf.load(f)
        assert result["UserLocalConfigStore"]["Apps"]["42"]["UseSteamControllerConfig"] == "2"

    def test_no_change_doesnt_write(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {}}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        lc_path = userdata / "config" / "localconfig.vdf"
        mtime_before = os.path.getmtime(str(lc_path))
        # default mode on non-existent app -> no change
        adapter.set_steam_input_config([999], mode="default")
        mtime_after = os.path.getmtime(str(lc_path))
        assert mtime_before == mtime_after

    def test_parse_error_returns_early(self, tmp_path):
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        config_dir = userdata / "config"
        config_dir.mkdir(parents=True)
        with open(str(config_dir / "localconfig.vdf"), "w") as f:
            f.write("not valid vdf {{{")
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        # Should not raise
        adapter.set_steam_input_config([42], mode="force_on")

    def test_multiple_app_ids(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {}}}
        adapter = self._make_adapter_with_localconfig(tmp_path, data)
        adapter.set_steam_input_config([100, 200, 300], mode="force_on")
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        with open(str(userdata / "config" / "localconfig.vdf")) as f:
            result = vdf.load(f)
        for app_id in ["100", "200", "300"]:
            assert result["UserLocalConfigStore"]["Apps"][app_id]["UseSteamControllerConfig"] == "2"

    def test_write_failure_logged(self, tmp_path):
        data = {"UserLocalConfigStore": {"Apps": {}}}
        logger = logging.getLogger("test_write_fail")
        userdata = tmp_path / ".local" / "share" / "Steam" / "userdata" / "123"
        config_dir = userdata / "config"
        config_dir.mkdir(parents=True)
        with open(str(config_dir / "localconfig.vdf"), "w", encoding="utf-8") as f:
            vdf.dump(data, f, pretty=True)
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logger)
        with patch("adapters.steam_config.vdf.dump", side_effect=OSError("disk full")):
            # Should not raise, just log
            adapter.set_steam_input_config([42], mode="force_on")


# ── check_retroarch_input_driver ────────────────────────────


class TestCheckRetroarchInputDriver:
    def test_finds_problematic_driver(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text('input_driver = "x"\n')
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)):
            result = adapter.check_retroarch_input_driver()
        assert result is not None
        assert result["warning"] is True
        assert result["current"] == "x"
        assert result["config_path"] == str(cfg_path)

    def test_finds_safe_driver(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text('input_driver = "sdl2"\n')
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)):
            result = adapter.check_retroarch_input_driver()
        assert result is not None
        assert result["warning"] is False
        assert result["current"] == "sdl2"

    def test_no_config_found_returns_none(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(tmp_path / "nonexistent.cfg")):
            result = adapter.check_retroarch_input_driver()
        assert result is None

    def test_no_input_driver_line(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text("some_other_setting = true\n")
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)):
            result = adapter.check_retroarch_input_driver()
        # None because it tries all candidates but none have input_driver
        assert result is None

    def test_input_driver_without_equals(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text("input_driver_something\n")
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)):
            result = adapter.check_retroarch_input_driver()
        assert result is None


# ── fix_retroarch_input_driver ──────────────────────────────


class TestFixRetroarchInputDriver:
    def test_fixes_problematic_driver(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text('other = "yes"\ninput_driver = "x"\nmore = "no"\n')
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)):
            result = adapter.fix_retroarch_input_driver()
        assert result["success"] is True
        content = cfg_path.read_text()
        assert 'input_driver = "sdl2"' in content
        assert 'other = "yes"' in content
        assert 'more = "no"' in content

    def test_no_fix_needed(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text('input_driver = "sdl2"\n')
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)):
            result = adapter.fix_retroarch_input_driver()
        assert result["success"] is False
        assert "No fix needed" in result["message"]

    def test_no_config_found(self, tmp_path):
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("adapters.steam_config.os.path.expanduser", return_value=str(tmp_path / "nope.cfg")):
            result = adapter.fix_retroarch_input_driver()
        assert result["success"] is False

    def test_write_error_returns_failure(self, tmp_path):
        cfg_path = tmp_path / "retroarch.cfg"
        cfg_path.write_text('input_driver = "x"\n')
        adapter = SteamConfigAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with (
            patch("adapters.steam_config.os.path.expanduser", return_value=str(cfg_path)),
            patch(
                "builtins.open",
                side_effect=[
                    open(str(cfg_path)),  # check_retroarch_input_driver read
                    open(str(cfg_path)),  # fix_retroarch_input_driver read lines
                    OSError("nope"),  # fix_retroarch_input_driver write
                ],
            ),
        ):
            result = adapter.fix_retroarch_input_driver()
        assert result["success"] is False
        assert "failed" in result["message"].lower()
