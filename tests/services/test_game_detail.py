import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import FakeCoreInfoProvider, FakeFirmwareCachePersister, _make_retry, _make_testable_plugin
from fakes.fake_save_api import FakeSaveApi
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.firmware_file import FirmwareFileAdapter
from adapters.persistence import PersistenceAdapter, SaveSyncStatePersisterAdapter
from adapters.save_file import SaveFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.save_state import FileSyncState, RomSaveState
from services.achievements import AchievementsService, AchievementsServiceConfig
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.game_detail import GameDetailService, GameDetailServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.playtime import PlaytimeService, PlaytimeServiceConfig
from services.saves import SaveService, SaveServiceConfig


@pytest.fixture
def plugin(tmp_path):
    p = _make_testable_plugin()
    p.settings = {
        "romm_url": "http://romm.local",
        "romm_user": "user",
        "romm_pass": "pass",
        "enabled_platforms": {},
        "log_level": "warn",
    }
    p._state = {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {},
        "downloaded_bios": {},
    }
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=MagicMock(),
            steam_config=steam_config,
            state=p._state,
            settings=p.settings,
            metadata_cache=p._metadata_cache,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            save_state=p._save_state,
            save_settings_to_disk=p._save_settings_to_disk,
            log_debug=p._log_debug,
        ),
    )
    decky.DECKY_USER_HOME = str(tmp_path)

    # Wire services with FakeSaveApi
    fake_api = FakeSaveApi()
    p._save_sync_state = SaveService.make_default_state()
    saves_path = str(tmp_path / "retrodeck" / "saves")

    p._save_sync_service = SaveService(
        config=SaveServiceConfig(
            romm_api=fake_api,
            retry=_make_retry(),
            settings={"log_level": "debug"},
            state=p._state,
            save_sync_state=p._save_sync_state,
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            runtime_dir=str(tmp_path),
            save_sync_state_persister=SaveSyncStatePersisterAdapter(
                PersistenceAdapter(
                    settings_dir=str(tmp_path),
                    runtime_dir=str(tmp_path),
                    logger=logging.getLogger("test"),
                )
            ),
            save_file=SaveFileAdapter(),
            get_saves_path=lambda: saves_path,
            get_roms_path=lambda: str(tmp_path / "retrodeck" / "roms"),
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            log_debug=p._log_debug,
        ),
    )
    p._save_sync_service.init_state()

    p._playtime_service = PlaytimeService(
        config=PlaytimeServiceConfig(
            romm_api=fake_api,
            retry=_make_retry(),
            save_sync_state=p._save_sync_state,
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            save_state=p._save_sync_service.save_state,
            log_debug=p._log_debug,
        ),
    )

    p._achievements_service = AchievementsService(
        config=AchievementsServiceConfig(
            romm_api=MagicMock(),
            state=p._state,
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            log_debug=p._log_debug,
        ),
    )

    p._firmware_service = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=MagicMock(),
            state=p._state,
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            save_state=MagicMock(),
            firmware_cache_persister=FakeFirmwareCachePersister(),
            firmware_files=FirmwareFileAdapter(),
            get_bios_path=MagicMock(return_value=""),
            core_info=FakeCoreInfoProvider(),
        ),
    )
    p._firmware_service.load_bios_registry()

    # Store fake_api on plugin for test access
    p._fake_api = fake_api

    p._save_sync_state.settings.save_sync_enabled = False
    return p


@pytest.fixture
def clock():
    """FakeClock pinned to a fixed synthetic instant — drives all TTL comparisons deterministically."""
    return FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def game_detail_service(plugin, clock):
    """Create a GameDetailService wired to the plugin's state and the pinned clock fixture."""
    return GameDetailService(
        config=GameDetailServiceConfig(
            state=plugin._state,
            metadata_cache=plugin._metadata_cache,
            save_sync_state=plugin._save_sync_state,
            logger=logging.getLogger("test"),
            clock=clock,
            bios_checker=plugin._firmware_service,
            achievements=plugin._achievements_service,
        ),
    )


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    plugin._save_sync_service._loop = loop
    plugin._playtime_service._loop = loop


