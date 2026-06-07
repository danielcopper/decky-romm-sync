"""Construction-time wiring bundle for ``SaveService``.

Holds every dependency SaveService needs at construction time —
Protocol-typed adapters, runtime infrastructure, live mutable state
references, plugin metadata, and callbacks into other services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        ActiveCoreReader,
        Clock,
        CoreNameProviderFn,
        DebugLogger,
        EventEmitter,
        HostnameReader,
        MachineIdReader,
        MigrationPendingFn,
        PluginMetadataReader,
        RetroDeckPaths,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
        SaveSortChangeFn,
        SettingsPersister,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class SaveServiceConfig:
    """Frozen wiring bundle handed to ``SaveService.__init__``.

    Parameters
    ----------
    romm_api:
        Protocol adapter for all RomM save/notes HTTP operations.
    retry:
        Retry strategy — provides ``with_retry`` and ``is_retryable``.
    settings:
        Live reference to the main plugin settings dict.
    settings_persister:
        Protocol-typed zero-arg flush for ``settings.json``. SaveService
        calls ``.save_settings()`` after mutating the save-sync feature
        toggles or the device label in the live ``settings`` dict — those
        values live in settings.json, not the save-sync aggregate.
    save_file_store:
        Protocol-typed filesystem adapter for local save files. Owns the
        raw POSIX, ``open()``, ``tempfile``, and ``hashlib``-on-file
        calls SaveService and its sub-services use when reading,
        writing, backing up, hashing, and removing local save files.
    loop:
        The plugin's ``asyncio`` event loop (for ``run_in_executor``).
    logger:
        Standard-library logger (replaces ``decky.logger``).
    retrodeck_paths:
        Bundled accessor for the four RetroDECK runtime directory
        paths. SaveService consumes ``saves_path()`` and ``roms_path()``;
        the BIOS and home accessors are unused here but the Protocol
        is bundled so every service shares a uniform shape.
    active_core:
        ``ActiveCoreReader`` seam resolving the active RetroArch core for a
        ROM by ``rom_id``. Returns ``(core_so, label)``; either may be None if
        unresolved. Folds the per-game ``emulator_override`` pin over the
        system-layer ES-DE resolution so the per-core save dir / save-emulator
        tag / core-change warning key off the same core the ROM launches with.
    hostname_provider:
        ``HostnameReader`` Protocol seam — supplies the local device
        hostname used as the registered device name during initial
        server-side device registration.
    machine_id_provider:
        ``MachineIdReader`` Protocol seam — supplies the stable
        ``/etc/machine-id`` value sent as the RomM ``hostname``
        fingerprint during initial server-side device registration so the
        server dedupes this device across reinstalls. ``None`` when the
        machine id is unreadable, which degrades registration to the
        no-fingerprint path.
    get_core_name:
        Callable returning the RetroArch canonical ``corename`` field
        from a core's ``.info`` file for a given ``core_so`` (e.g.
        ``"mgba_libretro"`` -> ``"mGBA"``). When
        ``sort_savefiles_enable`` is active on RetroArch, this is the
        authoritative name used for the per-core save subdirectory — it
        is NOT the same as the ES-DE UI label returned by the
        ``active_core`` reader (see the Config-Source-Parsers wiki page
        for the one-parser-per-source rationale). When resolution fails
        at runtime (the callable returns ``None``), SaveService warns
        and falls back to the parent directory path; see
        ``_resolve_retroarch_corename``.
    plugin_metadata:
        ``PluginMetadataReader`` Protocol seam — read once during
        :meth:`SaveService.__init__` to resolve the declared plugin
        version forwarded into user-agent strings and emitted events.
    plugin_dir:
        Plugin install directory (``decky.DECKY_PLUGIN_DIR``) passed to
        :meth:`PluginMetadataReader.read_version`.
    emit:
        Event emitter for pushing save-sync progress to the frontend.
    detect_sort_change:
        Synchronous callback that refreshes save-sort state from the
        live RetroArch config (wired to
        ``MigrationService.detect_save_sort_change`` in ``bootstrap``).
        Save-sync MUST see fresh save-sort state before computing
        ``saves_dir`` — otherwise a direct-Steam-launch with no
        pre-launch detect trigger would silently download stale server
        content to the wrong layout and destroy real user progress
        during the subsequent migration (#238). ``pre_launch_sync`` and
        ``post_exit_sync`` invoke this callback once at their entry
        point; failures are logged and swallowed so save-sync degrades
        gracefully to the previously-known state.
    is_retrodeck_migration_pending:
        Callback returning ``True`` when a RetroDECK migration is in
        flight; SaveService gates destructive operations on this signal.
    log_debug:
        ``DebugLogger`` Protocol seam — routes through the user's QAM
        log-level filter. Injected directly into each sub-service that
        needs it; not reached through the ``_save_service`` back-ref.
    uow_factory:
        ``UnitOfWorkFactory`` Protocol seam — opens a fresh transactional
        Unit of Work over the nine SQLite repositories. The saves vertical
        reads/writes the ``rom_save_states`` aggregate + ``kv_config``
        device id through it; each public callable owns a narrow
        read→I/O→write bracket (ADR-0006).
    """

    romm_api: RommSyncApi
    retry: RetryStrategy
    settings: dict[str, Any]
    settings_persister: SettingsPersister
    save_file_store: SaveFileStore
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    retrodeck_paths: RetroDeckPaths
    active_core: ActiveCoreReader
    hostname_provider: HostnameReader
    machine_id_provider: MachineIdReader
    log_debug: DebugLogger
    plugin_metadata: PluginMetadataReader
    plugin_dir: str
    get_core_name: CoreNameProviderFn
    emit: EventEmitter
    detect_sort_change: SaveSortChangeFn
    is_retrodeck_migration_pending: MigrationPendingFn
    uow_factory: UnitOfWorkFactory
