"""Construction-time wiring bundle for ``SaveService``.

Holds every dependency SaveService needs at construction time —
Protocol-typed adapters, runtime infrastructure, live mutable state
references, plugin metadata, and callbacks into other services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.save_state import SaveSyncState

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        Clock,
        CoreNameProviderFn,
        CoreResolverFn,
        DebugLogger,
        EventEmitter,
        HostnameProvider,
        MigrationPendingFn,
        PluginMetadataReader,
        RetroDeckPaths,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
        SaveSortChangeFn,
        SaveSyncStatePersister,
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
    state:
        Live reference to the main plugin state dict (``installed_roms``,
        ``shortcut_registry``).
    save_sync_state:
        Live reference to the typed :class:`SaveSyncState` aggregate.
        Caller should pre-populate via :meth:`SaveService.load_state`
        after construction; the aggregate ships with defaults out of
        the box.
    save_sync_state_persister:
        Protocol-typed I/O wrapper for ``save_sync_state.json``. The
        ``StateService`` uses ``.save(data)`` / ``.load() -> dict | None``
        — file path, locking, and atomic-write are adapter-internal.
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
    get_active_core:
        Callable resolving the active RetroArch core for a system/game.
        Returns ``(core_so, label)`` tuple; either may be None if
        unresolved. This is an ES-DE question (``which core runs this
        ROM?``).
    hostname_provider:
        ``HostnameProvider`` Protocol seam — supplies the local device
        hostname used as the registered device name during initial
        server-side device registration.
    get_core_name:
        Callable returning the RetroArch canonical ``corename`` field
        from a core's ``.info`` file for a given ``core_so`` (e.g.
        ``"mgba_libretro"`` -> ``"mGBA"``). Optional. When
        ``sort_savefiles_enable`` is active on RetroArch, this is the
        authoritative name used for the per-core save subdirectory — it
        is NOT the same as the ES-DE UI label returned by
        ``get_active_core`` (see the Config-Source-Parsers wiki page
        for the one-parser-per-source rationale). When ``None`` or when
        resolution fails at runtime, SaveService warns and falls back
        to the parent directory path; see
        ``_resolve_retroarch_corename``.
    plugin_metadata:
        ``PluginMetadataReader`` Protocol seam — read once during
        :meth:`SaveService.__init__` to resolve the declared plugin
        version forwarded into user-agent strings and emitted events.
    plugin_dir:
        Plugin install directory (``decky.DECKY_PLUGIN_DIR``) passed to
        :meth:`PluginMetadataReader.read_version`.
    emit:
        Optional event emitter for pushing save-sync progress to the
        frontend. ``None`` disables emission (used in unit tests).
    detect_sort_change:
        Optional synchronous callback that refreshes save-sort state
        from the live RetroArch config (wired to
        ``MigrationService.detect_save_sort_change`` in ``bootstrap``).
        Save-sync MUST see fresh save-sort state before computing
        ``saves_dir`` — otherwise a direct-Steam-launch with no
        pre-launch detect trigger would silently download stale server
        content to the wrong layout and destroy real user progress
        during the subsequent migration (#238). ``pre_launch_sync`` and
        ``post_exit_sync`` invoke this callback once at their entry
        point. ``None`` disables the call (used only in unit tests
        where state is seeded explicitly); failures are logged and
        swallowed so save-sync degrades gracefully to the
        previously-known state.
    is_retrodeck_migration_pending:
        Optional callback returning ``True`` when a RetroDECK migration
        is in flight; SaveService gates destructive operations on this
        signal. ``None`` disables the gate (unit tests).
    log_debug:
        ``DebugLogger`` Protocol seam — routes through the user's QAM
        log-level filter. Injected directly into each sub-service that
        needs it; not reached through the ``_save_service`` back-ref.
    """

    romm_api: RommSyncApi
    retry: RetryStrategy
    settings: dict
    state: dict
    save_sync_state: SaveSyncState
    save_sync_state_persister: SaveSyncStatePersister
    save_file_store: SaveFileStore
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    retrodeck_paths: RetroDeckPaths
    get_active_core: CoreResolverFn
    hostname_provider: HostnameProvider
    log_debug: DebugLogger
    plugin_metadata: PluginMetadataReader
    plugin_dir: str
    get_core_name: CoreNameProviderFn | None = None
    emit: EventEmitter | None = None
    detect_sort_change: SaveSortChangeFn | None = None
    is_retrodeck_migration_pending: MigrationPendingFn | None = None
