import asyncio
import http.client
import json
import os
from unittest.mock import MagicMock

import pytest
from fakes.fake_metadata_cache_persister import FakeMetadataCachePersister
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_state_persister import FakeStatePersister
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen
from models.state import make_default_plugin_state

from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.metadata_cache_store import MetadataCacheStoreAdapter
from adapters.persistence import MetadataCachePersisterAdapter, PersistenceAdapter
from adapters.registry_store import RegistryStoreAdapter
from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._state = make_default_plugin_state()
    p._metadata_cache = {}

    import decky

    p._debug_logger = SettingsAwareDebugLogger(settings=p.settings, logger=decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._state_persister = FakeStatePersister()
    p._settings_persister = FakeSettingsPersister()
    p._metadata_cache_persister = FakeMetadataCachePersister()
    p._registry_store = RegistryStoreAdapter(state=p._state, logger=decky.logger)
    p._metadata_store = MetadataCacheStoreAdapter(metadata_cache=p._metadata_cache)

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            state=p._state,
            metadata_cache=p._metadata_cache,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            clock=FakeClock(),
            metadata_cache_persister=p._metadata_cache_persister,
            metadata_store=p._metadata_store,
            log_debug=p._log_debug,
        ),
    )
    p._metadata_service = metadata_service

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
            registry_store=p._registry_store,
            log_debug=p._log_debug,
            metadata_service=metadata_service,
            artwork=FakeArtworkManager(),
        ),
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop and service loops match the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()
    plugin._metadata_service._loop = asyncio.get_event_loop()


class TestExtractMetadata:
    """Tests for the extract_metadata helper."""

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
        result = plugin._metadata_service.extract_metadata(rom)
        assert result["summary"] == "An adventure game"
        assert result["genres"] == ("RPG", "Adventure")
        assert result["companies"] == ("Nintendo", "HAL Laboratory")
        assert result["first_release_date"] == 1082592000
        assert result["average_rating"] == 79.665
        assert result["game_modes"] == ("Single player", "Multiplayer")
        assert result["player_count"] == "1-4"
        assert result["cached_at"] > 0

    def test_first_release_date_ms_to_seconds(self, plugin):
        """Verify milliseconds are divided by 1000 for unix seconds."""
        rom = {"metadatum": {"first_release_date": 946684800000}}
        result = plugin._metadata_service.extract_metadata(rom)
        assert result["first_release_date"] == 946684800

    def test_missing_metadatum(self, plugin):
        """ROM with no metadatum field returns empty defaults."""
        rom = {"summary": "A game", "id": 1}
        result = plugin._metadata_service.extract_metadata(rom)
        assert result["summary"] == "A game"
        assert result["genres"] == ()
        assert result["companies"] == ()
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == ()
        assert result["player_count"] == ""

    def test_none_metadatum(self, plugin):
        """ROM with metadatum=None returns empty defaults."""
        rom = {"summary": "A game", "metadatum": None}
        result = plugin._metadata_service.extract_metadata(rom)
        assert result["genres"] == ()
        assert result["first_release_date"] is None

    def test_empty_summary(self, plugin):
        """ROM with empty/None summary returns empty string."""
        rom1 = {"summary": None, "metadatum": {}}
        rom2 = {"summary": "", "metadatum": {}}
        rom3 = {"metadatum": {}}
        assert plugin._metadata_service.extract_metadata(rom1)["summary"] == ""
        assert plugin._metadata_service.extract_metadata(rom2)["summary"] == ""
        assert plugin._metadata_service.extract_metadata(rom3)["summary"] == ""

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
        result = plugin._metadata_service.extract_metadata(rom)
        assert result["genres"] == ()
        assert result["companies"] == ()
        assert result["game_modes"] == ()
        assert result["player_count"] == ""

    def test_extract_metadata_includes_steam_categories(self, plugin):
        """extract_metadata should compute steam_categories from genres and game_modes."""
        rom = {
            "summary": "Test",
            "metadatum": {
                "genres": ["Action", "Puzzle"],
                "game_modes": ["Single player"],
            },
        }
        result = plugin._metadata_service.extract_metadata(rom)
        assert "steam_categories" in result
        assert 28 in result["steam_categories"]  # full controller support
        assert 21 in result["steam_categories"]  # Action
        assert 4 in result["steam_categories"]  # Puzzle
        assert 2 in result["steam_categories"]  # Single player


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
        plugin.settings["log_level"] = "warn"
        result = await plugin.get_rom_metadata(42)
        assert result["summary"] == "Cached summary"
        assert result["genres"] == ["RPG"]

    @pytest.mark.asyncio
    async def test_cache_miss_returns_empty_defaults(self, plugin):
        """Cache miss returns empty defaults without calling the API."""
        plugin.settings["log_level"] = "warn"

        result = await plugin.get_rom_metadata(42)

        assert result["summary"] == ""
        assert result["genres"] == ()
        assert result["companies"] == ()
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == ()
        assert result["player_count"] == ""
        assert result["cached_at"] == 0

    @pytest.mark.asyncio
    async def test_stale_cache_returns_stale_data(self, plugin):
        """Stale cache (>7 days) is still returned — refreshed on next sync."""
        import time

        plugin.settings["log_level"] = "warn"

        plugin._metadata_cache["42"] = {
            "summary": "Old summary",
            "genres": ["Action"],
            "companies": [],
            "first_release_date": None,
            "average_rating": None,
            "game_modes": [],
            "player_count": "",
            "cached_at": time.time() - (8 * 24 * 3600),
        }

        result = await plugin.get_rom_metadata(42)

        assert result["summary"] == "Old summary"
        assert result["genres"] == ["Action"]

    @pytest.mark.asyncio
    async def test_no_api_call_on_cache_miss(self, plugin):
        """Verify get_rom is never called — metadata comes only from cache."""
        from unittest.mock import patch

        plugin.settings["log_level"] = "warn"

        with patch.object(plugin._romm_api, "get_rom") as mock_get_rom:
            await plugin.get_rom_metadata(42)

        mock_get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_debug_logging_on_cache_hit(self, plugin):
        """Verify _log_debug is called during cache hit."""
        import time
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "debug"
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

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin.settings["log_level"] = "debug"

        romm_response = {"id": 42, "summary": "test", "metadatum": {}}

        with (
            patch.object(plugin._romm_api, "get_rom", return_value=romm_response),
            patch.object(decky.logger, "info") as mock_info,
        ):
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
        # Update service's reference too (same dict in production, but fixture re-assigned)
        plugin._metadata_service._metadata_cache = plugin._metadata_cache
        result = await plugin.get_all_metadata_cache()
        assert len(result) == 2
        assert result["1"]["summary"] == "Game 1"
        assert result["2"]["summary"] == "Game 2"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cache(self, plugin):
        plugin._metadata_cache = {}
        plugin._metadata_service._metadata_cache = plugin._metadata_cache
        result = await plugin.get_all_metadata_cache()
        assert result == {}


