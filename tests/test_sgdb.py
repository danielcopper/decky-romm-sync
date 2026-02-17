import pytest
import asyncio

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    p._metadata_cache = {}
    return p


class TestVerifySgdbApiKey:
    @pytest.mark.asyncio
    async def test_valid_api_key(self, plugin):
        from unittest.mock import MagicMock, patch
        import json as _json

        plugin.loop = asyncio.get_event_loop()

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"success": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = await plugin.verify_sgdb_api_key("valid-key-123")

        assert result["success"] is True
        assert "valid" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_invalid_api_key_401(self, plugin):
        from unittest.mock import patch
        import urllib.error

        plugin.loop = asyncio.get_event_loop()

        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "https://steamgriddb.com", 401, "Unauthorized", {}, None
        )):
            result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_invalid_api_key_403(self, plugin):
        from unittest.mock import patch
        import urllib.error

        plugin.loop = asyncio.get_event_loop()

        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "https://steamgriddb.com", 403, "Forbidden", {}, None
        )):
            result = await plugin.verify_sgdb_api_key("bad-key")

        assert result["success"] is False
        assert "Invalid API key" in result["message"]

    @pytest.mark.asyncio
    async def test_empty_string_falls_back_to_saved_key(self, plugin):
        from unittest.mock import MagicMock, patch
        import json as _json

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["steamgriddb_api_key"] = "saved-key-456"

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"success": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = await plugin.verify_sgdb_api_key("")

        assert result["success"] is True
        # Verify it used the saved key (in the Authorization header)
        req_obj = mock_open.call_args[0][0]
        assert "saved-key-456" in req_obj.get_header("Authorization")

    @pytest.mark.asyncio
    async def test_masked_value_falls_back_to_saved_key(self, plugin):
        from unittest.mock import MagicMock, patch
        import json as _json

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["steamgriddb_api_key"] = "saved-key-789"

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"success": True}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
            result = await plugin.verify_sgdb_api_key("••••")

        assert result["success"] is True
        req_obj = mock_open.call_args[0][0]
        assert "saved-key-789" in req_obj.get_header("Authorization")

    @pytest.mark.asyncio
    async def test_no_key_configured(self, plugin):
        plugin.loop = asyncio.get_event_loop()
        # No saved key, no provided key
        result = await plugin.verify_sgdb_api_key("")
        assert result["success"] is False
        assert "No API key configured" in result["message"]

    @pytest.mark.asyncio
    async def test_no_key_at_all_default_param(self, plugin):
        plugin.loop = asyncio.get_event_loop()
        result = await plugin.verify_sgdb_api_key()
        assert result["success"] is False
        assert "No API key configured" in result["message"]

    @pytest.mark.asyncio
    async def test_network_error(self, plugin):
        from unittest.mock import patch

        plugin.loop = asyncio.get_event_loop()

        with patch("urllib.request.urlopen", side_effect=ConnectionError("DNS resolution failed")):
            result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "Connection failed" in result["message"]

    @pytest.mark.asyncio
    async def test_sgdb_rejects_key(self, plugin):
        from unittest.mock import MagicMock, patch
        import json as _json

        plugin.loop = asyncio.get_event_loop()

        fake_resp = MagicMock()
        fake_resp.read.return_value = _json.dumps({"success": False}).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = await plugin.verify_sgdb_api_key("rejected-key")

        assert result["success"] is False
        assert "rejected" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_http_500_error(self, plugin):
        from unittest.mock import patch
        import urllib.error

        plugin.loop = asyncio.get_event_loop()

        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "https://steamgriddb.com", 500, "Internal Server Error", {}, None
        )):
            result = await plugin.verify_sgdb_api_key("some-key")

        assert result["success"] is False
        assert "HTTP 500" in result["message"]


