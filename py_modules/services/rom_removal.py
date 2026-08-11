"""RomRemovalService — installed-ROM file deletion and ``rom_installs`` cleanup.

Physically deletes a ROM's files from disk and drops its ``rom_installs``
record. Per [ADR-0007](docs/adr/0007-rom-retention-identity-anchor.md) an
uninstall is *not* a purge: the ``roms`` identity row, playtime, saves, and
metadata all survive — only the on-disk files and the install record go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.list_result import ErrorCode
from lib.path_safety import is_safe_rom_path

if TYPE_CHECKING:
    import asyncio
    import logging
    from collections.abc import Callable

    from models.prune import MutationOutcome, SourceClaim

    from domain.rom_install import RomInstall
    from services.protocols import (
        Clock,
        DownloadQueueCleanup,
        EventEmitter,
        RetroDeckPaths,
        RomFileStore,
        UnitOfWorkFactory,
    )

# Seconds between ``uninstall_progress`` frames while a multi-file removal runs.
# The terminal frame is never throttled.
_PROGRESS_INTERVAL_S = 0.5


@dataclass(frozen=True)
class RomRemovalServiceConfig:
    """Frozen wiring bundle handed to ``RomRemovalService.__init__``.

    Holds the runtime infrastructure, the Protocol-typed filesystem
    adapter, the RetroDECK paths bundle, the ``DownloadQueueCleanup``
    eviction seam (``None`` when no download cleanup is wired), and the
    SQLite Unit-of-Work factory (the transactional seam over the
    ``rom_installs`` repository). Decomposes the ctor so a new dependency
    does not push past the S107 parameter-count limit.
    """

    logger: logging.Logger
    loop: asyncio.AbstractEventLoop
    clock: Clock
    emit: EventEmitter
    rom_file_store: RomFileStore
    retrodeck_paths: RetroDeckPaths
    download_queue_cleanup: DownloadQueueCleanup | None
    uow_factory: UnitOfWorkFactory


class RomRemovalService:
    """Handles physical deletion of installed ROM files and ``rom_installs`` cleanup."""

    def __init__(
        self,
        *,
        config: RomRemovalServiceConfig,
    ):
        self._logger = config.logger
        self._loop = config.loop
        self._clock = config.clock
        self._emit = config.emit
        self._rom_file_store = config.rom_file_store
        self._retrodeck_paths = config.retrodeck_paths
        self._download_queue_cleanup = config.download_queue_cleanup
        self._uow_factory = config.uow_factory
        # Read and written only on the loop thread — every mutation brackets a
        # ``run_in_executor`` call rather than happening inside one — so both
        # removal entry points share it without a lock, which `services/` may
        # not import anyway (`.importlinter`, no-stdlib-io-in-services).
        self._removals_in_flight: set[int] = set()

    def _delete_rom_files(
        self,
        install: RomInstall,
        claims: dict[str, SourceClaim] | None = None,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> MutationOutcome:
        """Delete ROM files for an install record. Handles both single-file and multi-file ROMs.

        A multi-file ROM owns a dedicated per-ROM directory (``rom_dir`` is set)
        and is removed whole. A single-file ROM has no ``rom_dir`` (``None``) —
        it lives as a bare file in the shared ``<roms_base>/<system>/`` dir,
        which must **never** be removed — so only the launch file itself is
        deleted. ``is_safe_rom_path`` stays the path-containment guard before
        any removal.

        *claims* is the claim map a cleanup run that sealed a recovery bundle
        hands in; its absence marks a caller that has no bundle at all and
        therefore seals its own claim — see :meth:`_remove_under_claim`.
        """
        rom_dir = install.rom_dir
        file_path = install.file_path

        roms_base = self._retrodeck_paths.roms_path()
        if rom_dir:
            if not is_safe_rom_path(rom_dir, roms_base):
                raise ValueError(f"Refusing to delete path outside roms directory: {rom_dir}")
            if self._rom_file_store.exists(rom_dir) and not self._rom_file_store.is_dir(rom_dir):
                raise ValueError(f"Expected installed ROM directory, found another file type: {rom_dir}")
            return self._remove_under_claim(rom_dir, roms_base, claims, on_progress)
        if file_path:
            if not is_safe_rom_path(file_path, roms_base):
                raise ValueError(f"Refusing to delete path outside roms directory: {file_path}")
            if self._rom_file_store.is_dir(file_path):
                raise ValueError(f"Expected installed ROM file, found a directory: {file_path}")
            return self._remove_under_claim(file_path, roms_base, claims, on_progress)
        return {"success": True, "changed": False, "ambiguous": False, "message": "No installed path recorded"}

    def _remove_under_claim(
        self,
        path: str,
        roms_base: str,
        claims: dict[str, SourceClaim] | None,
        on_progress: Callable[[int, int], None] | None,
    ) -> MutationOutcome:
        """Remove one already-guarded path under the claim that authorizes it.

        A run that sealed a recovery bundle hands its claim map in, so a source
        it captured is removed under what the bundle proved, and one it did not
        capture takes a fresh content-bound claim here — the bundle exists, so
        the hashes still have a copy to bind to. A caller that hands in no map
        has no bundle anywhere: the claim it seals here is consumed a moment
        later by this same call, with nothing else holding the bytes, so it is
        sealed identity-only.

        That last case is also the only one allowed to adopt the debris of an
        attempt interrupted between the staging rename and the last unlink. It
        holds the install record proving the path is this ROM's, and its claim
        discipline is one it can simply re-seal, where a bundle-backed removal's
        authority came from a seal that a partially consumed source no longer
        matches.
        """
        self_sealed = claims is None
        claim = claims.get(path) if claims is not None else None
        if claim is None:
            claim = self._rom_file_store.claim_source(path, roms_base, digest=not self_sealed)
            if self_sealed and not claim["source_identity"]["exists"]:
                reclaimed = self._rom_file_store.reclaim_staged_source(path, roms_base)
                if reclaimed["changed"] or not reclaimed["success"]:
                    return reclaimed
        return self._rom_file_store.remove_claimed(path, roms_base, claim, on_progress)

    def _make_progress_callback(self, rom_id: int) -> Callable[[int, int], None]:
        """Build a throttled per-file removal callback for one uninstall.

        Single-file ROMs report nothing: a one-entry progress bar is noise. The
        callback runs on the ``run_in_executor`` worker, so the emit is marshaled
        to the loop thread.
        """
        last_emit = [0.0]

        def on_progress(removed: int, total: int) -> None:
            if total <= 1:
                return
            now = self._clock.monotonic()
            if now - last_emit[0] < _PROGRESS_INTERVAL_S and removed < total:
                return
            last_emit[0] = now
            self._loop.call_soon_threadsafe(self._publish_removal_progress, rom_id, removed, total)

        return on_progress

    def _publish_removal_progress(self, rom_id: int, removed: int, total: int) -> None:
        """Schedule one ``uninstall_progress`` emit. Runs on the loop thread."""
        self._loop.create_task(
            self._emit(
                "uninstall_progress",
                {"rom_id": rom_id, "files_removed": removed, "files_total": total},
            )
        )

    def _elapsed(self, started: float) -> str:
        """Render the time since *started* for a log line."""
        return f"{self._clock.monotonic() - started:.1f}s"

    def delete_rom_files(self, rom_id: int, claims: dict[str, SourceClaim] | None = None) -> dict[str, Any]:
        """Delete only installed content, leaving every database row untouched."""
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(int(rom_id))
        if install is None:
            return {"success": False, "reason": "not_installed", "message": "ROM not installed"}
        try:
            outcome = self._delete_rom_files(install, claims)
        except Exception as exc:
            self._logger.error(f"Failed to delete ROM files: {exc}")
            return {
                "success": False,
                "reason": ErrorCode.UNKNOWN.value,
                "message": str(exc),
                "changed": False,
                "ambiguous": False,
            }
        if not outcome["success"]:
            return {"reason": ErrorCode.UNKNOWN.value, **outcome}
        return dict(outcome)

    def _remove_rom_io(self, rom_id: int, install: RomInstall) -> None:
        """Sync helper for remove_rom — file deletion (outside UoW) then row delete in a short write UoW.

        Files are deleted outside any transaction (ADR-0006); only the
        ``rom_installs`` row delete is wrapped. Per ADR-0007 the ``roms`` row,
        playtime, saves, and metadata are left untouched — an uninstall drops
        only the files and the install record.

        A bound ROM has its recorded ``applied_launch_options`` reset to the
        uninstalled placeholder (``""``) in the same UoW: the frontend resets the
        kept shortcut's launch command to ``""`` on uninstall (#1146), so recording
        ``""`` keeps the next sync from re-touching an already-correct shortcut
        (delta apply, #1383). Fourth of the six recorded-state writer sites.
        """
        outcome = self._delete_rom_files(install, on_progress=self._make_progress_callback(rom_id))
        if not outcome["success"]:
            raise RuntimeError(outcome["message"])
        with self._uow_factory() as uow:
            uow.rom_installs.delete(rom_id)
            rom = uow.roms.get(rom_id)
            if rom is not None and rom.shortcut_app_id is not None:
                rom.record_applied_launch_options("")
                uow.roms.set_applied_launch_options(rom_id, rom.applied_launch_options)

    async def remove_rom(self, rom_id: int | str) -> dict[str, Any]:
        """Remove a single installed ROM: delete files and drop the install record.

        Refused while any removal that owns this ROM's tree is running — its own
        earlier press, or a bulk uninstall, which claims every ROM it is about
        to remove. The running one has renamed its source to a staging name, so
        a second attempt against it would report the source as vanished while
        the removal it duplicates is still working.
        """
        rom_id_int = int(rom_id)
        with self._uow_factory() as uow:
            install = uow.rom_installs.get(rom_id_int)
        if install is None:
            return {"success": False, "reason": "not_installed", "message": "ROM not installed"}
        if rom_id_int in self._removals_in_flight:
            return {
                "success": False,
                "reason": "in_progress",
                "message": "This ROM is already being uninstalled",
            }

        self._removals_in_flight.add(rom_id_int)
        started = self._clock.monotonic()
        self._logger.info(f"Uninstall started: rom_id={rom_id_int}")
        try:
            await self._loop.run_in_executor(None, self._remove_rom_io, rom_id_int, install)
        except Exception as e:
            self._logger.error(f"Failed to delete ROM files after {self._elapsed(started)}: {e}")
            return {"success": False, "reason": ErrorCode.UNKNOWN.value, "message": "Failed to delete ROM files"}
        finally:
            self._removals_in_flight.discard(rom_id_int)
        self._logger.info(f"Uninstall completed: rom_id={rom_id_int} in {self._elapsed(started)}")

        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.evict(rom_id_int)

        return {"success": True, "message": "ROM removed"}

    def _uninstall_all_roms_io(self, installs: list[RomInstall]) -> tuple[int, list[dict[str, str]], list[int]]:
        """Sync helper for uninstall_all_roms — bulk file deletion (outside UoW) then row deletes in a write UoW.

        Deletes the files of every already-claimed install outside any
        transaction (collecting per-ROM errors), then drops the install rows for
        the ROMs whose files were deleted in one write UoW. Per ADR-0007 the
        ``roms`` rows, playtime, saves, and metadata survive.

        *installs* is read and claimed by the caller on the loop thread rather
        than here, so the set of removals in flight has a single writer.

        Also returns the bound ``shortcut_app_id`` of each ROM whose files were
        deleted (unbound rows contribute none), so the frontend can reset those
        kept shortcuts' now-stale ``launch_options`` to the uninstalled
        placeholder (#1146).
        """
        count = 0
        errors: list[dict[str, str]] = []
        successfully_deleted: list[int] = []
        for install in installs:
            try:
                outcome = self._delete_rom_files(install)
                if not outcome["success"]:
                    raise RuntimeError(outcome["message"])
                count += 1
                successfully_deleted.append(install.rom_id)
            except Exception as e:
                errors.append({"rom_id": str(install.rom_id), "error": str(e)})
                self._logger.error(f"Failed to delete ROM {install.rom_id}: {e}")

        app_ids: list[int] = []
        with self._uow_factory() as uow:
            for rom_id in successfully_deleted:
                uow.rom_installs.delete(rom_id)
                rom = uow.roms.get(rom_id)
                if rom is not None and rom.shortcut_app_id is not None:
                    app_ids.append(rom.shortcut_app_id)
                    # The frontend resets each kept shortcut's launch command to ""
                    # (#1146); record that so the next sync skips it (delta apply,
                    # #1383). Same recorded-state writer site as remove_rom, bulk.
                    rom.record_applied_launch_options("")
                    uow.roms.set_applied_launch_options(rom_id, rom.applied_launch_options)
        return count, errors, app_ids

    async def uninstall_all_roms(self) -> dict[str, Any]:
        """Remove all installed ROMs: delete files and drop their install records.

        Returns ``success`` (True only when every per-ROM deletion
        succeeded), ``removed_count`` (number of ROMs whose files were
        deleted), ``errors`` (one ``{"rom_id", "error"}`` entry per
        failed deletion), and ``app_ids`` (the bound Steam ``shortcut_app_id``
        of each ROM whose files were deleted) so the frontend can reset those
        kept shortcuts' now-stale ``launch_options`` to the uninstalled
        placeholder (#1146). Install records for partially-failed bulk runs
        are left intact for the failing entries so the user can retry.

        Claims every ROM it is about to remove before dispatching the worker, so
        a single uninstall of any of them is refused while this runs and this is
        refused while any of them is already being removed — one bulk run and
        one single removal must never work the same tree. The refusal carries no
        removal payload, which is how the frontend already tells a refusal from
        a partial failure.
        """
        with self._uow_factory() as uow:
            installs = list(uow.rom_installs.iter_all())
        claimed = {install.rom_id for install in installs}
        if claimed & self._removals_in_flight:
            return {
                "success": False,
                "reason": "in_progress",
                "message": "A ROM is already being uninstalled",
            }

        self._removals_in_flight |= claimed
        started = self._clock.monotonic()
        self._logger.info("Bulk uninstall started")
        try:
            count, errors, app_ids = await self._loop.run_in_executor(None, self._uninstall_all_roms_io, installs)
        finally:
            self._removals_in_flight -= claimed
        self._logger.info(
            f"Bulk uninstall completed: {count} removed, {len(errors)} failed in {self._elapsed(started)}"
        )
        if self._download_queue_cleanup is not None:
            self._download_queue_cleanup.clear()
        return {
            "success": len(errors) == 0,
            "removed_count": count,
            "errors": errors,
            "app_ids": app_ids,
        }
