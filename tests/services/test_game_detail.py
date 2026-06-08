import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_retry, _make_testable_plugin
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_machine_id_reader import FakeMachineIdReader
from fakes.fake_plugin_metadata_reader import FakePluginMetadataReader
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_save_api import FakeSaveApi
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.firmware_file import FirmwareFileAdapter
from adapters.save_file import SaveFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.rom_save_state import FileSyncState
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

    import decky

    # Shared UoW so a metadata row seeded by a test is visible to the
    # GameDetailService read (both wrap the same instance via the factory).
    uow = FakeUnitOfWork()
    p._uow = uow
    p._tmp_path = tmp_path

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=MagicMock(),
            steam_config=steam_config,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            settings_persister=MagicMock(),
            log_debug=p._log_debug,
            artwork=FakeArtworkManager(),
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            active_core=FakeActiveCoreResolver(default=(None, None)),
        ),
    )
    decky.DECKY_USER_HOME = str(tmp_path)

    # Wire services with FakeSaveApi
    fake_api = FakeSaveApi()
    saves_path = str(tmp_path / "retrodeck" / "saves")

    p._save_sync_service = SaveService(
        config=SaveServiceConfig(
            romm_api=fake_api,
            retry=_make_retry(),
            settings={"log_level": "debug"},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            settings_persister=MagicMock(),
            save_file_store=SaveFileAdapter(),
            retrodeck_paths=FakeRetroDeckPaths(
                saves=saves_path,
                roms=str(tmp_path / "retrodeck" / "roms"),
            ),
            active_core=FakeActiveCoreResolver(default=(None, None)),
            hostname_provider=FakeHostnameReader(),
            machine_id_provider=FakeMachineIdReader(),
            log_debug=p._log_debug,
            plugin_metadata=FakePluginMetadataReader(version="0.14.0"),
            plugin_dir=str(tmp_path / "plugin"),
            emit=AsyncMock(),
            get_core_name=lambda core_so: None,
            detect_sort_change=lambda: None,
            is_retrodeck_migration_pending=lambda: False,
            uow_factory=FakeUnitOfWorkFactory(),
        ),
    )

    p._playtime_service = PlaytimeService(
        config=PlaytimeServiceConfig(
            romm_api=fake_api,
            retry=_make_retry(),
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            log_debug=p._log_debug,
            uow_factory=FakeUnitOfWorkFactory(),
        ),
    )

    p._achievements_service = AchievementsService(
        config=AchievementsServiceConfig(
            romm_api=MagicMock(),
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            log_debug=p._log_debug,
        ),
    )

    p._firmware_service = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=MagicMock(),
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            firmware_file_store=FirmwareFileAdapter(),
            retrodeck_paths=FakeRetroDeckPaths(),
            core_info=FakeCoreInfoProvider(),
            resolve_system=lambda platform_slug, platform_fs_slug=None: platform_slug,
            uow_factory=FakeUnitOfWorkFactory(),
        ),
    )
    p._firmware_service.load_bios_registry()

    # Store fake_api on plugin for test access
    p._fake_api = fake_api

    p.settings["save_sync_enabled"] = False
    return p


@pytest.fixture
def clock():
    """FakeClock pinned to a fixed synthetic instant — drives all TTL comparisons deterministically."""
    return FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def active_core_resolver():
    """Per-ROM active-core resolver fake the game-detail BIOS path resolves through.

    Defaults to ``(None, None)`` (system default); result-flip tests seed
    ``per_rom`` so two ROMs on one platform resolve to different cores.
    """
    return FakeActiveCoreResolver(default=(None, None))


@pytest.fixture
def game_detail_service(plugin, clock, active_core_resolver):
    """Create a GameDetailService wired to the plugin's shared UoW and pinned clock."""
    return GameDetailService(
        config=GameDetailServiceConfig(
            settings=plugin.settings,
            logger=logging.getLogger("test"),
            clock=clock,
            uow_factory=FakeUnitOfWorkFactory(uow=plugin._uow),
            bios_checker=plugin._firmware_service,
            achievements=plugin._achievements_service,
            active_core=active_core_resolver,
        ),
    )