class TestSyncMetadataCapture:
    """Tests for metadata capture during a sync run."""

    def test_extract_metadata_during_sync(self, plugin, tmp_path):
        """Verify that extract_metadata produces correct cache entries for ROM list items."""
        import decky

        from adapters.persistence import PersistenceAdapter

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

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
            plugin._metadata_cache[rom_id_str] = plugin._metadata_service.extract_metadata(rom)
        MetadataCachePersisterAdapter(plugin._persistence, plugin._metadata_cache).save_metadata()

        # Verify in-memory cache
        assert plugin._metadata_cache["1"]["summary"] == "Game one description"
        assert plugin._metadata_cache["1"]["genres"] == ("RPG",)
        assert plugin._metadata_cache["1"]["first_release_date"] == 946684800

        # ROM with None metadatum gets defaults
        assert plugin._metadata_cache["2"]["summary"] == ""
        assert plugin._metadata_cache["2"]["genres"] == ()
        assert plugin._metadata_cache["2"]["first_release_date"] is None

        # ROM without metadatum key gets defaults
        assert plugin._metadata_cache["3"]["summary"] == "Game three"
        assert plugin._metadata_cache["3"]["genres"] == ()

        # Verify disk cache (JSON serialization converts tuples to lists)
        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        assert os.path.exists(cache_path)
        with open(cache_path) as f:
            disk_cache = json.load(f)
        assert "1" in disk_cache
        assert "2" in disk_cache
        assert "3" in disk_cache
        assert disk_cache["1"]["genres"] == ["RPG"]

    def test_sync_preserves_existing_cache(self, plugin, tmp_path):
        """Pre-existing cache entries for other ROMs are preserved after sync adds new ones."""
        import decky

        from adapters.persistence import PersistenceAdapter

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

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
            plugin._metadata_cache[str(rom["id"])] = plugin._metadata_service.extract_metadata(rom)
        MetadataCachePersisterAdapter(plugin._persistence, plugin._metadata_cache).save_metadata()

        # Both old and new entries must be present
        assert "99" in plugin._metadata_cache
        assert plugin._metadata_cache["99"]["summary"] == "Existing game"
        assert plugin._metadata_cache["99"]["genres"] == ["Puzzle"]
        assert "1" in plugin._metadata_cache
        assert plugin._metadata_cache["1"]["summary"] == "New game"

        # Verify on disk too
        cache_path = os.path.join(str(tmp_path), "metadata_cache.json")
        with open(cache_path) as f:
            disk_cache = json.load(f)
        assert "99" in disk_cache
        assert "1" in disk_cache

    def test_sync_rom_without_metadatum(self, plugin):
        """ROM without metadatum field gets an empty-default cache entry during sync."""
        rom = {"id": 5, "summary": "No metadata here"}
        result = plugin._metadata_service.extract_metadata(rom)
        assert result["summary"] == "No metadata here"
        assert result["genres"] == ()
        assert result["companies"] == ()
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == ()
        assert result["player_count"] == ""
        assert result["cached_at"] > 0


