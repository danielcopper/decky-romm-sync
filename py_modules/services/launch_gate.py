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
    import logging

    from services.protocols.cross_service import (
        LaunchGateInstalledChecker,
        LaunchGateRomLookup,
        LaunchGateSaveStatusReader,
    )


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

    All three deps are Protocol-typed cross-service seams that the
    composition root satisfies with the existing library, download,
    and save-sync services. ``logger`` is carried for parity with
    sibling services even though this orchestration layer has no
    log surface of its own beyond the deps it forwards to.
    """

    rom_lookup: LaunchGateRomLookup
    installed_checker: LaunchGateInstalledChecker
    save_status_reader: LaunchGateSaveStatusReader
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
