import asyncio
from unittest.mock import MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_testable_plugin
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from adapters.debug_logger import SettingsAwareDebugLogger
from adapters.steam_config import SteamConfigAdapter
from domain.rom import Rom
from domain.rom_metadata import RomMetadata
from services.metadata import MetadataService, MetadataServiceConfig


def _seed_rom(uow, rom_id, *, app_id, name="Game", platform_slug="n64"):
    """Insert a bound (or unbound when app_id is None) ROM into the fake UoW."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(rom)


def _seed_metadata(uow, rom_id, meta, *, app_id=None):
    """Seed a Rom (FK parent) THEN its cached metadata, in one commit."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug="n64",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=app_id if app_id is not None else 1000 + rom_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(rom)
        uow.rom_metadata.save(rom_id, meta)


def _meta(**overrides):
    """Build a RomMetadata aggregate with sensible defaults."""
    base = {
        "summary": "",
        "genres": (),
        "companies": (),
        "first_release_date": None,
        "average_rating": None,
        "game_modes": (),
        "player_count": "",
        "cached_at": 1700000000.0,
        "steam_categories": (),
    }
    base.update(overrides)
    return RomMetadata(**base)


@pytest.fixture
def uow() -> FakeUnitOfWork:
    """Shared in-memory UoW the tests seed and assert against."""
    return FakeUnitOfWork()


@pytest.fixture
def plugin(uow):
    p = _make_testable_plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._romm_api = MagicMock()
    p._uow = uow

    import decky

    p._debug_logger = SettingsAwareDebugLogger(settings=p.settings, logger=decky.logger)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            log_debug=p._log_debug,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
        ),
    )
    p._metadata_service = metadata_service
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop and service loop match the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()
    plugin._metadata_service._loop = asyncio.get_event_loop()


class TestGetRomMetadata:
    """Tests for the get_rom_metadata callable."""

    @pytest.mark.asyncio
    async def test_cache_hit(self, plugin, uow):
        """Returns cached data as the frontend entry (list-shaped array fields)."""
        import time

        _seed_metadata(
            uow,
            42,
            _meta(
                summary="Cached summary",
                genres=("RPG",),
                companies=("Nintendo",),
                first_release_date=946684800,
                average_rating=85.0,
                game_modes=("Single player",),
                player_count="1",
                cached_at=time.time(),
            ),
        )
        plugin.settings["log_level"] = "warn"
        result = await plugin.get_rom_metadata(42)
        assert result["summary"] == "Cached summary"
        # Tuple fields flatten to lists for the wire shape.
        assert result["genres"] == ["RPG"]
        assert result["companies"] == ["Nintendo"]
        assert result["game_modes"] == ["Single player"]
        assert result["first_release_date"] == 946684800
        assert result["average_rating"] == 85.0

    @pytest.mark.asyncio
    async def test_cache_miss_returns_empty_defaults(self, plugin):
        """Cache miss returns empty defaults without calling the API."""
        plugin.settings["log_level"] = "warn"

        result = await plugin.get_rom_metadata(42)

        assert result["summary"] == ""
        assert result["genres"] == []
        assert result["companies"] == []
        assert result["first_release_date"] is None
        assert result["average_rating"] is None
        assert result["game_modes"] == []
        assert result["player_count"] == ""
        assert result["cached_at"] == 0

    @pytest.mark.asyncio
    async def test_stale_cache_returns_stale_data(self, plugin, uow):
        """Stale cache (>7 days) is still returned — refreshed on next sync."""
        import time

        plugin.settings["log_level"] = "warn"
        _seed_metadata(
            uow,
            42,
            _meta(summary="Old summary", genres=("Action",), cached_at=time.time() - (8 * 24 * 3600)),
        )

        result = await plugin.get_rom_metadata(42)

        assert result["summary"] == "Old summary"
        assert result["genres"] == ["Action"]

    @pytest.mark.asyncio
    async def test_no_api_call_on_cache_miss(self, plugin):
        """Verify get_rom is never called — metadata comes only from SQLite."""
        from unittest.mock import patch

        plugin.settings["log_level"] = "warn"

        with patch.object(plugin._romm_api, "get_rom") as mock_get_rom:
            await plugin.get_rom_metadata(42)

        mock_get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_debug_logging_on_cache_hit(self, plugin, uow):
        """Verify _log_debug is called during cache hit."""
        import time
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "debug"
        _seed_metadata(uow, 42, _meta(summary="cached", cached_at=time.time()))

        with patch.object(decky.logger, "info") as mock_info:
            await plugin.get_rom_metadata(42)
            logged = [str(c) for c in mock_info.call_args_list]
            assert any("cache hit" in m.lower() for m in logged)

    @pytest.mark.asyncio
    async def test_debug_logging_on_cache_miss(self, plugin):
        """Verify _log_debug is called during cache miss."""
        from unittest.mock import patch

        import decky

        plugin.settings["log_level"] = "debug"

        with patch.object(decky.logger, "info") as mock_info:
            await plugin.get_rom_metadata(42)
            logged = [str(c) for c in mock_info.call_args_list]
            assert any("cache miss" in m.lower() for m in logged)


class TestGetAllMetadataCache:
    """Tests for the get_all_metadata_cache callable — Record<str, entry> wire shape."""

    @pytest.mark.asyncio
    async def test_returns_full_cache(self, plugin, uow):
        _seed_metadata(uow, 1, _meta(summary="Game 1", genres=("RPG",), cached_at=100.0))
        _seed_metadata(uow, 2, _meta(summary="Game 2", cached_at=200.0))

        result = await plugin.get_all_metadata_cache()

        assert len(result) == 2
        # Keyed by str(rom_id).
        assert result["1"]["summary"] == "Game 1"
        assert result["2"]["summary"] == "Game 2"
        # Array fields are lists, not tuples, on the wire.
        assert result["1"]["genres"] == ["RPG"]
        assert isinstance(result["1"]["genres"], list)
        assert result["1"]["cached_at"] == 100.0

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cache(self, plugin):
        result = await plugin.get_all_metadata_cache()
        assert result == {}


class TestGetAppIdRomIdMap:
    """Tests for get_app_id_rom_id_map() — unchanged behaviour (reads uow.roms)."""

    def test_builds_mapping(self, plugin, uow):
        _seed_rom(uow, 10, app_id=1001, name="Game A")
        _seed_rom(uow, 20, app_id=1002, name="Game B")
        _seed_rom(uow, 30, app_id=None, name="Game C")  # unbound — excluded
        result = plugin._metadata_service.get_app_id_rom_id_map()
        assert result["1001"] == 10
        assert result["1002"] == 20
        # The unbound ROM (NULL shortcut_app_id) contributes no mapping.
        assert "None" not in result
        assert len(result) == 2

    def test_empty_registry(self, plugin):
        result = plugin._metadata_service.get_app_id_rom_id_map()
        assert result == {}
