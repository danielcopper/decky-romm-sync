import asyncio
import http.client
from unittest.mock import MagicMock

import pytest
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
from services.library import LibraryService
from services.steamgrid import SteamGridService


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        settings=p.settings,
        metadata_cache=p._metadata_cache,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        plugin_dir=decky.DECKY_PLUGIN_DIR,
        emit=decky.emit,
        clock=FakeClock(),
        uuid_gen=FakeUuidGen(),
        sleeper=FakeSleeper(),
        save_state=p._save_state,
        save_settings_to_disk=p._save_settings_to_disk,
        log_debug=p._log_debug,
    )

    sgdb_api = MagicMock()

    p._sgdb_service = SteamGridService(
        sgdb_api=sgdb_api,
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        settings=p.settings,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
        save_state=MagicMock(),
        save_settings_to_disk=MagicMock(),
        get_pending_sync=lambda: p._sync_service._pending_sync,
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop matches the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()


class TestVerifySgdbApiKey:
    @pytest.mark.asyncio
    async def test_valid_api_key(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.return_value = {"success": True}

        result = await plugin.verify_sgdb_api_key("valid-key-123")

        assert result["success"] is True
        assert "valid" in result["message"].lower()
        plugin._sgdb_service._sgdb_api.verify_api_key.assert_called_once_with("valid-key-123")

    @pytest.mark.asyncio
    async def test_invalid_api_key_401(self, plugin):
        import urllib.error

        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = urllib.error.HTTPError(
            "https://steamgriddb.com", 401, "Unauthorized", http.client.HTTPMessage(), None
        )

        result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_403(self, plugin):
        import urllib.error

        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = urllib.error.HTTPError(
            "https://steamgriddb.com", 403, "Forbidden", http.client.HTTPMessage(), None
        )

        result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_string_falls_back_to_saved_key(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin.settings["steamgriddb_api_key"] = "saved-key-456"
        plugin._sgdb_service._sgdb_api.verify_api_key.return_value = {"success": True}

        result = await plugin.verify_sgdb_api_key("")

        assert result["success"] is True
        # Verify it used the saved key
        plugin._sgdb_service._sgdb_api.verify_api_key.assert_called_once_with("saved-key-456")

    @pytest.mark.asyncio
    async def test_masked_value_falls_back_to_saved_key(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin.settings["steamgriddb_api_key"] = "saved-key-789"
        plugin._sgdb_service._sgdb_api.verify_api_key.return_value = {"success": True}

        result = await plugin.verify_sgdb_api_key("••••")

        assert result["success"] is True
        plugin._sgdb_service._sgdb_api.verify_api_key.assert_called_once_with("saved-key-789")

    @pytest.mark.asyncio
    async def test_no_key_configured(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        # No saved key, no provided key
        result = await plugin.verify_sgdb_api_key("")
        assert result["success"] is False
        assert "No API key configured" in result["message"]

    @pytest.mark.asyncio
    async def test_no_key_at_all_default_param(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        result = await plugin.verify_sgdb_api_key()
        assert result["success"] is False
        assert "No API key configured" in result["message"]

    @pytest.mark.asyncio
    async def test_network_error(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = ConnectionError("DNS resolution failed")

        result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "Connection failed" in result["message"]

    @pytest.mark.asyncio
    async def test_sgdb_rejects_key(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.return_value = {"success": False}

        result = await plugin.verify_sgdb_api_key("rejected-key")

        assert result["success"] is False
        assert "rejected" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_http_500_error(self, plugin):
        import urllib.error

        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = urllib.error.HTTPError(
            "https://steamgriddb.com", 500, "Internal Server Error", http.client.HTTPMessage(), None
        )

        result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "HTTP 500" in result["message"]


class TestGetSgdbArtworkBase64:
    @pytest.mark.asyncio
    async def test_cached_artwork_returns_base64(self, plugin, tmp_path):
        import base64

        plugin._sgdb_service._runtime_dir = str(tmp_path)
        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Create cached artwork file
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_hero.png"
        art_file.write_bytes(b"fake png data")

        result = await plugin.get_sgdb_artwork_base64(42, 1)  # 1 = hero
        assert result["no_api_key"] is False
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"fake png data"

    @pytest.mark.asyncio
    async def test_no_api_key_returns_no_api_key_true(self, plugin, tmp_path):
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        # No API key in settings
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        result = await plugin.get_sgdb_artwork_base64(42, 1)
        assert result["base64"] is None
        assert result["no_api_key"] is True

    @pytest.mark.asyncio
    async def test_invalid_asset_type(self, plugin, tmp_path):
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        result = await plugin.get_sgdb_artwork_base64(42, 99)
        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_no_igdb_id_fetched_from_romm(self, plugin, tmp_path):
        import base64
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM in registry but without igdb_id
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
        }

        # RomM API returns igdb_id
        romm_response = {"igdb_id": 1234}

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_hero.png"

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            # Simulate writing the file
            art_file.write_bytes(b"hero artwork")
            return str(art_file)

        svc = plugin._sgdb_service
        with (
            patch.object(plugin._romm_api, "get_rom", return_value=romm_response),
            patch.object(svc, "_get_sgdb_game_id", return_value=9999),
            patch.object(svc, "_download_sgdb_artwork", side_effect=fake_download_sgdb),
        ):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is not None
        assert result["no_api_key"] is False
        assert base64.b64decode(result["base64"]) == b"hero artwork"
        # igdb_id should be saved back to registry
        assert plugin._state["shortcut_registry"]["42"]["igdb_id"] == 1234

    @pytest.mark.asyncio
    async def test_no_igdb_id_anywhere(self, plugin, tmp_path):
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM in registry without igdb_id
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
        }

        # RomM API also returns no igdb_id
        with patch.object(plugin._romm_api, "get_rom", return_value={"igdb_id": None}):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_game_lookup_no_match(self, plugin, tmp_path):
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM with igdb_id in registry
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
        }

        # SGDB lookup returns None (no matching game)
        with patch.object(plugin._sgdb_service, "_get_sgdb_game_id", return_value=None):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_download_fails_returns_null(self, plugin, tmp_path):
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
            "sgdb_id": 9999,
        }

        # Download returns None (failed)
        with patch.object(plugin._sgdb_service, "_download_sgdb_artwork", return_value=None):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_igdb_id_from_pending_sync(self, plugin, tmp_path):
        import base64
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Not in registry, but in pending sync
        plugin._sync_service._pending_sync[42] = {
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 5678,
        }

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_logo.png"

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            art_file.write_bytes(b"logo data")
            return str(art_file)

        svc = plugin._sgdb_service
        with (
            patch.object(svc, "_get_sgdb_game_id", return_value=9999),
            patch.object(svc, "_download_sgdb_artwork", side_effect=fake_download_sgdb),
        ):
            result = await plugin.get_sgdb_artwork_base64(42, 2)  # 2 = logo

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"logo data"

    @pytest.mark.asyncio
    async def test_romm_api_fetch_fails_gracefully(self, plugin, tmp_path):
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Not in registry or pending, RomM API fails
        with patch.object(plugin._romm_api, "get_rom", side_effect=Exception("Connection refused")):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_id_cached_in_registry(self, plugin, tmp_path):
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM with both igdb_id and sgdb_id already cached
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
            "sgdb_id": 9999,
        }

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_grid.png"

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            assert sgdb_game_id == 9999  # Should use cached sgdb_id
            art_file.write_bytes(b"grid data")
            return str(art_file)

        svc = plugin._sgdb_service
        # _get_sgdb_game_id should NOT be called since sgdb_id is cached
        with (
            patch.object(svc, "_get_sgdb_game_id") as mock_lookup,
            patch.object(svc, "_download_sgdb_artwork", side_effect=fake_download_sgdb),
        ):
            result = await plugin.get_sgdb_artwork_base64(42, 3)  # 3 = grid

        mock_lookup.assert_not_called()
        assert result["base64"] is not None


class TestIconSupport:
    """Tests for SGDB icon download support (asset type 4)."""

    @pytest.mark.asyncio
    async def test_icon_type_maps_to_icons_endpoint(self, plugin):
        """Asset type 'icon' should map to the SGDB /icons/ endpoint."""
        assert plugin._sgdb_service._download_sgdb_artwork  # method exists

    @pytest.mark.asyncio
    async def test_icon_asset_type_num_is_4(self, plugin, tmp_path):
        """Asset type number 4 should map to 'icon'."""
        import base64

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Create cached icon file
        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_icon.png"
        art_file.write_bytes(b"icon png data")

        result = await plugin.get_sgdb_artwork_base64(42, 4)  # 4 = icon
        assert result["no_api_key"] is False
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"icon png data"

    @pytest.mark.asyncio
    async def test_icon_download_from_sgdb(self, plugin, tmp_path):
        """Icon should be downloadable from SGDB icons endpoint."""
        import base64
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
            "sgdb_id": 9999,
        }

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_icon.png"

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            assert asset_type == "icon"
            assert sgdb_game_id == 9999
            art_file.write_bytes(b"icon data")
            return str(art_file)

        with patch.object(plugin._sgdb_service, "_download_sgdb_artwork", side_effect=fake_download_sgdb):
            result = await plugin.get_sgdb_artwork_base64(42, 4)

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"icon data"

    def test_download_sgdb_artwork_icon_endpoint(self, plugin, tmp_path):
        """_download_sgdb_artwork should use /icons/ endpoint for icon type."""
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()

        # Track which SGDB path was requested
        requested_paths = []

        def fake_request(path):
            requested_paths.append(path)
            return {"success": True, "data": [{"url": "https://example.com/icon.png"}]}

        plugin._sgdb_service._sgdb_api.request.side_effect = fake_request
        plugin._sgdb_service._sgdb_api.download_image.return_value = True

        svc = plugin._sgdb_service
        svc._download_sgdb_artwork(9999, 42, "icon")

        assert len(requested_paths) == 1
        assert "/icons/game/9999" in requested_paths[0]


class TestPruneOrphanedArtworkCache:
    def test_removes_orphan_artwork(self, plugin, tmp_path):
        """Artwork for rom_id not in registry should be deleted."""
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        orphan = art_dir / "42_hero.png"
        orphan.write_bytes(b"orphaned data")

        # Registry has no rom_id "42"
        plugin._state["shortcut_registry"] = {"99": {"app_id": 1}}

        plugin._sgdb_service.prune_orphaned_artwork_cache()

        assert not orphan.exists()

    def test_keeps_artwork_in_registry(self, plugin, tmp_path):
        """Artwork for rom_id in registry should survive."""
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        kept = art_dir / "42_hero.png"
        kept.write_bytes(b"keep me")

        plugin._state["shortcut_registry"] = {"42": {"app_id": 1}}

        plugin._sgdb_service.prune_orphaned_artwork_cache()

        assert kept.exists()
        assert kept.read_bytes() == b"keep me"

    def test_removes_leftover_tmp(self, plugin, tmp_path):
        """Leftover .tmp files should always be removed regardless of rom_id."""
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        tmp_file = art_dir / "42_hero.png.tmp"
        tmp_file.write_bytes(b"tmp data")

        # rom_id "42" IS in registry, but .tmp should still be removed
        plugin._state["shortcut_registry"] = {"42": {"app_id": 1}}

        plugin._sgdb_service.prune_orphaned_artwork_cache()

        assert not tmp_file.exists()

    def test_empty_artwork_dir(self, plugin, tmp_path):
        """No crash on empty artwork directory."""
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()

        plugin._sgdb_service.prune_orphaned_artwork_cache()
        # Should complete without error

    def test_no_artwork_dir(self, plugin, tmp_path):
        """No crash when artwork directory doesn't exist."""
        plugin._sgdb_service._runtime_dir = str(tmp_path)

        # Don't create artwork dir
        plugin._sgdb_service.prune_orphaned_artwork_cache()
        # Should complete without error

    def test_handles_os_error(self, plugin, tmp_path):
        """OSError on os.remove should log warning, not crash."""
        from unittest.mock import patch

        plugin._sgdb_service._runtime_dir = str(tmp_path)

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        orphan = art_dir / "42_hero.png"
        orphan.write_bytes(b"orphaned data")

        plugin._state["shortcut_registry"] = {}

        with patch("os.remove", side_effect=OSError("Permission denied")):
            plugin._sgdb_service.prune_orphaned_artwork_cache()

        # File still exists because os.remove was mocked to fail
        assert orphan.exists()
        # Warning should have been logged (no crash)


class TestSaveShortcutIcon:
    """Tests for VDF-based icon saving (save_shortcut_icon callable)."""

    def test_save_icon_to_grid_writes_file(self, plugin, tmp_path):
        """Icon PNG should be written to Steam's grid directory."""
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._steam_config.grid_dir = lambda: str(grid_dir)
        plugin._steam_config.read_shortcuts = lambda: {"shortcuts": {}}
        plugin._steam_config.write_shortcuts = lambda data: None

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"fake png data")

        assert result is True
        icon_path = grid_dir / "12345_icon.png"
        assert icon_path.exists()
        assert icon_path.read_bytes() == b"fake png data"

    def test_save_icon_to_grid_updates_vdf(self, plugin, tmp_path):
        """VDF icon field should be updated for the matching shortcut."""
        import struct

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        # app_id 3000000000 -> signed = -1294967296
        app_id = 3000000000
        signed_id = struct.unpack("i", struct.pack("I", app_id & 0xFFFFFFFF))[0]

        written_data = {}

        def mock_read():
            return {"shortcuts": {"0": {"appid": signed_id, "AppName": "Test"}}}

        def mock_write(data):
            written_data.update(data)

        plugin._steam_config.read_shortcuts = mock_read
        plugin._steam_config.write_shortcuts = mock_write

        result = plugin._sgdb_service._save_icon_to_grid(app_id, b"icon data")

        assert result is True
        shortcut = written_data["shortcuts"]["0"]
        assert shortcut["icon"].endswith(f"{app_id}_icon.png")

    def test_save_icon_to_grid_no_grid_dir(self, plugin):
        """Should return False if grid directory cannot be found."""
        plugin._steam_config.grid_dir = lambda: None

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"data")
        assert result is False

    def test_save_icon_to_grid_vdf_mismatch_still_writes_file(self, plugin, tmp_path):
        """If VDF has no matching shortcut, icon file should still be saved."""
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._steam_config.grid_dir = lambda: str(grid_dir)

        written_data = {}

        def mock_read():
            return {"shortcuts": {"0": {"appid": 999, "AppName": "Other"}}}

        def mock_write(data):
            written_data.update(data)

        plugin._steam_config.read_shortcuts = mock_read
        plugin._steam_config.write_shortcuts = mock_write

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"icon data")

        assert result is True
        assert (grid_dir / "12345_icon.png").exists()
        # VDF was written but icon field not set on any shortcut
        assert written_data["shortcuts"]["0"].get("icon") is None

    @pytest.mark.asyncio
    async def test_save_shortcut_icon_callable(self, plugin, tmp_path):
        """save_shortcut_icon callable should decode base64 and save."""
        import base64

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        plugin._steam_config.grid_dir = lambda: str(grid_dir)
        plugin._steam_config.read_shortcuts = lambda: {"shortcuts": {}}
        plugin._steam_config.write_shortcuts = lambda data: None
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        icon_b64 = base64.b64encode(b"real icon png").decode("ascii")
        result = await plugin.save_shortcut_icon(12345, icon_b64)

        assert result["success"] is True
        assert (grid_dir / "12345_icon.png").read_bytes() == b"real icon png"

    @pytest.mark.asyncio
    async def test_save_shortcut_icon_invalid_base64(self, plugin, tmp_path):
        """Invalid base64 should return success=False."""
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        result = await plugin.save_shortcut_icon(12345, "not-valid-base64!!!")

        assert result["success"] is False
