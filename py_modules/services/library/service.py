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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.late_binding import LateBinding
from services.library._state import LibrarySyncStateBox
from services.library.fetcher import LibraryFetcher, LibraryFetcherConfig
from services.library.reporter import SyncReporter, SyncReporterConfig
from services.library.sync_orchestrator import SyncOrchestrator, SyncOrchestratorConfig

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.preview_delta import PreviewDelta
    from domain.sync_state import SyncState
    from services.protocols import (
        ActiveCoreReader,
        ArtworkManager,
        Clock,
        DebugLogger,
        DiscResolver,
        EventEmitter,
        RommLibraryApi,
        SettingsPersister,
        Sleeper,
        SteamConfigStore,
        UnitOfWorkFactory,
        UuidGen,
    )


@dataclass(frozen=True)
class LibraryServiceConfig:
    """Frozen wiring bundle handed to ``LibraryService.__init__``.

    Holds the Protocol-typed adapters, the live settings dict, runtime
    infrastructure, time/sleep/uuid seams, plugin-dir reference, event
    emitter, the ``settings.json`` persister and the SQLite Unit-of-Work
    factory (the synced-ROM registry, last-sync timestamp, sync stats and
    metadata cache now live in ``roms`` / ``sync_runs`` / ``rom_metadata``
    via the UoW), debug-logger seam, the artwork peer service, and the
    shared per-ROM ``active_core`` resolver (used to bake each ROM's full
    active core into ``launch_options`` at sync) and the shared ``disc_resolver``
    (used to bake each multi-disc ROM's selected disc into ``launch_options`` at
    sync).
    """

    romm_api: RommLibraryApi
    steam_config: SteamConfigStore
    settings: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    plugin_dir: str
    emit: EventEmitter
    clock: Clock
    uuid_gen: UuidGen
    sleeper: Sleeper
    settings_persister: SettingsPersister
    log_debug: DebugLogger
    artwork: ArtworkManager
    uow_factory: UnitOfWorkFactory
    active_core: ActiveCoreReader
    disc_resolver: DiscResolver


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
                settings=config.settings,
                loop=config.loop,
                logger=config.logger,
                plugin_dir=config.plugin_dir,
                settings_persister=config.settings_persister,
                log_debug=config.log_debug,
                uow_factory=config.uow_factory,
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
                settings=config.settings,
                loop=config.loop,
                logger=config.logger,
                plugin_dir=config.plugin_dir,
                emit=config.emit,
                clock=config.clock,
                uuid_gen=config.uuid_gen,
                sleeper=config.sleeper,
                uow_factory=config.uow_factory,
                sync_state_box=self._box,
                fetcher=self._fetcher,
                reporter=reporter_binding,
                artwork=config.artwork,
                active_core=config.active_core,
                disc_resolver=config.disc_resolver,
            )
        )

        self._reporter = SyncReporter(
            config=SyncReporterConfig(
                steam_config=config.steam_config,
                settings=config.settings,
                loop=config.loop,
                logger=config.logger,
                emit=config.emit,
                clock=config.clock,
                uow_factory=config.uow_factory,
                sync_state_box=self._box,
                emit_progress=self._emit_progress_proxy,
                artwork=config.artwork,
            )
        )
        reporter_binding.set(lambda: self._reporter)

    async def _emit_progress_proxy(self, stage, **kwargs):
        """Late-bound proxy to the orchestrator's emit_progress.

        Threaded into the fetcher's config at ctor time before
        ``self._orchestrator`` exists — calls resolve at invocation
        time, by which point both sub-services are wired.
        """
        await self._orchestrator.emit_progress(stage, **kwargs)

    # ── Public properties ────────────────────────────────────────

    @property
    def sync_state(self) -> SyncState:
        """Current sync state (read-only)."""
        return self._box.sync_state

    @property
    def pending_sync(self) -> dict[int, dict[str, Any]]:
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
    def _pending_sync(self) -> dict[int, dict[str, Any]]:
        return self._box.pending_sync

    @_pending_sync.setter
    def _pending_sync(self, value: dict[int, dict[str, Any]]) -> None:
        self._box.pending_sync = value

    @property
    def _pending_delta(self) -> PreviewDelta | None:
        return self._box.pending_delta

    @_pending_delta.setter
    def _pending_delta(self, value: PreviewDelta | None) -> None:
        self._box.pending_delta = value

    @property
    def _pending_collection_memberships(self) -> dict[str, list[int]]:
        return self._box.pending_collection_memberships

    @_pending_collection_memberships.setter
    def _pending_collection_memberships(self, value: dict[str, list[int]]) -> None:
        self._box.pending_collection_memberships = value

    @property
    def _pending_platform_rom_ids(self) -> set[int] | None:
        return self._box.pending_platform_rom_ids

    @_pending_platform_rom_ids.setter
    def _pending_platform_rom_ids(self, value: set[int] | None) -> None:
        self._box.pending_platform_rom_ids = value

    @property
    def _sync_progress(self) -> dict[str, Any]:
        return self._box.sync_progress

    @_sync_progress.setter
    def _sync_progress(self, value: dict[str, Any]) -> None:
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
    def _settings(self) -> dict[str, Any]:
        return self._config.settings

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

    def save_collection_sync(self, collection_id, kind, enabled):
        return self._fetcher.save_collection_sync(collection_id, kind, enabled)

    async def set_all_collections_sync(self, enabled, scope=None):
        return await self._fetcher.set_all_collections_sync(enabled, scope)

    # Sync control
    def start_sync(self):
        return self._orchestrator.start_sync()

    def cancel_sync(self, run_id=None):
        return self._orchestrator.cancel_sync(run_id)

    def sync_heartbeat(self):
        return self._orchestrator.sync_heartbeat()

    # Preview / apply
    async def sync_preview(self):
        return await self._orchestrator.sync_preview()

    async def sync_apply_delta(self, preview_id):
        return await self._orchestrator.sync_apply_delta(preview_id)

    def sync_cancel_preview(self):
        return self._orchestrator.sync_cancel_preview()

    def get_sync_status(self):
        return self._orchestrator.get_sync_status()

    # Reporting
    async def report_unit_results(self, rom_id_to_app_id, run_id, unit_id):
        return await self._reporter.report_unit_results(rom_id_to_app_id, run_id, unit_id)

    # Registry queries
    def get_registry_platforms(self):
        return self._reporter.get_registry_platforms()

    def clear_sync_cache(self):
        return self._reporter.clear_sync_cache()

    def get_sync_stats(self):
        return self._reporter.get_sync_stats()

    def get_rom_by_steam_app_id(self, app_id):
        return self._reporter.get_rom_by_steam_app_id(app_id)
