from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from dataclasses import asdict
from typing import ClassVar

from models.saves import SaveConflict

from domain.emulator_tag import detect_core_change
from domain.save_extensions import get_save_extensions
from domain.save_path import resolve_save_dir, sanitize_save_filename
from lib.errors import classify_error
from lib.iso_time import parse_iso_to_epoch
from services.protocols import (
    RetryStrategy,
    RommApiProtocol,
)
from services.saves._config import SaveServiceConfig
from services.saves._helpers import _local_save_target
from services.saves._messages import DEVICE_NOT_REGISTERED, SAVE_SYNC_DISABLED
from services.saves.slots import SlotsService
from services.saves.slots.service import _NO_MIGRATION
from services.saves.state import StateService
from services.saves.status import StatusService
from services.saves.sync_engine import SyncEngine
from services.saves.versions import VersionsService


class SaveService:
    """Façade for bidirectional save file sync between RetroDECK and RomM.

    Composes the save-sync sub-services (state, sync_engine, status,
    versions, slots) and exposes the callable surface consumed by the
    Decky entrypoints. All RomM communication routes through
    ``RommApiProtocol``; no direct ``import decky``.

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
        Live reference to the save-sync state dict. Caller should
        pre-populate via :meth:`init_state` / :meth:`load_state`.
    config:
        Construction-time wiring bundle (paths, callbacks, asyncio loop,
        logger, plugin metadata). See :class:`SaveServiceConfig` for the
        per-field rationale.
    """

    _LOG_LEVELS: ClassVar[dict[str, int]] = {"debug": 0, "info": 1, "warn": 2, "error": 3}

    def __init__(
        self,
        *,
        romm_api: RommApiProtocol,
        retry: RetryStrategy,
        settings: dict,
        state: dict,
        save_sync_state: dict,
        config: SaveServiceConfig,
    ) -> None:
        self._romm_api = romm_api
        self._retry = retry
        self._settings = settings
        self._state = state
        self._config = config
        self._state_svc = StateService(
            save_sync_state=save_sync_state,
            state=state,
            persister=config.save_sync_state_persister,
            logger=config.logger,
        )
        # Alias the dict so the dozens of self._save_sync_state[...] call
        # sites elsewhere in SaveService keep working unchanged. Both names
        # reference the same underlying dict object.
        self._save_sync_state = self._state_svc.data
        # Convenience aliases — the rest of the class body (and sub-services
        # via the ``_save_service`` back-ref) read these attributes directly.
        self._loop = config.loop
        self._logger = config.logger
        self._clock = config.clock
        self._save_file = config.save_file
        self._get_saves_path = config.get_saves_path
        self._get_roms_path = config.get_roms_path
        self._get_active_core = config.get_active_core
        self._get_core_name = config.get_core_name
        self._plugin_version = config.plugin_version
        self._emit = config.emit
        self._detect_sort_change = config.detect_sort_change
        self._is_retrodeck_migration_pending = config.is_retrodeck_migration_pending
        # Per-rom lock dict — serializes concurrent sync operations on the
        # same rom_id (pre_launch_sync, post_exit_sync, manual sync, resolve).
        self._rom_sync_locks: dict[int, asyncio.Lock] = {}
        self._sync_engine = SyncEngine(
            save_service=self,
            state_svc=self._state_svc,
            romm_api=self._romm_api,
            retry=self._retry,
            logger=self._logger,
            clock=self._clock,
            save_file=self._save_file,
        )
        self._versions = VersionsService(
            save_service=self,
            state_svc=self._state_svc,
            sync_engine=self._sync_engine,
            romm_api=self._romm_api,
            logger=self._logger,
        )
        self._status = StatusService(
            save_service=self,
            state_svc=self._state_svc,
            sync_engine=self._sync_engine,
            romm_api=self._romm_api,
            logger=self._logger,
        )
        self._slots = SlotsService(
            save_service=self,
            state_svc=self._state_svc,
            sync_engine=self._sync_engine,
            romm_api=self._romm_api,
            retry=self._retry,
            logger=self._logger,
            clock=self._clock,
            save_file=self._save_file,
        )

    def _rom_lock(self, rom_id: int) -> asyncio.Lock:
        """Return the lock for this rom_id, creating it lazily."""
        if rom_id not in self._rom_sync_locks:
            self._rom_sync_locks[rom_id] = asyncio.Lock()
        return self._rom_sync_locks[rom_id]

    # ------------------------------------------------------------------
    # Debug logging helper
    # ------------------------------------------------------------------

    def _log_debug(self, msg: str) -> None:
        configured = self._settings.get("log_level", "warn")
        if self._LOG_LEVELS.get("debug", 0) >= self._LOG_LEVELS.get(configured, 2):
            self._logger.info(msg)

    def _get_server_device_id(self) -> str | None:
        """Return the server device ID if registered, else None."""
        return self._save_sync_state.get("server_device_id")

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------

    @staticmethod
    def make_default_state() -> dict:
        """Return a fresh default save-sync state dict."""
        return StateService.make_default_state()

    def init_state(self) -> None:
        """Populate ``_save_sync_state`` with defaults (idempotent)."""
        self._state_svc.init_state()

    def _migrate_loaded_state(self) -> None:
        """Apply schema migrations to data just read from disk."""
        self._state_svc._migrate_loaded_state()

    def load_state(self) -> None:
        """Load save sync state from disk, merging with defaults."""
        self._state_svc.load_state()

    def save_state(self) -> None:
        """Persist save sync state to disk (atomic write)."""
        self._state_svc.save_state()

    def prune_orphaned_state(self) -> None:
        """Remove save sync state entries for rom_ids no longer in shortcut registry."""
        self._state_svc.prune_orphaned_state()

    # ------------------------------------------------------------------
    # ROM / path helpers
    # ------------------------------------------------------------------

    def _resolve_retroarch_corename(self, system: str, rom_filename: str) -> tuple[str | None, str | None]:
        """Resolve the RetroArch ``corename`` for a system/ROM.

        Asks ES-DE (via ``get_active_core``) **which** core is active for
        this ROM, then asks the RetroArch ``.info`` parser (via
        ``get_core_name``) **what** RetroArch calls that core in its own
        subsystem — which is the authoritative name used for per-core save
        subdirectories when ``sort_savefiles_enable`` is active.

        One parser per source: the ES-DE label (second element of the
        ``get_active_core`` tuple) is NOT a valid substitute for the
        RetroArch corename. See the Config-Source-Parsers wiki page and
        the reference implementation in ``MigrationService``.

        Returns ``(corename, core_so)``. Either element may be ``None``
        when resolution fails at that step: ``core_so`` is ``None`` when
        ES-DE cannot determine the active core, ``corename`` is ``None``
        when ``.info`` parsing returns nothing (or when ``get_core_name``
        is not injected). Returning the tuple — rather than just
        ``corename`` — lets callers include ``core_so`` in diagnostic
        logs so users can identify which ``.info`` file is at fault.
        Callers choose their own fallback strategy (e.g. warn and fall
        back for critical-path SaveService flows; skip and warn for
        one-shot migrations).
        """
        if self._get_core_name is None:
            return (None, None)
        core_so, _label = self._get_active_core(system, rom_filename)
        if not core_so:
            return (None, None)
        corename = self._get_core_name(core_so)
        return (corename or None, core_so)

    def _get_rom_save_info(self, rom_id: int) -> dict | None:
        """Get save-related info for an installed ROM.

        Returns dict with keys: system, rom_name, saves_dir, platform_slug, file_path
        or None if not installed.
        """
        rom_id_str = str(int(rom_id))
        installed = self._state["installed_roms"].get(rom_id_str)
        if not installed:
            return None
        system = installed.get("system", "")
        file_path = installed.get("file_path", "")
        platform_slug = installed.get("platform_slug", "")
        if not system or not file_path:
            return None
        rom_name = os.path.splitext(os.path.basename(file_path))[0]

        # Use domain save path resolution.
        # Read sort settings from state (populated by MigrationService at startup).
        # When a save-sort migration is pending, prefer the *previous* layout:
        # RetroArch caches its runtime save-path at game-load time, so the
        # session that just ended still wrote to the old directory. Reading
        # the current settings here would point sync at the wrong location
        # and risk downloading stale server content to the new layout (#238).
        saves_base = self._get_saves_path()
        roms_base = self._get_roms_path()
        sort_state = self._pending_sort_settings() or self._state.get("save_sort_settings")
        if sort_state:
            sort_by_content = sort_state.get("sort_by_content", True)
            sort_by_core = sort_state.get("sort_by_core", False)
        else:
            sort_by_content, sort_by_core = True, False  # RetroDECK defaults

        # When sort-by-core is active, RetroArch writes per-core subdirs named
        # by the .info ``corename`` field. Resolve it via the dedicated parser.
        # See docs: Config-Source-Parsers wiki page ("one parser per source").
        # Decision: warn-and-fallback (not fail-loud like MigrationService).
        # SaveService is the critical-path sync flow — every game launch
        # depends on it. Fail-loud would take down save sync entirely on any
        # .info hiccup. MigrationService can afford strictness (one-shot),
        # SaveService cannot (continuous). See issue #232 for history.
        core_name: str | None = None
        if sort_by_core:
            rom_filename = os.path.basename(file_path)
            core_name, core_so = self._resolve_retroarch_corename(system, rom_filename)
            if core_name is None:
                self._logger.warning(
                    "SaveService: unable to resolve RetroArch corename for "
                    "%s/%s (core_so=%s) while sort_by_core is enabled. "
                    "Falling back to the parent save directory, which will "
                    "not match what RetroArch reads at runtime. Check that "
                    "the core's .info file is readable under the RetroDECK "
                    "Flatpak cores directory.",
                    system,
                    rom_filename,
                    core_so if core_so else "unresolved",
                )

        saves_dir = resolve_save_dir(
            file_path,
            saves_base,
            system,
            roms_base=roms_base,
            sort_by_content=sort_by_content,
            sort_by_core=sort_by_core,
            core_name=core_name,
        )

        return {
            "system": system,
            "rom_name": rom_name,
            "saves_dir": saves_dir,
            "platform_slug": platform_slug,
            "file_path": file_path,
        }

    def _pending_sort_settings(self) -> dict | None:
        """Return previous save-sort settings if a migration is pending, else None.

        Rejects empty dicts to avoid the half-state where ``_get_rom_save_info``'s
        ``or`` fallback would treat ``{}`` as "no pending migration" (and read
        current settings) while ``_is_save_sort_changed`` would treat the same
        ``{}`` as "pending" (and gate sync). Both call sites must agree on
        what counts as pending — see #238 review finding 3.
        """
        prev = self._state.get("save_sort_settings_previous")
        return prev if prev else None

    def _is_save_sort_changed(self) -> bool:
        """Check if a save sort migration is pending (detected by MigrationService)."""
        return self._pending_sort_settings() is not None

    # ------------------------------------------------------------------
    # File Helpers
    # ------------------------------------------------------------------

    def _file_md5(self, path: str) -> str:
        """Compute MD5 hash of a file for sync drift detection.

        Delegates to the injected ``SaveFileAdapter``. The hash is used
        for content-comparison only — drift detection between local
        file and the recorded ``last_sync_hash`` baseline. A collision
        here would mean two different save files treated as identical
        → "sync misses an update", not a security breach.
        """
        return self._save_file.checksum_md5(path)

    def _find_save_files(self, rom_id: int) -> list[dict]:
        """Find local save files for a ROM.

        Returns list of ``{"path": str, "filename": str}``.
        """
        info = self._get_rom_save_info(rom_id)
        if not info:
            return []
        rom_name = info["rom_name"]
        saves_dir = info["saves_dir"]
        platform_slug = info["platform_slug"]
        if not self._save_file.is_dir(saves_dir):
            return []
        results = []
        for ext in get_save_extensions(platform_slug):
            save_path = os.path.join(saves_dir, rom_name + ext)
            if self._save_file.is_file(save_path):
                results.append({"path": save_path, "filename": rom_name + ext})
        return results

    def _is_save_sync_enabled(self) -> bool:
        """Check if save sync feature is enabled."""
        return self._save_sync_state.get("settings", {}).get("save_sync_enabled", False)

    # ------------------------------------------------------------------
    # Public async API (callable endpoints)
    # ------------------------------------------------------------------

    async def ensure_device_registered(self) -> dict:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        if not self._is_save_sync_enabled():
            return {"success": False, "device_id": "", "device_name": "", "disabled": True}

        # Probe the RomM version when it has not been observed yet. Device
        # registration is the entrypoint reached from background launchers
        # that never call test_connection first, so the version on the API
        # adapter would otherwise stay None and version-gated server-side
        # features couldn't be enabled until the next manual connection
        # test. Probe failures are non-fatal — the registration call below
        # still proceeds and the adapter just retains its current version.
        if not self._romm_api.get_version():
            try:
                heartbeat = await self._loop.run_in_executor(None, self._romm_api.heartbeat)
                with contextlib.suppress(AttributeError, TypeError):
                    version = heartbeat.get("SYSTEM", {}).get("VERSION")
                    if version:
                        self._romm_api.set_version(version)
            except Exception as e:
                self._logger.debug(f"ensure_device_registered: version probe failed (non-fatal): {e}")

        # Already registered
        has_device_id = self._save_sync_state.get("device_id")
        has_server_id = self._save_sync_state.get("server_device_id")
        if has_device_id and has_server_id:
            with contextlib.suppress(Exception):
                await self._loop.run_in_executor(
                    None,
                    lambda: self._romm_api.update_device(has_server_id, client_version=self._plugin_version),
                )
            return {
                "success": True,
                "device_id": self._save_sync_state["device_id"],
                "device_name": self._save_sync_state.get("device_name", ""),
                "server_device_id": has_server_id,
            }

        hostname = socket.gethostname()

        try:
            result = await self._loop.run_in_executor(
                None,
                lambda: self._romm_api.register_device(
                    name=hostname,
                    platform="linux",
                    client="decky-romm-sync",
                    client_version=self._plugin_version,
                ),
            )
            server_device_id = result.get("id") or result.get("device_id")
            if server_device_id:
                self._save_sync_state["device_id"] = str(server_device_id)
                self._save_sync_state["device_name"] = hostname
                self._save_sync_state["server_device_id"] = str(server_device_id)
                self.save_state()
                self._logger.info(f"Device registered with server: {server_device_id} ({hostname})")
                return {
                    "success": True,
                    "device_id": str(server_device_id),
                    "device_name": hostname,
                    "server_device_id": str(server_device_id),
                }
        except Exception as e:
            self._logger.warning(f"Server device registration failed: {e}")

        return {"success": False, "device_id": "", "device_name": "", "error": "registration_failed"}

    async def get_save_status(self, rom_id: int) -> dict:
        """Get save sync status for a ROM (local files, server saves, conflict state)."""
        rom_id = int(rom_id)

        server_saves: list[dict] = []
        try:
            device_id = self._get_server_device_id()
            server_saves = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_saves(rom_id, device_id=device_id)),
            )
        except Exception as e:
            self._log_debug(f"Failed to fetch saves for rom {rom_id}: {e}")

        return await self._loop.run_in_executor(None, self._status._get_save_status_io, rom_id, server_saves)

    async def check_save_status_background(self, rom_id: int) -> None:
        """Run full save status check in background and emit result to frontend."""
        try:
            result = await self.get_save_status(rom_id)
            if self._emit is not None:
                await self._emit("save_status_updated", result)
        except Exception as e:
            self._log_debug(f"Background save status check failed for rom {rom_id}: {e}")

    async def list_devices(self) -> dict:
        """List all devices registered with the RomM server for this user."""
        if not self._is_save_sync_enabled():
            return {"success": False, "devices": [], "disabled": True}
        try:
            own_id = self._get_server_device_id()
            devices = await self._loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_devices()),
            )
            own_id_str = str(own_id or "")
            enriched = [
                {**d, "is_current_device": bool(own_id_str) and (str(d.get("id") or "")) == own_id_str} for d in devices
            ]
            return {"success": True, "devices": enriched}
        except Exception as e:
            self._log_debug(f"list_devices failed: {e}")
            return {"success": False, "devices": [], "error": "list_failed"}

    def check_core_change(self, rom_id: int) -> dict:
        """Check if emulator core changed since last sync for a ROM."""
        if not self._is_save_sync_enabled():
            return {"changed": False}

        rom_id_str = str(rom_id)
        save_entry = self._save_sync_state.get("saves", {}).get(rom_id_str)
        if not save_entry:
            return {"changed": False}  # Never synced

        stored_core = save_entry.get("last_synced_core")
        system = save_entry.get("system")
        if not stored_core or not system:
            return {"changed": False}

        # Resolve ROM filename for per-game core detection
        rom_filename = None
        installed = self._state.get("installed_roms", {}).get(rom_id_str)
        if installed:
            file_path = installed.get("file_path", "")
            if file_path:
                rom_filename = os.path.basename(file_path)

        # Core labels come from ES-DE config which may differ from RetroArch's
        # corename (e.g. "Snes9x - Current" vs "Snes9x"). Aligning with RetroArch
        # core names is tracked in #208.
        try:
            active_core, active_label = self._get_active_core(system, rom_filename)
        except Exception:
            return {"changed": False}

        changed = detect_core_change(stored_core, active_core)

        if not changed:
            return {"changed": False}

        # Strip _libretro suffix for display (stored_core is guaranteed non-None here)
        old_label = stored_core.replace("_libretro", "")

        return {
            "changed": True,
            "old_core": stored_core,
            "new_core": active_core,
            "old_label": old_label,
            "new_label": active_label or (active_core.replace("_libretro", "") if active_core else None),
        }

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
        config read error.
        """
        if self._detect_sort_change is None:
            return
        try:
            await self._loop.run_in_executor(None, self._detect_sort_change)
        except Exception as e:
            self._logger.warning(
                "%s: detect_sort_change failed (%s) — proceeding with stale state",
                where,
                e,
            )

    async def pre_launch_sync(self, rom_id: int) -> dict:
        """Download newer saves from server before game launch."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._is_save_sync_enabled():
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: block pre_launch_sync if a future caller bypasses
            # the @migration_blocked decorator at the public callable. saves_dir
            # would otherwise resolve under the new home and silently desync from
            # files still living at the old home. Internal _sync_rom_saves callers
            # (sync_all_saves, rollback_to_version) are protected by the decorator
            # on their own public callables — this guard is for pre_launch_sync.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            # Refresh save-sort state before the migration gate — see #238.
            await self._refresh_save_sort_state("pre_launch_sync")

            if self._is_save_sort_changed():
                return {
                    "success": False,
                    "message": "RetroArch save sorting changed — migrate saves in Settings first",
                    "synced": 0,
                    "save_sort_changed": True,
                }

            settings = self._save_sync_state.get("settings", {})
            if not settings.get("sync_before_launch", True):
                return {"success": True, "message": "Pre-launch sync disabled", "synced": 0}

            if not self._save_sync_state.get("device_id"):
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(
                None, self._sync_engine._sync_rom_saves, rom_id
            )
            self.save_state()

            msg = f"Downloaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def post_exit_sync(self, rom_id: int) -> dict:
        """Upload changed saves after game exit."""
        self._logger.info("post_exit_sync called for rom_id=%d", rom_id)
        rom_id = int(rom_id)

        async with self._rom_lock(rom_id):
            if not self._is_save_sync_enabled():
                self._logger.info("post_exit_sync skipped: save sync disabled")
                return {"success": True, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Defense in depth: same rationale as pre_launch_sync — internal
            # _sync_rom_saves callers are protected by @migration_blocked on
            # their public callables; this guard covers post_exit_sync only.
            if self._is_retrodeck_migration_pending and self._is_retrodeck_migration_pending():
                self._logger.info("post_exit_sync skipped: retrodeck migration pending")
                return {
                    "success": False,
                    "message": "Pending RetroDECK migration. Open the plugin QAM to migrate or dismiss.",
                    "synced": 0,
                    "blocked_by_migration": True,
                }

            settings = self._save_sync_state.get("settings", {})
            if not settings.get("sync_after_exit", True):
                self._logger.info("post_exit_sync skipped: sync_after_exit disabled")
                return {"success": True, "message": "Post-exit sync disabled", "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            await self._refresh_save_sort_state("post_exit_sync")

            try:
                await self._loop.run_in_executor(None, self._romm_api.heartbeat)
            except Exception:
                self._logger.info("post_exit_sync skipped: server offline")
                return {"success": False, "message": "Server offline", "synced": 0, "offline": True}

            if not self._save_sync_state.get("device_id"):
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(
                None, self._sync_engine._sync_rom_saves, rom_id
            )
            self.save_state()

            self._logger.info(
                "post_exit_sync complete for rom_id=%d: synced=%d, errors=%d, conflicts=%d",
                rom_id,
                synced,
                len(errors),
                len(conflicts),
            )

            msg = f"Uploaded {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def sync_rom_saves(self, rom_id: int) -> dict:
        """Bidirectional sync for a single ROM (manual trigger from game detail)."""
        rom_id = int(rom_id)
        async with self._rom_lock(rom_id):
            if not self._is_save_sync_enabled():
                return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0}

            # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
            # Manual sync paths must observe fresh sort state too: a user could
            # edit retroarch.cfg outside of a session and then trigger a manual
            # sync before any detect has fired.
            await self._refresh_save_sort_state("sync_rom_saves")

            if not self._save_sync_state.get("device_id"):
                reg = await self.ensure_device_registered()
                if not reg.get("success"):
                    return {"success": False, "message": DEVICE_NOT_REGISTERED}

            synced, errors, conflicts = await self._loop.run_in_executor(
                None, self._sync_engine._sync_rom_saves, rom_id
            )
            self.save_state()

            msg = f"Synced {synced} save(s)"
            if errors:
                msg += f", {len(errors)} error(s)"
            if conflicts:
                msg += f", {len(conflicts)} conflict(s)"
            return {
                "success": len(errors) == 0,
                "message": msg,
                "synced": synced,
                "errors": errors,
                "conflicts": [c if isinstance(c, dict) else asdict(c) for c in conflicts],
            }

    async def get_save_slots(self, rom_id: int) -> dict:
        """List available save slots for a ROM."""
        return await self._slots.get_save_slots(rom_id)

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Fetch server save files for a specific slot."""
        return await self._slots.get_slot_saves(rom_id, slot)

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict:
        """Switch the active save slot with immediate state sync."""
        return await self._slots.switch_slot(rom_id, new_slot)

    # ------------------------------------------------------------------
    # Save Setup Wizard
    # ------------------------------------------------------------------

    def is_save_tracking_configured(self, rom_id: int) -> dict:
        """Check if save slot tracking is configured for a game."""
        return self._slots.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id: int) -> dict:
        """Get info needed for the first-sync setup wizard."""
        return await self._slots.get_save_setup_info(rom_id)

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = _NO_MIGRATION,
    ) -> dict:
        """Confirm which slot to use for a game's save sync.

        ``migrate_from_slot`` may be the ``_NO_MIGRATION`` sentinel, ``None``,
        or ``"__no_migration__"`` (the string the frontend sends when no
        migration is requested). All three are treated as "no migration".
        """
        if migrate_from_slot is None or migrate_from_slot == "__no_migration__":
            migrate_from_slot = _NO_MIGRATION
        return await self._slots.confirm_slot_choice(rom_id, chosen_slot, migrate_from_slot)

    async def sync_all_saves(self) -> dict:
        """Manual full sync of all ROMs with shortcuts (both directions)."""
        if not self._is_save_sync_enabled():
            return {"success": False, "message": SAVE_SYNC_DISABLED, "synced": 0, "conflicts": 0}

        # Refresh save-sort state before _sync_rom_saves reads saves_dir — see #238.
        # Manual sync paths must observe fresh sort state too: a user could
        # edit retroarch.cfg outside of a session and then trigger a manual
        # sync before any detect has fired.
        await self._refresh_save_sort_state("sync_all_saves")

        if not self._save_sync_state.get("device_id"):
            reg = await self.ensure_device_registered()
            if not reg.get("success"):
                return {"success": False, "message": DEVICE_NOT_REGISTERED}

        total_synced = 0
        total_errors: list[str] = []
        all_conflicts: list[SaveConflict | dict] = []
        rom_count = 0

        # Only iterate installed ROMs — non-installed ROMs have no save files
        rom_ids = set(self._state["installed_roms"].keys())
        self._log_debug(f"sync_all_saves: {len(rom_ids)} ROMs to check")

        for rom_id_str in sorted(rom_ids):
            rom_count += 1
            rom_id_int = int(rom_id_str)
            async with self._rom_lock(rom_id_int):
                synced, errors, conflicts = await self._loop.run_in_executor(
                    None, self._sync_engine._sync_rom_saves, rom_id_int
                )
            total_synced += synced
            total_errors.extend(errors)
            all_conflicts.extend(conflicts)

        self.save_state()

        conflicts_count = len(all_conflicts)
        msg = f"Synced {total_synced} save(s) across {rom_count} ROM(s)"
        if total_errors:
            msg += f", {len(total_errors)} error(s)"
        if conflicts_count:
            msg += f", {conflicts_count} conflict(s)"
        return {
            "success": len(total_errors) == 0,
            "message": msg,
            "synced": total_synced,
            "conflicts": conflicts_count,
            "conflicts_list": [c if isinstance(c, dict) else asdict(c) for c in all_conflicts],
            "roms_checked": rom_count,
            "errors": total_errors,
        }

    async def resolve_sync_conflict(
        self,
        rom_id: int,
        filename: str,
        action: str,
    ) -> dict:
        """Resolve a pending sync conflict (true two-sided divergence).

        Reached when ``compute_sync_action`` returned ``Conflict`` — the
        server moved AND local diverged from baseline, so the user picked a
        side via the conflict UI.

        ``action`` is one of:

        - ``"keep_local"`` — push local to the current server save (PUT). When
          the local content already matches the server's content hash we adopt
          it silently without re-uploading.
        - ``"use_server"`` — download the current server save, replacing local.
        """
        rom_id = int(rom_id)
        rom_id_str = str(rom_id)

        if action not in ("keep_local", "use_server"):
            return {"success": False, "message": f"Invalid action: {action}"}

        # Frontend-supplied filename flows into ``os.path.join(saves_dir, …)``
        # via ``_resolve_conflict_keep_local``. Reject anything that isn't
        # already a clean basename — legitimate callers always pass one.
        try:
            sanitized = sanitize_save_filename(filename)
        except ValueError as e:
            self._logger.warning(
                "resolve_sync_conflict(rom_id=%d, action=%s) rejected invalid filename: %s",
                rom_id,
                action,
                e,
            )
            return {"success": False, "message": "Invalid filename"}
        if sanitized != filename:
            self._logger.warning(
                "resolve_sync_conflict(rom_id=%d, action=%s) rejected non-basename filename",
                rom_id,
                action,
            )
            return {"success": False, "message": "Invalid filename"}

        async with self._rom_lock(rom_id):
            info = self._get_rom_save_info(rom_id)
            if not info:
                return {"success": False, "message": "ROM not installed"}
            system = info["system"]
            saves_dir = info["saves_dir"]

            try:
                device_id = self._get_server_device_id()
                server_saves = await self._loop.run_in_executor(
                    None,
                    lambda: self._retry.with_retry(
                        lambda: self._romm_api.list_saves(rom_id, device_id=device_id),
                    ),
                )
            except Exception as e:
                _code, _msg = classify_error(e)
                return {"success": False, "message": f"Failed to fetch saves: {_msg}"}

            save_state = self._save_sync_state["saves"].get(rom_id_str, {})
            active_slot = save_state.get("active_slot")
            server_in_slot = SyncEngine._filter_server_saves_to_slot(server_saves, active_slot)
            if not server_in_slot:
                return {"success": False, "message": "No server save in active slot"}
            server = max(server_in_slot, key=lambda s: parse_iso_to_epoch(s.get("updated_at")) or 0.0)

            try:
                if action == "use_server":
                    await self._loop.run_in_executor(
                        None,
                        self._resolve_conflict_use_server,
                        rom_id_str,
                        server,
                        saves_dir,
                        system,
                        info["rom_name"],
                    )
                    self._logger.info(
                        "resolve_sync_conflict(rom_id=%d, filename=%s, action=%s) -> success",
                        rom_id,
                        filename,
                        action,
                    )
                    return {"success": True, "action": "use_server"}

                # keep_local
                await self._loop.run_in_executor(
                    None,
                    self._resolve_conflict_keep_local,
                    rom_id,
                    rom_id_str,
                    filename,
                    server,
                    saves_dir,
                    system,
                )
                self._logger.info(
                    "resolve_sync_conflict(rom_id=%d, filename=%s, action=%s) -> success",
                    rom_id,
                    filename,
                    action,
                )
                return {"success": True, "action": "keep_local"}
            except Exception as e:
                self._logger.error(f"resolve_sync_conflict({rom_id}, {filename}, {action}) failed: {e}")
                return {"success": False, "message": str(e)}

    def _resolve_conflict_use_server(
        self,
        rom_id_str: str,
        server: dict,
        saves_dir: str,
        system: str,
        rom_name: str,
    ) -> None:
        """Download *server* into the canonical local save file and update state.

        The write path is always ``<rom_name>.<server.file_extension>`` — the
        path RetroArch reads. Drives state-key consistency too:
        ``_update_file_sync_state`` receives the same target name the file
        lands at.
        """
        target = _local_save_target(server, rom_name)
        self._sync_engine._do_download_save(server, saves_dir, target, rom_id_str, system)
        self.save_state()

    def _resolve_conflict_keep_local(
        self,
        rom_id: int,
        rom_id_str: str,
        filename: str,
        server: dict,
        saves_dir: str,
        system: str,
    ) -> None:
        """Push the local file to *server* (PUT). Adopt-without-upload when the
        local content already matches the server's content hash.
        """
        local_path = os.path.join(saves_dir, filename)
        if not self._save_file.is_file(local_path):
            raise FileNotFoundError(f"Local save not found: {local_path}")
        local_hash = self._file_md5(local_path)
        try:
            server_hash = self._retry.with_retry(self._sync_engine._get_server_save_hash, server)
        except Exception:
            server_hash = None

        if server_hash and local_hash == server_hash:
            # Hashes match — adopt server's id without re-uploading.
            self._log_debug(
                f"keep_local: hash matches server, adopting without upload (rom={rom_id} filename={filename})"
            )
            saves = self._save_sync_state.setdefault("saves", {})
            rom_entry = saves.setdefault(rom_id_str, {"files": {}})
            files = rom_entry.setdefault("files", {})
            file_state = files.setdefault(filename, {})
            file_state["tracked_save_id"] = server.get("id")
            file_state["last_sync_hash"] = local_hash
            file_state["last_sync_at"] = self._clock.now().isoformat()
            file_state["last_sync_server_updated_at"] = server.get("updated_at", "")
            file_state["last_sync_server_size"] = server.get("file_size_bytes")
            file_state["last_sync_local_mtime"] = self._save_file.get_mtime(local_path)
            file_state["last_sync_local_size"] = self._save_file.get_size(local_path)
            self.save_state()
            return

        # Upload local content as a PUT against the existing server save.
        self._sync_engine._do_upload_save(rom_id, local_path, filename, rom_id_str, system, server)
        self.save_state()

    # ------------------------------------------------------------------
    # Version History API
    # ------------------------------------------------------------------

    async def list_file_versions(self, rom_id: int, slot: str, filename: str) -> list[dict]:
        """Delegate to :class:`VersionsService` — see its docstring."""
        return await self._versions.list_file_versions(rom_id, slot, filename)

    async def rollback_to_version(self, rom_id: int, slot: str, save_id: int) -> dict:
        """Delegate to :class:`VersionsService` — see its docstring."""
        return await self._versions.rollback_to_version(rom_id, slot, save_id)

    def get_save_sync_settings(self) -> dict:
        """Return current save sync settings."""
        settings = self._save_sync_state.get("settings", {})
        # Defensive defaults for keys added after initial release
        settings.setdefault("default_slot", "default")
        settings.setdefault("autocleanup_limit", 10)
        if not self._save_sync_state.get("settings"):
            settings.setdefault("save_sync_enabled", False)
            settings.setdefault("sync_before_launch", True)
            settings.setdefault("sync_after_exit", True)
        return settings

    @staticmethod
    def _sanitize_setting(key: str, value: object) -> tuple[object, bool]:
        """Validate and coerce a single settings key/value pair.

        Returns (coerced_value, skip) where skip=True means the value should
        be discarded (e.g. empty slot name).
        """
        if key == "default_slot":
            if value is None:
                return None, False  # None = legacy mode
            coerced = str(value).strip()
            return (coerced if coerced else None), False  # empty -> None
        if key == "autocleanup_limit":
            return max(1, int(value)), False  # type: ignore[arg-type]
        if key in ("save_sync_enabled", "sync_before_launch", "sync_after_exit"):
            return bool(value), False
        return value, False

    def update_save_sync_settings(self, settings: dict) -> dict:
        """Update save sync settings (sync toggles, slot, etc.)."""
        allowed_keys = {
            "save_sync_enabled",
            "sync_before_launch",
            "sync_after_exit",
            "default_slot",
            "autocleanup_limit",
        }

        current = self._save_sync_state.setdefault("settings", {})

        for key, value in settings.items():
            if key not in allowed_keys:
                continue
            value, skip = self._sanitize_setting(key, value)
            if skip:
                continue
            current[key] = value

        self.save_state()
        return {"success": True, "settings": current}

    def _delete_saves_for_roms(self, rom_ids: list[int]) -> tuple[int, list[str]]:
        """Delete local save files for the given ROM IDs and clear file tracking state.

        For each ROM ID, enumerates files via ``_find_save_files``, removes them
        on disk (counting successes and collecting per-file error strings), and
        clears the ROM's per-file tracking dict via ``StateService.clear_files_state``.
        Slot config (``active_slot``, ``slot_confirmed``, ``emulator``,
        ``last_synced_core``, ``own_upload_ids``, ``slots``, ``system``) is
        preserved. Persists state once at the end via ``save_state()``.

        Returns a ``(total_deleted, errors)`` tuple.
        """
        total_deleted = 0
        errors: list[str] = []
        for rom_id in rom_ids:
            rom_id_str = str(rom_id)
            files = self._find_save_files(rom_id)
            for f in files:
                try:
                    self._save_file.remove(f["path"])
                    total_deleted += 1
                except Exception as e:
                    errors.append(f"{f['filename']}: {e}")
            self._state_svc.clear_files_state(rom_id_str)

        self.save_state()
        return total_deleted, errors

    def delete_local_saves(self, rom_id: int) -> dict:
        """Delete local save files (.srm, .rtc) for a ROM."""
        rom_id = int(rom_id)

        deleted, errors = self._delete_saves_for_roms([rom_id])

        if deleted == 0 and not errors:
            return {"success": True, "deleted_count": 0, "message": "No local save files found"}

        if errors:
            return {
                "success": False,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        return {
            "success": True,
            "deleted_count": deleted,
            "message": f"Deleted {deleted} save file(s)",
        }

    def delete_platform_saves(self, platform_slug: str) -> dict:
        """Delete local save files for all installed ROMs on a platform."""
        rom_ids: list[int] = []
        for rom_id_str, entry in self._state["installed_roms"].items():
            if entry.get("platform_slug") != platform_slug:
                continue
            rom_ids.append(int(rom_id_str))

        rom_count = len(rom_ids)
        total_deleted, total_errors = self._delete_saves_for_roms(rom_ids)

        if total_errors:
            return {
                "success": False,
                "deleted_count": total_deleted,
                "message": (f"Deleted {total_deleted} file(s) from {rom_count} ROM(s), {len(total_errors)} error(s)"),
            }
        return {
            "success": True,
            "deleted_count": total_deleted,
            "message": f"Deleted {total_deleted} save file(s) from {rom_count} ROM(s)",
        }

    # ------------------------------------------------------------------
    # Slot deletion
    # ------------------------------------------------------------------

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict:
        """Return info about what deleting a slot would do, for the confirmation modal."""
        return await self._slots.get_slot_delete_info(rom_id, slot)

    async def delete_slot(self, rom_id: int, slot: str) -> dict:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        return await self._slots.delete_slot(rom_id, slot)
