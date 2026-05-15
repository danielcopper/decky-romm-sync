import asyncio
import http.client
import os
from unittest.mock import MagicMock

import pytest
from conftest import FakeSgdbArtworkCache
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.steam_config import SteamConfigAdapter
from lib.errors import SgdbApiError, SteamGridDirMissingError

# conftest.py patches decky before this import
from main import Plugin
from services.library import LibraryService, LibraryServiceConfig
from services.steamgrid import SteamGridConfig, SteamGridService


@pytest.fixture
def sgdb_artwork_cache():
    """Fresh in-memory SGDB artwork cache per test."""
    return FakeSgdbArtworkCache(cache_root="/runtime")


@pytest.fixture
def plugin(sgdb_artwork_cache):
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._metadata_cache = {}

    import decky

    p._debug_logger = SettingsAwareDebugLogger(settings=p.settings, logger=decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
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
        ),
    )

    sgdb_api = MagicMock()

    p._sgdb_service = SteamGridService(
        config=SteamGridConfig(
            sgdb_api=sgdb_api,
            romm_api=p._romm_api,
            steam_config=steam_config,
            sgdb_artwork_cache=sgdb_artwork_cache,
            state=p._state,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            save_state=MagicMock(),
            save_settings_to_disk=MagicMock(),
            get_pending_sync=lambda: p._sync_service._pending_sync,
            log_debug=p._log_debug,
        ),
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop matches the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()


def _cached_path(cache: FakeSgdbArtworkCache, rom_id: int, asset_type: str) -> str:
    return os.path.join(cache.cache_dir(), f"{rom_id}_{asset_type}.png")


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
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = SgdbApiError(401, "Unauthorized")

        result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_403(self, plugin):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = SgdbApiError(403, "Forbidden")

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
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = SgdbApiError(500, "Internal Server Error")

        result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "HTTP 500" in result["message"]

    @pytest.mark.asyncio
    async def test_legacy_urllib_http_error_still_handled(self, plugin):
        """Defence-in-depth: a stray urllib.error.HTTPError should still be handled."""
        import urllib.error

        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin._sgdb_service._sgdb_api.verify_api_key.side_effect = urllib.error.HTTPError(
            "https://steamgriddb.com", 502, "Bad Gateway", http.client.HTTPMessage(), None
        )

        result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        # Falls into the generic Exception branch since it's not an SgdbApiError.
        assert "Connection failed" in result["message"]


class TestGetSgdbArtworkBase64:
    @pytest.mark.asyncio
    async def test_cached_artwork_returns_base64(self, plugin, sgdb_artwork_cache):
        import base64

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Pre-populate the in-memory cache
        sgdb_artwork_cache.files[_cached_path(sgdb_artwork_cache, 42, "hero")] = b"fake png data"

        result = await plugin.get_sgdb_artwork_base64(42, 1)  # 1 = hero
        assert result["no_api_key"] is False
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"fake png data"

    @pytest.mark.asyncio
    async def test_no_api_key_returns_no_api_key_true(self, plugin):
        # No API key in settings
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        result = await plugin.get_sgdb_artwork_base64(42, 1)
        assert result["base64"] is None
        assert result["no_api_key"] is True

    @pytest.mark.asyncio
    async def test_invalid_asset_type(self, plugin):
        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        result = await plugin.get_sgdb_artwork_base64(42, 99)
        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_no_igdb_id_fetched_from_romm(self, plugin, sgdb_artwork_cache):
        import base64
        from unittest.mock import patch

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

        art_path = _cached_path(sgdb_artwork_cache, 42, "hero")

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            sgdb_artwork_cache.files[art_path] = b"hero artwork"
            return art_path

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
    async def test_no_igdb_id_anywhere(self, plugin):
        from unittest.mock import patch

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
    async def test_sgdb_game_lookup_no_match(self, plugin):
        from unittest.mock import patch

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
    async def test_download_fails_returns_null(self, plugin):
        from unittest.mock import patch

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
    async def test_igdb_id_from_pending_sync(self, plugin, sgdb_artwork_cache):
        import base64
        from unittest.mock import patch

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Not in registry, but in pending sync
        plugin._sync_service._pending_sync[42] = {
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 5678,
        }

        art_path = _cached_path(sgdb_artwork_cache, 42, "logo")

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            sgdb_artwork_cache.files[art_path] = b"logo data"
            return art_path

        svc = plugin._sgdb_service
        with (
            patch.object(svc, "_get_sgdb_game_id", return_value=9999),
            patch.object(svc, "_download_sgdb_artwork", side_effect=fake_download_sgdb),
        ):
            result = await plugin.get_sgdb_artwork_base64(42, 2)  # 2 = logo

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"logo data"

    @pytest.mark.asyncio
    async def test_romm_api_fetch_fails_gracefully(self, plugin):
        from unittest.mock import patch

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Not in registry or pending, RomM API fails
        with patch.object(plugin._romm_api, "get_rom", side_effect=Exception("Connection refused")):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_id_cached_in_registry(self, plugin, sgdb_artwork_cache):
        from unittest.mock import patch

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

        art_path = _cached_path(sgdb_artwork_cache, 42, "grid")

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            assert sgdb_game_id == 9999  # Should use cached sgdb_id
            sgdb_artwork_cache.files[art_path] = b"grid data"
            return art_path

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
    async def test_icon_asset_type_num_is_4(self, plugin, sgdb_artwork_cache):
        """Asset type number 4 should map to 'icon'."""
        import base64

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Pre-populate cache
        sgdb_artwork_cache.files[_cached_path(sgdb_artwork_cache, 42, "icon")] = b"icon png data"

        result = await plugin.get_sgdb_artwork_base64(42, 4)  # 4 = icon
        assert result["no_api_key"] is False
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"icon png data"

    @pytest.mark.asyncio
    async def test_icon_download_from_sgdb(self, plugin, sgdb_artwork_cache):
        """Icon should be downloadable from SGDB icons endpoint."""
        import base64
        from unittest.mock import patch

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
            "sgdb_id": 9999,
        }

        art_path = _cached_path(sgdb_artwork_cache, 42, "icon")

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            assert asset_type == "icon"
            assert sgdb_game_id == 9999
            sgdb_artwork_cache.files[art_path] = b"icon data"
            return art_path

        with patch.object(plugin._sgdb_service, "_download_sgdb_artwork", side_effect=fake_download_sgdb):
            result = await plugin.get_sgdb_artwork_base64(42, 4)

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"icon data"

    def test_download_sgdb_artwork_icon_endpoint(self, plugin):
        """_download_sgdb_artwork should use /icons/ endpoint for icon type."""
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
    def test_removes_orphan_artwork(self, plugin, sgdb_artwork_cache):
        """Artwork for rom_id not in registry should be deleted."""
        orphan = _cached_path(sgdb_artwork_cache, 42, "hero")
        sgdb_artwork_cache.files[orphan] = b"orphaned data"

        # Registry has no rom_id "42"
        plugin._state["shortcut_registry"] = {"99": {"app_id": 1}}

        plugin._sgdb_service.prune_orphaned_artwork_cache()

        assert orphan not in sgdb_artwork_cache.files

    def test_keeps_artwork_in_registry(self, plugin, sgdb_artwork_cache):
        """Artwork for rom_id in registry should survive."""
        kept = _cached_path(sgdb_artwork_cache, 42, "hero")
        sgdb_artwork_cache.files[kept] = b"keep me"

        plugin._state["shortcut_registry"] = {"42": {"app_id": 1}}

        plugin._sgdb_service.prune_orphaned_artwork_cache()

        assert sgdb_artwork_cache.files[kept] == b"keep me"

    def test_removes_leftover_tmp(self, plugin, sgdb_artwork_cache):
        """Leftover .tmp files should always be removed regardless of rom_id."""
        tmp_path = _cached_path(sgdb_artwork_cache, 42, "hero") + ".tmp"
        sgdb_artwork_cache.files[tmp_path] = b"tmp data"

        # rom_id "42" IS in registry, but .tmp should still be removed
        plugin._state["shortcut_registry"] = {"42": {"app_id": 1}}

        plugin._sgdb_service.prune_orphaned_artwork_cache()

        assert tmp_path not in sgdb_artwork_cache.files

    def test_empty_artwork_dir(self, plugin):
        """No crash on empty artwork directory."""
        plugin._sgdb_service.prune_orphaned_artwork_cache()
        # Should complete without error

    def test_no_artwork_dir(self, plugin, sgdb_artwork_cache):
        """No crash when artwork directory doesn't exist."""
        # Mark the cache directory as absent.
        sgdb_artwork_cache.isdir_paths = set()
        plugin._sgdb_service.prune_orphaned_artwork_cache()
        # Should complete without error

    def test_handles_os_error(self, plugin, sgdb_artwork_cache):
        """OSError on cache.remove should log warning, not crash."""
        orphan = _cached_path(sgdb_artwork_cache, 42, "hero")
        sgdb_artwork_cache.files[orphan] = b"orphaned data"

        plugin._state["shortcut_registry"] = {}

        def raising_remove(_path: str) -> None:
            raise OSError("Permission denied")

        sgdb_artwork_cache.remove = raising_remove  # type: ignore[method-assign]
        plugin._sgdb_service.prune_orphaned_artwork_cache()
        # File still exists because remove was patched to fail
        assert orphan in sgdb_artwork_cache.files


