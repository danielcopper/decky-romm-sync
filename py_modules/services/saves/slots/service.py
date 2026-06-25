"""Slots facade and config: the public entry point for slot lifecycle.

Anything that creates, lists, switches, migrates, or deletes slots —
including the first-sync setup wizard — is exposed by ``SlotsService``.
The implementations live in the sibling sub-modules
(:mod:`services.saves.slots.listing`,
:mod:`services.saves.slots.switching`,
:mod:`services.saves.slots.setup`,
:mod:`services.saves.slots.deletion`); ``SlotsService`` wires them
from the config and delegates. The newest-wins matrix executor lives in
SyncEngine, status reporting in StatusService; on-disk state
persistence is each operation's own narrow Unit of Work (ADR-0006).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from services.saves.slots.deletion import SlotDeleter
from services.saves.slots.listing import SlotListing
from services.saves.slots.setup import SetupWizard
from services.saves.slots.switching import SlotSwitcher

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from services.protocols import (
        Clock,
        DebugLogger,
        RetryStrategy,
        RommSaveApi,
        SaveFileStore,
        UnitOfWorkFactory,
    )
    from services.saves.rom_info import RomInfoService
    from services.saves.status import StatusService
    from services.saves.sync_engine import SyncEngine
    from services.saves.sync_engine.devices import DeviceRegistry


__all__ = ["SlotsService", "SlotsServiceConfig"]


@dataclass(frozen=True)
class SlotsServiceConfig:
    """Frozen wiring bundle handed to ``SlotsService.__init__``.

    Holds the live ``settings.json`` dict (save-sync toggles + default
    slot), the Unit-of-Work factory (the transactional seam over the
    SQLite repositories), the peer save sub-services (sync_engine,
    status, rom_info, and the shared :class:`DeviceRegistry` that owns the
    server device id), the core resolver used to stamp the upload
    emulator tag, the Protocol-typed RomM adapter and retry strategy,
    runtime infrastructure (loop, logger, clock), the Protocol-typed
    filesystem adapter, and the ``DebugLogger`` seam.
    """

    settings: dict[str, Any]
    uow_factory: UnitOfWorkFactory
    sync_engine: SyncEngine
    device_registry: DeviceRegistry
    status_service: StatusService
    rom_info: RomInfoService
    resolve_core: Callable[[int], str | None]
    romm_api: RommSaveApi
    retry: RetryStrategy
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    save_file_store: SaveFileStore
    log_debug: DebugLogger


class SlotsService:
    """Slot lifecycle entry point — composes the listing, switching, setup, and deletion sub-modules."""

    def __init__(self, *, config: SlotsServiceConfig) -> None:
        self._config = config

        self._listing = SlotListing(
            settings=config.settings,
            uow_factory=config.uow_factory,
            device_registry=config.device_registry,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            log_debug=config.log_debug,
        )
        self._switcher = SlotSwitcher(
            settings=config.settings,
            uow_factory=config.uow_factory,
            sync_engine=config.sync_engine,
            device_registry=config.device_registry,
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
            settings=config.settings,
            uow_factory=config.uow_factory,
            device_registry=config.device_registry,
            rom_info=config.rom_info,
            resolve_core=config.resolve_core,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            logger=config.logger,
            save_file_store=config.save_file_store,
            log_debug=config.log_debug,
            sync_engine=config.sync_engine,
        )
        self._deleter = SlotDeleter(
            settings=config.settings,
            uow_factory=config.uow_factory,
            device_registry=config.device_registry,
            rom_info=config.rom_info,
            romm_api=config.romm_api,
            retry=config.retry,
            loop=config.loop,
            logger=config.logger,
            log_debug=config.log_debug,
            sync_engine=config.sync_engine,
        )

    # ------------------------------------------------------------------
    # Slot listing — delegates to :class:`SlotListing`.
    # ------------------------------------------------------------------

    async def get_save_slots(self, rom_id: int) -> dict[str, Any]:
        """List available save slots for a ROM."""
        return await self._listing.get_save_slots(rom_id)

    async def get_slot_saves(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Fetch server save files for a specific slot."""
        return await self._listing.get_slot_saves(rom_id, slot)

    # ------------------------------------------------------------------
    # Active slot mutation + slot switching — delegate to :class:`SlotSwitcher`.
    # Kept on the facade so tests that reach for ``svc._slots.set_active_slot``
    # continue to drive the same code path.
    # ------------------------------------------------------------------

    async def set_active_slot(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Set the active save slot for a specific game."""
        return await self._switcher.set_active_slot(rom_id, slot)

    async def switch_slot(self, rom_id: int, new_slot: str) -> dict[str, Any]:
        """Switch the active save slot with immediate state sync."""
        return await self._switcher.switch_slot(rom_id, new_slot)

    # ------------------------------------------------------------------
    # Save setup wizard — delegates to :class:`SetupWizard`.
    # ------------------------------------------------------------------

    def is_save_tracking_configured(self, rom_id: int) -> dict[str, Any]:
        """Check if save slot tracking is configured for a game."""
        return self._setup.is_save_tracking_configured(rom_id)

    async def get_save_setup_info(self, rom_id: int) -> dict[str, Any]:
        """Get info needed for the first-sync setup wizard."""
        return await self._setup.get_save_setup_info(rom_id)

    async def confirm_slot_choice(
        self,
        rom_id: int,
        chosen_slot: str | None,
        migrate: bool = False,
        migrate_from_slot: str | None = None,
    ) -> dict[str, Any]:
        """Confirm which slot to use for a game's save sync."""
        return await self._setup.confirm_slot_choice(rom_id, chosen_slot, migrate, migrate_from_slot)

    # ------------------------------------------------------------------
    # Slot deletion — delegates to :class:`SlotDeleter`.
    # ------------------------------------------------------------------

    async def get_slot_delete_info(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Return info about what deleting a slot would do."""
        return await self._deleter.get_slot_delete_info(rom_id, slot)

    async def delete_slot(self, rom_id: int, slot: str) -> dict[str, Any]:
        """Delete a save slot and all its saves (local state + server if applicable)."""
        return await self._deleter.delete_slot(rom_id, slot)