class TestGetRomMetadata404:
    """Test get_rom_metadata when API returns HTTP 404."""

    @pytest.mark.asyncio
    async def test_rom_not_found_returns_defaults(self, plugin):
        """API 404 with no cache returns empty defaults."""
        import urllib.error
        from unittest.mock import patch

        plugin.settings["log_level"] = "warn"

        http_404 = urllib.error.HTTPError(
            url="http://example.com/api/roms/999",
            code=404,
            msg="Not Found",
            hdrs=http.client.HTTPMessage(),
            fp=None,
        )

        with patch.object(plugin._romm_api, "get_rom", side_effect=http_404):
            result = await plugin.get_rom_metadata(999)

        assert result["summary"] == ""
        assert result["genres"] == ()
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["cached_at"] == 0

    @pytest.mark.asyncio
    async def test_rom_not_found_returns_stale_cache(self, plugin):
        """API 404 with stale cache returns cached data."""
        import urllib.error
        from unittest.mock import patch

        plugin.settings["log_level"] = "warn"

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
            hdrs=http.client.HTTPMessage(),
            fp=None,
        )

        with patch.object(plugin._romm_api, "get_rom", side_effect=http_404):
            result = await plugin.get_rom_metadata(999)

        assert result["summary"] == "Old cached data"
        assert result["genres"] == ["Action"]


# ── Tests for uncovered MetadataService methods ──────────