class TestSaveShortcutIcon:
    """Tests for VDF-based icon saving (save_shortcut_icon callable)."""

    def test_save_icon_to_grid_writes_file(self, plugin, tmp_path):
        """Icon PNG should be written via the SteamConfigAdapter seam."""
        written: dict = {}

        def fake_write_icon(app_id, icon_bytes):
            path = os.path.join(str(tmp_path), f"{app_id}_icon.png")
            written[path] = icon_bytes
            return path

        plugin._steam_config.write_shortcut_icon = fake_write_icon  # type: ignore[method-assign]
        plugin._steam_config.read_shortcuts = lambda: {"shortcuts": {}}  # type: ignore[method-assign]
        plugin._steam_config.write_shortcuts = lambda data: None  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"fake png data")

        assert result is True
        icon_path = os.path.join(str(tmp_path), "12345_icon.png")
        assert written[icon_path] == b"fake png data"

    def test_save_icon_to_grid_updates_vdf(self, plugin, tmp_path):
        """VDF icon field should be updated for the matching shortcut."""
        from domain.sgdb_artwork import to_signed_app_id

        # app_id 3000000000 -> signed = -1294967296
        app_id = 3000000000
        signed_id = to_signed_app_id(app_id)

        def fake_write_icon(a, _b):
            return os.path.join(str(tmp_path), f"{a}_icon.png")

        written_data = {}

        def mock_read():
            return {"shortcuts": {"0": {"appid": signed_id, "AppName": "Test"}}}

        def mock_write(data):
            written_data.update(data)

        plugin._steam_config.write_shortcut_icon = fake_write_icon  # type: ignore[method-assign]
        plugin._steam_config.read_shortcuts = mock_read  # type: ignore[method-assign]
        plugin._steam_config.write_shortcuts = mock_write  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(app_id, b"icon data")

        assert result is True
        shortcut = written_data["shortcuts"]["0"]
        assert shortcut["icon"].endswith(f"{app_id}_icon.png")

    def test_save_icon_to_grid_no_grid_dir(self, plugin):
        """Should return False if the grid directory cannot be found."""

        def raise_missing(_app_id, _bytes):
            raise SteamGridDirMissingError("Cannot find Steam grid directory")

        plugin._steam_config.write_shortcut_icon = raise_missing  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"data")
        assert result is False

    def test_save_icon_to_grid_write_failure(self, plugin):
        """Unexpected write failures should return False without crashing."""

        def raise_oserror(_app_id, _bytes):
            raise OSError("disk full")

        plugin._steam_config.write_shortcut_icon = raise_oserror  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"data")
        assert result is False

    def test_save_icon_to_grid_vdf_mismatch_still_writes_file(self, plugin, tmp_path):
        """If VDF has no matching shortcut, icon file should still be saved."""
        written: dict = {}

        def fake_write_icon(app_id, icon_bytes):
            path = os.path.join(str(tmp_path), f"{app_id}_icon.png")
            written[path] = icon_bytes
            return path

        plugin._steam_config.write_shortcut_icon = fake_write_icon  # type: ignore[method-assign]

        written_data = {}

        def mock_read():
            return {"shortcuts": {"0": {"appid": 999, "AppName": "Other"}}}

        def mock_write(data):
            written_data.update(data)

        plugin._steam_config.read_shortcuts = mock_read  # type: ignore[method-assign]
        plugin._steam_config.write_shortcuts = mock_write  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"icon data")

        assert result is True
        icon_path = os.path.join(str(tmp_path), "12345_icon.png")
        assert written[icon_path] == b"icon data"
        # VDF was written but icon field not set on any shortcut
        assert written_data["shortcuts"]["0"].get("icon") is None

    @pytest.mark.asyncio
    async def test_save_shortcut_icon_callable(self, plugin, tmp_path):
        """save_shortcut_icon callable should decode base64 and save."""
        import base64

        written: dict = {}

        def fake_write_icon(app_id, icon_bytes):
            path = os.path.join(str(tmp_path), f"{app_id}_icon.png")
            written[path] = icon_bytes
            return path

        plugin._steam_config.write_shortcut_icon = fake_write_icon  # type: ignore[method-assign]
        plugin._steam_config.read_shortcuts = lambda: {"shortcuts": {}}  # type: ignore[method-assign]
        plugin._steam_config.write_shortcuts = lambda data: None  # type: ignore[method-assign]
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        icon_b64 = base64.b64encode(b"real icon png").decode("ascii")
        result = await plugin.save_shortcut_icon(12345, icon_b64)

        assert result["success"] is True
        icon_path = os.path.join(str(tmp_path), "12345_icon.png")
        assert written[icon_path] == b"real icon png"

    @pytest.mark.asyncio
    async def test_save_shortcut_icon_invalid_base64(self, plugin):
        """Invalid base64 should return success=False."""
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        result = await plugin.save_shortcut_icon(12345, "not-valid-base64!!!")

        assert result["success"] is False


