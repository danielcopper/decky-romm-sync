"""Tests for the bootstrap composition root."""

import asyncio
import logging
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from bootstrap import (
    AdapterBundle,
    BootstrapResult,
    CallbackBundle,
    RuntimeBundle,
    StateBundle,
    WiringConfig,
    bootstrap,
    wire_services,
)
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore
from fakes.fake_download_file_store import FakeDownloadFileStore
from fakes.fake_download_queue_store import FakeDownloadQueueStore
from fakes.fake_firmware_cache_persister import FakeFirmwareCachePersister
from fakes.fake_firmware_file_store import FakeFirmwareFileStore
from fakes.fake_hostname_reader import FakeHostnameReader
from fakes.fake_migration_file_store import FakeMigrationFileStore
from fakes.fake_path_exists_reader import FakePathExistsReader
from fakes.fake_plugin_metadata_reader import FakePluginMetadataReader
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_rom_file_store import FakeRomFileStore
from fakes.fake_save_file_store import FakeSaveFileStore
from fakes.fake_sgdb_artwork_cache import FakeSgdbArtworkCache
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen
from models.state import ShortcutRegistryEntry, make_default_plugin_state

from adapters.metadata_cache_store import MetadataCacheStoreAdapter
from adapters.registry_store import RegistryStoreAdapter
from adapters.retrodeck_paths import RetroDeckPathsAdapter
from adapters.romm.http import RommHttpAdapter
from adapters.romm.romm_api import RommApiAdapter
from adapters.steam_config import SteamConfigAdapter
from services.achievements import AchievementsService
from services.cores import CoreService
from services.downloads import DownloadService
from services.firmware import FirmwareService
from services.library import LibraryService
from services.metadata import MetadataService
from services.playtime import PlaytimeService
from services.saves import SaveService
from services.steamgrid import SteamGridService


def _bootstrap_for(tmp_path) -> BootstrapResult:
    return bootstrap(
        settings_dir=str(tmp_path / "settings"),
        runtime_dir=str(tmp_path / "runtime"),
        plugin_dir=str(tmp_path / "plugin"),
        user_home=str(tmp_path / "home"),
        logger=logging.getLogger("test"),
    )