def _seed_rom(
    plugin,
    rom_id,
    *,
    app_id,
    name="Game",
    platform_slug="snes",
    fs_name="",
    ra_id=None,
):
    """Seed one ``Rom`` row into the shared UoW (the synced-shortcut registry).

    *app_id* is the bound Steam shortcut id (``None`` = unbound). Children
    (install / save state / metadata) must be seeded after the Rom for the FK.
    """
    from domain.rom import Rom

    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=fs_name or f"game_{rom_id}.sfc",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
        ra_id=ra_id,
    )
    with plugin._uow:
        plugin._uow.roms.save(rom)


def _seed_platform_names(plugin, mapping):
    """Seed the offline ``platform_slug → display_name`` cache row in kv_config."""
    import json

    with plugin._uow:
        plugin._uow.kv_config.set("platform_names", json.dumps(mapping))


def _seed_save_state(plugin, rom_id, *, files, last_sync_check_at):
    """Seed a ``RomSaveState`` for *rom_id* (Rom must already exist for the FK)."""
    from domain.rom_save_state import RomSaveState

    with plugin._uow:
        plugin._uow.rom_save_states.save(
            rom_id,
            RomSaveState(files=files, last_sync_check_at=last_sync_check_at),
        )


def _seed_metadata(plugin, rom_id, *, cached_at, summary="", genres=(), app_id=None, platform_slug="snes"):
    """Seed cached metadata for *rom_id* in the shared UoW (Rom first for the FK)."""
    from domain.rom import Rom
    from domain.rom_metadata import RomMetadata

    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.sfc",
        shortcut_app_id=app_id if app_id is not None else 1000 + rom_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    meta = RomMetadata(
        summary=summary,
        genres=tuple(genres),
        companies=(),
        first_release_date=None,
        average_rating=None,
        game_modes=(),
        player_count="",
        cached_at=cached_at,
        steam_categories=(),
    )
    with plugin._uow:
        plugin._uow.roms.save(rom)
        plugin._uow.rom_metadata.save(rom_id, meta)


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    plugin._save_sync_service._loop = loop
    plugin._playtime_service._loop = loop