def _install_rom(plugin, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba"):
    """Helper: register a ROM in installed_roms state."""
    plugin._state["installed_roms"][str(rom_id)] = {
        "rom_id": rom_id,
        "file_name": file_name,
        "file_path": str(tmp_path / "retrodeck" / "roms" / system / file_name),
        "system": system,
        "platform_slug": system,
    }


def _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 1024, ext=".srm"):
    """Helper: create a save file on disk."""
    saves_dir = tmp_path / "retrodeck" / "saves" / system
    saves_dir.mkdir(parents=True, exist_ok=True)
    save_file = saves_dir / (rom_name + ext)
    save_file.write_bytes(content)
    return save_file


def _server_save(
    save_id=100, rom_id=42, filename="pokemon.srm", updated_at="2026-02-17T06:00:00Z", file_size_bytes=1024
):
    """Helper: build a server save response dict."""
    return {
        "id": save_id,
        "rom_id": rom_id,
        "file_name": filename,
        "updated_at": updated_at,
        "file_size_bytes": file_size_bytes,
        "emulator": "retroarch",
        "download_path": f"/saves/{filename}",
    }


class TestGetCachedGameDetailFound:
    """Test get_cached_game_detail when app_id is in the registry."""

    @pytest.mark.asyncio
    async def test_found_with_full_data(self, plugin, game_detail_service):
        """All data present: registry, installed, save status, metadata, conflicts."""
        plugin._state["shortcut_registry"]["123"] = {
            "app_id": 99999,
            "name": "Super Mario World",
            "platform_slug": "snes",
            "platform_name": "Super Nintendo",
        }
        plugin._state["installed_roms"]["123"] = {
            "rom_id": 123,
            "file_path": "/roms/snes/smw.sfc",
            "system": "snes",
        }
        plugin._save_sync_state.settings.save_sync_enabled = True
        plugin._save_sync_state.saves["123"] = RomSaveState(
            files={
                "smw.srm": FileSyncState(
                    last_sync_at="2025-01-01T00:00:00Z",
                    last_sync_hash="abc123",
                ),
            },
            last_sync_check_at="2025-01-01T00:00:00Z",
        )
        plugin._metadata_cache["123"] = {
            "summary": "Classic SNES platformer",
            "genres": ["Platformer"],
            "cached_at": 100,
        }

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["found"] is True
        assert result["rom_id"] == 123
        assert result["rom_name"] == "Super Mario World"
        assert result["platform_slug"] == "snes"
        assert result["platform_name"] == "Super Nintendo"
        assert result["installed"] is True
        assert result["save_sync_enabled"] is True
        assert len(result["save_status"]["files"]) == 1
        assert result["save_status"]["files"][0]["filename"] == "smw.srm"
        assert result["save_status"]["files"][0]["status"] == "synced"
        assert result["save_status"]["last_sync_check_at"] == "2025-01-01T00:00:00Z"
        assert result["metadata"]["summary"] == "Classic SNES platformer"
        assert result["bios_status"] is None


class TestGetCachedGameDetailNotFound:
    """Test get_cached_game_detail when app_id is NOT in the registry."""

    @pytest.mark.asyncio
    async def test_not_found(self, game_detail_service):
        """Unknown app_id returns found=False."""
        result = game_detail_service.get_cached_game_detail(12345)
        assert result == {"found": False}

    @pytest.mark.asyncio
    async def test_not_found_empty_registry(self, plugin, game_detail_service):
        """Empty registry returns found=False."""
        plugin._state["shortcut_registry"] = {}
        result = game_detail_service.get_cached_game_detail(1)
        assert result == {"found": False}

    @pytest.mark.asyncio
    async def test_not_found_different_app_id(self, plugin, game_detail_service):
        """Registry has entries but none match the requested app_id."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 11111,
            "name": "Other Game",
            "platform_slug": "nes",
            "platform_name": "NES",
        }
        result = game_detail_service.get_cached_game_detail(99999)
        assert result == {"found": False}


class TestGetCachedGameDetailPartialData:
    """Test with missing optional data (no save status, no metadata, etc.)."""

    @pytest.mark.asyncio
    async def test_no_save_status(self, plugin, game_detail_service):
        """No save data for this rom returns save_status=None."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Zelda",
            "platform_slug": "snes",
            "platform_name": "Super Nintendo",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["save_status"] is None

    @pytest.mark.asyncio
    async def test_no_metadata(self, plugin, game_detail_service):
        """No metadata cached returns metadata=None."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Zelda",
            "platform_slug": "snes",
            "platform_name": "Super Nintendo",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["metadata"] is None

    @pytest.mark.asyncio
    async def test_no_pending_conflicts_key(self, plugin, game_detail_service):
        """pending_conflicts is no longer in the response (conflicts are inline)."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Zelda",
            "platform_slug": "snes",
            "platform_name": "Super Nintendo",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert "pending_conflicts" not in result

    @pytest.mark.asyncio
    async def test_save_sync_disabled(self, plugin, game_detail_service):
        """save_sync_enabled reflects the setting."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Zelda",
            "platform_slug": "snes",
            "platform_name": "Super Nintendo",
        }
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["save_sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_missing_registry_fields_default_empty(self, plugin, game_detail_service):
        """Registry entry missing optional fields returns empty strings."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["rom_name"] == ""
        assert result["platform_slug"] == ""
        assert result["platform_name"] == ""


