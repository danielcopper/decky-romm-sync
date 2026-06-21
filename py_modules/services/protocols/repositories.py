"""Repository Protocols — one per aggregate root in the SQLite persistence layer.

Services declare dependencies on these Protocols rather than on concrete SQLite
adapter classes. Adapters implement them; the composition root wires them. This
keeps the dependency direction clean (adapters → Protocols, never services →
adapters) and makes each repository swappable in tests with a fake.

One Repository per aggregate root, not per table. ``RomSaveStateRepository``
spans two tables (``rom_save_states`` + ``rom_save_files``); that is an adapter
concern — services see a single aggregate.

The 9 Protocols match the 9 aggregate roots settled in ADR-0003: ``Rom``,
``RomInstall``, ``RomMetadata``, ``Playtime``, ``RomSaveState``, ``BiosFile``,
``FirmwareCacheEntry``, ``SyncRun``, and the ``kv_config`` key-value surface.
``SyncSettings``/``Platform``/``Device`` are NOT repositories — ADR-0003 dropped
those aggregates.

Repository methods take no database connection parameter. The Unit-of-Work layer
(#783) injects connections into concrete adapter constructors; services see only
this Protocol surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.bios_file import BiosFile
    from domain.firmware_cache import FirmwareCacheEntry
    from domain.playtime import Playtime
    from domain.rom import Rom
    from domain.rom_install import RomInstall
    from domain.rom_metadata import RomMetadata
    from domain.rom_save_state import RomSaveState
    from domain.sync_run import SyncRun


class RomRepository(Protocol):
    """Persistence seam for the ``Rom`` aggregate (the synced-shortcut registry)."""

    def get(self, rom_id: int) -> Rom | None:
        """Return the ROM with *rom_id*, or ``None`` when absent.

        (artwork.py:73, game_detail.py:66, achievements.py:124)
        """
        ...

    def get_by_app_id(self, app_id: int) -> Rom | None:
        """Return the ROM bound to Steam *app_id*, or ``None``.

        (library/reporter.py app_id reverse lookup, game_detail.py:64)
        """
        ...

    def save(self, rom: Rom) -> None:
        """Upsert *rom*. (library/reporter.py apply_sync upsert)"""
        ...

    def delete(self, rom_id: int) -> None:
        """Remove the ROM with *rom_id*. Idempotent. (library/reporter.py, shortcut_removal.py)"""
        ...

    def iter_all(self) -> Iterator[Rom]:
        """Iterate every ROM in the registry. (library/reporter.py full scan, startup_healing.py)"""
        ...

    def iter_by_platform(self, platform_slug: str) -> Iterator[Rom]:
        """Iterate ROMs on *platform_slug*. (firmware.py platform filter)"""
        ...

    def count(self) -> int:
        """Return the number of ROMs in the registry. (library/reporter.py len registry, shortcut_removal.py stats)"""
        ...

    def set_emulator_override(self, rom_id: int, label: str | None) -> None:
        """Pin (or clear with ``None``) the per-game emulator override for *rom_id*.

        The only write path for ``emulator_override``; the sync upsert in
        :meth:`save` never touches it, so a re-sync preserves the pin.
        """
        ...

    def get_all_emulator_overrides(self) -> dict[int, str]:
        """Return ``rom_id`` -> pinned core label for every ROM with an override (NULL rows omitted)."""
        ...

    def set_selected_disc(self, rom_id: int, filename: str | None) -> None:
        """Pin (or clear with ``None``) the per-game disc selection for *rom_id*.

        The only write path for ``selected_disc``; the sync upsert in
        :meth:`save` never touches it, so a re-sync preserves the pick.
        """
        ...


class RomInstallRepository(Protocol):
    """Persistence seam for the ``RomInstall`` aggregate (installed-ROM file records)."""

    def get(self, rom_id: int) -> RomInstall | None:
        """Return the install record for *rom_id*, or ``None``. (downloads.py, game_detail.py, saves/rom_info.py)"""
        ...

    def save(self, install: RomInstall) -> None:
        """Upsert the install record. (downloads.py)"""
        ...

    def delete(self, rom_id: int) -> None:
        """Remove the install record for *rom_id*. Idempotent. (rom_removal.py)"""
        ...

    def iter_all(self) -> Iterator[RomInstall]:
        """Iterate every install record. (migration.py, saves/sync_engine/engine.py)"""
        ...


class RomMetadataRepository(Protocol):
    """Persistence seam for the ``RomMetadata`` aggregate (cached RomM metadata).

    Keyed by *rom_id*, which is supplied externally rather than carried on the
    aggregate.
    """

    def get(self, rom_id: int) -> RomMetadata | None:
        """Return cached metadata for *rom_id*, or ``None``. (metadata.py, game_detail.py)"""
        ...

    def save(self, rom_id: int, metadata: RomMetadata) -> None:
        """Upsert *metadata* under *rom_id*. (metadata.py flush)"""
        ...

    def delete(self, rom_id: int) -> None:
        """Remove cached metadata for *rom_id*. Idempotent. (metadata.py)"""
        ...

    def iter_all(self) -> Iterator[tuple[int, RomMetadata]]:
        """Iterate ``(rom_id, metadata)`` for every ROM. (metadata.py get_all_metadata_cache)"""
        ...


class PlaytimeRepository(Protocol):
    """Persistence seam for the ``Playtime`` aggregate (per-ROM session totals).

    Keyed by *rom_id*, which is supplied externally rather than carried on the
    aggregate.
    """

    def get(self, rom_id: int) -> Playtime | None:
        """Return playtime for *rom_id*, or ``None``. (playtime.py)"""
        ...

    def save(self, rom_id: int, playtime: Playtime) -> None:
        """Upsert *playtime* under *rom_id*. (playtime.py session start/end)"""
        ...

    def delete(self, rom_id: int) -> None:
        """Remove playtime for *rom_id*. Idempotent. (saves/state.py orphan prune)"""
        ...

    def iter_all(self) -> Iterator[tuple[int, Playtime]]:
        """Iterate ``(rom_id, playtime)`` for every ROM. (playtime.py get_all_playtime, saves/state.py)"""
        ...


class RomSaveStateRepository(Protocol):
    """Persistence seam for the ``RomSaveState`` aggregate (per-ROM save-sync state).

    Keyed by *rom_id*. The aggregate spans two tables — the adapter reconstructs
    the per-file ``files{}`` mapping from ``rom_save_files``; services see one
    aggregate.
    """

    def get(self, rom_id: int) -> RomSaveState | None:
        """Return the save-sync state for *rom_id*, or ``None``. (saves/state.py, saves/sync_engine)"""
        ...

    def save(self, rom_id: int, state: RomSaveState) -> None:
        """Upsert *state* under *rom_id*, replacing its child file rows. (saves/state.py, saves/sync_engine)"""
        ...

    def delete(self, rom_id: int) -> None:
        """Remove the save-sync state for *rom_id*. Idempotent. (saves/state.py orphan prune)"""
        ...

    def iter_all(self) -> Iterator[tuple[int, RomSaveState]]:
        """Iterate ``(rom_id, state)`` for every ROM. (saves/state.py orphan scan)"""
        ...


class BiosFileRepository(Protocol):
    """Persistence seam for the ``BiosFile`` aggregate (downloaded BIOS records).

    Identity is the composite ``(platform_slug, file_name)``.
    """

    def get(self, platform_slug: str, file_name: str) -> BiosFile | None:
        """Return the BIOS record for the composite key, or ``None``. (migration.py existence check)"""
        ...

    def save(self, bios_file: BiosFile) -> None:
        """Upsert *bios_file*. (firmware.py)"""
        ...

    def delete(self, platform_slug: str, file_name: str) -> None:
        """Remove the BIOS record for the composite key. Idempotent. (firmware.py)"""
        ...

    def iter_all(self) -> Iterator[BiosFile]:
        """Iterate every downloaded BIOS record. (migration.py migration sweep)"""
        ...

    def iter_by_platform(self, platform_slug: str) -> Iterator[BiosFile]:
        """Iterate BIOS records on *platform_slug*. (firmware.py delete platform BIOS)"""
        ...


class FirmwareCacheRepository(Protocol):
    """Persistence seam for the ``FirmwareCacheEntry`` aggregate (cached firmware listing).

    Identity is the composite ``(platform_slug, name)``. The cache is refreshed
    wholesale on a TTL, not mutated per row.
    """

    def get(self, platform_slug: str, name: str) -> FirmwareCacheEntry | None:
        """Return the firmware entry for the composite key, or ``None``."""
        ...

    def iter_all(self) -> Iterator[FirmwareCacheEntry]:
        """Iterate every cached firmware entry. (firmware.py display)"""
        ...

    def replace_all(self, entries: list[FirmwareCacheEntry]) -> None:
        """Replace the entire cache with *entries*. (firmware.py wholesale TTL refresh)"""
        ...

    def clear(self) -> None:
        """Drop every cached firmware entry. (firmware.py invalidate)"""
        ...

    def get_cache_epoch(self) -> float | None:
        """Return the cache's last-refresh timestamp, or ``None`` when empty. (firmware.py TTL check)"""
        ...