class TestMarkMetadataDirty:
    """Tests for mark_metadata_dirty() — covers lines 67-70."""

    def test_increments_count(self, plugin):
        plugin._metadata_service._metadata_dirty_count = 0
        plugin._metadata_service.mark_metadata_dirty()
        assert plugin._metadata_service._metadata_dirty_count == 1

    def test_flushes_at_interval(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._metadata_service._metadata_dirty_count = 49
        plugin._metadata_service.mark_metadata_dirty()
        # After reaching 50, should have flushed and reset
        assert plugin._metadata_service._metadata_dirty_count == 0

    def test_does_not_flush_below_interval(self, plugin):
        plugin._metadata_service._metadata_dirty_count = 10
        plugin._metadata_service.mark_metadata_dirty()
        assert plugin._metadata_service._metadata_dirty_count == 11


class TestFlushMetadataIfDirty:
    """Tests for flush_metadata_if_dirty() — covers lines 75-76."""

    def test_flushes_when_dirty(self, plugin, tmp_path):
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._metadata_service._metadata_dirty_count = 5
        plugin._metadata_service.flush_metadata_if_dirty()
        assert plugin._metadata_service._metadata_dirty_count == 0

    def test_noop_when_clean(self, plugin):
        plugin._metadata_service._metadata_dirty_count = 0
        # Ensure the persister is NOT triggered (we'd get an error if it tried to write)
        plugin._metadata_service._metadata_cache_persister = MagicMock()
        plugin._metadata_service.flush_metadata_if_dirty()
        plugin._metadata_service._metadata_cache_persister.save_metadata.assert_not_called()


class TestRecordUnitMetadata:
    """Tests for record_unit_metadata() — the per-applied-unit cache stamp (#738)."""

    def test_stamps_metadata_for_roms_with_metadatum(self, plugin, tmp_path):
        """Each ROM with a ``metadatum`` field lands in the metadata cache."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        roms = [
            {"id": 1, "summary": "Game 1", "metadatum": {"genres": ["RPG"]}},
            {"id": 2, "summary": "Game 2", "metadatum": {"genres": ["Action"]}},
        ]

        plugin._metadata_service.record_unit_metadata(roms)

        assert "1" in plugin._metadata_cache
        assert plugin._metadata_cache["1"]["summary"] == "Game 1"
        assert plugin._metadata_cache["1"]["genres"] == ("RPG",)
        assert "2" in plugin._metadata_cache
        assert plugin._metadata_cache["2"]["summary"] == "Game 2"

    def test_skips_roms_without_metadatum(self, plugin, tmp_path):
        """Thin ROMs (no ``metadatum`` field) MUST NOT erase populated entries.

        This is the load-bearing guard against #738 cache corruption:
        if a registry-reconstructed thin ROM ever reaches this method
        (it shouldn't — the orchestrator gates them out via the skip
        flag), the populated cache entry must survive.
        """
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        # Pre-existing populated entry.
        plugin._metadata_cache["1"] = {
            "summary": "Populated existing entry",
            "genres": ("RPG",),
            "companies": (),
            "first_release_date": None,
            "average_rating": None,
            "game_modes": (),
            "player_count": "",
            "cached_at": 100.0,
            "steam_categories": (),
        }

        # Thin ROM (no metadatum) — exactly what the registry-reconstruction
        # path produces.
        thin_roms = [{"id": 1, "name": "Game 1"}]

        plugin._metadata_service.record_unit_metadata(thin_roms)

        # Cache entry untouched.
        assert plugin._metadata_cache["1"]["summary"] == "Populated existing entry"
        assert plugin._metadata_cache["1"]["genres"] == ("RPG",)

    def test_skips_roms_with_falsy_metadatum(self, plugin, tmp_path):
        """ROMs with ``metadatum: None`` or ``metadatum: {}`` are skipped."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._metadata_cache["1"] = {
            "summary": "Existing",
            "genres": ("RPG",),
            "companies": (),
            "first_release_date": None,
            "average_rating": None,
            "game_modes": (),
            "player_count": "",
            "cached_at": 100.0,
            "steam_categories": (),
        }

        plugin._metadata_service.record_unit_metadata([{"id": 1, "metadatum": None}])
        assert plugin._metadata_cache["1"]["summary"] == "Existing"

        plugin._metadata_service.record_unit_metadata([{"id": 1, "metadatum": {}}])
        assert plugin._metadata_cache["1"]["summary"] == "Existing"

    def test_empty_list_is_noop(self, plugin, tmp_path):
        """Empty ROM list doesn't blow up."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._metadata_service.record_unit_metadata([])
        # No assertion — just verify no exception raised.

    def test_flushes_dirty_cache(self, plugin):
        """After processing all ROMs, dirty cache is flushed (persister save_metadata called)."""
        roms = [{"id": 1, "metadatum": {"genres": ["RPG"]}}]
        # FakeMetadataCachePersister counts save_metadata calls.
        save_count_before = plugin._metadata_cache_persister.save_count

        plugin._metadata_service.record_unit_metadata(roms)

        # Dirty counter reset after final flush.
        assert plugin._metadata_service._metadata_dirty_count == 0
        # Persister was invoked at least once for the final flush.
        assert plugin._metadata_cache_persister.save_count > save_count_before

    def test_mixed_thin_and_full_roms(self, plugin, tmp_path):
        """Mixed batch: full ROMs stamped, thin ROMs skipped."""
        import decky

        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        # Pre-existing populated entry for rom 2 (the thin one).
        plugin._metadata_cache["2"] = {
            "summary": "Populated existing",
            "genres": ("RPG",),
            "companies": (),
            "first_release_date": None,
            "average_rating": None,
            "game_modes": (),
            "player_count": "",
            "cached_at": 100.0,
            "steam_categories": (),
        }

        mixed = [
            {"id": 1, "summary": "New game", "metadatum": {"genres": ["Action"]}},
            {"id": 2, "name": "Thin"},  # no metadatum → skip
            {"id": 3, "summary": "Another", "metadatum": {"genres": ["Puzzle"]}},
        ]

        plugin._metadata_service.record_unit_metadata(mixed)

        # rom 1 stamped fresh
        assert plugin._metadata_cache["1"]["summary"] == "New game"
        # rom 2 untouched (populated existing entry survived)
        assert plugin._metadata_cache["2"]["summary"] == "Populated existing"
        # rom 3 stamped fresh
        assert plugin._metadata_cache["3"]["summary"] == "Another"


class TestGetAppIdRomIdMap:
    """Tests for get_app_id_rom_id_map() — covers lines 121-126."""

    def test_builds_mapping(self, plugin):
        plugin._state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A"},
            "20": {"app_id": 1002, "name": "Game B"},
            "30": {"name": "Game C"},  # no app_id
        }
        result = plugin._metadata_service.get_app_id_rom_id_map()
        assert result["1001"] == 10
        assert result["1002"] == 20
        assert "30" not in result

    def test_empty_registry(self, plugin):
        result = plugin._metadata_service.get_app_id_rom_id_map()
        assert result == {}