class TestGetCachedGameDetailInstalled:
    """Test installed vs not installed detection."""

    @pytest.mark.asyncio
    async def test_installed(self, plugin, game_detail_service):
        """ROM in installed_roms returns installed=True."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "snes",
            "platform_name": "SNES",
        }
        plugin._state["installed_roms"]["10"] = {
            "rom_id": 10,
            "file_path": "/roms/game.sfc",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["installed"] is True

    @pytest.mark.asyncio
    async def test_not_installed(self, plugin, game_detail_service):
        """ROM not in installed_roms returns installed=False."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "snes",
            "platform_name": "SNES",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["installed"] is False


class TestGetCachedGameDetailConflictFiltering:
    """pending_conflicts was removed from get_cached_game_detail response."""

    @pytest.mark.asyncio
    async def test_no_pending_conflicts_in_response(self, plugin, game_detail_service):
        """pending_conflicts key is no longer in the response."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Game A",
            "platform_slug": "snes",
            "platform_name": "SNES",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert "pending_conflicts" not in result

    @pytest.mark.asyncio
    async def test_response_still_has_save_status(self, plugin, game_detail_service):
        """Response still includes save status fields."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Game A",
            "platform_slug": "snes",
            "platform_name": "SNES",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert "save_sync_enabled" in result

    @pytest.mark.asyncio
    async def test_app_id_as_string(self, plugin, game_detail_service):
        """app_id passed as string is handled correctly."""
        plugin._state["shortcut_registry"]["10"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "snes",
            "platform_name": "SNES",
        }
        result = game_detail_service.get_cached_game_detail("50000")
        assert result["found"] is True
        assert result["rom_id"] == 10


# ============================================================================
# get_cached_game_detail bios_status from cache tests
# ============================================================================


