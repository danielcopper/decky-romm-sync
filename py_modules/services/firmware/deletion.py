"""Removing the BIOS files the plugin itself downloaded — and only those.

Authority to delete comes from having placed the file, and a ``downloaded_bios``
record is the only evidence of that, so the records are this module's whole
input: it iterates them, unlinks the path each one holds, and prunes the row.
Nothing here consults a status listing, a file's existence, or what the library
currently offers.

Three buttons reach it — the platform's Delete BIOS, a file row's Delete, a
declared folder's Delete — and they share ONE removal loop, differing only in
which records they select. A second copy of that loop is what the register's
BIOS-delete rule warns about: the copies would drift silently, and this is the
loop that once destroyed a user's file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain import firmware_paths
from lib.list_result import ErrorCode

if TYPE_CHECKING:
    import asyncio
    import logging

    from domain.bios_file import BiosFile
    from services.firmware.listing import FirmwareListing
    from services.protocols import FirmwareFileStore, UnitOfWorkFactory


@dataclass(frozen=True)
class PlatformBiosDeleterConfig:
    """Frozen wiring bundle handed to ``PlatformBiosDeleter.__init__``.

    Holds the listing peer whose cache a removal invalidates, the file store
    the unlinks go through, the Unit-of-Work factory the records are read and
    pruned through, and runtime infrastructure.
    """

    listing: FirmwareListing
    firmware_file_store: FirmwareFileStore
    uow_factory: UnitOfWorkFactory
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger


class PlatformBiosDeleter:
    """The Delete BIOS action — the plugin's own downloads for one platform."""

    def __init__(self, *, config: PlatformBiosDeleterConfig) -> None:
        self._listing = config.listing
        self._firmware_file_store = config.firmware_file_store
        self._uow_factory = config.uow_factory
        self._loop = config.loop
        self._logger = config.logger

    def _delete_recorded_io(self, platform_slug, selects=None) -> tuple[int, list[str]]:
        """Remove the plugin's own downloads for *platform_slug*, filtered by *selects*.

        The one removal loop, and deliberately the only one: three buttons reach
        it — the platform's Delete BIOS, a file row's Delete, a declared folder's
        Delete — and they differ only in **which records they select**, never in
        what a removal is. A second copy of this loop is what the register warns
        about, because a divergence between the copies would be silent and this
        is the loop that once destroyed a user's file.

        Runs in an executor. Every filesystem removal happens outside any
        transaction: the records are read in one short UoW before the loop and
        the rows they leave behind are dropped in one after it (ADR-0006).

        The download records are the whole input: a ``downloaded_bios`` row is
        written in the download path and nowhere else, so it is the only
        evidence the plugin put the file there — and having put it there is what
        authorises removing it. A status row's ``downloaded`` proves nothing of
        the sort: it is ``os.path.exists``, equally true of firmware RetroDECK
        ships with its own components (``dolphin-emu/Sys/codehandler.bin`` is
        one) and of a file the user placed by hand under a name the server
        happens to share. Neither can be fetched back, so neither is ours to
        delete.

        The row also says WHERE, and that is why the status rows are not
        consulted for the path either. ``BiosFile.file_path`` is where the
        download actually wrote the file (kept current by the home migration's
        ``relocate``), while a status row's ``local_path`` is recomputed from
        today's placement — and placements move with every emu-atlas bump. A
        file fetched while nothing declared it landed flat in the BIOS root; let
        the resolver later declare a subdirectory for it and the recomputed path
        names whatever now sits there instead, which for ``codehandler.bin`` is
        RetroDECK's own copy. Unlinking the recorded path can reach nothing but
        what the plugin wrote.

        ``on_server`` is deliberately not part of the test. It describes what
        the library holds *now*, not who wrote the file: a firmware file removed
        from RomM after we downloaded it flips to ``on_server: False``, and
        refusing that row would strand our own download with nothing in the UI
        able to clean it up.

        *selects* is a predicate over records this platform already owns, so
        the worst a wrong one can do is act on that whole set — never on a file
        the plugin did not place. ``None`` selects all of them, which is the
        platform-wide button; the folder button guards its own degenerate path
        so that "a folder's files" cannot quietly become "the platform's".

        A record whose file is already gone is not a deletion and not an error —
        the row is dropped and nothing is counted. That is also what makes two
        rows for one file name under different firmware slugs harmless: they
        name one path, the first unlink takes it, and the second prunes its row
        over an absence.
        """
        deleted = 0
        errors: list[str] = []
        pruned: list[tuple[str, str]] = []
        for record in self._recorded_bios_files(platform_slug):
            if selects is not None and not selects(record):
                continue
            if self._firmware_file_store.exists(record.file_path):
                try:
                    self._firmware_file_store.remove_file(record.file_path)
                except OSError as e:
                    self._logger.warning(f"Failed to remove BIOS file {record.file_name}: {e}")
                    errors.append(f"{record.file_name}: {e}")
                    continue
                deleted += 1
            pruned.append((record.platform_slug, record.file_name))

        if pruned:
            self._prune_bios_records(pruned)

        return deleted, errors

    def _recorded_bios_files(self, platform_slug) -> list[BiosFile]:
        """The plugin's own download records for *platform_slug*.

        The BIOS rows are keyed by the firmware-directory slug stored at download
        time, which may differ from the platform slug (e.g. ``psx`` → ``ps``), so
        every candidate spelling is read. Each record is kept whole rather than
        reduced to a name: the delete needs its ``file_path`` to unlink and its
        ``platform_slug`` to address the row again. One short read UoW, closed
        before any file I/O.
        """
        records: list[BiosFile] = []
        with self._uow_factory() as uow:
            for slug in firmware_paths.resolve_firmware_slugs(platform_slug):
                records.extend(uow.bios_files.iter_by_platform(slug))
        return records

    def _prune_bios_records(self, keys) -> None:
        """Delete the ``BiosFile`` records named by the ``(slug, file_name)`` *keys*.

        One short write UoW, after the file I/O it follows. A key reaches here
        for a row whose file was removed AND for one whose file was already
        gone; only a row whose removal *failed* is left standing.
        """
        with self._uow_factory() as uow:
            for slug, file_name in keys:
                uow.bios_files.delete(slug, file_name)

    async def delete_bios_folder(self, platform_slug, folder_path) -> dict[str, Any]:
        """Delete the BIOS files the plugin downloaded inside *folder_path*.

        The folder row's twin of :meth:`delete_bios_file`, and the same
        :meth:`_delete_recorded_io` again under a containment predicate. A
        declared folder has no name a record could carry, so its files are
        matched by being written underneath it — a filter over this platform's
        own records, which narrows and can never widen. The folder itself is
        never removed: the emulator lists it, and it is not ours.

        This is deliberately not the same rule as the download side's: a folder
        is never offered as a DOWNLOAD, because there is no file to fetch into a
        name the emulator lists. That says nothing about the files already
        inside it.
        """
        # A degenerate path would make the predicate "/" and select every
        # absolute recorded path — still only this platform's own downloads, but
        # the platform-wide set rather than a folder's, which is not what the
        # button offered. Unreachable from the UI (a folder row with no
        # `local_path` is stamped `deletable_count: 0`), refused here so the
        # narrowing claim holds for whoever calls it next.
        root = folder_path.rstrip("/")
        if not root:
            return {"success": True, "deleted_count": 0, "message": "Nothing to delete in this folder"}
        prefix = f"{root}/"
        deleted, errors = await self._loop.run_in_executor(
            None, self._delete_recorded_io, platform_slug, lambda record: record.file_path.startswith(prefix)
        )
        self._listing.invalidate()

        if errors:
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "deleted_count": deleted,
                "message": f"Could not delete every file: {errors[0]}",
            }
        if deleted == 0:
            return {"success": True, "deleted_count": 0, "message": "Nothing to delete in this folder"}
        return {"success": True, "deleted_count": deleted, "message": f"Deleted {deleted} BIOS file(s)"}

    async def delete_bios_file(self, platform_slug, file_name) -> dict[str, Any]:
        """Delete one BIOS file the plugin downloaded, by name.

        The per-row twin of :meth:`delete_platform_bios`, running the same
        :meth:`_delete_recorded_io` under a name predicate rather than a copy of
        it: a ``downloaded_bios`` record is the only evidence the plugin placed
        the file, and a row with no record removes nothing and says so. A caller
        cannot widen this into a presence-based delete by passing a different
        name.
        """
        deleted, errors = await self._loop.run_in_executor(
            None, self._delete_recorded_io, platform_slug, lambda record: record.file_name == file_name
        )
        self._listing.invalidate()

        if errors:
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "deleted_count": deleted,
                "message": f"Could not delete {file_name}: {errors[0]}",
            }
        if deleted == 0:
            return {"success": True, "deleted_count": 0, "message": f"Nothing to delete for {file_name}"}
        return {"success": True, "deleted_count": deleted, "message": f"Deleted {file_name}"}

    async def delete_platform_bios(self, platform_slug) -> dict[str, Any]:
        """Delete the BIOS files the plugin downloaded for a platform.

        Scoped to the plugin's own downloads, never to everything sitting in the
        platform's BIOS locations — see :meth:`_delete_recorded_io`. The
        download records are the only input: a status listing would re-introduce
        the library as a gate, and our own download is deletable long after RomM
        stops offering it (the case that used to hide the button entirely).
        """
        deleted, errors = await self._loop.run_in_executor(None, self._delete_recorded_io, platform_slug)
        self._listing.invalidate()

        if errors:
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "deleted_count": deleted,
                "message": f"Deleted {deleted} file(s), {len(errors)} error(s)",
            }
        if deleted == 0:
            return {"success": True, "deleted_count": 0, "message": "No BIOS files for this platform"}
        return {"success": True, "deleted_count": deleted, "message": f"Deleted {deleted} BIOS file(s)"}
