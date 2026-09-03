"""Fetching firmware out of the RomM library and onto the machine.

The only writer of a ``downloaded_bios`` record, which is what later authorises
a delete: having placed the file is the authority, and this record is the sole
evidence of it. Every entry point resolves the destination through the machine's
demand rather than inventing a layout, so a file lands where the emulator will
open it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain import firmware_paths
from domain.bios_file import BiosFile
from lib.errors import error_response
from lib.path_safety import PathTraversalError

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Mapping

    from domain.firmware_wants import FirmwarePlacement
    from services.firmware.demand import FirmwareDemand
    from services.firmware.listing import FirmwareListing
    from services.protocols import (
        Clock,
        CoreInfoProvider,
        FirmwareFileStore,
        RommFirmwareApi,
        SystemResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class FirmwareDownloaderConfig:
    """Frozen wiring bundle handed to ``FirmwareDownloader.__init__``.

    Holds the RomM API adapter the bytes come from, the two peer sub-services
    (the listing a platform's rows are picked out of, the demand each
    destination is resolved through), the ES-DE core reads the required-only
    filter keys on, the file store, the clock the download timestamp is taken
    from, the Unit-of-Work factory the record is written through, and runtime
    infrastructure.
    """

    romm_api: RommFirmwareApi
    listing: FirmwareListing
    demand: FirmwareDemand
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    firmware_file_store: FirmwareFileStore
    clock: Clock
    uow_factory: UnitOfWorkFactory
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger


class FirmwareDownloader:
    """The firmware download entry points and the record each one leaves behind."""

    def __init__(self, *, config: FirmwareDownloaderConfig) -> None:
        self._romm_api = config.romm_api
        self._listing = config.listing
        self._demand = config.demand
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._firmware_file_store = config.firmware_file_store
        self._clock = config.clock
        self._uow_factory = config.uow_factory
        self._loop = config.loop
        self._logger = config.logger

    def _download_firmware_post_io(self, fw, firmware_id, dest, tmp_path):
        """Sync worker for download_firmware — file rename, hash verification, DB persist.

        Runs in an executor. The filesystem work (rename, checksum) happens
        outside any transaction; only the ``BiosFile`` upsert is wrapped in a
        short write UoW (ADR-0006).

        Returns ``(md5_match, error)``. ``error`` is a string when the firmware is
        malformed — RomM data that fails the ``BiosFile`` invariants (empty
        slug/file_name) — in which case the renamed file is removed and nothing
        is persisted; otherwise ``None``.
        """
        file_name = fw.get("file_name", "")
        self._firmware_file_store.rename(tmp_path, dest)

        expected_md5 = fw.get("md5_hash", "")
        local_md5 = self._firmware_file_store.checksum_md5(dest) if expected_md5 else None
        md5_match = local_md5 == expected_md5 if expected_md5 and local_md5 is not None else None

        try:
            bios_file = BiosFile.mark_downloaded(
                platform_slug=firmware_paths.parse_firmware_slug(fw.get("file_path", "")),
                file_name=file_name,
                file_path=dest,
                downloaded_at=self._clock.now().isoformat(),
                firmware_id=firmware_id,
            )
        except ValueError as e:
            # Malformed RomM firmware (e.g. file_path with no parseable slug):
            # the aggregate's invariant rejects it. Drop the renamed file so we
            # don't leave it untracked, and signal a download failure.
            self._firmware_file_store.remove_file(dest)
            return md5_match, f"Invalid firmware metadata: {e}"

        with self._uow_factory() as uow:
            uow.bios_files.save(bios_file)

        return md5_match, None

    async def download_firmware(self, firmware_id) -> dict[str, Any]:
        """Download one firmware file — with none of the batch's eligibility checks.

        The folder-declaration refusal is among them, and no callable exposes
        this method, which is the only reason that gap is unreachable.
        """
        placements = await self._loop.run_in_executor(None, self._demand.placement_index)
        return await self._download_one(firmware_id, placements)

    async def _download_one(self, firmware_id, placements: Mapping[str, FirmwarePlacement]) -> dict[str, Any]:
        """Fetch, place and record one firmware file against a pre-read demand index.

        The index is a parameter rather than a per-call read so a batch pays for
        the machine-wide question once instead of once per file.
        """
        firmware_id = int(firmware_id)
        try:
            fw = await self._loop.run_in_executor(None, self._romm_api.get_firmware, firmware_id)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware {firmware_id}: {e}")
            return error_response(e)

        file_name = fw.get("file_name", "")
        try:
            dest = self._demand.dest_path(fw, placements.get(file_name))
        except PathTraversalError as e:
            self._logger.error(f"Rejected firmware with unsafe file name {file_name!r}: {e}")
            return {
                "success": False,
                "reason": "path_traversal",
                "message": "Server sent an unsafe firmware file name — download aborted",
            }
        tmp_path = dest + ".tmp"

        try:
            await self._loop.run_in_executor(None, self._firmware_file_store.make_dirs, os.path.dirname(dest))
            await self._loop.run_in_executor(None, self._romm_api.download_firmware, firmware_id, file_name, tmp_path)
        except Exception as e:
            await self._loop.run_in_executor(None, self._firmware_file_store.remove_file, tmp_path)
            self._logger.error(f"Failed to download firmware {file_name}: {e}")
            return error_response(e)

        md5_match, post_io_error = await self._loop.run_in_executor(
            None, self._download_firmware_post_io, fw, firmware_id, dest, tmp_path
        )
        if post_io_error is not None:
            self._logger.error(f"Failed to persist firmware {file_name}: {post_io_error}")
            return error_response(ValueError(post_io_error))

        self._listing.invalidate()
        self._logger.info(f"Firmware downloaded: {file_name} -> {dest}")
        return {"success": True, "file_path": dest, "md5_match": md5_match}

    async def _platform_firmware_rows(self, platform_slug) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """The library rows filed under *platform_slug*, or the failure to return instead.

        The three download entry points ask the same two questions first — what
        does the library hold, and which of it is this platform's — and the
        second is not a plain slug match: ``psx`` is filed under ``psx`` and
        ``ps`` both. Answering it in one place is what keeps a button from
        fetching a set the button beside it would not.

        The second element is a ready-made failure response when the listing
        could not be read; a caller returns it as it stands.
        """
        try:
            firmware_list = await self._loop.run_in_executor(None, self._listing.get_firmware_list)
        except Exception as e:
            self._logger.error(f"Failed to fetch firmware: {e}")
            resp = error_response(e)
            resp["downloaded"] = 0
            return [], resp

        fw_slugs = firmware_paths.resolve_firmware_slugs(platform_slug)
        rows = [fw for fw in firmware_list if firmware_paths.parse_firmware_slug(fw.get("file_path", "")) in fw_slugs]
        return rows, None

    async def download_all_firmware(self, platform_slug) -> dict[str, Any]:
        """Download all firmware for a given platform slug."""
        platform_firmware, failure = await self._platform_firmware_rows(platform_slug)
        if failure is not None:
            return failure

        placements = await self._loop.run_in_executor(None, self._demand.placement_index)
        downloaded, errors = await self._download_firmware_batch(platform_firmware, placements)

        msg = f"Downloaded {downloaded} firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}

    async def _download_firmware_batch(
        self, platform_firmware, placements: Mapping[str, FirmwarePlacement]
    ) -> tuple[int, list[str]]:
        """Download a batch of firmware files, skipping already-downloaded ones.

        *placements* is the machine's demand index, read once by the caller: the
        question costs hundreds of milliseconds, and a batch that asked it per
        file would pay that for every download.

        The already-there skip probes the disk rather than reading the
        catalogue's answer (:meth:`FirmwareDemand.is_downloaded`) — that answer
        predates every download this batch has performed, and re-reading the
        whole machine per file to refresh it is the cost the index exists to
        avoid.

        A folder declaration is skipped whatever is at its destination: the
        emulator lists that name, so there is no file to fetch into it.
        """
        downloaded = 0
        errors = []
        for fw in platform_firmware:
            placement = placements.get(fw.get("file_name", ""))
            if placement is not None and placement.declares_directory:
                continue
            dest = self._demand.safe_dest_path(fw, placement)
            if dest is not None and self._firmware_file_store.exists(dest):
                continue
            result = await self._download_one(fw["id"], placements)
            if result.get("success"):
                downloaded += 1
            else:
                errors.append(fw.get("file_name", str(fw["id"])))
        return downloaded, errors

    async def download_platform_firmware_file(self, platform_slug, file_name) -> dict[str, Any]:
        """Download the one firmware file *file_name* the library holds for *platform_slug*.

        The per-row Download button's backend. Addressed by name within the
        platform rather than by RomM's firmware id: the id is the server's, and
        the row it would come from is a status row the page may have been holding
        for a while — resolving the name against the current listing here keeps
        the platform scoping identical to :meth:`download_all_firmware` and the
        two buttons beside it. A name the platform's listing does not hold is a
        ``not_in_library`` refusal, never a silent no-op — and a plain reason
        rather than ``NOT_FOUND``, which is RomM's entity layer answering and
        carries deletion authority downstream.

        Answers in the batch shape (``downloaded`` 0 or 1) because the file may
        already be at its destination, which the batch skips — the same outcome
        as pressing Download all with nothing left to fetch. What it does not
        borrow from the batch is the error fold: one press wants the reason the
        one file failed, so the single fetch's own failure response is returned
        as it stands rather than collapsed into a name in a list.
        """
        rows, failure = await self._platform_firmware_rows(platform_slug)
        if failure is not None:
            return failure

        wanted = [fw for fw in rows if fw.get("file_name") == file_name]
        if not wanted:
            return {
                "success": False,
                "reason": "not_in_library",
                "message": f"{file_name} is not in your RomM library for {platform_slug}",
                "downloaded": 0,
            }

        placements = await self._loop.run_in_executor(None, self._demand.placement_index)
        fw = wanted[0]
        # The already-there skip probes the disk rather than reading the
        # catalogue, for the reason the batch states: the catalogue's answer
        # predates every download since it was read.
        dest = self._demand.safe_dest_path(fw, placements.get(file_name))
        if dest is not None and self._firmware_file_store.exists(dest):
            return {"success": True, "message": f"{file_name} is already here", "downloaded": 0}

        result = await self._download_one(fw["id"], placements)
        if not result.get("success"):
            result["downloaded"] = 0
            return result
        return {**result, "message": f"Downloaded {file_name}", "downloaded": 1}

    async def download_required_firmware(self, platform_slug) -> dict[str, Any]:
        """Download only the firmware the platform's launching core will not run without."""
        rows, failure = await self._platform_firmware_rows(platform_slug)
        if failure is not None:
            return failure

        core_so, _ = self._core_info.get_active_core(self._resolve_system(platform_slug))
        placements = await self._loop.run_in_executor(None, self._demand.placement_index)
        platform_firmware = [fw for fw in rows if _required_by(placements.get(fw.get("file_name", "")), core_so)]

        downloaded, errors = await self._download_firmware_batch(platform_firmware, placements)

        msg = f"Downloaded {downloaded} required firmware files"
        if errors:
            msg += f" ({len(errors)} failed: {', '.join(errors)})"
        return {"success": True, "message": msg, "downloaded": downloaded}


def _required_by(placement: FirmwarePlacement | None, core_so: str | None) -> bool:
    """Will *core_so* refuse to run without the file *placement* describes?

    ``None`` for the core — the platform's default could not be resolved — falls
    back to "any emulator requires it", the same permissive default the status
    surfaces use when they cannot name the launching core.
    """
    if placement is None:
        return False
    if core_so is None:
        return placement.required_by_any
    return any(want.core_so == core_so and want.required for want in placement.wants)