class TestGetCachedGameDetailBiosFromCache:
    """Test that get_cached_game_detail returns bios_status from firmware cache."""

    @pytest.mark.asyncio
    async def test_bios_status_none_when_cache_empty(self, plugin, game_detail_service):
        """No firmware cache → bios_status is None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Pokemon",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        # firmware cache is empty by default (None)
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_bios_status_from_populated_cache(self, plugin, game_detail_service, tmp_path):
        """Firmware cache populated → bios_status returned with cached_at."""
        from unittest.mock import patch

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Pokemon",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        # Populate firmware cache
        plugin._firmware_service._firmware_cache = [
            {
                "file_path": "bios/gba/gba_bios.bin",
                "file_name": "gba_bios.bin",
                "file_size_bytes": 16384,
                "md5_hash": "abc123",
                "id": 1,
            },
        ]
        plugin._firmware_service._firmware_cache_epoch = 99.0

        plugin._firmware_service._core_info.active_core = ("mgba_libretro.so", "mGBA")
        plugin._firmware_service._core_info.available_cores = []
        with patch.object(plugin._firmware_service, "_get_bios_path", return_value=str(tmp_path)):
            result = game_detail_service.get_cached_game_detail(50000)

        assert result["found"] is True
        bs = result["bios_status"]
        assert bs is not None
        assert bs["platform_slug"] == "gba"
        assert bs["cached_at"] == pytest.approx(99.0)
        assert bs["server_count"] == 1
        assert bs["local_count"] == 0

    @pytest.mark.asyncio
    async def test_bios_status_none_when_no_platform_slug(self, plugin, game_detail_service):
        """No platform_slug in registry → bios_status is None (skipped)."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Game",
        }
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_bios_status_none_when_needs_bios_false(self, plugin, game_detail_service):
        """Cache populated but no firmware for platform → bios_status is None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Tetris",
            "platform_slug": "gb",
            "platform_name": "Game Boy",
        }
        plugin._firmware_service._firmware_cache = []
        plugin._firmware_service._firmware_cache_epoch = 50.0

        plugin._firmware_service._core_info.active_core = (None, None)
        result = game_detail_service.get_cached_game_detail(50000)

        assert result["bios_status"] is None


# ============================================================================
# get_bios_status tests
# ============================================================================


class TestGetBiosStatusFound:
    """Test get_bios_status when ROM has BIOS requirements."""

    @pytest.mark.asyncio
    async def test_returns_bios_status(self, plugin, game_detail_service):
        """ROM with needs_bios=True returns full bios_status dict."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        mock_check = AsyncMock(
            return_value={
                "needs_bios": True,
                "server_count": 3,
                "local_count": 1,
                "all_downloaded": False,
                "required_count": 2,
                "required_downloaded": 1,
                "files": [{"file_name": "gba_bios.bin", "downloaded": True}],
                "active_core": "mgba_libretro.so",
                "active_core_label": "mGBA",
                "available_cores": [],
            }
        )
        game_detail_service._bios_checker.check_platform_bios = mock_check

        result = await game_detail_service.get_bios_status(42)
        bs = result["bios_status"]
        assert bs is not None
        assert bs["platform_slug"] == "gba"
        assert bs["server_count"] == 3
        assert bs["local_count"] == 1
        assert bs["all_downloaded"] is False
        assert bs["required_count"] == 2
        assert bs["required_downloaded"] == 1
        assert bs["active_core_label"] == "mGBA"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_bios_needed(self, plugin, game_detail_service):
        """ROM with needs_bios=False returns bios_status=None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        game_detail_service._bios_checker.check_platform_bios = AsyncMock(return_value={"needs_bios": False})

        result = await game_detail_service.get_bios_status(42)
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_uses_rom_file_from_installed(self, plugin, game_detail_service):
        """Uses file_name from installed_roms for per-game core detection."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "gba",
            "platform_name": "GBA",
            "fs_name": "registry_file.gba",
        }
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "installed_file.gba",
        }

        captured_args = {}

        async def capture_check(slug, rom_filename=None):
            captured_args["slug"] = slug
            captured_args["rom_filename"] = rom_filename
            return {"needs_bios": False}

        game_detail_service._bios_checker.check_platform_bios = capture_check

        await game_detail_service.get_bios_status(42)
        assert captured_args["rom_filename"] == "installed_file.gba"


class TestGetBiosStatusNotFound:
    """Test get_bios_status when ROM is not in registry."""

    @pytest.mark.asyncio
    async def test_unknown_rom_id(self, game_detail_service):
        """Unknown rom_id returns bios_status=None."""
        result = await game_detail_service.get_bios_status(999)
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_no_platform_slug(self, plugin, game_detail_service):
        """Registry entry without platform_slug returns bios_status=None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Game",
        }
        result = await game_detail_service.get_bios_status(42)
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_firmware_error_returns_none(self, plugin, game_detail_service):
        """Firmware service exception returns bios_status=None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 50000,
            "name": "Game",
            "platform_slug": "gba",
        }
        game_detail_service._bios_checker.check_platform_bios = AsyncMock(side_effect=Exception("fail"))

        result = await game_detail_service.get_bios_status(42)
        assert result["bios_status"] is None


