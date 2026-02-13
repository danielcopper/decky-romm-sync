import pytest
import json
import os
import asyncio

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    # Manually init what _main() would do
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    return p


class TestAppIdGeneration:
    def test_generates_signed_int32(self, plugin):
        app_id = plugin._generate_app_id("/path/to/exe", "Test Game")
        assert isinstance(app_id, int)
        assert app_id < 0  # Should be negative (high bit set)

    def test_deterministic(self, plugin):
        id1 = plugin._generate_app_id("/path/exe", "Game")
        id2 = plugin._generate_app_id("/path/exe", "Game")
        assert id1 == id2

    def test_different_names_different_ids(self, plugin):
        id1 = plugin._generate_app_id("/path/exe", "Game A")
        id2 = plugin._generate_app_id("/path/exe", "Game B")
        assert id1 != id2


class TestArtworkIdGeneration:
    def test_generates_unsigned(self, plugin):
        art_id = plugin._generate_artwork_id("/path/exe", "Game")
        assert art_id > 0

    def test_matches_app_id_bits(self, plugin):
        # artwork_id and app_id should share the same CRC base
        art_id = plugin._generate_artwork_id("/path/exe", "Game")
        assert art_id & 0x80000000  # High bit set


class TestResolveSystem:
    def test_exact_slug_match(self, plugin):
        result = plugin._resolve_system("n64")
        assert result == "n64"

    def test_fs_slug_fallback(self, plugin):
        # A slug not in the map but its fs_slug is
        result = plugin._resolve_system("nonexistent-slug", "n64")
        assert result == "n64"

    def test_fallback_returns_slug_as_is(self, plugin):
        result = plugin._resolve_system("totally-unknown-platform")
        assert result == "totally-unknown-platform"


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_settings_masks_password(self, plugin):
        plugin.settings["romm_pass"] = "secret123"
        result = await plugin.get_settings()
        assert result["romm_pass_masked"] == "••••"
        assert "secret123" not in str(result)

    @pytest.mark.asyncio
    async def test_get_settings_empty_password(self, plugin):
        plugin.settings["romm_pass"] = ""
        result = await plugin.get_settings()
        assert result["romm_pass_masked"] == ""

    @pytest.mark.asyncio
    async def test_save_settings_skips_masked_password(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["romm_pass"] = "original"
        await plugin.save_settings("http://example.com", "user", "••••")
        assert plugin.settings["romm_pass"] == "original"

    @pytest.mark.asyncio
    async def test_save_settings_updates_real_password(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_SETTINGS_DIR = str(tmp_path)
        plugin.settings["romm_pass"] = "old"
        await plugin.save_settings("http://example.com", "user", "newpass")
        assert plugin.settings["romm_pass"] == "newpass"


class TestPlatformMap:
    def test_loads_config_json(self, plugin):
        pm = plugin._load_platform_map()
        assert isinstance(pm, dict)
        assert "n64" in pm
        assert "snes" in pm
        assert len(pm) > 50  # Should have many entries
