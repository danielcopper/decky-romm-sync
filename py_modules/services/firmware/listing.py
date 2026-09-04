"""What the RomM library holds in firmware, and the cache that answers for it.

The one place the ``list_firmware`` roundtrip is made and the one place its
answer is kept. Everything downstream — the status surfaces, the download
entry points — asks here rather than the server, so a single fetch serves a
whole page and an unreachable server degrades in one place instead of five.

The cache is durable by way of the SQLite ``firmware_cache`` table, so a
restart still has a listing to answer from while the server is unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain import firmware_paths
from domain.firmware_cache import FirmwareCacheEntry

if TYPE_CHECKING:
    import logging

    from services.protocols import Clock, RommFirmwareApi, UnitOfWorkFactory

_FIRMWARE_CACHE_TTL = 3600  # 1 hour


@dataclass(frozen=True)
class FirmwareListingConfig:
    """Frozen wiring bundle handed to ``FirmwareListing.__init__``.

    Holds the RomM API adapter, the clock the TTL is measured against, the
    logger, and the SQLite Unit-of-Work factory the durable half of the cache
    is read and written through.
    """

    romm_api: RommFirmwareApi
    logger: logging.Logger
    clock: Clock
    uow_factory: UnitOfWorkFactory


class FirmwareListing:
    """The RomM firmware listing and its cache — the subsystem's only ``list_firmware`` caller."""

    def __init__(self, *, config: FirmwareListingConfig) -> None:
        self._romm_api = config.romm_api
        self._logger = config.logger
        self._clock = config.clock
        self._uow_factory = config.uow_factory
        self._firmware_cache: list[dict[str, Any]] | None = None
        self._firmware_cache_epoch: float = 0
        self._restore_firmware_cache()

    def _restore_firmware_cache(self) -> None:
        """Rebuild the in-memory firmware cache from the SQLite cache table.

        The ``firmware_cache`` table is a thin record per ADR-0003 — it carries
        the already-parsed ``platform_slug`` and ``name`` but not the raw RomM
        ``file_path`` or ``md5_hash``. We synthesize a ``bios/<slug>/<name>``
        ``file_path`` that round-trips through ``parse_firmware_slug`` so a
        restart still has a listing to answer from while the server is
        unreachable; ``md5_hash`` is intentionally absent (display-only).
        """
        try:
            with self._uow_factory() as uow:
                entries = list(uow.firmware_cache.iter_all())
                epoch = uow.firmware_cache.get_cache_epoch()
        except Exception as e:
            self._logger.warning(f"Failed to load firmware cache from DB: {e}")
            return

        if not entries or epoch is None:
            return

        self._firmware_cache = [self._entry_to_firmware_dict(entry) for entry in entries]
        self._firmware_cache_epoch = epoch
        self._logger.info("Restored firmware cache from DB (%d items)", len(entries))

    @staticmethod
    def _entry_to_firmware_dict(entry: FirmwareCacheEntry) -> dict[str, Any]:
        """Reconstruct an in-memory firmware dict from a thin cache aggregate."""
        return {
            "id": entry.id,
            "file_name": entry.name,
            "file_path": f"bios/{entry.platform_slug}/{entry.name}",
            "file_size_bytes": entry.file_size_bytes,
            "md5_hash": "",
        }

    def _persist_firmware_cache(self) -> None:
        """Replace the SQLite firmware cache with the current in-memory listing.

        Maps each raw RomM firmware dict to a thin ``FirmwareCacheEntry`` (slug
        pre-parsed from ``file_path``) and writes them wholesale.
        """
        if self._firmware_cache is None:
            return
        entries = [
            FirmwareCacheEntry.cached(
                id=fw.get("id"),
                name=fw.get("file_name", ""),
                platform_slug=firmware_paths.parse_firmware_slug(fw.get("file_path", "")),
                file_size_bytes=fw.get("file_size_bytes", 0),
                cached_at=self._firmware_cache_epoch,
            )
            for fw in self._firmware_cache
        ]
        try:
            with self._uow_factory() as uow:
                uow.firmware_cache.replace_all(entries)
        except Exception as e:
            self._logger.warning(f"Failed to persist firmware cache: {e}")

    def get_firmware_list(self) -> list[dict[str, Any]]:
        """Return firmware list, using cache if TTL has not expired.

        TTL is checked against the wall-clock cache epoch so a cache
        restored from disk after a plugin restart still expires.

        On HTTP error, falls back to cached data if there is any and RAISES
        otherwise. Returning an empty list instead would be indistinguishable
        from a server that genuinely holds no firmware, and ``check_platform_bios``
        answers a confident "needs none" for that — so the raise is what lets a
        failed fetch be reported as unknown rather than as a negative (#1693).
        """
        now = self._clock.time()
        if self._firmware_cache is not None and (now - self._firmware_cache_epoch) < _FIRMWARE_CACHE_TTL:
            return self._firmware_cache

        try:
            result = self._romm_api.list_firmware()
            self._firmware_cache = result
            self._firmware_cache_epoch = self._clock.time()
            self._persist_firmware_cache()
            return result
        except Exception as e:
            self._logger.warning(f"Failed to fetch firmware list: {e}")
            if self._firmware_cache is not None:
                return self._firmware_cache
            raise

    def invalidate(self) -> None:
        """Clear cached firmware list so the next call re-fetches."""
        self._firmware_cache = None
        self._firmware_cache_epoch = 0
        try:
            with self._uow_factory() as uow:
                uow.firmware_cache.clear()
        except Exception as e:
            self._logger.warning(f"Failed to clear persisted firmware cache: {e}")
