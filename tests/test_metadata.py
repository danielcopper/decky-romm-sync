import pytest
import json
import os
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


class TestExtractMetadata:
    """Tests for the _extract_metadata helper."""

    def test_full_metadatum(self, plugin):
        rom = {
            "summary": "An adventure game",
            "metadatum": {
                "genres": ["RPG", "Adventure"],
                "companies": ["Nintendo", "HAL Laboratory"],
                "first_release_date": 1082592000000,
                "average_rating": 79.665,
                "game_modes": ["Single player", "Multiplayer"],
                "player_count": "1-4",
            },
        }
        result = plugin._extract_metadata(rom)
        assert result["summary"] == "An adventure game"
        assert result["genres"] == ["RPG", "Adventure"]
        assert result["companies"] == ["Nintendo", "HAL Laboratory"]
        assert result["first_release_date"] == 1082592000
        assert result["average_rating"] == 79.665
        assert result["game_modes"] == ["Single player", "Multiplayer"]
        assert result["player_count"] == "1-4"
        assert result["cached_at"] > 0

    def test_first_release_date_ms_to_seconds(self, plugin):
        """Verify milliseconds are divided by 1000 for unix seconds."""
        rom = {"metadatum": {"first_release_date": 946684800000}}
        result = plugin._extract_metadata(rom)
        assert result["first_release_date"] == 946684800

    def test_missing_metadatum(self, plugin):
        """ROM with no metadatum field returns empty defaults."""
        rom = {"summary": "A game", "id": 1}
        result = plugin._extract_metadata(rom)
        assert result["summary"] == "A game"
        assert result["genres"] == []
        assert result["companies"] == []
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == []
        assert result["player_count"] == ""

    def test_none_metadatum(self, plugin):
        """ROM with metadatum=None returns empty defaults."""
        rom = {"summary": "A game", "metadatum": None}
        result = plugin._extract_metadata(rom)
        assert result["genres"] == []
        assert result["first_release_date"] is None

    def test_empty_summary(self, plugin):
        """ROM with empty/None summary returns empty string."""
        rom1 = {"summary": None, "metadatum": {}}
        rom2 = {"summary": "", "metadatum": {}}
        rom3 = {"metadatum": {}}
        assert plugin._extract_metadata(rom1)["summary"] == ""
        assert plugin._extract_metadata(rom2)["summary"] == ""
        assert plugin._extract_metadata(rom3)["summary"] == ""

    def test_none_fields_in_metadatum(self, plugin):
        """Metadatum fields that are None return empty list/string."""
        rom = {
            "metadatum": {
                "genres": None,
                "companies": None,
                "game_modes": None,
                "player_count": None,
            },
        }
        result = plugin._extract_metadata(rom)
        assert result["genres"] == []
        assert result["companies"] == []
        assert result["game_modes"] == []
        assert result["player_count"] == ""


