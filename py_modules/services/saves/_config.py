"""Construction-time wiring bundle for ``SaveService``.

Holds the environment-bound dependencies SaveService needs at
construction time — runtime paths, callbacks into other services,
the asyncio loop, the logger, plugin metadata. Live mutable state
references (``settings``, ``state``, ``save_sync_state``) and abstract
protocol deps (``romm_api``, ``retry``) are intentionally NOT bundled
here — they remain explicit ``SaveService`` ctor parameters because
they have different lifecycles and ownership semantics from this
immutable wiring bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import (
        Clock,
        CoreNameProviderFn,
        CoreResolverFn,
        EventEmitter,
        RomsPathProvider,
        SaveFileAdapter,
        SavesPathProvider,
        SaveSyncStatePersister,
    )


@dataclass(frozen=True)
class SaveServiceConfig:
    """Frozen wiring bundle handed to ``SaveService.__init__``.

    Parameters
    ----------
    runtime_dir:
        Absolute path to the plugin runtime directory. Consumed by
        SaveService for ad-hoc runtime files; ``save_sync_state.json``
        persistence routes through ``save_sync_state_persister``.
    save_sync_state_persister:
        Protocol-typed I/O wrapper for ``save_sync_state.json``. The
        ``StateService`` uses ``.save(data)`` / ``.load() -> dict | None``
        — file path, locking, and atomic-write are adapter-internal.
    save_file:
        Protocol-typed filesystem adapter for local save files. Owns the
        raw POSIX, ``open()``, ``tempfile``, and ``hashlib``-on-file
        calls SaveService and its sub-services use when reading,
        writing, backing up, hashing, and removing local save files.
    loop:
        The plugin's ``asyncio`` event loop (for ``run_in_executor``).
    logger:
        Standard-library logger (replaces ``decky.logger``).
    get_saves_path:
        Callable returning the current RetroDECK saves directory.
    get_roms_path:
        Callable returning the current RetroDECK roms directory.
    get_active_core:
        Callable resolving the active RetroArch core for a system/game.
        Returns ``(core_so, label)`` tuple; either may be None if
        unresolved. This is an ES-DE question (``which core runs this
        ROM?``).
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
    plugin_version:
        Plugin version string used in user-agent and emitted events.
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
    """

    runtime_dir: str
    save_sync_state_persister: SaveSyncStatePersister
    save_file: SaveFileAdapter
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    get_saves_path: SavesPathProvider
    get_roms_path: RomsPathProvider
    get_active_core: CoreResolverFn
    get_core_name: CoreNameProviderFn | None = None
    plugin_version: str = "0.0.0"
    emit: EventEmitter | None = None
    detect_sort_change: Callable[[], None] | None = None
    is_retrodeck_migration_pending: Callable[[], bool] | None = None