class TestGetSgdbArtworkBase64:
    @pytest.mark.asyncio
    async def test_cached_artwork_returns_base64(self, plugin, tmp_path):
        import base64
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

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
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        # No API key in settings
        plugin.loop = asyncio.get_event_loop()

        result = await plugin.get_sgdb_artwork_base64(42, 1)
        assert result["base64"] is None
        assert result["no_api_key"] is True

    @pytest.mark.asyncio
    async def test_invalid_asset_type(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        result = await plugin.get_sgdb_artwork_base64(42, 99)
        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_no_igdb_id_fetched_from_romm(self, plugin, tmp_path):
        from unittest.mock import patch, MagicMock
        import base64
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        # ROM in registry but without igdb_id
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001, "name": "Zelda", "platform_name": "N64",
        }

        # RomM API returns igdb_id
        romm_response = {"igdb_id": 1234}
        # SGDB game lookup returns an ID
        sgdb_game_response = {"success": True, "data": {"id": 9999}}

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_hero.png"

        def fake_sgdb_request(path):
            if "/games/igdb/" in path:
                return sgdb_game_response
            if "/heroes/" in path:
                return {"success": True, "data": [{"url": "https://example.com/hero.png"}]}
            return None

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            # Simulate writing the file
            art_file.write_bytes(b"hero artwork")
            return str(art_file)

        with patch.object(plugin, "_romm_request", return_value=romm_response), \
             patch.object(plugin, "_get_sgdb_game_id", return_value=9999), \
             patch.object(plugin, "_download_sgdb_artwork", side_effect=fake_download_sgdb):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is not None
        assert result["no_api_key"] is False
        assert base64.b64decode(result["base64"]) == b"hero artwork"
        # igdb_id should be saved back to registry
        assert plugin._state["shortcut_registry"]["42"]["igdb_id"] == 1234

    @pytest.mark.asyncio
    async def test_no_igdb_id_anywhere(self, plugin, tmp_path):
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        # ROM in registry without igdb_id
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001, "name": "Zelda", "platform_name": "N64",
        }

        # RomM API also returns no igdb_id
        with patch.object(plugin, "_romm_request", return_value={"igdb_id": None}):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_game_lookup_no_match(self, plugin, tmp_path):
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        # ROM with igdb_id in registry
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001, "name": "Zelda", "platform_name": "N64",
            "igdb_id": 1234,
        }

        # SGDB lookup returns None (no matching game)
        with patch.object(plugin, "_get_sgdb_game_id", return_value=None):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_download_fails_returns_null(self, plugin, tmp_path):
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001, "name": "Zelda", "platform_name": "N64",
            "igdb_id": 1234, "sgdb_id": 9999,
        }

        # Download returns None (failed)
        with patch.object(plugin, "_download_sgdb_artwork", return_value=None):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_igdb_id_from_pending_sync(self, plugin, tmp_path):
        from unittest.mock import patch
        import base64
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        # Not in registry, but in pending sync
        plugin._pending_sync[42] = {
            "name": "Zelda", "platform_name": "N64", "igdb_id": 5678,
        }

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_logo.png"

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            art_file.write_bytes(b"logo data")
            return str(art_file)

        with patch.object(plugin, "_get_sgdb_game_id", return_value=9999), \
             patch.object(plugin, "_download_sgdb_artwork", side_effect=fake_download_sgdb):
            result = await plugin.get_sgdb_artwork_base64(42, 2)  # 2 = logo

        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"logo data"

    @pytest.mark.asyncio
    async def test_romm_api_fetch_fails_gracefully(self, plugin, tmp_path):
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        # Not in registry or pending, RomM API fails
        with patch.object(plugin, "_romm_request", side_effect=Exception("Connection refused")):
            result = await plugin.get_sgdb_artwork_base64(42, 1)

        assert result["base64"] is None
        assert result["no_api_key"] is False

    @pytest.mark.asyncio
    async def test_sgdb_id_cached_in_registry(self, plugin, tmp_path):
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.settings["steamgriddb_api_key"] = "some-key"
        plugin.loop = asyncio.get_event_loop()

        # ROM with both igdb_id and sgdb_id already cached
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 100001, "name": "Zelda", "platform_name": "N64",
            "igdb_id": 1234, "sgdb_id": 9999,
        }

        art_dir = tmp_path / "artwork"
        art_dir.mkdir()
        art_file = art_dir / "42_grid.png"

        def fake_download_sgdb(sgdb_game_id, rom_id, asset_type):
            assert sgdb_game_id == 9999  # Should use cached sgdb_id
            art_file.write_bytes(b"grid data")
            return str(art_file)

        # _get_sgdb_game_id should NOT be called since sgdb_id is cached
        with patch.object(plugin, "_get_sgdb_game_id") as mock_lookup, \
             patch.object(plugin, "_download_sgdb_artwork", side_effect=fake_download_sgdb):
            result = await plugin.get_sgdb_artwork_base64(42, 3)  # 3 = grid

        mock_lookup.assert_not_called()
        assert result["base64"] is not None
