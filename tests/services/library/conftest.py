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
import importlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.cover_art_file_store import CoverArtFileStoreAdapter
from adapters.persistence import (
    MetadataCachePersisterAdapter,
    PersistenceAdapter,
    StatePersisterAdapter,
)
from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
from services.artwork import ArtworkService, ArtworkServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.metadata import MetadataService, MetadataServiceConfig
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig

if TYPE_CHECKING:
    # basedpyright resolves ``conftest`` to the nearest local file. The
    # actual fakes live in the parent ``tests/conftest.py``; declare the
    # symbols for typing purposes here and let runtime resolve through
    # ``importlib`` below.
    from conftest import FakeCoverArtFileStore, FakeSettingsPersister

# Runtime: pytest ensures the root ``tests/conftest.py`` is loaded under
# the module name ``conftest`` before this local conftest is imported,
# so the symbols are already on the loaded module.
_root_conftest = importlib.import_module("conftest")
FakeSettingsPersister = _root_conftest.FakeSettingsPersister
FakeCoverArtFileStore = _root_conftest.FakeCoverArtFileStore


@pytest.fixture
def plugin(tmp_path):
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._romm_api = MagicMock()
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._metadata_cache = {}

    import decky

    # _persistence is wired so disk-touching tests round-trip through the real
    # adapter. The Protocol-typed persisters are bound to the same instance and
    # the live state/settings/metadata_cache dicts so service writes land on disk.
    p._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
    p._state_persister = StatePersisterAdapter(p._persistence, p._state)
    p._settings_persister = FakeSettingsPersister()
    p._metadata_cache_persister = MetadataCachePersisterAdapter(p._persistence, p._metadata_cache)
    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    metadata_service = MetadataService(
        config=MetadataServiceConfig(
            state=p._state,
            metadata_cache=p._metadata_cache,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            clock=FakeClock(),
            metadata_cache_persister=p._metadata_cache_persister,
            log_debug=p._log_debug,
        ),
    )
    p._metadata_service = metadata_service

    artwork_service = ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            cover_art_file_store=CoverArtFileStoreAdapter(),
            state=p._state,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            get_pending_sync=dict,
        ),
    )
    p._artwork_service = artwork_service

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
            metadata_service=metadata_service,
            artwork=artwork_service,
        ),
    )

    p._shortcut_removal_service = ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            state=p._state,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            emit=decky.emit,
            state_persister=p._state_persister,
            artwork_remover=artwork_service,
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
    plugin._sync_service._loop = asyncio.get_event_loop()
    plugin._artwork_service._loop = asyncio.get_event_loop()
    plugin._shortcut_removal_service._loop = asyncio.get_event_loop()
    plugin._metadata_service._loop = asyncio.get_event_loop()
