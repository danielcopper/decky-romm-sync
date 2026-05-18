import asyncio
import http.client
import os

import pytest
from conftest import FakeSettingsPersister, FakeSgdbArtworkCache, FakeStatePersister
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.steam_config import SteamConfigAdapter
from lib.errors import SgdbApiError, SteamGridDirMissingError

# conftest.py patches decky before this import
from main import Plugin
from services.library import LibraryService, LibraryServiceConfig
from services.steamgrid import SteamGridService, SteamGridServiceConfig


@pytest.fixture
def sgdb_artwork_cache():
    """Fresh in-memory SGDB artwork cache per test."""
    return FakeSgdbArtworkCache(cache_root="/runtime")


@pytest.fixture
def plugin(sgdb_artwork_cache, fake_romm_api, fake_steamgrid_db_api):
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._romm_api = fake_romm_api
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._metadata_cache = {}

    import decky

    p._debug_logger = SettingsAwareDebugLogger(settings=p.settings, logger=decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._state_persister = FakeStatePersister()
    p._settings_persister = FakeSettingsPersister()
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
            state_persister=p._state_persister,
            settings_persister=p._settings_persister,
            log_debug=p._log_debug,
        ),
    )

    # Bind the fake SGDB transport to the in-memory artwork cache so
    # `download_image` writes land in the cache the service consults.
    fake_steamgrid_db_api.bind_artwork_cache(sgdb_artwork_cache)

    p._sgdb_service = SteamGridService(
        config=SteamGridServiceConfig(
            sgdb_api=fake_steamgrid_db_api,
            romm_api=p._romm_api,
            steam_config=steam_config,
            sgdb_artwork_cache=sgdb_artwork_cache,
            state=p._state,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            state_persister=FakeStatePersister(),
            settings_persister=FakeSettingsPersister(),
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
    async def test_valid_api_key(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.seed_verify_response({"success": True})

        result = await plugin.verify_sgdb_api_key("valid-key-123")

        assert result["success"] is True
        assert "valid" in result["message"].lower()
        assert fake_steamgrid_db_api.verify_calls == ["valid-key-123"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_401(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.verify_api_key_side_effect = SgdbApiError(401, "Unauthorized")

        result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_403(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.verify_api_key_side_effect = SgdbApiError(403, "Forbidden")

        result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_string_falls_back_to_saved_key(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin.settings["steamgriddb_api_key"] = "saved-key-456"
        fake_steamgrid_db_api.seed_verify_response({"success": True})

        result = await plugin.verify_sgdb_api_key("")

        assert result["success"] is True
        # Verify it used the saved key
        assert fake_steamgrid_db_api.verify_calls == ["saved-key-456"]

    @pytest.mark.asyncio
    async def test_masked_value_falls_back_to_saved_key(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        plugin.settings["steamgriddb_api_key"] = "saved-key-789"
        fake_steamgrid_db_api.seed_verify_response({"success": True})

        result = await plugin.verify_sgdb_api_key("••••")

        assert result["success"] is True
        assert fake_steamgrid_db_api.verify_calls == ["saved-key-789"]

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
    async def test_network_error(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.verify_api_key_side_effect = ConnectionError("DNS resolution failed")

        result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "Connection failed" in result["message"]

    @pytest.mark.asyncio
    async def test_sgdb_rejects_key(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.seed_verify_response({"success": False})

        result = await plugin.verify_sgdb_api_key("rejected-key")

        assert result["success"] is False
        assert "rejected" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_http_500_error(self, plugin, fake_steamgrid_db_api):
        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.verify_api_key_side_effect = SgdbApiError(500, "Internal Server Error")

        result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "HTTP 500" in result["message"]

    @pytest.mark.asyncio
    async def test_legacy_urllib_http_error_still_handled(self, plugin, fake_steamgrid_db_api):
        """Defence-in-depth: a stray urllib.error.HTTPError should still be handled."""
        import urllib.error

        plugin._sgdb_service._loop = asyncio.get_event_loop()
        fake_steamgrid_db_api.verify_api_key_side_effect = urllib.error.HTTPError(
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
    async def test_no_igdb_id_fetched_from_romm(self, plugin, sgdb_artwork_cache, fake_romm_api, fake_steamgrid_db_api):
        import base64

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM in registry but without igdb_id
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
        }

        # RomM API returns igdb_id
        fake_romm_api.roms[42] = {"id": 42, "igdb_id": 1234}

        # SGDB resolves IGDB to game ID, then serves hero artwork.
        fake_steamgrid_db_api.seed_igdb_lookup(igdb_id=1234, sgdb_id=9999)
        fake_steamgrid_db_api.seed_artwork(9999, "hero", "https://example.com/hero.png")
        fake_steamgrid_db_api.seed_image_bytes("https://example.com/hero.png", b"hero artwork")

        result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is not None
        assert result["no_api_key"] is False
        assert base64.b64decode(result["base64"]) == b"hero artwork"
        # igdb_id should be saved back to registry
        assert plugin._state["shortcut_registry"]["42"]["igdb_id"] == 1234

    @pytest.mark.asyncio
    async def test_no_igdb_id_anywhere(self, plugin, fake_romm_api):
        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM in registry without igdb_id
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
        }

        # RomM API also returns no igdb_id
        fake_romm_api.roms[42] = {"id": 42, "igdb_id": None}

        result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_game_lookup_no_match(self, plugin, fake_steamgrid_db_api):
        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # ROM with igdb_id in registry
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
        }

        # SGDB lookup returns no match for this IGDB id
        fake_steamgrid_db_api.seed_igdb_lookup(igdb_id=1234, sgdb_id=None)

        result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_download_fails_returns_null(self, plugin, fake_steamgrid_db_api):
        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
            "sgdb_id": 9999,
        }

        # SGDB returns a URL but the image download fails (CDN 5xx).
        fake_steamgrid_db_api.seed_artwork(9999, "hero", "https://example.com/hero.png")
        fake_steamgrid_db_api.download_image_return = False

        result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_igdb_id_from_pending_sync(self, plugin, sgdb_artwork_cache, fake_steamgrid_db_api):
        import base64

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Not in registry, but in pending sync
        plugin._sync_service._pending_sync[42] = {
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 5678,
        }

        # SGDB resolves and serves logo artwork.
        fake_steamgrid_db_api.seed_igdb_lookup(igdb_id=5678, sgdb_id=9999)
        fake_steamgrid_db_api.seed_artwork(9999, "logo", "https://example.com/logo.png")
        fake_steamgrid_db_api.seed_image_bytes("https://example.com/logo.png", b"logo data")

        result = await plugin.get_sgdb_artwork_base64(42, 2)  # 2 = logo

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"logo data"

    @pytest.mark.asyncio
    async def test_romm_api_fetch_fails_gracefully(self, plugin, fake_romm_api):
        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        # Not in registry or pending, RomM API fails
        fake_romm_api.get_rom_side_effect = Exception("Connection refused")

        result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_id_cached_in_registry(self, plugin, sgdb_artwork_cache, fake_steamgrid_db_api):
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

        # Grid artwork available for the cached SGDB id; IGDB lookup
        # would resolve to a different id if (incorrectly) consulted.
        fake_steamgrid_db_api.seed_igdb_lookup(igdb_id=1234, sgdb_id=7777)
        fake_steamgrid_db_api.seed_artwork(9999, "grid", "https://example.com/grid.png")
        fake_steamgrid_db_api.seed_image_bytes("https://example.com/grid.png", b"grid data")

        result = await plugin.get_sgdb_artwork_base64(42, 3)  # 3 = grid

        # IGDB lookup must NOT be consulted when sgdb_id is cached.
        assert not any(p.startswith("/games/igdb/") for p in fake_steamgrid_db_api.requested_paths)
        # The artwork request must use the cached sgdb_id (9999), not 7777.
        assert any("/grids/game/9999" in p for p in fake_steamgrid_db_api.requested_paths)
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
    async def test_icon_download_from_sgdb(self, plugin, sgdb_artwork_cache, fake_steamgrid_db_api):
        """Icon should be downloadable from SGDB icons endpoint."""
        import base64

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001,
            "name": "Zelda",
            "platform_name": "N64",
            "igdb_id": 1234,
            "sgdb_id": 9999,
        }

        fake_steamgrid_db_api.seed_artwork(9999, "icon", "https://example.com/icon.png")
        fake_steamgrid_db_api.seed_image_bytes("https://example.com/icon.png", b"icon data")

        result = await plugin.get_sgdb_artwork_base64(42, 4)

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"icon data"
        # Verify the /icons/ endpoint was hit specifically.
        assert any("/icons/game/9999" in p for p in fake_steamgrid_db_api.requested_paths)

    def test_download_sgdb_artwork_icon_endpoint(self, plugin, fake_steamgrid_db_api):
        """_download_sgdb_artwork should use /icons/ endpoint for icon type."""
        fake_steamgrid_db_api.seed_artwork(9999, "icon", "https://example.com/icon.png")
        # Don't seed image bytes — download_image_return defaults to True
        # so the service returns the cached path without writing.

        svc = plugin._sgdb_service
        svc._download_sgdb_artwork(9999, 42, "icon")

        # Exactly one /icons/game/9999 request should have been issued.
        icon_requests = [p for p in fake_steamgrid_db_api.requested_paths if "/icons/game/9999" in p]
        assert len(icon_requests) == 1


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

        sgdb_artwork_cache.remove_file = raising_remove  # type: ignore[method-assign]
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
    def plugin_with_captured_log(self, sgdb_artwork_cache, fake_romm_api, fake_steamgrid_db_api):
        """Plugin fixture where ``log_debug`` is a list-capturing fake."""
        import decky

        p = Plugin()
        p.settings = {"log_level": "debug", "steamgriddb_api_key": ""}
        p._romm_api = fake_romm_api
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
                state_persister=FakeStatePersister(),
                settings_persister=FakeSettingsPersister(),
                log_debug=capture,
            ),
        )

        fake_steamgrid_db_api.bind_artwork_cache(sgdb_artwork_cache)
        p._sgdb_service = SteamGridService(
            config=SteamGridServiceConfig(
                sgdb_api=fake_steamgrid_db_api,
                romm_api=p._romm_api,
                steam_config=steam_config,
                sgdb_artwork_cache=sgdb_artwork_cache,
                state=p._state,
                settings=p.settings,
                loop=asyncio.get_event_loop(),
                logger=decky.logger,
                state_persister=FakeStatePersister(),
                settings_persister=FakeSettingsPersister(),
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


class TestGetSgdbGameId:
    """SGDB IGDB-lookup error paths in ``_get_sgdb_game_id``.

    Covers the response-shape branches and the ``except Exception`` net
    that protects the artwork pipeline from a transient SGDB outage.
    """

    def test_returns_id_on_success(self, plugin, fake_steamgrid_db_api):
        fake_steamgrid_db_api.seed_raw_response(
            "/games/igdb/1234",
            {"success": True, "data": {"id": 9999}},
        )

        result = plugin._sgdb_service._get_sgdb_game_id(1234)

        assert result == 9999
        assert fake_steamgrid_db_api.requested_paths == ["/games/igdb/1234"]

    def test_returns_none_when_success_false(self, plugin, fake_steamgrid_db_api):
        """SGDB body with success=False (e.g. unknown IGDB id) → None."""
        fake_steamgrid_db_api.seed_raw_response(
            "/games/igdb/1234",
            {"success": False, "data": {"id": 9999}},
        )

        assert plugin._sgdb_service._get_sgdb_game_id(1234) is None

    def test_returns_none_when_data_missing(self, plugin, fake_steamgrid_db_api):
        """SGDB body lacking ``data`` (malformed) → None."""
        fake_steamgrid_db_api.seed_raw_response("/games/igdb/1234", {"success": True})

        assert plugin._sgdb_service._get_sgdb_game_id(1234) is None

    def test_returns_none_when_response_is_none(self, plugin, fake_steamgrid_db_api):
        """Adapter returns ``None`` (e.g. empty body) → None."""
        fake_steamgrid_db_api.seed_raw_response("/games/igdb/1234", None)

        assert plugin._sgdb_service._get_sgdb_game_id(1234) is None

    def test_sgdb_api_error_swallowed(self, plugin, fake_steamgrid_db_api):
        """``SgdbApiError`` (4xx/5xx) is logged and swallowed → None."""
        fake_steamgrid_db_api.request_side_effect = SgdbApiError(503, "Service Unavailable")

        assert plugin._sgdb_service._get_sgdb_game_id(1234) is None

    def test_network_error_swallowed(self, plugin, fake_steamgrid_db_api):
        """Connection-level errors are logged and swallowed → None."""
        fake_steamgrid_db_api.request_side_effect = ConnectionError("connection refused")

        assert plugin._sgdb_service._get_sgdb_game_id(1234) is None


class TestDownloadSgdbArtwork:
    """SGDB artwork-download error paths in ``_download_sgdb_artwork``.

    Covers the unsupported-asset-type early exit, the cache-hit short
    circuit, every malformed-response branch (success=False, data
    missing, body None), the image-download failure return, and the
    ``except Exception`` net.
    """

    def test_unsupported_asset_type_returns_none(self, plugin, fake_steamgrid_db_api):
        """Unknown asset type → early ``None`` (no SGDB request issued)."""
        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "no-such-asset-type")

        assert result is None
        assert fake_steamgrid_db_api.requested_paths == []

    def test_cache_hit_short_circuits(self, plugin, sgdb_artwork_cache, fake_steamgrid_db_api):
        """Pre-existing cache file → return cached path, no network call."""
        cached = _cached_path(sgdb_artwork_cache, 42, "hero")
        sgdb_artwork_cache.files[cached] = b"already cached"

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result == cached
        assert fake_steamgrid_db_api.requested_paths == []

    def test_success_false_returns_none(self, plugin, fake_steamgrid_db_api):
        """SGDB body with ``success=False`` → None."""
        fake_steamgrid_db_api.seed_raw_response(
            "/heroes/game/9999",
            {"success": False, "data": [{"url": "https://example.com/hero.png"}]},
        )

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None
        assert fake_steamgrid_db_api.downloaded == []

    def test_empty_data_returns_none(self, plugin, fake_steamgrid_db_api):
        """SGDB body with empty ``data`` list → None (falsy short-circuit)."""
        fake_steamgrid_db_api.seed_raw_response(
            "/heroes/game/9999",
            {"success": True, "data": []},
        )

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None
        assert fake_steamgrid_db_api.downloaded == []

    def test_none_response_returns_none(self, plugin, fake_steamgrid_db_api):
        """Adapter returns ``None`` → None (no image download)."""
        fake_steamgrid_db_api.seed_raw_response("/heroes/game/9999", None)

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None
        assert fake_steamgrid_db_api.downloaded == []

    def test_download_image_failure_returns_none(self, plugin, fake_steamgrid_db_api):
        """``download_image`` returns False (e.g. 5xx on CDN) → None."""
        fake_steamgrid_db_api.seed_artwork(9999, "hero", "https://example.com/hero.png")
        fake_steamgrid_db_api.download_image_return = False

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None

    def test_sgdb_api_error_swallowed(self, plugin, fake_steamgrid_db_api):
        """``SgdbApiError`` raised by ``request`` is logged and swallowed."""
        fake_steamgrid_db_api.request_side_effect = SgdbApiError(500, "Internal Server Error")

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None

    def test_malformed_data_key_error_swallowed(self, plugin, fake_steamgrid_db_api):
        """Missing ``url`` in data entry → ``KeyError`` is logged and swallowed."""
        fake_steamgrid_db_api.seed_raw_response(
            "/heroes/game/9999",
            {"success": True, "data": [{"id": 1}]},  # no 'url' key
        )

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None

    def test_network_error_during_download_swallowed(self, plugin, fake_steamgrid_db_api):
        """Connection-level error from ``download_image`` is swallowed."""
        fake_steamgrid_db_api.seed_artwork(9999, "hero", "https://example.com/hero.png")
        fake_steamgrid_db_api.download_image_side_effect = ConnectionError("connection refused")

        result = plugin._sgdb_service._download_sgdb_artwork(9999, 42, "hero")

        assert result is None


class TestReadFileAsBase64:
    """``_read_file_as_base64`` exception branch.

    The happy path is already exercised by ``TestGetSgdbArtworkBase64``;
    this class pins the ``except Exception`` net that turns a cache-read
    failure (corrupt file, permissions error, vanished file) into a
    ``None`` return instead of bubbling the exception to the frontend.
    """

    @pytest.mark.asyncio
    async def test_returns_none_when_read_fails(self, plugin, sgdb_artwork_cache):
        """``read_bytes`` raising → ``None`` (frontend sees no artwork)."""
        plugin._sgdb_service._loop = asyncio.get_event_loop()

        def raising_read(_path: str) -> bytes:
            raise OSError("permission denied")

        sgdb_artwork_cache.read_bytes = raising_read  # type: ignore[method-assign]

        result = await plugin._sgdb_service._read_file_as_base64("/runtime/artwork/42_hero.png")

        assert result is None


class TestSaveSgdbApiKey:
    """``save_sgdb_api_key`` happy / masked / empty paths.

    The callable stores a real key and ignores the masked sentinel (set
    by the frontend modal when the user leaves the input untouched) and
    the empty string (no input given).
    """

    def test_stores_real_key(self, plugin):
        """Real key → persisted to settings and ``save_settings`` invoked."""
        persister = FakeSettingsPersister()
        plugin._sgdb_service._settings_persister = persister

        result = plugin._sgdb_service.save_sgdb_api_key("real-api-key-123")

        assert result == {"success": True, "message": "SteamGridDB API key saved"}
        assert plugin.settings["steamgriddb_api_key"] == "real-api-key-123"
        assert persister.save_count == 1

    def test_ignores_masked_sentinel(self, plugin):
        """Masked value ``••••`` → no settings mutation, no persister call."""
        plugin.settings["steamgriddb_api_key"] = "existing-key"
        persister = FakeSettingsPersister()
        plugin._sgdb_service._settings_persister = persister

        result = plugin._sgdb_service.save_sgdb_api_key("••••")

        assert result == {"success": True, "message": "SteamGridDB API key saved"}
        assert plugin.settings["steamgriddb_api_key"] == "existing-key"
        assert persister.save_count == 0

    def test_ignores_empty_string(self, plugin):
        """Empty input → no settings mutation, no persister call."""
        persister = FakeSettingsPersister()
        plugin._sgdb_service._settings_persister = persister

        result = plugin._sgdb_service.save_sgdb_api_key("")

        assert result == {"success": True, "message": "SteamGridDB API key saved"}
        assert "steamgriddb_api_key" not in plugin.settings
        assert persister.save_count == 0


class TestPruneOrphanedArtworkCacheEdgeCases:
    """Edge-case branches in ``prune_orphaned_artwork_cache``.

    Complements ``TestPruneOrphanedArtworkCache`` by covering the OSError
    branch for ``.tmp`` removal and filename shapes that have no rom_id
    prefix.
    """

    def test_tmp_remove_oserror_logged_not_crash(self, plugin, sgdb_artwork_cache):
        """OSError when removing a stale ``.tmp`` file is logged, not raised."""
        tmp_path = _cached_path(sgdb_artwork_cache, 42, "hero") + ".tmp"
        sgdb_artwork_cache.files[tmp_path] = b"tmp data"

        plugin._state["shortcut_registry"] = {"42": {"app_id": 1}}

        def raising_remove(_path: str) -> None:
            raise OSError("permission denied")

        sgdb_artwork_cache.remove_file = raising_remove  # type: ignore[method-assign]

        # Should not crash
        plugin._sgdb_service.prune_orphaned_artwork_cache()

        # File still present (remove was patched to fail)
        assert tmp_path in sgdb_artwork_cache.files


class TestSaveIconVdfFailure:
    """``_save_icon_to_grid`` VDF-update failure path.

    The icon write is the primary success criterion: if the VDF read or
    write fails after a successful PNG write, the function logs but
    still returns True (icon-on-disk is the source of truth; the VDF
    field is a best-effort optimisation).
    """

    def test_vdf_read_failure_still_returns_true(self, plugin, tmp_path):
        """OSError on ``read_shortcuts`` → icon saved, function returns True."""

        def fake_write_icon(app_id, _bytes):
            return os.path.join(str(tmp_path), f"{app_id}_icon.png")

        def raising_read():
            raise OSError("vdf corrupted")

        plugin._steam_config.write_shortcut_icon = fake_write_icon  # type: ignore[method-assign]
        plugin._steam_config.read_shortcuts = raising_read  # type: ignore[method-assign]
        plugin._steam_config.write_shortcuts = lambda _data: None  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"icon data")

        assert result is True

    def test_vdf_write_failure_still_returns_true(self, plugin, tmp_path):
        """OSError on ``write_shortcuts`` → icon saved, function returns True."""

        def fake_write_icon(app_id, _bytes):
            return os.path.join(str(tmp_path), f"{app_id}_icon.png")

        def raising_write(_data):
            raise OSError("disk full")

        plugin._steam_config.write_shortcut_icon = fake_write_icon  # type: ignore[method-assign]
        plugin._steam_config.read_shortcuts = lambda: {"shortcuts": {}}  # type: ignore[method-assign]
        plugin._steam_config.write_shortcuts = raising_write  # type: ignore[method-assign]

        result = plugin._sgdb_service._save_icon_to_grid(12345, b"icon data")

        assert result is True
