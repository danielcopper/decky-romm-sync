"""LaunchGateService — single-callable launch-time gating verdict.

Composes the three pieces of information the frontend's pre-launch
interceptor needs into one round-trip: is this Steam app a RomM ROM,
is that ROM installed, and does it have an unresolved save conflict.
Returns a typed ``LaunchVerdict`` carrying the allow/block decision
plus optional reason and toast strings. Per-service migration-store
checks and the fire-and-forget migration refresh stay on the frontend
because they touch synchronous in-memory state and intentionally do
not block the launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols.cross_service import (
        LaunchGateDriftReader,
        LaunchGateInstalledChecker,
        LaunchGateRomLookup,
        LaunchGateSaveStatusReader,
    )
    from services.protocols.files import SaveFileStore


_TOAST_TITLE_NOT_INSTALLED = "RomM Sync"
_TOAST_BODY_NOT_INSTALLED = "ROM not downloaded. Open the game page to download it first."
_TOAST_TITLE_SAVE_CONFLICT = "RomM Save Sync"
_TOAST_BODY_SAVE_CONFLICT = "Save conflict detected — open game page to resolve before playing"
_TOAST_TITLE_SAVE_STATUS_FAILED = "RomM Save Sync"
_TOAST_BODY_SAVE_STATUS_FAILED = "Save-status check failed — retry?"


@dataclass(frozen=True)
class LaunchVerdict:
    """Outcome of a pre-launch evaluation for a single Steam app id.

    ``action="allow"`` means the launch may proceed (the ROM is either
    not a RomM ROM, is installed with save-sync disabled, is installed
    with no save conflict, or the save status check failed for a ROM
    with no tracked saves). ``action="warn"``
    means the launch may proceed but the frontend should surface a
    soft toast — used when ``get_save_status`` failed for a ROM that
    *does* have tracked saves, where silent allow would risk data loss
    on an unseen conflict. ``action="block"`` carries a machine-readable
    ``reason`` and the human-readable toast title and body the frontend
    surfaces to the user.
    """

    action: Literal["allow", "warn", "block"]
    reason: Literal["not_installed", "save_conflict", "save_status_failed"] | None = None
    toast_title: str | None = None
    toast_body: str | None = None


@dataclass(frozen=True)
class LaunchGateServiceConfig:
    """Frozen wiring bundle handed to ``LaunchGateService.__init__``.

    ``rom_lookup`` / ``installed_checker`` / ``save_status_reader`` are
    Protocol-typed cross-service seams that the composition root satisfies
    with the existing library, download, and save-sync services.
    ``drift_reader`` is the local-save-file enumeration + baseline-hash seam
    used by :meth:`LaunchGateService.check_local_drift`; ``save_file_store``
    hashes the on-disk files (content MD5) and ``loop`` offloads that
    blocking hash I/O to the executor. ``logger`` is carried for parity with
    sibling services and to log drift-check internal errors.
    """

    rom_lookup: LaunchGateRomLookup
    installed_checker: LaunchGateInstalledChecker
    save_status_reader: LaunchGateSaveStatusReader
    drift_reader: LaunchGateDriftReader
    save_file_store: SaveFileStore
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger


def _has_any_save_conflict(save_status: dict[str, Any] | None) -> bool:
    """Mirror the frontend ``hasAnySaveConflict`` predicate.

    The canonical conflict signal is a non-empty ``conflicts`` list on
    the save-status payload. Per-file ``status == "conflict"`` checks
    are intentionally not consulted — the backend emits a single
    ``sync_conflict`` type for true two-sided divergence and surfaces
    it via the ``conflicts`` array.
    """
    if not save_status:
        return False
    conflicts = save_status.get("conflicts")
    return bool(conflicts)


class LaunchGateService:
    """Pre-launch gating verdict composed from three cross-service reads."""

    def __init__(self, *, config: LaunchGateServiceConfig) -> None:
        self._rom_lookup = config.rom_lookup
        self._installed_checker = config.installed_checker
        self._save_status_reader = config.save_status_reader
        self._drift_reader = config.drift_reader
        self._save_file_store = config.save_file_store
        self._loop = config.loop
        self._logger = config.logger

    async def evaluate(self, steam_app_id: int) -> LaunchVerdict:
        """Return the launch-time verdict for the given Steam app id.

        Parameters
        ----------
        steam_app_id:
            Steam app id of the (potentially non-Steam) shortcut about
            to be launched.

        Returns
        -------
        LaunchVerdict
            ``allow`` when the app is not a RomM ROM, when the ROM is
            installed but save-sync is disabled, when the ROM is
            installed with no save conflict, or when the save-status
            read failed for a ROM with no tracked saves. ``warn`` with
            ``reason="save_status_failed"`` when the save-status read
            failed for a ROM that has tracked saves — the frontend
            surfaces the warning toast but lets the launch proceed.
            ``block`` with ``reason="not_installed"`` or
            ``reason="save_conflict"`` and the matching toast strings
            otherwise.
        """
        rom = self._rom_lookup.get_rom_by_steam_app_id(steam_app_id)
        if rom is None:
            return LaunchVerdict(action="allow")

        rom_id = rom["rom_id"]

        installed = self._installed_checker.get_installed_rom(rom_id)
        if not installed:
            return LaunchVerdict(
                action="block",
                reason="not_installed",
                toast_title=_TOAST_TITLE_NOT_INSTALLED,
                toast_body=_TOAST_BODY_NOT_INSTALLED,
            )

        # Save-sync off → there is no conflict state to gate on. Allow the
        # launch and skip the get_save_status round-trip entirely. Otherwise a
        # stale server-side conflict (e.g. another device moved the save while
        # sync was disabled) would block every launch with no way to resolve
        # it — the Saves tab is hidden while the feature is off — leaving the
        # game permanently unplayable.
        if not self._save_status_reader.is_save_sync_enabled():
            return LaunchVerdict(action="allow")

        try:
            save_status = await self._save_status_reader.get_save_status(rom_id)
        except Exception as e:
            # A failed conflict check must not silently allow the launch
            # for ROMs with tracked saves — an unseen conflict would
            # corrupt the wrong slot. Soft-warn instead so the user can
            # retry; pure-allow only when nothing is tracked.
            self._logger.warning(f"LaunchGate save-status check failed for rom_id={rom_id}: {e}")
            if self._save_status_reader.has_tracked_save(rom_id):
                return LaunchVerdict(
                    action="warn",
                    reason="save_status_failed",
                    toast_title=_TOAST_TITLE_SAVE_STATUS_FAILED,
                    toast_body=_TOAST_BODY_SAVE_STATUS_FAILED,
                )
            return LaunchVerdict(action="allow")

        if _has_any_save_conflict(save_status):
            return LaunchVerdict(
                action="block",
                reason="save_conflict",
                toast_title=_TOAST_TITLE_SAVE_CONFLICT,
                toast_body=_TOAST_BODY_SAVE_CONFLICT,
            )

        return LaunchVerdict(action="allow")

    async def check_local_drift(self, rom_id: int) -> dict[str, Any]:
        """Report whether the ROM's local save files diverge from their sync baseline.

        Used by the offline launch path: when the server is unreachable we
        cannot run a real sync, so this purely-local probe warns the user that
        an out-of-band local change would otherwise be silently overwritten the
        next time sync succeeds.

        Enumerates the ROM's local save files the same way the sync/status path
        does (``find_local_save_files`` → the shared ``RomInfoService``
        discovery), hashes each present file (content MD5 via the injected
        ``SaveFileStore``, run on the executor), and compares it to that file's
        persisted ``last_sync_hash``. ``drifted`` is ``True`` when any present
        file's current hash differs from its non-``None`` baseline. A file with
        no baseline yet (``last_sync_hash is None``) is NOT drift — there is no
        recorded state to diverge from. A ROM that is not installed or has no
        tracked files reports ``drifted: False``.

        Never raises: any internal error (file vanished mid-hash, repository
        read failure, …) collapses to ``drifted: False``. A false offline
        warning is worse than skipping it — treat the unknown as not-drifted.
        """
        rom_id = int(rom_id)
        try:
            return await self._loop.run_in_executor(None, self._check_local_drift_io, rom_id)
        except Exception as e:
            self._logger.warning(f"LaunchGate drift check failed for rom_id={rom_id}: {e}")
            return {"drifted": False, "rom_id": rom_id}

    def _check_local_drift_io(self, rom_id: int) -> dict[str, Any]:
        """Synchronous drift worker — runs on the executor thread.

        Enumerates local save files + per-file baselines and hashes each
        present file. Returns the ``{"drifted", "rom_id"}`` shape. Raised
        exceptions propagate to :meth:`check_local_drift`, which collapses them
        to ``drifted: False``.
        """
        local_files = self._drift_reader.find_local_save_files(rom_id)
        if not local_files:
            return {"drifted": False, "rom_id": rom_id}

        baselines = self._drift_reader.last_sync_hashes(rom_id)
        for entry in local_files:
            filename = entry["filename"]
            baseline = baselines.get(filename)
            if baseline is None:
                # No recorded baseline → nothing to diverge from (not drift).
                continue
            current = self._save_file_store.checksum_md5(entry["path"])
            if current != baseline:
                return {"drifted": True, "rom_id": rom_id}

        return {"drifted": False, "rom_id": rom_id}