class TestGetRomMetadata:
    """Tests for the get_rom_metadata callable."""

    @pytest.mark.asyncio
    async def test_cache_hit(self, plugin):
        """Returns cached data without API call when cache is fresh."""
        import time
        plugin._metadata_cache["42"] = {
            "summary": "Cached summary",
            "genres": ["RPG"],
            "companies": ["Nintendo"],
            "first_release_date": 946684800,
            "average_rating": 85.0,
            "game_modes": ["Single player"],
            "player_count": "1",
            "cached_at": time.time(),
        }
        plugin.settings["debug_logging"] = False
        result = await plugin.get_rom_metadata(42)
        assert result["summary"] == "Cached summary"
        assert result["genres"] == ["RPG"]

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_api(self, plugin, tmp_path):
        """Cache miss fetches from RomM API, caches to disk."""
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        romm_response = {
            "id": 42,
            "summary": "API summary",
            "metadatum": {
                "genres": ["Adventure"],
                "companies": ["Capcom"],
                "first_release_date": 1000000000000,
                "average_rating": 90.0,
                "game_modes": ["Single player"],
                "player_count": "1",
            },
        }

        with patch.object(plugin, "_romm_request", return_value=romm_response):
            result = await plugin.get_rom_metadata(42)

        assert result["summary"] == "API summary"
        assert result["genres"] == ["Adventure"]
        assert result["companies"] == ["Capcom"]
        assert result["first_release_date"] == 1000000000
        assert result["average_rating"] == 90.0
        # Verify cached in memory
        assert "42" in plugin._metadata_cache
        # Verify written to disk
        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        assert os.path.exists(cache_path)
        with open(cache_path, "r") as f:
            disk_cache = json.load(f)
        assert "42" in disk_cache

    @pytest.mark.asyncio
    async def test_cache_expired_refetches(self, plugin, tmp_path):
        """Re-fetches when cached_at is older than 7 days."""
        from unittest.mock import patch
        import time
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        # Set cache as 8 days old
        plugin._metadata_cache["42"] = {
            "summary": "Old summary",
            "genres": [],
            "companies": [],
            "first_release_date": None,
            "average_rating": None,
            "game_modes": [],
            "player_count": "",
            "cached_at": time.time() - (8 * 24 * 3600),
        }

        romm_response = {
            "id": 42,
            "summary": "Fresh summary",
            "metadatum": {"genres": ["RPG"]},
        }

        with patch.object(plugin, "_romm_request", return_value=romm_response):
            result = await plugin.get_rom_metadata(42)

        assert result["summary"] == "Fresh summary"
        assert result["genres"] == ["RPG"]

    @pytest.mark.asyncio
    async def test_network_error_returns_stale_cache(self, plugin):
        """On network error, returns stale cached data if available."""
        from unittest.mock import patch

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        # Stale cache (8 days old)
        plugin._metadata_cache["42"] = {
            "summary": "Stale summary",
            "genres": ["RPG"],
            "companies": [],
            "first_release_date": None,
            "average_rating": None,
            "game_modes": [],
            "player_count": "",
            "cached_at": 0,
        }

        with patch.object(plugin, "_romm_request", side_effect=Exception("Connection refused")):
            result = await plugin.get_rom_metadata(42)

        assert result["summary"] == "Stale summary"
        assert result["genres"] == ["RPG"]

    @pytest.mark.asyncio
    async def test_network_error_no_cache_returns_defaults(self, plugin):
        """On network error with no cache, returns empty defaults."""
        from unittest.mock import patch

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        with patch.object(plugin, "_romm_request", side_effect=Exception("Connection refused")):
            result = await plugin.get_rom_metadata(42)

        assert result["summary"] == ""
        assert result["genres"] == []
        assert result["companies"] == []
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == []
        assert result["player_count"] == ""

    @pytest.mark.asyncio
    async def test_rom_missing_metadatum_from_api(self, plugin, tmp_path):
        """ROM exists in API but has no metadatum — returns empty fields."""
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        romm_response = {"id": 42, "summary": "Just a summary"}

        with patch.object(plugin, "_romm_request", return_value=romm_response):
            result = await plugin.get_rom_metadata(42)

        assert result["summary"] == "Just a summary"
        assert result["genres"] == []
        assert result["first_release_date"] is None

    @pytest.mark.asyncio
    async def test_debug_logging_on_cache_hit(self, plugin):
        """Verify _log_debug is called during cache hit."""
        from unittest.mock import patch
        import time
        import decky

        plugin.settings["debug_logging"] = True
        plugin._metadata_cache["42"] = {
            "summary": "cached",
            "genres": [],
            "companies": [],
            "first_release_date": None,
            "average_rating": None,
            "game_modes": [],
            "player_count": "",
            "cached_at": time.time(),
        }

        with patch.object(decky.logger, "info") as mock_info:
            await plugin.get_rom_metadata(42)
            logged = [str(c) for c in mock_info.call_args_list]
            assert any("cache hit" in m.lower() for m in logged)

    @pytest.mark.asyncio
    async def test_debug_logging_on_cache_miss(self, plugin, tmp_path):
        """Verify _log_debug is called during cache miss."""
        from unittest.mock import patch
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = True

        romm_response = {"id": 42, "summary": "test", "metadatum": {}}

        with patch.object(plugin, "_romm_request", return_value=romm_response), \
             patch.object(decky.logger, "info") as mock_info:
            await plugin.get_rom_metadata(42)
            logged = [str(c) for c in mock_info.call_args_list]
            assert any("cache miss" in m.lower() for m in logged)


class TestGetAllMetadataCache:
    """Tests for the get_all_metadata_cache callable."""

    @pytest.mark.asyncio
    async def test_returns_full_cache(self, plugin):
        plugin._metadata_cache = {
            "1": {"summary": "Game 1", "cached_at": 100},
            "2": {"summary": "Game 2", "cached_at": 200},
        }
        result = await plugin.get_all_metadata_cache()
        assert len(result) == 2
        assert result["1"]["summary"] == "Game 1"
        assert result["2"]["summary"] == "Game 2"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cache(self, plugin):
        plugin._metadata_cache = {}
        result = await plugin.get_all_metadata_cache()
        assert result == {}