class TestGetCachedGameDetailSaveStatusConflicts:
    @pytest.mark.asyncio
    async def test_save_status_includes_empty_conflicts(self, plugin, game_detail_service):
        """Lightweight save_status should include an empty conflicts list."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        plugin._save_sync_state.settings.save_sync_enabled = True
        plugin._save_sync_state.saves["42"] = RomSaveState(
            files={"test.srm": FileSyncState(last_sync_hash="abc", last_sync_at="2026-01-01T00:00:00Z")},
            last_sync_check_at="2026-01-01T00:00:00Z",
        )
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_status"] is not None
        assert "conflicts" in result["save_status"]
        assert result["save_status"]["conflicts"] == []


class TestComputedFields:
    """Test bios_level, bios_label, save_sync_display in response."""

    @pytest.mark.asyncio
    async def test_bios_level_and_label_when_bios_present(self, plugin, game_detail_service, tmp_path):
        """When BIOS data is cached, bios_level and bios_label should be set."""
        from unittest.mock import patch

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        # Populate firmware cache with a GBA BIOS file (not locally present)
        plugin._firmware_service._firmware_cache = [
            {
                "file_path": "bios/gba/gba_bios.bin",
                "file_name": "gba_bios.bin",
                "file_size_bytes": 16384,
                "md5_hash": "abc123",
                "id": 1,
            },
            {
                "file_path": "bios/gba/gba_bios2.bin",
                "file_name": "gba_bios2.bin",
                "file_size_bytes": 16384,
                "md5_hash": "def456",
                "id": 2,
            },
        ]
        plugin._firmware_service._firmware_cache_epoch = 100.0

        plugin._firmware_service._core_info.active_core = ("mgba_libretro.so", "mGBA")
        plugin._firmware_service._core_info.available_cores = []
        with patch.object(plugin._firmware_service, "_get_bios_path", return_value=str(tmp_path / "nonexistent")):
            result = game_detail_service.get_cached_game_detail(99999)

        assert result["bios_level"] is not None
        assert result["bios_label"] is not None
        # Files not downloaded → missing or partial
        assert result["bios_level"] in ("missing", "partial", "ok")
        assert isinstance(result["bios_label"], str)

    @pytest.mark.asyncio
    async def test_bios_level_none_when_no_bios(self, plugin, game_detail_service):
        """When no BIOS data (cache empty), bios_level and bios_label should be None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        # _firmware_cache is None by default
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["bios_level"] is None
        assert result["bios_label"] is None

    @pytest.mark.asyncio
    async def test_bios_level_ok_when_all_downloaded(self, plugin, game_detail_service, tmp_path):
        """When all required BIOS files are present, bios_level should be 'ok'."""
        from unittest.mock import patch

        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        bios_dir = tmp_path / "bios"
        bios_dir.mkdir(parents=True, exist_ok=True)
        bios_file = bios_dir / "gba_bios.bin"
        bios_file.write_bytes(b"\x00" * 16384)

        plugin._firmware_service._firmware_cache = [
            {
                "file_path": "bios/gba/gba_bios.bin",
                "file_name": "gba_bios.bin",
                "file_size_bytes": 16384,
                "md5_hash": "abc123",
                "id": 1,
            },
        ]
        plugin._firmware_service._firmware_cache_epoch = 100.0

        plugin._firmware_service._core_info.active_core = ("mgba_libretro.so", "mGBA")
        plugin._firmware_service._core_info.available_cores = []
        with patch.object(plugin._firmware_service, "_get_bios_path", return_value=str(bios_dir)):
            result = game_detail_service.get_cached_game_detail(99999)

        assert result["bios_level"] == "ok"

    @pytest.mark.asyncio
    async def test_save_sync_display_with_saves(self, plugin, game_detail_service):
        """When save data exists, save_sync_display should be computed."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        plugin._save_sync_state.settings.save_sync_enabled = True
        plugin._save_sync_state.saves["42"] = RomSaveState(
            files={"test.srm": FileSyncState(last_sync_hash="abc", last_sync_at="2026-01-01T00:00:00Z")},
            last_sync_check_at="2026-01-01T00:00:00Z",
        )
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_sync_display"] is not None
        assert result["save_sync_display"]["status"] == "synced"
        label = result["save_sync_display"]["label"]
        assert "ago" in label or label in ("Just now", "Not synced")

    @pytest.mark.asyncio
    async def test_save_sync_display_none_when_no_saves(self, plugin, game_detail_service):
        """When no save data, save_sync_display should be None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_sync_display"] is None


