"""FirmwareService façade.

Owns the public callable surface exposed via ``main.py`` (the System-page
overview, the per-platform BIOS check, the download entry points, the delete)
and composes the four sub-services behind it. Implementation lives in those
modules: :class:`FirmwareListing` for what the RomM library holds and its
cache, :class:`FirmwareDemand` for what the installed emulators want and where
each file goes, :class:`FirmwareStatusReader` for every status-bearing answer,
:class:`FirmwareDownloader` for fetching, :class:`PlatformBiosDeleter` for
removing what was fetched. The façade itself only wires the pieces together and
delegates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from services.firmware.deletion import PlatformBiosDeleter, PlatformBiosDeleterConfig
from services.firmware.demand import FirmwareDemand, FirmwareDemandConfig
from services.firmware.downloads import FirmwareDownloader, FirmwareDownloaderConfig
from services.firmware.listing import FirmwareListing, FirmwareListingConfig
from services.firmware.status import FirmwareStatusReader, FirmwareStatusReaderConfig

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        Clock,
        CoreInfoProvider,
        FirmwareFileStore,
        FirmwareFolderVerdictFn,
        FirmwareResolver,
        PlatformCoreReader,
        RetroDeckPaths,
        RommFirmwareApi,
        SystemResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class FirmwareServiceConfig:
    """Frozen wiring bundle handed to ``FirmwareService.__init__``.

    Holds the API adapter, runtime infrastructure, Protocol-typed file
    adapters, the SQLite Unit-of-Work factory, and the provider callables
    the firmware subsystem needs at construction time. Decomposes the ctor
    so a new dependency does not push past the S107 parameter-count
    limit.
    """

    romm_api: RommFirmwareApi
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    clock: Clock
    firmware_file_store: FirmwareFileStore
    firmware_resolver: FirmwareResolver
    firmware_folder_verdicts: FirmwareFolderVerdictFn
    retrodeck_paths: RetroDeckPaths
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    platform_core_reader: PlatformCoreReader
    uow_factory: UnitOfWorkFactory


class FirmwareService:
    """BIOS/firmware management: what is wanted, what is here, downloads, deletion."""

    def __init__(
        self,
        *,
        config: FirmwareServiceConfig,
    ) -> None:
        self._config = config

        # Sub-service: the RomM listing and its cache. Constructed first —
        # every other sub-service either reads the listing or invalidates it,
        # and they share this one instance so a download's invalidation is seen
        # by the query that follows it.
        self._listing = FirmwareListing(
            config=FirmwareListingConfig(
                romm_api=config.romm_api,
                logger=config.logger,
                clock=config.clock,
                uow_factory=config.uow_factory,
            )
        )

        # Sub-service: the machine's demand. The single owner of both resolver
        # seams and of the BIOS root, so a file's destination and its presence
        # are derived once and read the same way by the status surfaces and the
        # download paths alike.
        self._demand = FirmwareDemand(
            config=FirmwareDemandConfig(
                firmware_resolver=config.firmware_resolver,
                firmware_folder_verdicts=config.firmware_folder_verdicts,
                retrodeck_paths=config.retrodeck_paths,
                firmware_file_store=config.firmware_file_store,
                logger=config.logger,
            )
        )

        self._status = FirmwareStatusReader(
            config=FirmwareStatusReaderConfig(
                demand=self._demand,
                listing=self._listing,
                core_info=config.core_info,
                resolve_system=config.resolve_system,
                platform_core_reader=config.platform_core_reader,
                firmware_file_store=config.firmware_file_store,
                uow_factory=config.uow_factory,
                loop=config.loop,
                logger=config.logger,
            )
        )

        self._downloads = FirmwareDownloader(
            config=FirmwareDownloaderConfig(
                romm_api=config.romm_api,
                listing=self._listing,
                demand=self._demand,
                core_info=config.core_info,
                resolve_system=config.resolve_system,
                firmware_file_store=config.firmware_file_store,
                clock=config.clock,
                uow_factory=config.uow_factory,
                loop=config.loop,
                logger=config.logger,
            )
        )

        self._deletion = PlatformBiosDeleter(
            config=PlatformBiosDeleterConfig(
                listing=self._listing,
                firmware_file_store=config.firmware_file_store,
                uow_factory=config.uow_factory,
                loop=config.loop,
                logger=config.logger,
            )
        )

    def invalidate_firmware_cache(self) -> None:
        """Clear the cached firmware list so the next query re-fetches."""
        self._listing.invalidate()

    async def get_firmware_status(self) -> dict[str, Any]:
        """Return BIOS/firmware status for every platform the page can speak for."""
        return await self._status.get_firmware_status()

    async def check_platform_bios(self, platform_slug, active_core_so=None) -> dict[str, Any]:
        """Return the platform's BIOS status, filtered by what *active_core_so* needs."""
        return await self._status.check_platform_bios(platform_slug, active_core_so)

    async def download_firmware(self, firmware_id) -> dict[str, Any]:
        """Download one firmware file by its RomM id."""
        return await self._downloads.download_firmware(firmware_id)

    async def download_all_firmware(self, platform_slug) -> dict[str, Any]:
        """Download all firmware the library holds for a platform."""
        return await self._downloads.download_all_firmware(platform_slug)

    async def download_required_firmware(self, platform_slug) -> dict[str, Any]:
        """Download only the firmware the platform's launching core will not run without."""
        return await self._downloads.download_required_firmware(platform_slug)

    async def delete_platform_bios(self, platform_slug) -> dict[str, Any]:
        """Delete the BIOS files the plugin downloaded for a platform."""
        return await self._deletion.delete_platform_bios(platform_slug)
