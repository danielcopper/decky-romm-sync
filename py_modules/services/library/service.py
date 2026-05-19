"""LibraryService façade.

Owns the public callable surface exposed via ``main.py`` (platform/
collection metadata, sync preview/apply/cancel, reporting,
registry queries) and the shared :class:`LibrarySyncStateBox` that
threads through every sub-service. Implementation lives in the
sub-service modules: :class:`LibraryFetcher` for ROM/metadata
roundtrips, :class:`SyncOrchestrator` for the preview/apply
lifecycle and safety heartbeat, :class:`SyncReporter` for post-apply
finalisation and registry queries. The façade itself only wires the
pieces together and delegates — anything that touches RomM or mutates
in-flight sync state belongs in a sub-service.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from models.state import MetadataCache, PluginState

from lib.late_binding import LateBinding
from services.library._state import LibrarySyncStateBox
from services.library.fetcher import LibraryFetcher, LibraryFetcherConfig
from services.library.reporter import SyncReporter, SyncReporterConfig
from services.library.sync_orchestrator import SyncOrchestrator, SyncOrchestratorConfig

if TYPE_CHECKING:
    import logging

    from domain.preview_delta import PreviewDelta
    from domain.sync_state import SyncState
    from services.protocols import (
        ArtworkManager,
        Clock,
        DebugLogger,
        EventEmitter,
        MetadataExtractor,
        RommLibraryApi,
        SettingsPersister,
        ShortcutRegistryStore,
        Sleeper,
        StatePersister,
        SteamConfigStore,
        UuidGen,
    )


@dataclass(frozen=True)
class LibraryServiceConfig:
    """Frozen wiring bundle handed to ``LibraryService.__init__``.

    Holds the Protocol-typed adapters, the live state/settings/metadata
    cache dicts, runtime infrastructure, time/sleep/uuid seams, plugin-
    dir reference, event emitter, persistence callbacks, debug-logger
    seam, and the metadata/artwork peer services LibraryService needs
    at construction time.
    """

    romm_api: RommLibraryApi
    steam_config: SteamConfigStore
    state: PluginState
    settings: dict
    metadata_cache: MetadataCache
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    state_persister: StatePersister
    settings_persister: SettingsPersister
    registry_store: ShortcutRegistryStore
    log_debug: DebugLogger
    metadata_service: MetadataExtractor
    artwork: ArtworkManager


class LibraryService:
    """Façade for the library sync pipeline.

    Composes :class:`LibraryFetcher` (platform/collection roundtrips +
    metadata-cache stamping), :class:`SyncOrchestrator` (preview/apply
    lifecycle + safety heartbeat), and :class:`SyncReporter`
    (post-apply finalisation + registry queries) over a single shared
    :class:`LibrarySyncStateBox`. The façade itself owns the box and
    exposes the callable surface; every implementation method lives on
    one of the sub-services.
    """

    def __init__(self, *, config: LibraryServiceConfig) -> None:
        self._config = config
        self._logger = config.logger
        self._box = LibrarySyncStateBox()

        # Sub-service: fetcher. Constructed first because the orchestrator
        # holds a reference to it for the per-unit fetch pipeline. The
        # progress-emit proxy late-binds to ``self._orchestrator`` so it
        # can be threaded into the fetcher's config before the
        # orchestrator exists.
        self._fetcher = LibraryFetcher(
            config=LibraryFetcherConfig(
                romm_api=config.romm_api,
                state=config.state,
                settings=config.settings,
                metadata_cache=config.metadata_cache,
                loop=config.loop,
                logger=config.logger,
                plugin_dir=config.plugin_dir,
                settings_persister=config.settings_persister,
                log_debug=config.log_debug,
                sync_state_box=self._box,
                emit_progress=self._emit_progress_proxy,
            )
        )

        # The orchestrator dispatches the per-unit pipeline's finalize
        # step (sync_collections + sync_complete) through the reporter,
        # but the reporter doesn't exist yet at this point in __init__.
        # Thread the forward reference through a LateBinding rather than
        # writing to a sub-service private after the fact.
        reporter_binding: LateBinding[SyncReporter] = LateBinding("reporter")
        self._orchestrator = SyncOrchestrator(
            config=SyncOrchestratorConfig(
                state=config.state,
                settings=config.settings,
                loop=config.loop,
                logger=config.logger,
                plugin_dir=config.plugin_dir,
                emit=config.emit,
                clock=config.clock,
                uuid_gen=config.uuid_gen,
                sleeper=config.sleeper,
                state_persister=config.state_persister,
                sync_state_box=self._box,
                fetcher=self._fetcher,
                reporter=reporter_binding,
                metadata_service=config.metadata_service,
                artwork=config.artwork,
            )
        )

        self._reporter = SyncReporter(
            config=SyncReporterConfig(
                steam_config=config.steam_config,
                state=config.state,
                settings=config.settings,
                loop=config.loop,
                logger=config.logger,
                emit=config.emit,
                clock=config.clock,
                state_persister=config.state_persister,
                registry_store=config.registry_store,
                sync_state_box=self._box,
                emit_progress=self._emit_progress_proxy,
                artwork=config.artwork,
            )
        )
        reporter_binding.set(lambda: self._reporter)

    async def _emit_progress_proxy(self, phase, **kwargs):
        """Late-bound proxy to the orchestrator's _emit_progress.

        Threaded into the fetcher's config at ctor time before
        ``self._orchestrator`` exists — calls resolve at invocation
        time, by which point both sub-services are wired.
        """
        await self._orchestrator._emit_progress(phase, **kwargs)

    # ── Public properties ────────────────────────────────────────

    @property
    def sync_state(self) -> SyncState:
        """Current sync state (read-only)."""
        return self._box.sync_state

    @property
    def pending_sync(self) -> dict:
        """Public accessor for pending sync data (used by SteamGridService)."""
        return self._box.pending_sync

    # ── State accessors preserving the pre-decomposition attribute shape ──
    #
    # The bootstrap-style ``get_pending_sync=lambda: service._pending_sync``
    # callback and fixture-level test setup poke at the legacy private
    # attribute names. Proxy them through the shared state box so external
    # readers and writers see the live values mutated by sub-services.

    @property
    def _sync_state(self) -> SyncState:
        return self._box.sync_state

    @_sync_state.setter
    def _sync_state(self, value: SyncState) -> None:
        self._box.sync_state = value

    @property
    def _pending_sync(self) -> dict:
        return self._box.pending_sync

    @_pending_sync.setter
    def _pending_sync(self, value: dict) -> None:
        self._box.pending_sync = value

    @property
    def _pending_delta(self) -> PreviewDelta | None:
        return self._box.pending_delta

    @_pending_delta.setter
    def _pending_delta(self, value: PreviewDelta | None) -> None:
        self._box.pending_delta = value

    @property
    def _pending_collection_memberships(self) -> dict:
        return self._box.pending_collection_memberships

    @_pending_collection_memberships.setter
    def _pending_collection_memberships(self, value: dict) -> None:
        self._box.pending_collection_memberships = value

    @property
    def _pending_platform_rom_ids(self) -> set[int] | None:
        return self._box.pending_platform_rom_ids

    @_pending_platform_rom_ids.setter
    def _pending_platform_rom_ids(self, value: set[int] | None) -> None:
        self._box.pending_platform_rom_ids = value

    @property
    def _sync_progress(self) -> dict:
        return self._box.sync_progress

    @_sync_progress.setter
    def _sync_progress(self, value: dict) -> None:
        self._box.sync_progress = value

    @property
    def _sync_last_heartbeat(self) -> float:
        return self._box.sync_last_heartbeat

    @_sync_last_heartbeat.setter
    def _sync_last_heartbeat(self, value: float) -> None:
        self._box.sync_last_heartbeat = value

    @property
    def _current_sync_id(self) -> str | None:
        return self._box.current_sync_id

    @_current_sync_id.setter
    def _current_sync_id(self, value: str | None) -> None:
        self._box.current_sync_id = value

    @property
    def _loop(self) -> asyncio.AbstractEventLoop:
        return self._fetcher._loop

    @_loop.setter
    def _loop(self, value: asyncio.AbstractEventLoop) -> None:
        # Test fixtures rebind the loop after construction; propagate the
        # override to every sub-service so async calls land on the live
        # asyncio loop.
        self._fetcher._loop = value
        self._orchestrator._loop = value
        self._reporter._loop = value

    @property
    def _state(self) -> PluginState:
        return self._config.state

    @property
    def _settings(self) -> dict:
        return self._config.settings

    @property
    def _metadata_cache(self) -> MetadataCache:
        return self._config.metadata_cache

    # Getters mirror the pre-decomposition attribute shape for external
    # readers (tests, bootstrap-style callbacks). Only attributes still
    # poked at by tests at the façade level are surfaced; sub-services
    # read these directly via their own ctor-bound references.

    @property
    def _romm_api(self):
        return self._fetcher._romm_api

    @property
    def _clock(self):
        return self._orchestrator._clock

    @property
    def _sleeper(self):
        return self._orchestrator._sleeper

    @_sleeper.setter
    def _sleeper(self, value) -> None:
        self._orchestrator._sleeper = value

    @property
    def _settings_persister(self):
        return self._fetcher._settings_persister

    @_settings_persister.setter
    def _settings_persister(self, value) -> None:
        self._fetcher._settings_persister = value

    # ── Public callable surface ──────────────────────────────────

    def shutdown(self) -> None:
        """Request graceful shutdown — cancels sync if running."""
        self._orchestrator.shutdown()

    # Platform metadata
    async def get_platforms(self):
        return await self._fetcher.get_platforms()

    def save_platform_sync(self, platform_id, enabled):
        return self._fetcher.save_platform_sync(platform_id, enabled)

    async def set_all_platforms_sync(self, enabled):
        return await self._fetcher.set_all_platforms_sync(enabled)

    # Collection metadata
    async def get_collections(self):
        return await self._fetcher.get_collections()

    def save_collection_sync(self, collection_id, enabled):
        return self._fetcher.save_collection_sync(collection_id, enabled)

    async def set_all_collections_sync(self, enabled, category=None):
        return await self._fetcher.set_all_collections_sync(enabled, category)

    # Sync control
    def start_sync(self):
        return self._orchestrator.start_sync()

    def cancel_sync(self):
        return self._orchestrator.cancel_sync()

    def sync_heartbeat(self):
        return self._orchestrator.sync_heartbeat()

    # Preview / apply
    async def sync_preview(self):
        return await self._orchestrator.sync_preview()

    async def sync_apply_delta(self, preview_id):
        return await self._orchestrator.sync_apply_delta(preview_id)

    def sync_cancel_preview(self):
        return self._orchestrator.sync_cancel_preview()

    # Reporting
    async def report_unit_results(self, rom_id_to_app_id):
        return await self._reporter.report_unit_results(rom_id_to_app_id)

    # Registry queries
    def get_registry_platforms(self):
        return self._reporter.get_registry_platforms()

    def clear_sync_cache(self):
        return self._reporter.clear_sync_cache()

    def get_sync_stats(self):
        return self._reporter.get_sync_stats()

    def get_rom_by_steam_app_id(self, app_id):
        return self._reporter.get_rom_by_steam_app_id(app_id)