class TestBootstrap:
    def test_returns_typed_bootstrap_result(self, tmp_path):
        result = _bootstrap_for(tmp_path)
        assert isinstance(result, BootstrapResult)

    def test_http_adapter_shares_settings_reference(self, tmp_path):
        """RommHttpAdapter binds the same dict the StateBundle exposes."""
        result = _bootstrap_for(tmp_path)
        # Mutate the live settings dict — http_adapter holds the same ref.
        result.stores.settings["romm_url"] = "http://changed.com"
        assert result.adapters.http_adapter._settings["romm_url"] == "http://changed.com"
        assert result.adapters.http_adapter._settings is result.stores.settings

    def test_returns_http_adapter(self, tmp_path):
        result = _bootstrap_for(tmp_path)
        assert isinstance(result.adapters.http_adapter, RommHttpAdapter)

    def test_returns_steam_config(self, tmp_path):
        result = _bootstrap_for(tmp_path)
        assert isinstance(result.adapters.steam_config, SteamConfigAdapter)

    def test_returns_romm_api(self, tmp_path):
        result = _bootstrap_for(tmp_path)
        assert isinstance(result.adapters.romm_api, RommApiAdapter)

    def test_returns_retrodeck_paths_adapter(self, tmp_path):
        """Bootstrap instantiates the RetroDECK paths adapter for the callbacks bundle."""
        result = _bootstrap_for(tmp_path)
        assert isinstance(result.callbacks.retrodeck_paths, RetroDeckPathsAdapter)

    def test_returns_core_info_provider_on_adapters(self, tmp_path):
        """``core_info_provider`` (CoreResolver) is bundled with adapters, not callbacks.

        Lock-in for #671 — stateful adapter sits next to ``gamelist_editor``
        in :class:`AdapterBundle`. :class:`CallbackBundle` carries only
        provider callables and persisters.
        """
        result = _bootstrap_for(tmp_path)
        # AdapterBundle exposes the stateful CoreResolver.
        assert result.adapters.core_info_provider is not None
        # CallbackBundle no longer carries it.
        assert not hasattr(result.callbacks, "core_info_provider")

    def test_persistence_loaded_with_default_state_factory(self, tmp_path):
        """First-run state contains the keys ``_default_state()`` ships.

        Regression for #671: ``_default_state`` must be a factory, not a
        shared module-level dict — services mutate ``state`` in place.
        """
        result = _bootstrap_for(tmp_path)
        state = result.stores.state
        # Inner containers were independently constructed; mutating them
        # does not bleed into a future bootstrap call's state.
        state["shortcut_registry"]["sentinel"] = cast("ShortcutRegistryEntry", {"app_id": 1})
        state["sync_stats"]["platforms"] = 42

        second = _bootstrap_for(tmp_path)
        assert second.stores.state["shortcut_registry"] == {}
        assert second.stores.state["sync_stats"] == {"platforms": 0, "roms": 0}

    def test_handles_debug_logger_exposed(self, tmp_path):
        """``BootstrapHandles.debug_logger`` is the same instance the CallbackBundle wires."""
        result = _bootstrap_for(tmp_path)
        assert result.handles.debug_logger is result.callbacks.log_debug

    def test_runtime_adapters_bundle_populated(self, tmp_path):
        """Bootstrap owns instantiation of clock/uuid/sleeper/hostname for ``main.py`` to compose RuntimeBundle."""
        result = _bootstrap_for(tmp_path)
        assert result.runtime_adapters.clock is not None
        assert result.runtime_adapters.uuid_gen is not None
        assert result.runtime_adapters.sleeper is not None
        assert result.runtime_adapters.hostname_provider is not None

    def test_user_agent_threaded_to_romm_http_adapter(self, tmp_path):
        """Bootstrap reads ``package.json`` once and threads the resulting
        ``decky-romm-sync/<version>`` string to ``RommHttpAdapter`` (#249, #719).

        Without a User-Agent, Cloudflare Bot Fight Mode 403s the default
        ``Python-urllib`` UA before the request reaches self-hosted RomM
        behind a tunnel.
        """
        import json

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"version": "1.2.3"}))
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(plugin_dir),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
        )
        assert result.adapters.http_adapter._user_agent == "decky-romm-sync/1.2.3"

    def test_user_agent_threaded_to_steamgriddb_adapter(self, tmp_path):
        """Bootstrap threads the same ``decky-romm-sync/<version>`` UA into
        ``SteamGridDbAdapter`` so SGDB sees a non-default UA on every site
        (#719). SGDB rejects ``Python-urllib`` with 403.
        """
        import json

        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text(json.dumps({"version": "1.2.3"}))
        result = bootstrap(
            settings_dir=str(tmp_path / "settings"),
            runtime_dir=str(tmp_path / "runtime"),
            plugin_dir=str(plugin_dir),
            user_home=str(tmp_path / "home"),
            logger=logging.getLogger("test"),
        )
        assert result.adapters.sgdb_adapter._user_agent == "decky-romm-sync/1.2.3"

    def test_user_agent_falls_back_when_package_json_missing(self, tmp_path):
        """When ``package.json`` is absent, the adapter's documented
        fallback (``0.0.0``) feeds into the UA string."""
        result = _bootstrap_for(tmp_path)
        assert result.adapters.http_adapter._user_agent == "decky-romm-sync/0.0.0"
        assert result.adapters.sgdb_adapter._user_agent == "decky-romm-sync/0.0.0"