def _install_rom(plugin, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba"):
    """Helper: seed a ``RomInstall`` record (Rom must already exist for the FK)."""
    from domain.rom_install import RomInstall

    install_dir = tmp_path / "retrodeck" / "roms" / system
    with plugin._uow:
        plugin._uow.rom_installs.save(
            RomInstall(
                rom_id=rom_id,
                file_path=str(install_dir / file_name),
                rom_dir=None,
                platform_slug=system,
                system=system,
                installed_at="2025-01-01T00:00:00",
            )
        )


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
    """Test get_cached_game_detail when the ROM is bound in ``uow.roms``."""

    @pytest.mark.asyncio
    async def test_found_with_full_data(self, plugin, game_detail_service):
        """All data present: rom, install, save status, metadata, platform-name cache."""
        # _seed_metadata seeds the Rom (123, snes, app_id 99999) + its metadata.
        _seed_metadata(
            plugin,
            123,
            cached_at=100,
            summary="Classic SNES platformer",
            genres=("Platformer",),
            app_id=99999,
            platform_slug="snes",
        )
        _seed_platform_names(plugin, {"snes": "Super Nintendo"})
        _install_rom(plugin, plugin._tmp_path, rom_id=123, system="snes", file_name="smw.sfc")
        plugin.settings["save_sync_enabled"] = True
        _seed_save_state(
            plugin,
            123,
            files={"smw.srm": FileSyncState(last_sync_at="2025-01-01T00:00:00Z", last_sync_hash="abc123")},
            last_sync_check_at="2025-01-01T00:00:00Z",
        )

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["found"] is True
        assert result["rom_id"] == 123
        assert result["rom_name"] == "Game 123"
        assert result["platform_slug"] == "snes"
        assert result["platform_name"] == "Super Nintendo"
        assert result["installed"] is True
        assert result["rom_file"] == "smw.sfc"
        assert result["save_sync_enabled"] is True
        assert len(result["save_status"]["files"]) == 1
        assert result["save_status"]["files"][0]["filename"] == "smw.srm"
        assert result["save_status"]["files"][0]["status"] == "synced"
        assert result["save_status"]["last_sync_check_at"] == "2025-01-01T00:00:00Z"
        assert result["metadata"]["summary"] == "Classic SNES platformer"
        assert result["bios_status"] is None


class TestGetCachedGameDetailNotFound:
    """Test get_cached_game_detail when no ROM is bound to the app_id."""

    @pytest.mark.asyncio
    async def test_not_found(self, game_detail_service):
        """Unknown app_id returns found=False."""
        result = game_detail_service.get_cached_game_detail(12345)
        assert result == {"found": False}

    @pytest.mark.asyncio
    async def test_not_found_empty_registry(self, game_detail_service):
        """Empty roms table returns found=False."""
        result = game_detail_service.get_cached_game_detail(1)
        assert result == {"found": False}

    @pytest.mark.asyncio
    async def test_not_found_different_app_id(self, plugin, game_detail_service):
        """roms has entries but none match the requested app_id."""
        _seed_rom(plugin, 10, app_id=11111, name="Other Game", platform_slug="nes")
        result = game_detail_service.get_cached_game_detail(99999)
        assert result == {"found": False}


class TestGetCachedGameDetailPartialData:
    """Test with missing optional data (no save status, no metadata, etc.)."""

    @pytest.mark.asyncio
    async def test_no_save_status(self, plugin, game_detail_service):
        """No save state for this rom returns save_status=None."""
        _seed_rom(plugin, 10, app_id=50000, name="Zelda", platform_slug="snes")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["save_status"] is None

    @pytest.mark.asyncio
    async def test_no_metadata(self, plugin, game_detail_service):
        """No metadata cached returns metadata=None."""
        _seed_rom(plugin, 10, app_id=50000, name="Zelda", platform_slug="snes")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["metadata"] is None

    @pytest.mark.asyncio
    async def test_no_pending_conflicts_key(self, plugin, game_detail_service):
        """pending_conflicts is no longer in the response (conflicts are inline)."""
        _seed_rom(plugin, 10, app_id=50000, name="Zelda", platform_slug="snes")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert "pending_conflicts" not in result

    @pytest.mark.asyncio
    async def test_save_sync_disabled(self, plugin, game_detail_service):
        """save_sync_enabled reflects the setting."""
        _seed_rom(plugin, 10, app_id=50000, name="Zelda", platform_slug="snes")
        plugin.settings["save_sync_enabled"] = False
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["save_sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_empty_platform_slug_defaults_empty(self, plugin, game_detail_service):
        """A ROM with an empty platform_slug degrades platform_name to empty."""
        _seed_rom(plugin, 10, app_id=50000, name="", platform_slug="")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["rom_name"] == ""
        assert result["platform_slug"] == ""
        assert result["platform_name"] == ""

    @pytest.mark.asyncio
    async def test_platform_name_degrades_to_slug_when_cache_absent(self, plugin, game_detail_service):
        """No platform-name cache row → platform_name degrades to the slug."""
        _seed_rom(plugin, 10, app_id=50000, name="Zelda", platform_slug="snes")
        # No _seed_platform_names — the kv_config cache row is absent.
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["platform_slug"] == "snes"
        assert result["platform_name"] == "snes"


class TestGetCachedGameDetailInstalled:
    """Test installed vs not installed detection + rom_file resolution."""

    @pytest.mark.asyncio
    async def test_installed(self, plugin, game_detail_service):
        """ROM with a rom_installs row returns installed=True."""
        _seed_rom(plugin, 10, app_id=50000, name="Game", platform_slug="snes")
        _install_rom(plugin, plugin._tmp_path, rom_id=10, system="snes", file_name="game.sfc")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["installed"] is True
        assert result["rom_file"] == "game.sfc"

    @pytest.mark.asyncio
    async def test_not_installed(self, plugin, game_detail_service):
        """ROM without a rom_installs row returns installed=False, rom_file from fs_name."""
        _seed_rom(plugin, 10, app_id=50000, name="Game", platform_slug="snes", fs_name="game_10.sfc")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["installed"] is False
        # No install record → rom_file falls back to Rom.fs_name.
        assert result["rom_file"] == "game_10.sfc"


class TestGetCachedGameDetailConflictFiltering:
    """pending_conflicts was removed from get_cached_game_detail response."""

    @pytest.mark.asyncio
    async def test_no_pending_conflicts_in_response(self, plugin, game_detail_service):
        """pending_conflicts key is no longer in the response."""
        _seed_rom(plugin, 10, app_id=50000, name="Game A", platform_slug="snes")
        result = game_detail_service.get_cached_game_detail(50000)
        assert "pending_conflicts" not in result

    @pytest.mark.asyncio
    async def test_response_still_has_save_status(self, plugin, game_detail_service):
        """Response still includes save status fields."""
        _seed_rom(plugin, 10, app_id=50000, name="Game A", platform_slug="snes")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert "save_sync_enabled" in result

    @pytest.mark.asyncio
    async def test_app_id_as_string(self, plugin, game_detail_service):
        """app_id passed as string is handled correctly."""
        _seed_rom(plugin, 10, app_id=50000, name="Game", platform_slug="snes")
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
        _seed_rom(plugin, 42, app_id=50000, name="Pokemon", platform_slug="gba")
        # firmware cache is empty by default (None)
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["found"] is True
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_bios_status_from_populated_cache(self, plugin, game_detail_service, tmp_path):
        """Firmware cache populated → bios_status returned with cached_at."""
        from unittest.mock import patch

        _seed_rom(plugin, 42, app_id=50000, name="Pokemon", platform_slug="gba")
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

        plugin._firmware_service._core_info.active_core = ("mgba_libretro", "mGBA")
        plugin._firmware_service._core_info.available_cores = []
        with patch.object(plugin._firmware_service, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path))):
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
        """No platform_slug on the ROM → bios_status is None (skipped)."""
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="")
        result = game_detail_service.get_cached_game_detail(50000)
        assert result["bios_status"] is None

    @pytest.mark.asyncio
    async def test_bios_status_none_when_needs_bios_false(self, plugin, game_detail_service):
        """Cache populated but no firmware for platform → bios_status is None."""
        _seed_rom(plugin, 42, app_id=50000, name="Tetris", platform_slug="gb")
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
        """ROM with needs_bios=True returns the BIOS status dict + pre-computed level/label.

        The BIOS payload carries no core fields after #923 — core info is served via
        the dedicated ``get_platform_core_info`` path.
        """
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="gba")
        mock_check = AsyncMock(
            return_value={
                "needs_bios": True,
                "server_count": 3,
                "local_count": 1,
                "all_downloaded": False,
                "required_count": 2,
                "required_downloaded": 1,
                "files": [{"file_name": "gba_bios.bin", "downloaded": True}],
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
        # bios_level/bios_label are computed against the active core's required
        # counts (core-aware badge) by the BIOS checker, NOT a platform default.
        assert result["bios_level"] == "partial"
        assert result["bios_label"] == "1/2 required"

    @pytest.mark.asyncio
    async def test_badge_keys_off_active_core(self, plugin, game_detail_service):
        """The missing-BIOS badge is computed against the ACTIVE CORE's requirements (#923).

        Two cores for the same platform produce different ``required_count`` /
        ``required_downloaded`` from ``check_platform_bios`` (it filters by the
        active core). ``get_bios_status`` derives bios_level/label straight from
        those counts — so the badge follows the active core, not a platform default.
        """
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="gba")

        # gpSP requires gba_bios.bin and it is missing → missing badge.
        gpsp_payload = {
            "needs_bios": True,
            "server_count": 1,
            "local_count": 0,
            "all_downloaded": False,
            "required_count": 1,
            "required_downloaded": 0,
            "files": [{"file_name": "gba_bios.bin", "downloaded": False}],
        }
        game_detail_service._bios_checker.check_platform_bios = AsyncMock(return_value=gpsp_payload)
        result = await game_detail_service.get_bios_status(42)
        assert result["bios_level"] == "missing"
        assert result["bios_label"] == "Missing"

        # mGBA treats gba_bios.bin as optional → required_count 0 → no missing badge.
        mgba_payload = {
            "needs_bios": True,
            "server_count": 1,
            "local_count": 0,
            "all_downloaded": False,
            "required_count": 0,
            "required_downloaded": 0,
            "files": [{"file_name": "gba_bios.bin", "downloaded": False}],
        }
        game_detail_service._bios_checker.check_platform_bios = AsyncMock(return_value=mgba_payload)
        result = await game_detail_service.get_bios_status(42)
        assert result["bios_level"] == "ok"
        assert result["bios_label"] == "OK"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_bios_needed(self, plugin, game_detail_service):
        """ROM with needs_bios=False returns bios_status / level / label all None."""
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="gba")
        game_detail_service._bios_checker.check_platform_bios = AsyncMock(return_value={"needs_bios": False})

        result = await game_detail_service.get_bios_status(42)
        assert result["bios_status"] is None
        assert result["bios_level"] is None
        assert result["bios_label"] is None

    @pytest.mark.asyncio
    async def test_passes_resolved_per_game_core_to_bios_check(self, plugin, game_detail_service, active_core_resolver):
        """The per-game active core (resolved by rom_id) is threaded into the BIOS filter.

        The BIOS check no longer receives a ROM filename — game-detail resolves
        the active ``.so`` through ``ActiveCoreReader`` (which folds the per-game
        emulator_override pin) and passes the resolved core in, so the core-aware
        filter keys off the pin rather than a platform default.
        """
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="gba")
        active_core_resolver.per_rom[42] = ("gpsp_libretro", "gpSP")

        captured = {}

        async def capture_check(slug, active_core_so=None):
            captured["slug"] = slug
            captured["active_core_so"] = active_core_so
            return {"needs_bios": False}

        game_detail_service._bios_checker.check_platform_bios = capture_check

        await game_detail_service.get_bios_status(42)
        assert captured["slug"] == "gba"
        assert captured["active_core_so"] == "gpsp_libretro"
        assert active_core_resolver.calls == [42]

    @pytest.mark.asyncio
    async def test_bios_check_differs_by_per_game_override(self, plugin, game_detail_service, active_core_resolver):
        """RESULT-FLIP: two gba ROMs, one pinned to gpSP + one default, drive different BIOS results.

        gpSP requires ``gba_bios.bin`` → missing badge; the default core treats it
        as optional → ok. The badge flips on the per-game override alone, proving
        the resolved core (not a platform default) feeds the BIOS filter.
        """
        _seed_rom(plugin, 42, app_id=50000, name="Pinned", platform_slug="gba")
        _seed_rom(plugin, 43, app_id=50001, name="Default", platform_slug="gba")
        active_core_resolver.per_rom[42] = ("gpsp_libretro", "gpSP")
        # rom 43 falls through to the default (None, None) → system default.

        async def fake_check(slug, active_core_so=None):
            if active_core_so == "gpsp_libretro":
                return {
                    "needs_bios": True,
                    "server_count": 1,
                    "local_count": 0,
                    "all_downloaded": False,
                    "required_count": 1,
                    "required_downloaded": 0,
                    "files": [{"file_name": "gba_bios.bin", "downloaded": False}],
                }
            return {"needs_bios": False}

        game_detail_service._bios_checker.check_platform_bios = fake_check

        pinned = await game_detail_service.get_bios_status(42)
        plain = await game_detail_service.get_bios_status(43)

        assert pinned["bios_level"] == "missing"
        assert plain["bios_status"] is None
        assert active_core_resolver.calls == [42, 43]


