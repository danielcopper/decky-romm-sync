"""SyncEngine entry point: per-rom lock dispatch and public-callable orchestration.

Owns the rom-level concurrency seam (``_rom_sync_locks``) and the
sequencing rules every public save-sync callable must follow (save-sync
enabled check, retrodeck migration gate, save-sort detect, device-
registration fallback, dispatch into the matrix executor, persistence).
Each public callable owns a narrow Unit of Work (ADR-0006): it reads the
``RomSaveSyncState`` aggregate + ``device_id`` at the start, performs all
server/file I/O outside any transaction, and writes the mutated
aggregate back in a short write UoW at the end. The implementation of
the actual file/server transfers lives in
:mod:`services.saves.sync_engine.matrix`; device registration lives in
:mod:`services.saves.sync_engine.devices`; conflict-resolution rollback
lives in :mod:`services.saves.sync_engine.rollback`. SyncEngine wires
those sub-modules together and exposes the surface peer save services
(status, versions, slots) consume.

There is a single sync code path (ADR-0017, superseding ADR-0016's
routing fork): every ROM decides via the local ``compute_sync_action``
matrix (driven by ``list_saves``), the sole detection authority. RomM's
``negotiate`` operation is kept only as a session **transport** for a
confirmed non-legacy ROM — its planned ``operations`` are ignored; the run
opens a session for the play-session/telemetry envelope, keeps just the
``session_id``, runs the bare matrix, then completes the session in a
``finally``. A legacy or unconfirmed ROM runs the same matrix without a
session wrapper. Any failure opening the session degrades to a bare
matrix run — the session is an envelope, never a gate on sync.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.rom_save_sync_state import RomSaveSyncState
from domain.save_layout import ContentDir
from lib.errors import RommConnectionError, RommSyncDisabledError, RommTimeoutError, classify_error
from lib.list_result import ErrorCode
from services.saves._messages import (
    DEVICE_NOT_REGISTERED,
    DEVICE_NOT_REGISTERED_REASON,
    DEVICE_SYNC_DISABLED,
    DEVICE_SYNC_DISABLED_REASON,
    SAVE_SYNC_DISABLED,
    SAVE_SYNC_DISABLED_REASON,
    SAVE_SYNC_IN_CONTENT_DIR,
    SAVE_SYNC_IN_CONTENT_DIR_REASON,
)
from services.saves._settings import (
    autocleanup_limit,
    resolve_default_slot,
    save_sync_enabled,
    sync_after_exit,
    sync_before_launch,
)
from services.saves.sync_engine._gate import (
    POST_EXIT_GATE_TIMEOUT,
    PRE_LAUNCH_GATE_TIMEOUT,
    SYNC_ALL_GATE_TIMEOUT,
    SYNC_ROM_GATE_TIMEOUT,
    SaveSyncGate,
    SaveSyncTimeoutError,
)
from services.saves.sync_engine.matrix import MatrixExecutor, MatrixOutcome
from services.saves.sync_engine.rollback import RollbackOrchestrator

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator

    from domain.save_layout import SaveLayout
    from services.protocols import (
        ActiveCoreReader,
        Clock,
        DebugLogger,
        HostnameReader,
        MachineIdReader,
        MigrationPendingFn,
        ResolveUploadConflictFn,
        RetryStrategy,
        RommSyncApi,
        SaveFileStore,
        SaveInventoryBuilderFn,
        SaveSortChangeFn,
        UnitOfWorkFactory,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.sync_engine.devices import DeviceRegistry


__all__ = ["MatrixOutcome", "SyncEngine", "SyncEngineConfig"]


def _first_error_reason(errors: list[str]) -> str:
    """Return the classified reason of the first sync error, stripped of its source.

    Per-file dispatch records each failure as ``"<source>: <reason>"`` where
    ``<source>`` is the save filename (or a fixed label like ``"Failed to fetch
    saves"``) and ``<reason>`` is the :func:`lib.errors.classify_error` message.
    Split on the LAST ``": "`` (``rpartition``): the source can itself contain a
    colon — a save filename derives from the ROM name, which may include one
    (e.g. ``"Grand Theft Auto: San Andreas.srm"``) — so splitting on the last
    separator keeps that colon on the source side and isolates the reason.

    Tradeoff, stated honestly: a ``<reason>`` that itself contained ``": "``
    would be truncated to its final segment. Every classified ``classify_error``
    message (auth, forbidden, SSL, timeout, connection, server, not-found,
    unsupported) uses an em-dash separator and never contains ``": "``, and the
    fixed dispatch labels don't either — so the common cases are exact; only the
    rare ``str(exc)`` fallback (UNKNOWN / generic ``RommApiError``) carrying an
    internal ``": "`` loses its head. Falls back to the whole entry when there is
    no ``": "`` separator. *errors* must be non-empty.
    """
    _source, sep, reason = errors[0].rpartition(": ")
    return reason if sep and reason else errors[0]


def _summarize_sync_result(base: str, *, synced: int, errors: list[str], conflicts: int) -> str:
    """Compose a sync callable's result ``message``, surfacing the failure reason (#1334).

    A total failure (``synced == 0`` with errors) leads with the first error's
    classified reason — never the "Uploaded 0 save(s), 1 error(s)" count summary
    that buries it in ``errors[0]`` — and appends ``"(+N more)"`` when other files
    also failed. A partial run (some transferred, some failed) keeps the ``base``
    count summary and appends the first reason after an em-dash. A clean run
    returns ``base`` unchanged. A surfaced-conflict count is appended after the
    error clause in every case, preserving the pre-#1334 conflict suffix.
    """
    if errors:
        reason = _first_error_reason(errors)
        if synced == 0:
            extra = len(errors) - 1
            msg = f"{reason} (+{extra} more)" if extra else reason
        else:
            msg = f"{base}, {len(errors)} error(s) — {reason}"
    else:
        msg = base
    if conflicts:
        msg += f", {conflicts} conflict(s)"
    return msg


@dataclass(frozen=True)
class SyncEngineConfig:
    """Frozen wiring bundle handed to ``SyncEngine.__init__``.

    Holds the live ``settings.json`` dict (home of the save-sync feature
    toggles), the Unit-of-Work factory (the transactional seam over the
    SQLite repositories), the peer save sub-services (rom_info and the
    shared :class:`DeviceRegistry` that owns the server device id), the
    Protocol-typed RomM adapter and retry strategy, the upload-409
    resolution kernel (``resolve_upload_conflict``), runtime
    infrastructure (loop, logger, clock), the Protocol-typed filesystem
    adapter, the ``DebugLogger`` seam, the per-ROM active-core resolver,
    the hostname provider + machine-id provider passed through to device
    registration, and the optional sort-change and migration-pending
    callbacks SyncEngine consults at the entry of every public flow.
    """

    settings: dict[str, Any]
    uow_factory: UnitOfWorkFactory
    rom_info: RomInfoService
    device_registry: DeviceRegistry
    romm_api: RommSyncApi
    retry: RetryStrategy
    resolve_upload_conflict: ResolveUploadConflictFn
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    save_file_store: SaveFileStore
    log_debug: DebugLogger
    active_core: ActiveCoreReader
    hostname_provider: HostnameReader
    machine_id_provider: MachineIdReader
    detect_sort_change: SaveSortChangeFn
    is_retrodeck_migration_pending: MigrationPendingFn
    build_inventory: SaveInventoryBuilderFn


class SyncEngine:
    """Newest-wins matrix executor, sync orchestration callables, and rom-level lock dispatch."""

    def __init__(self, *, config: SyncEngineConfig) -> None:
        self._config = config
        self._settings = config.settings
        self._uow_factory = config.uow_factory
        self._rom_info = config.rom_info
        self._devices = config.device_registry
        self._romm_api = config.romm_api
        self._retry = config.retry
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._save_file_store = config.save_file_store
        self._log_debug = config.log_debug
        self._active_core = config.active_core
        self._hostname_provider = config.hostname_provider
        self._machine_id_provider = config.machine_id_provider
        self._detect_sort_change = config.detect_sort_change
        self._is_retrodeck_migration_pending = config.is_retrodeck_migration_pending
        self._build_inventory = config.build_inventory
        # Last observed RetroArch save-file layout, refreshed by
        # ``_refresh_save_sort_state`` at the entry of every public sync flow.
        # ``None`` until the first refresh — treated as "not blocked" so a
        # transient cfg read error fails OPEN (never blocks sync on a blip).
        self._current_layout: SaveLayout | None = None
        # Per-rom lock dict — serializes concurrent sync operations on the
        # same rom_id (pre_launch_sync, post_exit_sync, manual sync, resolve).
        self._rom_sync_locks: dict[int, asyncio.Lock] = {}

        self._matrix = MatrixExecutor(
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            retry=config.retry,
            resolve_upload_conflict=config.resolve_upload_conflict,
            logger=config.logger,
            clock=config.clock,
            save_file_store=config.save_file_store,
            log_debug=config.log_debug,
        )
        self._rollback = RollbackOrchestrator(
            uow_factory=config.uow_factory,
            rom_info=config.rom_info,
            device_registry=self._devices,
            romm_api=config.romm_api,
            matrix=self._matrix,
            retry=config.retry,
            clock=config.clock,
            save_file_store=config.save_file_store,
            logger=config.logger,
            log_debug=config.log_debug,
            resolve_core=self.resolve_core,
            settings=config.settings,
        )
        # Device-level single-owner serialization gate: only one save-sync run
        # in flight at a time per device. A second trigger queues behind the
        # in-flight one, bounded so a stuck run never traps the launch path.
        # Sits OUTSIDE the per-ROM ``rom_lock`` — wraps the whole run body.
        self._device_gate = SaveSyncGate()

    def rom_lock(self, rom_id: int) -> asyncio.Lock:
        """Return the lock for this rom_id, creating it lazily."""
        if rom_id not in self._rom_sync_locks:
            self._rom_sync_locks[rom_id] = asyncio.Lock()
        return self._rom_sync_locks[rom_id]

    # ------------------------------------------------------------------
    # Settings / device-id / core helpers
    # ------------------------------------------------------------------

    def is_save_sync_enabled(self) -> bool:
        """Whether the save-sync feature toggle is on (settings.json)."""
        return save_sync_enabled(self._settings)

    def get_device_id(self) -> str | None:
        """Server-side device id (None when unregistered).

        Delegates to the shared :class:`DeviceRegistry` — the single owner of
        ``kv_config["device_id"]`` — so the id is read once and cached rather
        than re-queried per sync flow.
        """
        return self._devices.get_device_id()

    def resolve_core(self, rom_id: int) -> str | None:
        """Resolve the active RetroArch core for a ROM, or ``None``.

        Gates on the install record (an uninstalled ROM has no launch and
        nothing to stamp), then resolves the per-game active core by ``rom_id``
        through the shared :class:`ActiveCoreResolver` — folding the per-game
        ``emulator_override`` pin over the system default. Used to stamp the
        upload emulator tag.
        """
        info = self._rom_info.get_rom_save_info(rom_id)
        if not info:
            return None
        core_so, _label = self._active_core.active_core_for_rom(rom_id)
        return core_so

    # ------------------------------------------------------------------
    # Matrix-executor delegates — consumed by tests, peer services, and
    # internal orchestration. Kept on SyncEngine so monkey-patching
    # `svc._sync_engine.do_sync_rom_saves = stub` continues to short-circuit
    # the public callables that drive `do_sync_rom_saves` through
    # `self.do_sync_rom_saves`.
    # ------------------------------------------------------------------

    def do_sync_rom_saves(
        self,
        rom_id: int,
        save_state: RomSaveSyncState,
        device_id: str | None,
        core_so: str | None,
        default_slot: str | None = None,
        autocleanup_limit: int | None = None,
    ) -> tuple[int, int, list[str], list[dict[str, Any]]]:
        """Sync saves for a single ROM (delegate to :class:`MatrixExecutor`).

        Returns ``(uploaded, downloaded, errors, conflicts)`` — the per-direction
        transfer counts (#250).
        """
        return self._matrix.sync_rom_saves(rom_id, save_state, device_id, core_so, default_slot, autocleanup_limit)

    def do_download_save(
        self,
        server_save: dict[str, Any],
        saves_dir: str,
        filename: str,
        save_state: RomSaveSyncState,
        device_id: str | None,
        system: str,
        default_slot: str | None = None,
    ) -> None:
        """Download a save file from server (delegate to :class:`MatrixExecutor`)."""
        self._matrix.do_download_save(server_save, saves_dir, filename, save_state, device_id, system, default_slot)

    def quarantine_local_file(self, saves_dir: str, filename: str) -> bool:
        """Back up a local save into ``.romm-backup`` (delegate to :class:`MatrixExecutor`)."""
        return self._matrix.quarantine_local_file(saves_dir, filename)

    def do_upload_save(
        self,
        rom_id: int,
        file_path: str,
        filename: str,
        save_state: RomSaveSyncState,
        device_id: str | None,
        system: str,
        core_so: str | None,
        server_save: dict[str, Any] | None = None,
        default_slot: str | None = None,
        autocleanup_limit: int | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Upload a local save file to server (delegate to :class:`MatrixExecutor`)."""
        return self._matrix.do_upload_save(
            rom_id,
            file_path,
            filename,
            save_state,
            device_id,
            system,
            core_so,
            server_save,
            default_slot,
            autocleanup_limit=autocleanup_limit,
            overwrite=overwrite,
        )

    def iter_matrix_outcomes(
        self,
        rom_id: int,
        server_in_slot: list[dict[str, Any]],
        *,
        save_state: RomSaveSyncState | None,
        device_id: str | None,
        info: dict[str, Any],
    ) -> Iterator[MatrixOutcome]:
        """Yield one :class:`MatrixOutcome` per save file in the ROM's active slot."""
        return self._matrix.iter_matrix_outcomes(
            rom_id, server_in_slot, save_state=save_state, device_id=device_id, info=info
        )

    def adopt_baseline_hash(self, save_state: RomSaveSyncState, filename: str, local_hash: str) -> None:
        """Record ``local_hash`` as the file's ``last_sync_hash`` baseline."""
        self._matrix.adopt_baseline_hash(save_state, filename, local_hash)

    @staticmethod
    def filter_server_saves_to_slot(
        server_saves: list[dict[str, Any]], active_slot: str | None
    ) -> list[dict[str, Any]]:
        """Filter server saves to the active slot."""
        return MatrixExecutor.filter_server_saves_to_slot(server_saves, active_slot)

    def build_sync_conflict_entry(
        self,
        rom_id: int,
        filename: str,
        server: dict[str, Any],
        local_path: str | None,
        local_hash: str | None,
    ) -> dict[str, Any]:
        """Build a Phase-2 ``sync_conflict`` descriptor for the frontend."""
        return self._matrix.build_sync_conflict_entry(rom_id, filename, server, local_path, local_hash)

    # ------------------------------------------------------------------
    # Device registration — entrypoint for every sync flow that needs
    # ``device_id``. Kept on SyncEngine because pre_launch_sync,
    # post_exit_sync, sync_rom_saves, and sync_all_saves all fall back
    # to this when ``device_id`` is missing; co-locating the fallback
    # with its callers avoids a constructor callback.
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict[str, Any]:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        return await self._devices.ensure_device_registered(
            loop=self._loop,
            hostname_provider=self._hostname_provider,
            machine_id_provider=self._machine_id_provider,
        )

    async def _ensure_device_live_or_fail(self) -> dict[str, Any] | None:
        """Register-or-heal the server device id before a sync consumes it.

        Runs :meth:`ensure_device_registered` UNCONDITIONALLY — not only when
        the id is absent. Its best-effort ``update_device`` touch doubles as a
        liveness probe, so a dead-but-present cached id (e.g. after a RomM
        database wipe/restore, where ``kv_config`` still holds an id the server
        now 404s) is detected and re-registered HERE, before
        :meth:`_run_rom_sync` reads the id and calls ``list_saves`` with a dead
        one (#1560). Gating the heal on ``if not get_device_id()`` — presence,
        not liveness — is the exact bug one level up from the touch's own
        presence-not-liveness gate. A live id costs one extra 200 touch per
        sync: the accepted price of correctness (no "recently validated"
        caching).

        Returns the canonical ``DEVICE_NOT_REGISTERED`` failure dict when a live
        registration could not be established (the caller returns it verbatim),
        else ``None`` — the caller proceeds and the sync reads the fresh,
        possibly-healed id through :meth:`_read_sync_inputs` (and, for the bulk
        sweep, the re-read at ``sync_all_saves``), never a value captured before
        the heal.
        """
        reg = await self.ensure_device_registered()
        if not reg.get("success"):
            return {
                "success": False,
                "reason": DEVICE_NOT_REGISTERED_REASON,
                "message": DEVICE_NOT_REGISTERED,
            }
        return None

    async def list_devices(self) -> dict[str, Any]:
        """List all devices registered with the RomM server for this user."""
        return await self._devices.list_devices(loop=self._loop)

    # ------------------------------------------------------------------
    # Narrow-UoW read/write helpers (ADR-0006)
    # ------------------------------------------------------------------

    def _read_sync_inputs(self, rom_id: int) -> tuple[RomSaveSyncState, str | None]:
        """Short read UoW: load the ROM's save state + device id.

        Returns the loaded :class:`RomSaveSyncState` (a fresh default when absent)
        and the server device id (read through the shared
        :class:`DeviceRegistry`, the single device-id owner). The aggregate is
        mutated outside the transaction by the matrix worker;
        :meth:`_write_save_state` persists it.
        """
        with self._uow_factory() as uow:
            state = uow.rom_save_sync_states.get(rom_id) or RomSaveSyncState()
        return state, self._devices.get_device_id()

    def _write_save_state(self, rom_id: int, save_state: RomSaveSyncState) -> None:
        """Short write UoW: persist the mutated save state for *rom_id*."""
        with self._uow_factory() as uow:
            uow.rom_save_sync_states.save(rom_id, save_state)

    # ------------------------------------------------------------------
    # Public sync orchestration callables
    # ------------------------------------------------------------------

    async def _refresh_save_sort_state(self, where: str) -> None:
        """Refresh save-sort state from the live RetroArch config.

        Save-sync must observe fresh save-sort state before computing
        ``saves_dir``. This call ensures ``detect_save_sort_change`` has
        run at least once before we read state, closing the race where
        another frontend detect trigger arrives after our backend entry
        point. Without this, a direct-Steam-launch with no pre-detect
        would silently download stale server content to the wrong
        layout and destroy real user progress during the subsequent
        migration (#238).

        Graceful degradation: if detect fails (e.g. retroarch.cfg is
        temporarily unreadable) we log and continue with the
        previously-known state — save-sync must not abort because of a
        config read error. The returned ``SaveLayout`` is stashed on
        ``_current_layout`` so :meth:`_save_sync_blocked` can hard-gate
        sync when RetroArch writes saves to the content dir (#239); on
        failure ``_current_layout`` is left as-is (fail-OPEN).
        """
        try:
            self._current_layout = await self._loop.run_in_executor(None, self._detect_sort_change)
        except Exception as e:
            self._logger.warning(
                "%s: detect_sort_change failed (%s) — proceeding with stale state",
                where,
                e,
            )

    def _save_sync_blocked(self) -> bool:
        """Whether save sync must be hard-gated off for the live save-file layout.

        ``True`` only when the last observed layout is ``ContentDir``
        (RetroArch ``savefiles_in_content_dir=true``) — saves live next to
        the ROM, outside the saves tree the plugin syncs, so every sync
        flow short-circuits with the benign-skip shape (#239). ``None``
        (no layout observed yet, or a refresh that failed) is not blocked:
        a transient cfg read error must never disable sync.
        """
        return isinstance(self._current_layout, ContentDir)

    async def content_dir_blocked(self, where: str) -> bool:
        """Refresh the live layout and report whether ContentDir gates save writes.

        The shared gate every save-WRITE callable consults at its entry —
        the four sync entry points (``pre_launch_sync`` / ``post_exit_sync``
        / ``sync_rom_saves`` / ``sync_all_saves``) inline the refresh + check
        themselves; the secondary write callables (rollback, slot switch,
        conflict resolve, slot-choice migration) call this so the
        ``saves_dir`` write is never attempted in content-dir mode (#239).

        Public (peer-called, no leading underscore): the slots / versions /
        rollback sub-services invoke it across the saves bounded context.
        Refreshes ``_current_layout`` from the live RetroArch config (fail
        OPEN on a transient read error) before reporting the verdict.
        """
        await self._refresh_save_sort_state(where)
        return self._save_sync_blocked()

    @staticmethod
    def _content_dir_skip(*, all_saves: bool = False) -> dict[str, Any]:
        """Build the benign-skip result returned when saves go to the content dir.

        Carries ``success: False`` + the ``savefiles_in_content_dir`` reason
        slug the frontend routes on (treat as skip, no error, launch
        proceeds) alongside zero/empty counts. *all_saves* selects the
        ``sync_all_saves`` return shape (``conflicts`` int + ``conflicts_list``
        / ``roms_checked``) over the single-ROM shape (``conflicts`` list).
        """
        base: dict[str, Any] = {
            "success": False,
            "reason": SAVE_SYNC_IN_CONTENT_DIR_REASON,
            "message": SAVE_SYNC_IN_CONTENT_DIR,
            "synced": 0,
            "errors": [],
        }
        if all_saves:
            base["conflicts"] = 0
            base["conflicts_list"] = []
            base["roms_checked"] = 0
        else:
            base["conflicts"] = []
        return base

    def _heartbeat_failure_result(self, where: str, exc: Exception) -> dict[str, Any]:
        """Build the sync-result dict for a heartbeat failure, classified by type.

        Only a genuine reachability failure (``RommConnectionError`` /
        ``RommTimeoutError``) is reported as "Server offline" with the additive
        ``offline`` flag the launch path routes on. Any other typed error — a
        revoked token (401 → ``AUTH_FAILED``), an SSL misconfig, a 5xx, etc. —
        flows through :func:`classify_error` so the result carries its OWN
        ``reason`` + ``message`` and the UI stops claiming the server is
        unreachable when it is plainly reachable (#971). The raw exception is
        always logged at debug so the offline branch is no longer a silent
        swallow.
        """
        self._log_debug(f"{where}: heartbeat failed ({type(exc).__name__}: {exc})")
        if isinstance(exc, (RommConnectionError, RommTimeoutError)):
            self._logger.info("%s skipped: server offline", where)
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Server offline",
                "synced": 0,
                "offline": True,
            }
        reason, message = classify_error(exc)
        self._logger.info("%s skipped: %s", where, message)
        return {
            "success": False,
            "reason": reason,
            "message": message,
            "synced": 0,
        }

    async def _run_rom_sync(
        self,
        rom_id: int,
        *,
        require_confirmed: bool = False,
        session_id: int | None = None,
        session_counts: list[int] | None = None,
    ) -> tuple[int, int, list[str], list[dict[str, Any]]]:
        """Read inputs → sync in executor → persist, for one ROM under its lock.

        Returns ``(uploaded, downloaded, errors, conflicts)`` — the matrix
        worker's per-direction transfer counts (#250). The negotiate session
        close and the bulk-sweep *session_counts* accumulator record the
        combined ``uploaded + downloaded`` as the session's completed-op count.

        The narrow-UoW shape (ADR-0006): a short read UoW loads the aggregate +
        device id, the matrix transfer runs outside any transaction mutating the
        aggregate in memory, then a short write UoW persists it.

        A ROM with no install record has nothing to sync — and no ``roms`` row
        to anchor a ``rom_save_sync_states`` write against (ADR-0007 FK) — so we
        short-circuit before touching the aggregate.

        When *require_confirmed* is set (the bulk ``sync_all_saves`` sweep), a ROM
        whose slot the user has not confirmed is skipped entirely — no transfer,
        no write — so a never-configured ROM's possibly-stale local save can't be
        auto-uploaded into the default slot and overwrite another device's newer
        progress (#1055). The single-ROM entry points leave it unset.

        Detection is always the local ``compute_sync_action`` matrix
        (``do_sync_rom_saves``); ADR-0017 collapsed the old routing fork. A
        confirmed non-legacy ROM (``slot_confirmed`` + a named ``active_slot``)
        additionally opens a transport-only ``negotiate`` session around the run —
        its planned ``operations`` are ignored, only the ``session_id`` is kept —
        so the session envelope (play-session / telemetry) still gets recorded.
        A legacy/unconfirmed ROM runs the bare matrix without a session. When the
        bulk sweep already opened one whole-device session, its *session_id* is
        threaded in (this call does not open its own) and *session_counts* (the
        shared ``[completed, failed]`` accumulator) collects the run's tallies;
        the bulk run completes that session once after the loop.
        """
        info = await self._loop.run_in_executor(None, self._rom_info.get_rom_save_info, rom_id)
        if not info:
            self._log_debug(f"_run_rom_sync({rom_id}): ROM not installed, skipping")
            return 0, 0, [], []
        save_state, device_id = await self._loop.run_in_executor(None, self._read_sync_inputs, rom_id)
        if require_confirmed and not save_state.slot_confirmed:
            self._log_debug(f"_run_rom_sync({rom_id}): slot not confirmed, skipping bulk sync")
            return 0, 0, [], []
        core_so = await self._loop.run_in_executor(None, self.resolve_core, rom_id)
        default_slot = resolve_default_slot(self._settings)
        cleanup_limit = autocleanup_limit(self._settings)

        # Confirmed non-legacy ROM with no caller-supplied session → open a
        # transport-only session of our own (operations ignored, ADR-0017).
        own_session_id: int | None = None
        # Share one content_hash per save across this ROM's passes — the negotiate
        # inventory, the newest-wins matrix, and the post-op baseline write all
        # hash the same files (#1457). Reentrant with the bulk-sweep scope.
        with self._save_file_store.hash_memo_scope():
            if session_id is None and save_state.slot_confirmed and save_state.active_slot:
                own_session_id = await self._open_negotiate_session(rom_id, device_id)

            uploaded = 0
            downloaded = 0
            errors: list[str] = []
            conflicts: list[dict[str, Any]] = []
            try:
                uploaded, downloaded, errors, conflicts = await self._loop.run_in_executor(
                    None, self.do_sync_rom_saves, rom_id, save_state, device_id, core_so, default_slot, cleanup_limit
                )
                await self._loop.run_in_executor(None, self._write_save_state, rom_id, save_state)
            finally:
                synced = uploaded + downloaded
                if own_session_id is not None:
                    await self._close_negotiate_session(own_session_id, synced, len(errors))
                elif session_counts is not None:
                    session_counts[0] += synced
                    session_counts[1] += len(errors)
            return uploaded, downloaded, errors, conflicts

    async def _open_negotiate_session(self, rom_id: int, device_id: str | None) -> int | None:
        """Open a transport-only negotiate session for a confirmed ROM; ``None`` on failure.

        POSTs the ROM-scoped inventory to ``negotiate`` and keeps only the
        ``session_id`` — the planned ``operations`` are intentionally discarded
        because the local ``compute_sync_action`` matrix is the sole detection
        authority (ADR-0017). Any failure degrades to ``None`` — the sync run
        proceeds without a session envelope rather than aborting — EXCEPT the
        server-side sync-disabled 400 (:class:`RommSyncDisabledError`), which is
        re-raised so the run aborts with a visible policy reason (#1489). An
        unclosed session lingers harmlessly until this device's next
        ``negotiate`` cancels it, so a missed close is harmless.
        """
        try:
            inventory = await self._loop.run_in_executor(None, self._build_inventory, rom_id)
            response = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.negotiate_sync(device_id or "", inventory)),
            )
            return response["session_id"]
        except RommSyncDisabledError:
            raise
        except Exception as e:
            self._log_debug(f"_run_rom_sync({rom_id}): negotiate session open failed ({e}) — syncing without a session")
            return None

    async def _close_negotiate_session(self, session_id: int, completed: int, failed: int) -> None:
        """Close a negotiate session, reporting op counts (non-fatal).

        Invoked off-loop like :meth:`_write_save_state` and swallows any failure:
        a session the server never hears closed lingers until it is cancelled by
        this device's next ``negotiate``, so a failed close must never fail the
        sync run.
        """
        try:
            await self._loop.run_in_executor(
                None,
                lambda: self._romm_api.complete_sync_session(
                    session_id, operations_completed=completed, operations_failed=failed
                ),
            )
        except Exception as e:
            self._log_debug(f"complete_sync_session({session_id}) failed (non-fatal): {e}")

    async def pre_launch_sync(self, rom_id: int) -> dict[str, Any]:
        """Download newer saves from server before game launch."""
        rom_id = int(rom_id)
        # Cheap stateless early-out before the device gate — never queue behind
        # an in-flight run just to report the feature is disabled.
        if not self.is_save_sync_enabled():
            return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

        try:
            async with self._device_gate.bounded_run(max_wait=PRE_LAUNCH_GATE_TIMEOUT), self.rom_lock(rom_id):
                # Defense in depth: block pre_launch_sync if a future caller bypasses
                # the @migration_blocked decorator at the public callable. saves_dir
                # would otherwise resolve under the new home and silently desync from
                # files still living at the old home. Internal do_sync_rom_saves callers
                # (sync_all_saves, rollback_to_version) are protected by the decorator
                # on their own public callables — this guard is for pre_launch_sync.
                if self._is_retrodeck_migration_pending():
                    return {
                        "success": False,
                        "reason": "blocked_by_migration",
                        "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                        "synced": 0,
                        "blocked_by_migration": True,
                    }

                # Refresh save-sort state before the migration gate — see #238.
                await self._refresh_save_sort_state("pre_launch_sync")

                # Hard-gate: saves go to the content dir — sync is impossible (#239).
                if self._save_sync_blocked():
                    return self._content_dir_skip()

                if self._rom_info.is_save_sort_changed():
                    return {
                        "success": False,
                        "reason": "save_sort_changed",
                        "message": "RetroArch save sorting changed — migrate saves in Settings first",
                        "synced": 0,
                        "save_sort_changed": True,
                    }

                if not sync_before_launch(self._settings):
                    return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

                # Pre-probe reachability before any sync work — mirror post_exit_sync.
                # A genuine reachability failure surfaces the canonical unreachable
                # shape (plus the additive ``offline`` flag) so the launch path can
                # warn on local drift instead of stalling on a doomed round-trip; an
                # auth/SSL/server error instead carries its OWN classified reason so
                # the UI stops lying about reachability (#971).
                try:
                    await self._loop.run_in_executor(None, self._romm_api.heartbeat)
                except Exception as e:
                    return self._heartbeat_failure_result("pre_launch_sync", e)

                failure = await self._ensure_device_live_or_fail()
                if failure is not None:
                    return failure

                uploaded, downloaded, errors, conflicts = await self._run_rom_sync(rom_id)
                synced = uploaded + downloaded

                msg = f"Downloaded {synced} save(s)"
                if errors:
                    msg += f", {len(errors)} error(s)"
                return {
                    "success": len(errors) == 0,
                    "message": msg,
                    "synced": synced,
                    "uploaded": uploaded,
                    "downloaded": downloaded,
                    "errors": errors,
                    "conflicts": list(conflicts),
                }
        except SaveSyncTimeoutError:
            # Another save-sync run held the device gate past the bounded wait —
            # treat as offline so the launch path warns on local drift instead
            # of trapping the Play button. Mirrors _heartbeat_failure_result.
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Save-sync busy — treating as offline",
                "synced": 0,
                "offline": True,
            }
        except RommSyncDisabledError:
            # RomM has save sync disabled for this device server-side. Mirror the
            # LOCAL toggle-off silent skip (a success-shaped result, no ``offline``
            # and no failure ``reason``) so the launch proceeds and the result
            # never routes into the offline/launch-gate flow (#1489).
            return {"success": True, "message": DEVICE_SYNC_DISABLED, "synced": 0}

    async def post_exit_sync(self, rom_id: int) -> dict[str, Any]:
        """Upload changed saves after game exit."""
        self._logger.info("post_exit_sync called for rom_id=%d", rom_id)
        rom_id = int(rom_id)

        # Cheap stateless early-out before the device gate — never queue behind
        # an in-flight run just to report the feature is disabled.
        if not self.is_save_sync_enabled():
            self._logger.info("post_exit_sync skipped: save sync disabled")
            return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

        try:
            async with self._device_gate.bounded_run(max_wait=POST_EXIT_GATE_TIMEOUT), self.rom_lock(rom_id):
                # Defense in depth: same rationale as pre_launch_sync — internal
                # do_sync_rom_saves callers are protected by @migration_blocked on
                # their public callables; this guard covers post_exit_sync only.
                if self._is_retrodeck_migration_pending():
                    self._logger.info("post_exit_sync skipped: retrodeck migration pending")
                    return {
                        "success": False,
                        "reason": "blocked_by_migration",
                        "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                        "synced": 0,
                        "blocked_by_migration": True,
                    }

                if not sync_after_exit(self._settings):
                    self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
                    return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

                # Refresh save-sort state before do_sync_rom_saves reads saves_dir — see #238.
                await self._refresh_save_sort_state("post_exit_sync")

                # Hard-gate: saves go to the content dir — sync is impossible (#239).
                if self._save_sync_blocked():
                    self._logger.info("post_exit_sync skipped: savefiles_in_content_dir")
                    return self._content_dir_skip()

                try:
                    await self._loop.run_in_executor(None, self._romm_api.heartbeat)
                except Exception as e:
                    return self._heartbeat_failure_result("post_exit_sync", e)

                failure = await self._ensure_device_live_or_fail()
                if failure is not None:
                    return failure

                uploaded, downloaded, errors, conflicts = await self._run_rom_sync(rom_id)
                synced = uploaded + downloaded

                self._logger.info(
                    "post_exit_sync complete for rom_id=%d: uploaded=%d, downloaded=%d, errors=%d, conflicts=%d",
                    rom_id,
                    uploaded,
                    downloaded,
                    len(errors),
                    len(conflicts),
                )

                msg = _summarize_sync_result(
                    f"Uploaded {synced} save(s)", synced=synced, errors=errors, conflicts=len(conflicts)
                )
                return {
                    "success": len(errors) == 0,
                    "message": msg,
                    "synced": synced,
                    "uploaded": uploaded,
                    "downloaded": downloaded,
                    "errors": errors,
                    "conflicts": list(conflicts),
                }
        except SaveSyncTimeoutError:
            # Another save-sync run held the device gate past the bounded wait —
            # skip the post-exit upload rather than block on a stuck run.
            self._logger.info("post_exit_sync skipped: save-sync busy")
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Save-sync busy — skipping post-exit sync",
                "synced": 0,
                "offline": True,
            }
        except RommSyncDisabledError:
            # RomM has save sync disabled for this device server-side — stop with
            # a visible policy reason. The session-end toast reads the dedicated
            # copy via ``_render_failure_toast`` keyed on this reason (#1489).
            self._logger.info("post_exit_sync stopped: sync disabled for this device on the server")
            return {
                "success": False,
                "reason": DEVICE_SYNC_DISABLED_REASON,
                "message": DEVICE_SYNC_DISABLED,
                "synced": 0,
                "errors": [],
                "conflicts": [],
            }

    async def sync_rom_saves(self, rom_id: int) -> dict[str, Any]:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        rom_id = int(rom_id)
        # Cheap stateless early-out before the device gate — never queue behind
        # an in-flight run just to report the feature is disabled.
        if not self.is_save_sync_enabled():
            return {
                "success": False,
                "reason": SAVE_SYNC_DISABLED_REASON,
                "message": SAVE_SYNC_DISABLED,
                "synced": 0,
            }

        try:
            async with self._device_gate.bounded_run(max_wait=SYNC_ROM_GATE_TIMEOUT), self.rom_lock(rom_id):
                # Refresh save-sort state before do_sync_rom_saves reads saves_dir — see #238.
                # Manual sync paths must observe fresh sort state too: a user could
                # edit retroarch.cfg outside of a session and then trigger a manual
                # sync before any detect has fired.
                await self._refresh_save_sort_state("sync_rom_saves")

                # Hard-gate: saves go to the content dir — sync is impossible (#239).
                if self._save_sync_blocked():
                    return self._content_dir_skip()

                failure = await self._ensure_device_live_or_fail()
                if failure is not None:
                    return failure

                uploaded, downloaded, errors, conflicts = await self._run_rom_sync(rom_id)
                synced = uploaded + downloaded

                msg = _summarize_sync_result(
                    f"Synced {synced} save(s)", synced=synced, errors=errors, conflicts=len(conflicts)
                )
                return {
                    "success": len(errors) == 0,
                    "message": msg,
                    "synced": synced,
                    "uploaded": uploaded,
                    "downloaded": downloaded,
                    "errors": errors,
                    "conflicts": list(conflicts),
                }
        except SaveSyncTimeoutError:
            # Another save-sync run held the device gate past the bounded wait.
            return {
                "success": False,
                "reason": "sync_busy",
                "message": "Another save-sync run is in progress",
                "synced": 0,
                "errors": [],
                "conflicts": [],
            }
        except RommSyncDisabledError:
            # RomM has save sync disabled for this device server-side — surface it
            # as a policy stop; the game-detail toast renders ``message`` (#1489).
            return {
                "success": False,
                "reason": DEVICE_SYNC_DISABLED_REASON,
                "message": DEVICE_SYNC_DISABLED,
                "synced": 0,
                "errors": [],
                "conflicts": [],
            }

    def _installed_rom_ids(self) -> list[int]:
        """Read the installed-ROM ids from the rom_installs aggregate (WS3)."""
        with self._uow_factory() as uow:
            return sorted(install.rom_id for install in uow.rom_installs.iter_all())

    async def _bulk_pre_negotiate(self, device_id: str | None) -> int | None:
        """Open one whole-device transport-only negotiate session for the bulk sweep.

        Builds the full ``ClientSaveState`` inventory (every confirmed non-legacy
        ROM with local saves) and POSTs ``negotiate`` once, keeping only the
        ``session_id`` — the planned ``operations`` are discarded because detection
        is the local ``compute_sync_action`` matrix (ADR-0017). Returns the
        ``session_id``, or ``None`` on an empty inventory (nothing to negotiate) or
        any negotiate failure. When ``None``, each confirmed non-legacy ROM opens
        its own per-ROM session inside ``_run_rom_sync`` — the sweep degrades,
        never aborts. The sync verdicts are unaffected either way; only the
        session envelope (one shared vs. per-ROM) differs. The one exception is
        the server-side sync-disabled 400 (:class:`RommSyncDisabledError`), which
        is re-raised so the whole sweep aborts with a visible policy reason (#1489).
        """
        full_inventory = await self._loop.run_in_executor(None, self._build_inventory, None)
        if not full_inventory:
            return None
        try:
            response = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.negotiate_sync(device_id or "", full_inventory)),
            )
            # Read the key INSIDE the guard: a 200 body missing session_id must
            # degrade like a failure (session_id None → per-ROM sessions), not
            # abort the whole sweep.
            return response["session_id"]
        except RommSyncDisabledError:
            raise
        except Exception as e:
            self._logger.warning("sync_all_saves: negotiate session open failed (%s) — per-ROM sessions", e)
            return None

    async def sync_all_saves(self) -> dict[str, Any]:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        # Cheap stateless early-out before the device gate — never queue behind
        # an in-flight run just to report the feature is disabled.
        if not self.is_save_sync_enabled():
            return {
                "success": False,
                "reason": SAVE_SYNC_DISABLED_REASON,
                "message": SAVE_SYNC_DISABLED,
                "synced": 0,
                "conflicts": 0,
            }

        try:
            # Device gate sits OUTSIDE the per-ROM locks — it wraps the whole
            # sweep; each ROM still takes its own rom_lock inside the loop.
            async with self._device_gate.bounded_run(max_wait=SYNC_ALL_GATE_TIMEOUT):
                # Refresh save-sort state before do_sync_rom_saves reads saves_dir — see #238.
                # Manual sync paths must observe fresh sort state too: a user could
                # edit retroarch.cfg outside of a session and then trigger a manual
                # sync before any detect has fired.
                await self._refresh_save_sort_state("sync_all_saves")

                # Hard-gate: saves go to the content dir — sync is impossible (#239).
                if self._save_sync_blocked():
                    return self._content_dir_skip(all_saves=True)

                failure = await self._ensure_device_live_or_fail()
                if failure is not None:
                    return failure

                # One whole-device transport-only negotiate session wraps the
                # sweep (ADR-0017); when it can't open, each confirmed non-legacy
                # ROM opens its own session inside _run_rom_sync. Detection is the
                # local matrix for every ROM regardless.
                device_id = self.get_device_id()
                # Share one content_hash per save across the whole sweep: the bulk
                # pre-negotiate inventory and every per-ROM matrix hash the same
                # files (#1457). The per-ROM scope inside _run_rom_sync nests under
                # this one (reentrant), so the memo spans the entire run.
                with self._save_file_store.hash_memo_scope():
                    session_id = await self._bulk_pre_negotiate(device_id)
                    session_counts = [0, 0]

                    total_synced = 0
                    total_errors: list[str] = []
                    all_conflicts: list[dict[str, Any]] = []
                    rom_count = 0

                    # Only iterate installed ROMs — non-installed ROMs have no save files
                    rom_ids = await self._loop.run_in_executor(None, self._installed_rom_ids)
                    self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

                    try:
                        for rom_id_int in rom_ids:
                            rom_count += 1
                            async with self.rom_lock(rom_id_int):
                                uploaded, downloaded, errors, conflicts = await self._run_rom_sync(
                                    rom_id_int,
                                    require_confirmed=True,
                                    session_id=session_id,
                                    session_counts=session_counts if session_id is not None else None,
                                )
                            total_synced += uploaded + downloaded
                            total_errors.extend(errors)
                            all_conflicts.extend(conflicts)
                    except RommSyncDisabledError:
                        # A per-ROM negotiate hit RomM's per-device sync-disabled
                        # switch mid-sweep (the bulk session had degraded to None).
                        # Abort the loop and report the partial totals accrued so
                        # far (#1489). The finally still closes any bulk session.
                        return {
                            "success": False,
                            "reason": DEVICE_SYNC_DISABLED_REASON,
                            "message": f"{DEVICE_SYNC_DISABLED} — stopped after syncing {total_synced} save(s)",
                            "synced": total_synced,
                            "conflicts": len(all_conflicts),
                            "conflicts_list": list(all_conflicts),
                            "roms_checked": rom_count,
                            "errors": total_errors,
                        }
                    finally:
                        if session_id is not None:
                            await self._close_negotiate_session(session_id, session_counts[0], session_counts[1])

                conflicts_count = len(all_conflicts)
                msg = _summarize_sync_result(
                    f"Synced {total_synced} save(s) across {rom_count} ROM(s)",
                    synced=total_synced,
                    errors=total_errors,
                    conflicts=conflicts_count,
                )
                return {
                    "success": len(total_errors) == 0,
                    "message": msg,
                    "synced": total_synced,
                    "conflicts": conflicts_count,
                    "conflicts_list": list(all_conflicts),
                    "roms_checked": rom_count,
                    "errors": total_errors,
                }
        except SaveSyncTimeoutError:
            # Another save-sync run held the device gate past the bounded wait.
            return {
                "success": False,
                "reason": "sync_busy",
                "message": "Another save-sync run is in progress",
                "synced": 0,
                "conflicts": 0,
                "conflicts_list": [],
                "roms_checked": 0,
                "errors": [],
            }
        except RommSyncDisabledError:
            # The whole-device bulk pre-negotiate hit RomM's per-device
            # sync-disabled switch before the sweep began — abort with the policy
            # reason and no partial totals (nothing was synced yet, #1489).
            return {
                "success": False,
                "reason": DEVICE_SYNC_DISABLED_REASON,
                "message": DEVICE_SYNC_DISABLED,
                "synced": 0,
                "conflicts": 0,
                "conflicts_list": [],
                "roms_checked": 0,
                "errors": [],
            }

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        server_save_id: int,
        action: str,
    ) -> dict[str, Any]:
        """Resolve a pending sync conflict (true two-sided divergence).

        Reached when ``compute_sync_action`` returned ``Conflict`` — the
        server moved AND local diverged from baseline, so the user picked a
        side via the conflict UI.

        ``server_save_id`` is the id of the server save that was surfaced to
        the user in the conflict modal. The backend round-trips it: if a
        third device has uploaded a newer save into the slot since the modal
        opened, the picked server head won't match and we return
        ``reason="stale_conflict"`` instead of silently overwriting the
        third device's work.

        ``action`` is one of:

        - ``"keep_local"`` — push local to the current server save
          (POST overwrite=true). When the local content already matches the
          server's content hash we adopt it silently without re-uploading.
        - ``"use_server"`` — download the current server save, replacing local.
        """
        rom_id_int = int(rom_id)
        async with self.rom_lock(rom_id_int):
            # #239: RetroArch writes saves to the content dir — both keep_local
            # (POST after reading the local file under saves_dir) and use_server
            # (download into saves_dir) write to a directory RetroArch ignores,
            # so the resolution could not take effect. Refuse before the
            # orchestrator does any server fetch or file write.
            if await self.content_dir_blocked("resolve_sync_conflict"):
                return {
                    "success": False,
                    "reason": SAVE_SYNC_IN_CONTENT_DIR_REASON,
                    "message": SAVE_SYNC_IN_CONTENT_DIR,
                }
            return await self._rollback.resolve(
                rom_id_int,
                filename,
                server_save_id,
                action,
                loop=self._loop,
            )