class TestWireServices:
    def _make_deps(self, tmp_path):
        logger = logging.getLogger("test_wire")
        settings = {}
        http_adapter = MagicMock(spec=RommHttpAdapter)
        steam_config = SteamConfigAdapter(user_home=str(tmp_path), logger=logger)
        state = make_default_plugin_state()
        metadata_cache: dict = {}
        romm_api = MagicMock(spec=RommApiAdapter)
        return {
            "http_adapter": http_adapter,
            "romm_api": romm_api,
            "steam_config": steam_config,
            "sgdb_adapter": MagicMock(),
            "cover_art_file_store": FakeCoverArtFileStore(),
            "sgdb_artwork_cache": FakeSgdbArtworkCache(),
            "download_file_store": FakeDownloadFileStore(),
            "download_queue": FakeDownloadQueueStore(),
            "firmware_file_store": FakeFirmwareFileStore(),
            "migration_file_store": FakeMigrationFileStore(),
            "rom_file_store": FakeRomFileStore(),
            "save_file_store": FakeSaveFileStore(),
            "gamelist_editor": MagicMock(),
            "path_probe": FakePathExistsReader(),
            "state": state,
            "settings": settings,
            "metadata_cache": metadata_cache,
            "save_sync_state": {"saves": {}, "playtime": {}, "settings": {}},
            "loop": asyncio.new_event_loop(),
            "logger": logger,
            "plugin_dir": str(tmp_path / "plugin"),
            "runtime_dir": str(tmp_path / "runtime"),
            "emit": AsyncMock(),
            "clock": FakeClock(),
            "uuid_gen": FakeUuidGen(),
            "sleeper": FakeSleeper(),
            "hostname_provider": FakeHostnameReader(),
            "min_required_version": (4, 8, 1),
            "retrodeck_paths": FakeRetroDeckPaths(
                saves=str(tmp_path / "saves"),
                roms=str(tmp_path / "retrodeck" / "roms"),
                bios=str(tmp_path / "retrodeck" / "bios"),
                home=str(tmp_path / "retrodeck"),
            ),
            "get_retroarch_save_sorting": MagicMock(return_value=(True, False)),
            "get_core_name": MagicMock(return_value="Snes9x"),
            "state_persister": MagicMock(),
            "settings_persister": MagicMock(),
            "metadata_cache_persister": MagicMock(),
            "firmware_cache_persister": FakeFirmwareCachePersister(),
            "core_info_provider": FakeCoreInfoProvider(),
            "save_sync_state_persister": MagicMock(load=MagicMock(return_value=None), save=MagicMock()),
            "registry_store": RegistryStoreAdapter(state=state, logger=logger),
            "metadata_store": MetadataCacheStoreAdapter(metadata_cache=metadata_cache),
            "log_debug": MagicMock(),
            "plugin_metadata": FakePluginMetadataReader(version="0.14.0"),
        }

    @staticmethod
    def _make_config(deps: dict) -> WiringConfig:
        """Build a WiringConfig from the flat deps dict produced by ``_make_deps``."""
        return WiringConfig(
            adapters=AdapterBundle(
                http_adapter=deps["http_adapter"],
                romm_api=deps["romm_api"],
                steam_config=deps["steam_config"],
                sgdb_adapter=deps["sgdb_adapter"],
                cover_art_file_store=deps["cover_art_file_store"],
                sgdb_artwork_cache=deps["sgdb_artwork_cache"],
                download_file_store=deps["download_file_store"],
                download_queue=deps["download_queue"],
                firmware_file_store=deps["firmware_file_store"],
                migration_file_store=deps["migration_file_store"],
                rom_file_store=deps["rom_file_store"],
                save_file_store=deps["save_file_store"],
                gamelist_editor=deps["gamelist_editor"],
                path_probe=deps["path_probe"],
                core_info_provider=deps["core_info_provider"],
            ),
            stores=StateBundle(
                state=deps["state"],
                settings=deps["settings"],
                metadata_cache=deps["metadata_cache"],
                save_sync_state=deps["save_sync_state"],
            ),
            runtime=RuntimeBundle(
                loop=deps["loop"],
                logger=deps["logger"],
                plugin_dir=deps["plugin_dir"],
                runtime_dir=deps["runtime_dir"],
                emit=deps["emit"],
                clock=deps["clock"],
                uuid_gen=deps["uuid_gen"],
                sleeper=deps["sleeper"],
                hostname_provider=deps["hostname_provider"],
            ),
            callbacks=CallbackBundle(
                retrodeck_paths=deps["retrodeck_paths"],
                get_retroarch_save_sorting=deps["get_retroarch_save_sorting"],
                get_core_name=deps["get_core_name"],
                state_persister=deps["state_persister"],
                settings_persister=deps["settings_persister"],
                metadata_cache_persister=deps["metadata_cache_persister"],
                firmware_cache_persister=deps["firmware_cache_persister"],
                save_sync_state_persister=deps["save_sync_state_persister"],
                registry_store=deps["registry_store"],
                metadata_store=deps["metadata_store"],
                log_debug=deps["log_debug"],
                plugin_metadata=deps["plugin_metadata"],
            ),
            min_required_version=deps["min_required_version"],
        )

    def test_returns_all_services(self, tmp_path):
        deps = self._make_deps(tmp_path)
        result = wire_services(self._make_config(deps))
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
        result = wire_services(self._make_config(deps))
        # download_service and sync_service should share the same state dict
        assert result["download_service"]._state is deps["state"]
        assert result["sync_service"]._state is deps["state"]
        deps["loop"].close()

    def test_returns_expected_services(self, tmp_path):
        deps = self._make_deps(tmp_path)
        result = wire_services(self._make_config(deps))
        assert len(result) == 19
        assert "migration_service" in result
        assert "game_detail_service" in result
        assert "rom_removal_service" in result
        assert "settings_service" in result
        assert "core_service" in result
        assert isinstance(result["core_service"], CoreService)
        assert "connection_service" in result
        assert "startup_healing_service" in result
        assert "launch_gate_service" in result
        assert "session_lifecycle_service" in result
        deps["loop"].close()

    def test_pending_sync_binding_observes_library_rebinds(self, tmp_path):
        """ArtworkService/SgdbService see live LibraryService._pending_sync rebinds.

        Regression for #349: the bootstrap binding must defer the read so
        post-bind reassignments of ``_pending_sync`` (e.g., line 417 of
        library.py after a sync diff) are visible to consumers.
        """
        deps = self._make_deps(tmp_path)
        result = wire_services(self._make_config(deps))
        sync_service = result["sync_service"]
        artwork_service = result["artwork_service"]
        sgdb_service = result["sgdb_service"]

        # Producer rebinds _pending_sync to a fresh dict (mirrors sync_apply_delta).
        sync_service._pending_sync = {42: {"name": "Game", "platform_name": "N64"}}

        assert artwork_service._get_pending_sync() == {42: {"name": "Game", "platform_name": "N64"}}
        assert sgdb_service._get_pending_sync() == {42: {"name": "Game", "platform_name": "N64"}}
        deps["loop"].close()

    def test_bios_files_index_binding_observes_firmware_rebinds(self, tmp_path):
        """MigrationService sees post-load reassignments of bios_files_index.

        Regression for #349: ``firmware_service.load_bios_registry()`` rebinds
        ``_bios_files_index`` to a fresh dict each call; the binding must
        re-resolve the property on every read.
        """
        deps = self._make_deps(tmp_path)
        result = wire_services(self._make_config(deps))
        firmware_service = result["firmware_service"]
        migration_service = result["migration_service"]

        # Re-loading reassigns _bios_files_index; mutate the new dict and
        # confirm migration's deferred-read picks up the change.
        firmware_service.load_bios_registry()
        firmware_service._bios_files_index["scph5501.bin"] = {
            "platform": "psx",
            "description": "PS1 BIOS",
        }

        assert "scph5501.bin" in migration_service._get_bios_files_index()
        deps["loop"].close()

    def test_migration_service_receives_get_core_name(self, tmp_path):
        """MigrationService must receive the get_core_name callback from wire_services."""
        deps = self._make_deps(tmp_path)
        get_core_name_mock = deps["get_core_name"]
        result = wire_services(self._make_config(deps))
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
        result = wire_services(self._make_config(deps))
        save_sync_service = result["save_sync_service"]
        assert save_sync_service._rom_info._get_core_name is get_core_name_mock
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
        result = wire_services(self._make_config(deps))
        save_sync_service = result["save_sync_service"]
        migration_service = result["migration_service"]
        # Bound method equality: same function + same bound instance.
        # ``is`` fails because Python creates a fresh bound method object
        # on each attribute access.
        # detect_sort_change is dispatched by the sync_engine sub-service; the
        # SaveServiceConfig.detect_sort_change wiring threads through to it.
        assert save_sync_service._sync_engine._detect_sort_change == migration_service.detect_save_sort_change
        # Also check it's the actual migration instance, not some other.
        assert save_sync_service._sync_engine._detect_sort_change.__self__ is migration_service  # type: ignore[union-attr]
        deps["loop"].close()

    def test_save_sync_and_migration_share_state_reference(self, tmp_path):
        """Regression test for #238: SaveService and MigrationService must
        observe the same state dict by reference.

        Without shared state, ``detect_save_sort_change`` would mutate
        MigrationService's local copy while SaveService reads its own
        stale copy — defeating the detect-first invariant.
        """
        deps = self._make_deps(tmp_path)
        result = wire_services(self._make_config(deps))
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
        result = wire_services(self._make_config(deps))
        save_sync_service = result["save_sync_service"]
        migration_service = result["migration_service"]
        # is_retrodeck_migration_pending is consumed by the sync_engine sub-service.
        assert save_sync_service._sync_engine._is_retrodeck_migration_pending == (
            migration_service.is_retrodeck_migration_pending
        )
        assert save_sync_service._sync_engine._is_retrodeck_migration_pending.__self__ is migration_service  # type: ignore[union-attr]
        deps["loop"].close()

    def test_download_service_receives_is_retrodeck_migration_pending(self, tmp_path):
        """Regression test for #251: DownloadService must receive the bound
        ``migration_service.is_retrodeck_migration_pending`` callback so
        the download poll loop pauses while a migration is pending."""
        deps = self._make_deps(tmp_path)
        result = wire_services(self._make_config(deps))
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
        # The default mock returns (True, False); no prior state seeded
        # (the factory default for ``save_sort_settings`` is ``None``).
        assert deps["state"]["save_sort_settings"] is None
        result = wire_services(self._make_config(deps))
        save_sync_service = result["save_sync_service"]

        # Invoke the bound detect callback SaveService received.
        save_sync_service._sync_engine._detect_sort_change()  # type: ignore[misc]

        # State now has the current sort settings written through the
        # shared dict — SaveService will read this on its next
        # _get_rom_save_info call.
        assert deps["state"]["save_sort_settings"] == {
            "sort_by_content": True,
            "sort_by_core": False,
        }
        deps["loop"].close()