class TestGetBiosStatusNotFound:
    """Test get_bios_status when ROM is not in registry."""

    @pytest.mark.asyncio
    async def test_unknown_rom_id(self, game_detail_service):
        """Unknown rom_id returns bios_status / level / label all None."""
        result = await game_detail_service.get_bios_status(999)
        assert result == {"bios_status": None, "bios_level": None, "bios_label": None}

    @pytest.mark.asyncio
    async def test_no_platform_slug(self, plugin, game_detail_service):
        """ROM without platform_slug returns bios_status / level / label all None."""
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="")
        result = await game_detail_service.get_bios_status(42)
        assert result == {"bios_status": None, "bios_level": None, "bios_label": None}

    @pytest.mark.asyncio
    async def test_firmware_error_returns_none(self, plugin, game_detail_service):
        """Firmware service exception returns bios_status / level / label all None."""
        _seed_rom(plugin, 42, app_id=50000, name="Game", platform_slug="gba")
        game_detail_service._bios_checker.check_platform_bios = AsyncMock(side_effect=Exception("fail"))

        result = await game_detail_service.get_bios_status(42)
        assert result == {"bios_status": None, "bios_level": None, "bios_label": None}


class TestGetCachedGameDetailSaveStatusConflicts:
    @pytest.mark.asyncio
    async def test_save_status_includes_empty_conflicts(self, plugin, game_detail_service):
        """Lightweight save_status should include an empty conflicts list."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        plugin.settings["save_sync_enabled"] = True
        _seed_save_state(
            plugin,
            42,
            files={"test.srm": FileSyncState(last_sync_hash="abc", last_sync_at="2026-01-01T00:00:00Z")},
            last_sync_check_at="2026-01-01T00:00:00Z",
        )
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_status"] is not None
        assert "conflicts" in result["save_status"]
        assert result["save_status"]["conflicts"] == []

    @pytest.mark.asyncio
    async def test_save_status_empty_files_unknown_status(self, plugin, game_detail_service):
        """A save state with an empty files{} returns an empty files list."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        _seed_save_state(plugin, 42, files={}, last_sync_check_at=None)
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_status"] is not None
        assert result["save_status"]["files"] == []
        assert result["save_status"]["last_sync_check_at"] is None


