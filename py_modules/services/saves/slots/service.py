"""Slots facade and config: the public entry point for slot lifecycle.

Anything that creates, lists, switches, migrates, or deletes slots —
including the first-sync setup wizard — is exposed by ``SlotsService``.
The implementations live in the sibling sub-modules
(:mod:`services.saves.slots.listing`,
:mod:`services.saves.slots.switching`,
:mod:`services.saves.slots.setup`,
:mod:`services.saves.slots.deletion`); ``SlotsService`` wires them
from the config and delegates. The newest-wins matrix executor lives in
SyncEngine, status reporting in StatusService, on-disk state
persistence in StateService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.saves.slots.deletion import SlotDeleter
from services.saves.slots.listing import SlotListing
from services.saves.slots.setup import _NO_MIGRATION, SetupWizard
from services.saves.slots.switching import SlotSwitcher

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        Clock,
        CoreResolverFn,
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        SaveFileStore,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.state import StateService
    from services.saves.status import StatusService
    from services.saves.sync_engine import SyncEngine


__all__ = ["_NO_MIGRATION", "SlotsService", "SlotsServiceConfig"]


@dataclass(frozen=True)
class SlotsServiceConfig:
    """Frozen wiring bundle handed to ``SlotsService.__init__``.

    Holds the main plugin state dict, the peer save sub-services
    (state, sync_engine, status, rom_info), the Protocol-typed RomM
    adapter and retry strategy, runtime infrastructure (loop, logger,
    clock), the Protocol-typed filesystem adapter, the ``DebugLogger``
    seam, and the ES-DE core resolver used during slot migration to
    build the emulator tag for re-upload.
    """

    state: dict
    state_svc: StateService
    sync_engine: SyncEngine
    status_service: StatusService
    rom_info: RomInfoService
    romm_api: RommSaveApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    save_file_store: SaveFileStore
    log_debug: DebugLogger
    get_active_core: CoreResolverFn


class SlotsService:
    """Slot lifecycle entry point — composes the listing, switching, setup, and deletion sub-modules."""

    def __init__(self, *, config: SlotsServiceConfig) -> None:
        self._config = config

        self._listing = SlotListing(
            state_svc=config.state_svc,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            log_debug=config.log_debug,
        )
        self._switcher = SlotSwitcher(
            state_svc=config.state_svc,
            sync_engine=config.sync_engine,
            status_service=config.status_service,
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            clock=config.clock,
            save_file_store=config.save_file_store,
            log_debug=config.log_debug,
        )
        self._setup = SetupWizard(
            state=config.state,
            state_svc=config.state_svc,
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            logger=config.logger,
            save_file_store=config.save_file_store,
            log_debug=config.log_debug,
            get_active_core=config.get_active_core,
        )
        self._deleter = SlotDeleter(
            state_svc=config.state_svc,
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            logger=config.logger,
            log_debug=config.log_debug,
        )

    # ------------------------------------------------------------------
    # Slot listing — delegates to :class:`SlotListing`.
    # ------------------------------------------------------------------

    async def get_save_slots(self, rom_id: int) -> dict:
        """List available save slots for a ROM."""
        return await self._listing.get_save_slots(rom_id)

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict:
        """Fetch server save files for a specific slot."""
        return await self._listing.get_slot_saves(rom_id, slot)

    # ------------------------------------------------------------------
    # Active slot mutation + slot switching — delegate to :class:`SlotSwitcher`.
    # Kept on the facade so tests that reach for ``svc._slots._set_active_slot``
    # continue to drive the same code path.
    # ------------------------------------------------------------------

    def _set_active_slot(self, rom_id: int, slot: str) -> dict:
        """Set the active save slot for a specific game."""
        return self._switcher._set_active_slot(rom_id, slot)

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict:
        """Switch the active save slot with immediate state sync."""
        return await self._switcher.switch_slot(rom_id, new_slot)

    # ------------------------------------------------------------------
    # Save setup wizard — delegates to :class:`SetupWizard`.
    # ------------------------------------------------------------------

    def is_save_tracking_configured(self, rom_id: int) -> dict:
        """Check if save slot tracking is configured for a game."""
        return self._setup.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id: int) -> dict:
        """Get info needed for the first-sync setup wizard."""
        return await self._setup.get_save_setup_info(rom_id)

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str,
        migrate_from_slot: str | None | object = _NO_MIGRATION,
    ) -> dict:
        """Confirm which slot to use for a game's save sync."""
        return await self._setup.confirm_slot_choice(rom_id, chosen_slot, migrate_from_slot)

    # ------------------------------------------------------------------
    # Slot deletion — delegates to :class:`SlotDeleter`.
    # ------------------------------------------------------------------

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict:
        """Return info about what deleting a slot would do."""
        return await self._deleter.get_slot_delete_info(rom_id, slot)

    async def delete_slot(self, rom_id: int, slot: str) -> dict:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        return await self._deleter.delete_slot(rom_id, slot)