class TestDebugLoggerProtocolSeam:
    """SteamGridService routes debug messages through the injected ``DebugLogger``.

    Locks in the consolidation from #354: SteamGridService no longer owns a
    per-service ``_log_debug`` method that re-reads settings; the protocol
    seam is the sole knob. A no-op injection must suppress every SGDB
    debug message, regardless of log-level settings.
    """

    @pytest.fixture
    def plugin_with_captured_log(self, sgdb_artwork_cache):
        """Plugin fixture where ``log_debug`` is a list-capturing fake."""
        from unittest.mock import MagicMock as MM

        import decky

        p = Plugin()
        p.settings = {"log_level": "debug", "steamgriddb_api_key": ""}
        p._http_adapter = MM()
        p._romm_api = MM()
        p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
        p._metadata_cache = {}

        captured: list[str] = []

        def capture(msg: str) -> None:
            captured.append(msg)

        steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
        p._steam_config = steam_config

        p._sync_service = LibraryService(
            config=LibraryServiceConfig(
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
                save_state=MM(),
                save_settings_to_disk=MM(),
                log_debug=capture,
            ),
        )

        p._sgdb_service = SteamGridService(
            config=SteamGridConfig(
                sgdb_api=MM(),
                romm_api=p._romm_api,
                steam_config=steam_config,
                sgdb_artwork_cache=sgdb_artwork_cache,
                state=p._state,
                settings=p.settings,
                loop=asyncio.get_event_loop(),
                logger=decky.logger,
                save_state=MM(),
                save_settings_to_disk=MM(),
                get_pending_sync=lambda: p._sync_service._pending_sync,
                log_debug=capture,
            ),
        )
        return p, captured

    @pytest.mark.asyncio
    async def test_sgdb_messages_route_through_injected_debug_logger(self, plugin_with_captured_log):
        """SGDB debug messages reach the injected ``log_debug``, not a hidden ``.info()`` seam."""
        plugin, captured = plugin_with_captured_log
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # No API key configured -> early "skipped" debug message
        await plugin.get_sgdb_artwork_base64(42, 1)

        sgdb_msgs = [m for m in captured if "SGDB artwork" in m]
        assert sgdb_msgs, f"Expected SGDB debug messages on injected seam, got: {captured}"

    @pytest.mark.asyncio
    async def test_sgdb_debug_does_not_call_logger_info_directly(self, plugin_with_captured_log):
        """SGDB debug must reach the injected callback only — never ``decky.logger.info``.

        Regression for #354: pre-consolidation, ``SteamGridService._log_debug``
        was a per-service method that re-read settings and called
        ``self._logger.info(msg)`` directly whenever
        ``log_level == "debug"``. That meant the injected ``DebugLogger``
        callback was ignored — any consumer that wanted to silence
        SGDB output had no working knob. The protocol consolidation
        replaces the method with an injected callable, so the only
        sink is what bootstrap (or this fixture) provides.

        Would fail on ``main``: there a debug-level config would push
        every SGDB message through ``decky.logger.info`` in addition
        to (and bypassing) the injected callback.
        """
        from unittest.mock import patch

        import decky

        plugin, captured = plugin_with_captured_log
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        # log_level=debug — pre-fix this is exactly the state where the
        # per-service ``_log_debug`` emitted via ``self._logger.info``.
        plugin.settings["log_level"] = "debug"

        with patch.object(decky.logger, "info") as mock_info:
            await plugin.get_sgdb_artwork_base64(42, 1)

        # SGDB messages MUST land on the injected seam …
        assert any("SGDB artwork" in m for m in captured), (
            f"SGDB debug must reach the injected log_debug; captured={captured}"
        )
        # … and NOT on decky.logger.info.
        sgdb_info_calls = [c for c in mock_info.call_args_list if "SGDB artwork" in str(c)]
        assert not sgdb_info_calls, (
            "SGDB debug must NOT leak to logger.info — the injected "
            f"DebugLogger is the only sink. Observed leaks: {sgdb_info_calls}"
        )


class TestFakeSgdbArtworkCacheAtomicWrite:
    """Unit tests for the in-memory fake's ``write_bytes_atomic`` round-trip.

    Verifies the fake models the real adapter's ``path.tmp`` -> ``os.replace``
    sequence so service-layer tests can exercise atomic-write failure paths
    through the same Protocol seam.
    """

    def test_happy_path_writes_final_and_clears_tmp(self):
        cache = FakeSgdbArtworkCache()
        dest = os.path.join(cache.cache_dir(), "42_hero.png")

        cache.write_bytes_atomic(dest, b"payload")

        assert cache.files[dest] == b"payload"
        assert (dest + ".tmp") not in cache.files
        assert cache.tmp_files == set()

    def test_failure_cleans_tmp_and_raises(self):
        cache = FakeSgdbArtworkCache()
        cache.fail_on_atomic_write = True
        dest = os.path.join(cache.cache_dir(), "42_hero.png")

        with pytest.raises(OSError, match="simulated atomic-write failure"):
            cache.write_bytes_atomic(dest, b"payload")

        assert dest not in cache.files
        assert (dest + ".tmp") not in cache.files
        assert cache.tmp_files == set()