class SyncRunRepository(Protocol):
    """Persistence seam for the ``SyncRun`` aggregate (sync-run history).

    Identity is a string UUID.
    """

    def get(self, run_id: str) -> SyncRun | None:
        """Return the run with *run_id*, or ``None``."""
        ...

    def save(self, run: SyncRun) -> None:
        """Upsert *run*. (library/reporter.py create at start, update at terminal)"""
        ...

    def get_latest_completed(self) -> SyncRun | None:
        """Return the newest run with status ``completed``, or ``None``. (library/reporter.py last_sync read)"""
        ...

    def get_running(self) -> SyncRun | None:
        """Return any run with status ``running``, or ``None`` (is-a-sync-running check)."""
        ...

    def delete_completed(self) -> None:
        """Delete every ``completed`` run so ``get_latest_completed`` returns ``None``.

        Backs the "Force Full Sync" reset: clearing the completed-run history
        resets the ``last_sync`` read the incremental-skip gate keys off, forcing
        the next sync to full-fetch every platform. (library/reporter.py)
        """
        ...


class KvConfigRepository(Protocol):
    """Persistence seam for the ``kv_config`` key-value table.

    No domain aggregate — a flat string-keyed, string-valued surface for the
    truly-singleton scalars (``device_id``) and cross-run change-detection
    markers. Callers own JSON encoding/decoding; values are always stored as
    TEXT.
    """

    def get(self, key: str) -> str | None:
        """Return the value for *key*, or ``None`` when absent. (migration.py)"""
        ...

    def set(self, key: str, value: str) -> None:
        """Insert-or-replace *value* under *key*; value is always TEXT. (migration.py)"""
        ...

    def delete(self, key: str) -> None:
        """Remove *key*. Idempotent. (migration.py)"""
        ...