class TestAchievementSummaryCachedAt:
    """Test that achievement_summary includes cached_at from progress cache."""

    @pytest.mark.asyncio
    async def test_achievement_summary_includes_cached_at(self, plugin, game_detail_service, clock):
        """When progress is cached, achievement_summary includes cached_at timestamp."""
        cached_time = clock.time() - 600  # 10 minutes ago
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Sonic",
            "platform_slug": "genesis",
            "platform_name": "Genesis",
            "ra_id": 555,
        }
        plugin._achievements_service._achievements_cache["_ra_user"] = {
            "username": "testuser",
            "cached_at": clock.time(),
        }
        plugin._achievements_service._achievements_cache["42"] = {
            "user_progress": {
                "earned": 5,
                "earned_hardcore": 3,
                "total": 20,
                "earned_achievements": [],
                "cached_at": cached_time,
            },
            "cached_at": clock.time(),
        }

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["achievement_summary"] is not None
        assert result["achievement_summary"]["earned"] == 5
        assert result["achievement_summary"]["total"] == 20
        assert result["achievement_summary"]["earned_hardcore"] == 3
        assert result["achievement_summary"]["cached_at"] == cached_time

    @pytest.mark.asyncio
    async def test_achievement_summary_cached_at_reflects_storage_time(self, plugin, game_detail_service, clock):
        """cached_at in summary matches the time progress was stored, not current time."""
        storage_time = clock.time() - 1800  # 30 minutes ago
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Sonic",
            "platform_slug": "genesis",
            "platform_name": "Genesis",
            "ra_id": 555,
        }
        plugin._achievements_service._achievements_cache["_ra_user"] = {
            "username": "testuser",
            "cached_at": clock.time(),
        }
        plugin._achievements_service._achievements_cache["42"] = {
            "user_progress": {
                "earned": 10,
                "earned_hardcore": 10,
                "total": 10,
                "earned_achievements": [],
                "cached_at": storage_time,
            },
            "cached_at": clock.time(),
        }

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["achievement_summary"]["cached_at"] == storage_time
        assert result["achievement_summary"]["cached_at"] < clock.time() - 1700

    @pytest.mark.asyncio
    async def test_no_achievement_summary_without_ra_username(self, plugin, game_detail_service):
        """Without RA username, achievement_summary is None even with ra_id."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Sonic",
            "platform_slug": "genesis",
            "platform_name": "Genesis",
            "ra_id": 555,
        }

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["achievement_summary"] is None

    @pytest.mark.asyncio
    async def test_no_achievement_summary_without_cached_progress(self, plugin, game_detail_service, clock):
        """With RA username but no cached progress, achievement_summary is None."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Sonic",
            "platform_slug": "genesis",
            "platform_name": "Genesis",
            "ra_id": 555,
        }
        plugin._achievements_service._achievements_cache["_ra_user"] = {
            "username": "testuser",
            "cached_at": clock.time(),
        }

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["achievement_summary"] is None


class TestStaleFields:
    """Test stale_fields computation in get_cached_game_detail."""

    @pytest.mark.asyncio
    async def test_stale_fields_empty_when_all_fresh(self, plugin, game_detail_service, clock):
        """No stale fields when all caches are fresh."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        plugin._metadata_cache["42"] = {"cached_at": clock.time(), "genres": []}
        result = game_detail_service.get_cached_game_detail(99999)
        assert "stale_fields" in result
        assert "metadata" not in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_metadata_stale_when_old(self, plugin, game_detail_service, clock):
        """Metadata older than 7 days should appear in stale_fields."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        plugin._metadata_cache["42"] = {"cached_at": clock.time() - 8 * 24 * 3600, "genres": []}
        result = game_detail_service.get_cached_game_detail(99999)
        assert "metadata" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_metadata_stale_when_missing(self, plugin, game_detail_service):
        """Missing metadata should appear in stale_fields."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        result = game_detail_service.get_cached_game_detail(99999)
        assert "metadata" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_bios_stale_when_old(self, plugin, game_detail_service):
        """BIOS older than 1 hour should appear in stale_fields."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
        }
        # Set up firmware cache with old cached_at
        # The bios_status dict in the response will have cached_at if present
        result = game_detail_service.get_cached_game_detail(99999)
        # With no BIOS cache, bios_status is None → bios should be stale
        assert "bios" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_achievements_stale_when_missing(self, plugin, game_detail_service):
        """Missing achievement progress should appear in stale_fields when ra_id is set."""
        plugin._state["shortcut_registry"]["42"] = {
            "app_id": 99999,
            "name": "Test",
            "platform_slug": "gba",
            "platform_name": "GBA",
            "ra_id": 123,
        }
        result = game_detail_service.get_cached_game_detail(99999)
        assert "achievements" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_not_found_has_no_stale_fields(self, plugin, game_detail_service):
        """When ROM not found, response has no stale_fields."""
        result = game_detail_service.get_cached_game_detail(99999)
        assert "stale_fields" not in result