class TestComputedFields:
    """Test bios_level, bios_label, save_sync_display in response."""

    @pytest.mark.asyncio
    async def test_bios_level_and_label_when_bios_present(self, plugin, game_detail_service, tmp_path):
        """When BIOS data is cached, bios_level and bios_label should be set."""
        from unittest.mock import patch

        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
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

        plugin._firmware_service._core_info.active_core = ("mgba_libretro", "mGBA")
        plugin._firmware_service._core_info.available_cores = []
        with patch.object(
            plugin._firmware_service, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "nonexistent"))
        ):
            result = game_detail_service.get_cached_game_detail(99999)

        assert result["bios_level"] is not None
        assert result["bios_label"] is not None
        # Files not downloaded → missing or partial
        assert result["bios_level"] in ("missing", "partial", "ok")
        assert isinstance(result["bios_label"], str)

    @pytest.mark.asyncio
    async def test_bios_level_none_when_no_bios(self, plugin, game_detail_service):
        """When no BIOS data (cache empty), bios_level and bios_label should be None."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        # _firmware_cache is None by default
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["bios_level"] is None
        assert result["bios_label"] is None

    @pytest.mark.asyncio
    async def test_bios_level_ok_when_all_downloaded(self, plugin, game_detail_service, tmp_path):
        """When all required BIOS files are present, bios_level should be 'ok'."""
        from unittest.mock import patch

        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
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

        plugin._firmware_service._core_info.active_core = ("mgba_libretro", "mGBA")
        plugin._firmware_service._core_info.available_cores = []
        with patch.object(plugin._firmware_service, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            result = game_detail_service.get_cached_game_detail(99999)

        assert result["bios_level"] == "ok"

    @pytest.mark.asyncio
    async def test_save_sync_display_with_saves(self, plugin, game_detail_service):
        """When save data exists, save_sync_display is the typed dataclass payload."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        plugin.settings["save_sync_enabled"] = True
        _seed_save_state(
            plugin,
            42,
            files={"test.srm": FileSyncState(last_sync_hash="abc", last_sync_at="2026-01-01T00:00:00Z")},
            last_sync_check_at="2026-01-01T00:00:00Z",
        )
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_sync_display"] is not None
        assert result["save_sync_display"]["status"] == "synced"
        # Synced + recorded check → backend leaves label None for frontend formatTimeAgo.
        assert result["save_sync_display"]["label"] is None
        assert result["save_sync_display"]["last_sync_check_at"] == "2026-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_save_sync_display_none_when_no_saves(self, plugin, game_detail_service):
        """When no save data, save_sync_display should be None."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        result = game_detail_service.get_cached_game_detail(99999)
        assert result["save_sync_display"] is None


class TestAchievementSummaryCachedAt:
    """Test that achievement_summary includes cached_at from progress cache."""

    @pytest.mark.asyncio
    async def test_achievement_summary_includes_cached_at(self, plugin, game_detail_service, clock):
        """When progress is cached, achievement_summary includes cached_at timestamp."""
        cached_time = clock.time() - 600  # 10 minutes ago
        _seed_rom(plugin, 42, app_id=99999, name="Sonic", platform_slug="genesis", ra_id=555)
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
        _seed_rom(plugin, 42, app_id=99999, name="Sonic", platform_slug="genesis", ra_id=555)
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
        _seed_rom(plugin, 42, app_id=99999, name="Sonic", platform_slug="genesis", ra_id=555)

        result = game_detail_service.get_cached_game_detail(99999)

        assert result["achievement_summary"] is None

    @pytest.mark.asyncio
    async def test_no_achievement_summary_without_cached_progress(self, plugin, game_detail_service, clock):
        """With RA username but no cached progress, achievement_summary is None."""
        _seed_rom(plugin, 42, app_id=99999, name="Sonic", platform_slug="genesis", ra_id=555)
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
        _seed_metadata(plugin, 42, cached_at=clock.time(), app_id=99999, platform_slug="gba")
        result = game_detail_service.get_cached_game_detail(99999)
        assert "stale_fields" in result
        assert "metadata" not in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_metadata_stale_when_old(self, plugin, game_detail_service, clock):
        """Metadata older than 7 days should appear in stale_fields."""
        _seed_metadata(plugin, 42, cached_at=clock.time() - 8 * 24 * 3600, app_id=99999, platform_slug="gba")
        result = game_detail_service.get_cached_game_detail(99999)
        assert "metadata" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_metadata_stale_when_missing(self, plugin, game_detail_service):
        """Missing metadata should appear in stale_fields."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        result = game_detail_service.get_cached_game_detail(99999)
        assert "metadata" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_bios_stale_when_old(self, plugin, game_detail_service):
        """BIOS older than 1 hour should appear in stale_fields."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba")
        result = game_detail_service.get_cached_game_detail(99999)
        # With no BIOS cache, bios_status is None → bios should be stale
        assert "bios" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_achievements_stale_when_missing(self, plugin, game_detail_service):
        """Missing achievement progress should appear in stale_fields when ra_id is set."""
        _seed_rom(plugin, 42, app_id=99999, name="Test", platform_slug="gba", ra_id=123)
        result = game_detail_service.get_cached_game_detail(99999)
        assert "achievements" in result["stale_fields"]

    @pytest.mark.asyncio
    async def test_not_found_has_no_stale_fields(self, game_detail_service):
        """When ROM not found, response has no stale_fields."""
        result = game_detail_service.get_cached_game_detail(99999)
        assert "stale_fields" not in result