class TestLoadMetadataCache:
    """Tests for _load_metadata_cache."""

    def test_loads_from_disk(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        cache_data = {"42": {"summary": "test", "cached_at": 100}}
        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        with open(cache_path, "w") as f:
            json.dump(cache_data, f)

        plugin._load_metadata_cache()
        assert plugin._metadata_cache == cache_data

    def test_empty_when_file_missing(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        plugin._metadata_cache = {"old": "data"}
        plugin._load_metadata_cache()
        assert plugin._metadata_cache == {}

    def test_empty_when_malformed_json(self, plugin, tmp_path):
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        with open(cache_path, "w") as f:
            f.write("not valid json{{{")

        plugin._load_metadata_cache()
        assert plugin._metadata_cache == {}


class TestSyncMetadataCapture:
    """Tests for metadata capture during _do_sync."""

    def test_extract_metadata_during_sync(self, plugin, tmp_path):
        """Verify that _extract_metadata produces correct cache entries for ROM list items."""
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        roms = [
            {
                "id": 1,
                "summary": "Game one description",
                "metadatum": {
                    "genres": ["RPG"],
                    "companies": ["Square"],
                    "first_release_date": 946684800000,
                    "average_rating": 95.0,
                    "game_modes": ["Single player"],
                    "player_count": "1",
                },
            },
            {
                "id": 2,
                "summary": None,
                "metadatum": None,
            },
            {
                "id": 3,
                "summary": "Game three",
            },
        ]

        for rom in roms:
            rom_id_str = str(rom["id"])
            plugin._metadata_cache[rom_id_str] = plugin._extract_metadata(rom)
        plugin._save_metadata_cache()

        # Verify in-memory cache
        assert plugin._metadata_cache["1"]["summary"] == "Game one description"
        assert plugin._metadata_cache["1"]["genres"] == ["RPG"]
        assert plugin._metadata_cache["1"]["first_release_date"] == 946684800

        # ROM with None metadatum gets defaults
        assert plugin._metadata_cache["2"]["summary"] == ""
        assert plugin._metadata_cache["2"]["genres"] == []
        assert plugin._metadata_cache["2"]["first_release_date"] is None

        # ROM without metadatum key gets defaults
        assert plugin._metadata_cache["3"]["summary"] == "Game three"
        assert plugin._metadata_cache["3"]["genres"] == []

        # Verify disk cache
        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        assert os.path.exists(cache_path)
        with open(cache_path, "r") as f:
            disk_cache = json.load(f)
        assert "1" in disk_cache
        assert "2" in disk_cache
        assert "3" in disk_cache
        assert disk_cache["1"]["genres"] == ["RPG"]

    def test_sync_preserves_existing_cache(self, plugin, tmp_path):
        """Pre-existing cache entries for other ROMs are preserved after sync adds new ones."""
        import decky
        decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)

        # Pre-existing cache entry
        plugin._metadata_cache["99"] = {
            "summary": "Existing game",
            "genres": ["Puzzle"],
            "companies": [],
            "first_release_date": None,
            "average_rating": None,
            "game_modes": [],
            "player_count": "",
            "cached_at": 100,
        }

        # Simulate sync adding new ROMs
        new_roms = [
            {"id": 1, "summary": "New game", "metadatum": {"genres": ["RPG"]}},
        ]
        for rom in new_roms:
            plugin._metadata_cache[str(rom["id"])] = plugin._extract_metadata(rom)
        plugin._save_metadata_cache()

        # Both old and new entries must be present
        assert "99" in plugin._metadata_cache
        assert plugin._metadata_cache["99"]["summary"] == "Existing game"
        assert plugin._metadata_cache["99"]["genres"] == ["Puzzle"]
        assert "1" in plugin._metadata_cache
        assert plugin._metadata_cache["1"]["summary"] == "New game"

        # Verify on disk too
        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        with open(cache_path, "r") as f:
            disk_cache = json.load(f)
        assert "99" in disk_cache
        assert "1" in disk_cache

    def test_sync_rom_without_metadatum(self, plugin):
        """ROM without metadatum field gets an empty-default cache entry during sync."""
        rom = {"id": 5, "summary": "No metadata here"}
        result = plugin._extract_metadata(rom)
        assert result["summary"] == "No metadata here"
        assert result["genres"] == []
        assert result["companies"] == []
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == []
        assert result["player_count"] == ""
        assert result["cached_at"] > 0


class TestGetRomMetadata404:
    """Test get_rom_metadata when API returns HTTP 404."""

    @pytest.mark.asyncio
    async def test_rom_not_found_returns_defaults(self, plugin):
        """API 404 with no cache returns empty defaults."""
        from unittest.mock import patch
        import urllib.error

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        http_404 = urllib.error.HTTPError(
            url="http://example.com/api/roms/999",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

        with patch.object(plugin, "_romm_request", side_effect=http_404):
            result = await plugin.get_rom_metadata(999)

        assert result["summary"] == ""
        assert result["genres"] == []
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["cached_at"] == 0

    @pytest.mark.asyncio
    async def test_rom_not_found_returns_stale_cache(self, plugin):
        """API 404 with stale cache returns cached data."""
        from unittest.mock import patch
        import urllib.error

        plugin.loop = asyncio.get_event_loop()
        plugin.settings["debug_logging"] = False

        plugin._metadata_cache["999"] = {
            "summary": "Old cached data",
            "genres": ["Action"],
            "companies": [],
            "first_release_date": 100000,
            "average_rating": 70.0,
            "game_modes": [],
            "player_count": "1",
            "cached_at": 0,
        }

        http_404 = urllib.error.HTTPError(
            url="http://example.com/api/roms/999",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )

        with patch.object(plugin, "_romm_request", side_effect=http_404):
            result = await plugin.get_rom_metadata(999)

        assert result["summary"] == "Old cached data"
        assert result["genres"] == ["Action"]
