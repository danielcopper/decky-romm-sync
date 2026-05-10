"""Tests for the bootstrap composition root."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from bootstrap import WiringConfig, bootstrap, wire_services
from conftest import FakeFirmwareCachePersister
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.persistence import PersistenceAdapter
from adapters.retroarch_config import RetroArchConfigAdapter
from adapters.retroarch_core_info import RetroArchCoreInfoAdapter
from adapters.retrodeck_paths import RetroDeckPathsAdapter
from adapters.romm.http import RommHttpAdapter
from adapters.romm.romm_api import RommApi
from adapters.steam_config import SteamConfigAdapter
from services.achievements import AchievementsService
from services.downloads import DownloadService
from services.firmware import FirmwareService
from services.library import LibraryService
from services.metadata import MetadataService
from services.playtime import PlaytimeService
from services.saves import SaveService
from services.steamgrid import SteamGridService


class TestBootstrap:
    def test_returns_persistence_adapter(self, tmp_path):
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(tmp_path / "plugin"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings={},
        )
        assert "persistence" in result
        assert isinstance(result["persistence"], PersistenceAdapter)

    def test_returns_http_adapter(self, tmp_path):
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(tmp_path / "plugin"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings={},
        )
        assert "http_adapter" in result
        assert isinstance(result["http_adapter"], RommHttpAdapter)

    def test_http_adapter_shares_settings_reference(self, tmp_path):
        settings = {"romm_url": "http://example.com"}
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(tmp_path / "plugin"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings=settings,
        )
        # Mutate original — client should see the change
        settings["romm_url"] = "http://changed.com"
        assert result["http_adapter"]._settings["romm_url"] == "http://changed.com"

    def test_persistence_has_correct_paths(self, tmp_path):
        settings_dir = str(tmp_path / "s")
        runtime_dir = str(tmp_path / "r")
        result = bootstrap(
            settings_dir=settings_dir,
            runtime_dir=runtime_dir,
            plugin_dir=str(tmp_path / "p"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings={},
        )
        assert result["persistence"]._settings_dir == settings_dir
        assert result["persistence"]._runtime_dir == runtime_dir

    def test_returns_steam_config(self, tmp_path):
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(tmp_path / "plugin"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings={},
        )
        assert "steam_config" in result
        assert isinstance(result["steam_config"], SteamConfigAdapter)

    def test_returns_romm_api(self, tmp_path):
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(tmp_path / "plugin"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings={},
        )
        assert "romm_api" in result
        assert isinstance(result["romm_api"], RommApi)

    def test_returns_split_retrodeck_adapters(self, tmp_path):
        """Bootstrap instantiates all three split adapters (paths, cfg, core_info)."""
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(tmp_path / "plugin"),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
            settings={},
        )
        assert isinstance(result["retrodeck_paths"], RetroDeckPathsAdapter)
        assert isinstance(result["retroarch_config"], RetroArchConfigAdapter)
        assert isinstance(result["retroarch_core_info"], RetroArchCoreInfoAdapter)
        # Old bundled key must no longer be present.
        assert "retrodeck_config" not in result


class TestWireServices:
    def _make_deps(self, tmp_path):
        logger = logging.getLogger("test_wire")
        settings = {}
        http_adapter = MagicMock(spec=RommHttpAdapter)
        steam_config = SteamConfigAdapter(user_home=str(tmp_path), logger=logger)
        state = {
            "shortcut_registry": {},
            "installed_roms": {},
            "last_sync": None,
            "sync_stats": {},
            "downloaded_bios": {},
        }
        romm_api = MagicMock(spec=RommApi)
        return {
            "http_adapter": http_adapter,
            "romm_api": romm_api,
            "steam_config": steam_config,
            "sgdb_adapter": MagicMock(),
            "state": state,
            "settings": settings,
            "metadata_cache": {},
            "save_sync_state": {"saves": {}, "playtime": {}, "settings": {}},
            "loop": asyncio.new_event_loop(),
            "logger": logger,
            "plugin_dir": str(tmp_path / "plugin"),
            "runtime_dir": str(tmp_path / "runtime"),
            "emit": AsyncMock(),
            "clock": FakeClock(),
            "uuid_gen": FakeUuidGen(),
            "sleeper": FakeSleeper(),
            "get_saves_path": MagicMock(return_value=str(tmp_path / "saves")),
            "get_roms_path": MagicMock(return_value=str(tmp_path / "retrodeck" / "roms")),
            "get_bios_path": MagicMock(return_value=str(tmp_path / "retrodeck" / "bios")),
            "get_retrodeck_home": MagicMock(return_value=str(tmp_path / "retrodeck")),
            "get_retroarch_save_sorting": MagicMock(return_value=(True, False)),
            "get_core_name": MagicMock(return_value="Snes9x"),
            "save_state": MagicMock(),
            "save_settings_to_disk": MagicMock(),
            "save_metadata_cache": MagicMock(),
            "firmware_cache_persister": FakeFirmwareCachePersister(),
            "save_sync_state_persister": MagicMock(load=MagicMock(return_value=None), save=MagicMock()),
            "log_debug": MagicMock(),
        }

    def test_returns_all_services(self, tmp_path):
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        assert isinstance(result["save_sync_service"], SaveService)
        assert isinstance(result["playtime_service"], PlaytimeService)
        assert isinstance(result["sync_service"], LibraryService)
        assert isinstance(result["download_service"], DownloadService)
        assert isinstance(result["firmware_service"], FirmwareService)
        assert isinstance(result["sgdb_service"], SteamGridService)
        assert isinstance(result["metadata_service"], MetadataService)
        assert isinstance(result["achievements_service"], AchievementsService)
        deps["loop"].close()

    def test_services_share_state_reference(self, tmp_path):
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        # download_service and sync_service should share the same state dict
        assert result["download_service"]._state is deps["state"]
        assert result["sync_service"]._state is deps["state"]
        deps["loop"].close()

    def test_returns_expected_services(self, tmp_path):
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        assert len(result) == 13
        assert "migration_service" in result
        assert "game_detail_service" in result
        assert "rom_removal_service" in result
        deps["loop"].close()

    def test_migration_service_receives_get_core_name(self, tmp_path):
        """MigrationService must receive the get_core_name callback from wire_services."""
        deps = self._make_deps(tmp_path)
        get_core_name_mock = deps["get_core_name"]
        result = wire_services(WiringConfig(**deps))
        migration_service = result["migration_service"]
        # Callback is stored as _get_core_name on the service
        assert migration_service._get_core_name is get_core_name_mock
        deps["loop"].close()

    def test_save_sync_service_receives_get_core_name(self, tmp_path):
        """Regression test for #232: SaveService must receive get_core_name.

        Without this callback, SaveService cannot resolve the RetroArch
        .info ``corename`` when ``sort_by_core`` is active, and silently
        builds save paths that RetroArch will not read.
        """
        deps = self._make_deps(tmp_path)
        get_core_name_mock = deps["get_core_name"]
        result = wire_services(WiringConfig(**deps))
        save_sync_service = result["save_sync_service"]
        assert save_sync_service._get_core_name is get_core_name_mock
        deps["loop"].close()

    def test_save_sync_service_receives_migration_detect_sort_change(self, tmp_path):
        """Regression test for #238: SaveService must receive
        ``migration_service.detect_save_sort_change`` via its
        ``detect_sort_change`` constructor parameter.

        Without this wiring, post_exit_sync could run with stale sort
        state and download stale server content to the wrong layout,
        causing real user progress to be destroyed during the next
        migration step.
        """
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        save_sync_service = result["save_sync_service"]
        migration_service = result["migration_service"]
        # Bound method equality: same function + same bound instance.
        # ``is`` fails because Python creates a fresh bound method object
        # on each attribute access.
        assert save_sync_service._detect_sort_change == migration_service.detect_save_sort_change
        # Also check it's the actual migration instance, not some other.
        assert save_sync_service._detect_sort_change.__self__ is migration_service  # type: ignore[union-attr]
        deps["loop"].close()

    def test_save_sync_and_migration_share_state_reference(self, tmp_path):
        """Regression test for #238: SaveService and MigrationService must
        observe the same state dict by reference.

        Without shared state, ``detect_save_sort_change`` would mutate
        MigrationService's local copy while SaveService reads its own
        stale copy — defeating the detect-first invariant.
        """
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        save_sync_service = result["save_sync_service"]
        migration_service = result["migration_service"]
        assert save_sync_service._state is deps["state"]
        assert migration_service._state is deps["state"]
        assert save_sync_service._state is migration_service._state
        deps["loop"].close()

    def test_save_service_receives_is_retrodeck_migration_pending(self, tmp_path):
        """Regression test for #251: SaveService must receive the bound
        ``migration_service.is_retrodeck_migration_pending`` callback so
        pre_launch_sync / post_exit_sync can short-circuit while the user
        still has files at the previous RetroDECK home."""
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        save_sync_service = result["save_sync_service"]
        migration_service = result["migration_service"]
        assert save_sync_service._is_retrodeck_migration_pending == migration_service.is_retrodeck_migration_pending
        assert save_sync_service._is_retrodeck_migration_pending.__self__ is migration_service  # type: ignore[union-attr]
        deps["loop"].close()

    def test_download_service_receives_is_retrodeck_migration_pending(self, tmp_path):
        """Regression test for #251: DownloadService must receive the bound
        ``migration_service.is_retrodeck_migration_pending`` callback so
        the download poll loop pauses while a migration is pending."""
        deps = self._make_deps(tmp_path)
        result = wire_services(WiringConfig(**deps))
        download_service = result["download_service"]
        migration_service = result["migration_service"]
        assert download_service._is_retrodeck_migration_pending == migration_service.is_retrodeck_migration_pending
        assert download_service._is_retrodeck_migration_pending.__self__ is migration_service  # type: ignore[union-attr]
        deps["loop"].close()

    def test_save_sync_detect_sort_change_mutates_shared_state(self, tmp_path):
        """Functional check for #238: invoking the wired detect callback
        from SaveService updates state that SaveService subsequently
        reads.

        The wired callback writes current sort settings into
        ``_state["save_sort_settings"]`` on first run. SaveService and
        MigrationService must see that write through the same live dict.
        """
        deps = self._make_deps(tmp_path)
        # The default mock returns (True, False); no prior state seeded.
        assert "save_sort_settings" not in deps["state"]
        result = wire_services(WiringConfig(**deps))
        save_sync_service = result["save_sync_service"]

        # Invoke the bound detect callback SaveService received.
        save_sync_service._detect_sort_change()  # type: ignore[misc]

        # State now has the current sort settings written through the
        # shared dict — SaveService will read this on its next
        # _get_rom_save_info call.
        assert deps["state"]["save_sort_settings"] == {
            "sort_by_content": True,
            "sort_by_core": False,
        }
        deps["loop"].close()
