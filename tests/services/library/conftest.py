"""Shared fixtures for the LibraryService sub-service test files.

Wires a ``Plugin`` instance with the full LibraryService composition
(fetcher + orchestrator + reporter) plus the peer services
LibraryService coordinates with (MetadataService, ArtworkService,
ShortcutRemovalService) and a mocked MigrationService. All test files
under ``tests/services/library/`` consume the same ``plugin`` fixture
so coverage of the façade integration and the sub-service internals
sits on top of an identical setup.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_testable_plugin
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_platform_core_reader import FakePlatformCoreReader
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.persistence import PersistenceAdapter
from adapters.steam_config import SteamConfigAdapter
from services.active_core_resolver import ActiveCoreResolver, ActiveCoreResolverConfig
from services.artwork import ArtworkService, ArtworkServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig
from tests.services.library._helpers import rebind_loop


@pytest.fixture
def plugin(tmp_path):
    p = _make_testable_plugin()
    p.settings = {
        "romm_url": "",
        "romm_user": "",
        "romm_pass": "",
        "enabled_platforms": {},
        "enabled_collections": {"user": {}, "smart": {}, "franchise": {}},
    }
    p._romm_api = MagicMock()

    import decky

    # _persistence is wired so disk-touching tests round-trip through the real
    # adapter (settings + firmware cache). The settings persister is faked.
    p._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
    p._settings_persister = FakeSettingsPersister()
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    # ONE shared FakeUnitOfWork across every sub-service + peer service so a
    # write by one (reporter upserting ``roms``) is visible to a read by
    # another (artwork resolving a cover, metadata building the app_id map).
    # Each service gets its own factory wrapping the same unit; ``p._uow``
    # is the handle tests seed/assert against.
    uow = FakeUnitOfWork()
    p._uow = uow

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            log_debug=p._log_debug,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
        ),
    )
    p._metadata_service = metadata_service

    artwork_service = ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            cover_art_file_store=CoverArtFileStoreAdapter(),
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            get_pending_sync=dict,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
        ),
    )
    p._artwork_service = artwork_service

    # Shared core-info fake so a sync-apply test can seed ``available_cores`` and
    # assert a per-game emulator_override (or per-platform core) re-bakes the
    # ``-e`` form. The real ActiveCoreResolver folds the DB override + the
    # per-platform map over this fake's es_systems default — the same seam the
    # orchestrator's bake site draws from.
    p._core_info = FakeCoreInfoProvider()
    p._platform_core_reader = FakePlatformCoreReader()
    p._active_core = ActiveCoreResolver(
        config=ActiveCoreResolverConfig(
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            core_info=p._core_info,
            platform_core_reader=p._platform_core_reader,
            resolve_system=lambda platform_slug, platform_fs_slug=None: platform_slug,
            logger=decky.logger,
        ),
    )

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            settings_persister=p._settings_persister,
            log_debug=p._log_debug,
            artwork=artwork_service,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            active_core=p._active_core,
            disc_resolver=FakeDiscResolver(),
        ),
    )

    p._shortcut_removal_service = ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            steam_config=steam_config,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            artwork_remover=artwork_service,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
        ),
    )
    # Default migration service mock — no migration pending. Tests that need
    # to exercise the @migration_blocked gate override this.
    p._migration_service = MagicMock()
    p._migration_service.is_retrodeck_migration_pending.return_value = False
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop matches the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()
    rebind_loop(plugin._sync_service, asyncio.get_event_loop())
    plugin._artwork_service._loop = asyncio.get_event_loop()
    plugin._shortcut_removal_service._loop = asyncio.get_event_loop()
    plugin._metadata_service._loop = asyncio.get_event_loop()
