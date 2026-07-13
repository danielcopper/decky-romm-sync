"""Repository Protocols — one per aggregate root in the SQLite persistence layer.

Services declare dependencies on these Protocols rather than on concrete SQLite
adapter classes. Adapters implement them; the composition root wires them. This
keeps the dependency direction clean (adapters → Protocols, never services →
adapters) and makes each repository swappable in tests with a fake.

One Repository per aggregate root, not per table. ``RomSaveStateRepository``
spans two tables (``rom_save_states`` + ``rom_save_files``); that is an adapter
concern — services see a single aggregate.

The Protocols match the aggregate roots settled in ADR-0003 — ``Rom``,
``RomInstall``, ``RomMetadata``, ``Playtime``, ``RomSaveState``, ``BiosFile``,
``FirmwareCacheEntry``, ``SyncRun`` — plus ``PlatformSyncState`` (the per-platform
completion stamp, ADR-0023) and the ``kv_config`` key-value surface.
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
    from domain.platform_sync_state import PlatformSyncState
    from domain.playtime import PendingSessionRow, Playtime
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

    def iter_by_group_key(self, group_key: str) -> Iterator[Rom]:
        """Iterate ROMs in the sibling group *group_key* (ADR-0021).

        Range-scans the migration-010 index; a NULL key never matches, so an
        unbackfilled / solo row is absent. (version_switch.py group resolution)
        """
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

    def set_applied_launch_options(self, rom_id: int, launch_options: str | None) -> None:
        """Record the ``launch_options`` last written to *rom_id*'s shortcut (#1383).

        The only write path for ``applied_launch_options``; the sync upsert in
        :meth:`save` never touches it, so a re-sync of an unchanged (un-re-acked)
        row preserves the recorded value the delta apply reads back.
        """
        ...

    def clear_all_applied_launch_options(self) -> None:
        """Reset every recorded launch command to "unknown" (NULL).

        NULL never matches a target, so the next apply re-touches everything —
        Force Full Sync's escape hatch for Steam-side drift the recorded value
        cannot see.
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
        """Iterate ``(rom_id, metadata)`` for every ROM.

        No production caller since the paged cache load (#1025) replaced the
        full-scan read with :meth:`iter_page` + :meth:`count`; retained for
        adapter/fake symmetry with the other repositories and exercised by the
        SQLite adapter's own tests.
        """
        ...

    def iter_page(self, offset: int, limit: int) -> Iterator[tuple[int, RomMetadata]]:
        """Iterate ``(rom_id, metadata)`` for one ``rom_id``-ordered page.

        Backs the paged frontend cache load so a large library never dumps every
        row through the size-limited callable bridge in one response (#1025).
        (metadata.py get_metadata_cache_page)
        """
        ...

    def count(self) -> int:
        """Return the number of cached metadata rows. (metadata.py get_metadata_cache_page total)"""
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

    def iter_pending_sessions(self, limit: int) -> list[PendingSessionRow]:
        """Return up to *limit* outbox rows directly (cheapest-first). (playtime.py flush)"""
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

    def get_latest_terminal(self) -> SyncRun | None:
        """Return the newest run in a terminal state, or ``None``.

        Terminal = ``completed`` / ``cancelled`` / ``paused`` / ``interrupted`` /
        ``errored``, ordered by ``finished_at``. Backs the "Last sync" last-attempt hint: when
        the newest terminal run did NOT complete, it is surfaced so a cancelled,
        interrupted, or crash-resumed run reads as an attempt instead of "Never". (library/reporter.py)
        """
        ...

    def get_running(self) -> SyncRun | None:
        """Return any run with status ``running``, or ``None`` (is-a-sync-running check)."""
        ...

    def delete_history(self) -> None:
        """Delete every terminal run (keeping any ``running`` one) so both the
        ``last_sync`` and last-attempt reads return ``None``.

        Backs the "Force Full Sync" reset: clearing the run history resets the
        ``last_sync`` the incremental-skip gate keys off (forcing a full re-fetch)
        AND drops the accumulated cancelled/paused/interrupted/errored runs the last-attempt
        hint reads — otherwise a stale cancelled run would surface as the "Last sync"
        state right after a reset. A ``running`` row is preserved so a reset can
        never orphan an in-flight run. (library/reporter.py)
        """
        ...


class PlatformSyncStateRepository(Protocol):
    """Persistence seam for the ``PlatformSyncState`` aggregate (per-platform completion stamp).

    Identity is the ``platform_slug``. Backs the incremental-skip gate's honoring
    of durable per-platform progress a cancelled/crashed run leaves behind
    (ADR-0023).
    """

    def get(self, platform_slug: str) -> PlatformSyncState | None:
        """Return the completion stamp for *platform_slug*, or ``None``. (library/fetcher.py skip gate)"""
        ...

    def save(self, state: PlatformSyncState) -> None:
        """Upsert the completion stamp. (library/reporter.py final-chunk commit)"""
        ...

    def delete(self, platform_slug: str) -> None:
        """Drop *platform_slug*'s stamp so that one platform full-fetches next run.

        A no-op when no stamp exists. Called at a platform unit's apply start
        (library/sync_orchestrator.py) so an interrupted re-apply leaves no stale
        stamp, and by the local destructive flows (services/shortcut_removal.py)
        that unbind a platform's shortcuts outside a sync (ADR-0023).
        """
        ...

    def clear(self) -> None:
        """Drop every stamp so no platform skips next run.

        Backs the "Force Full Sync" reset alongside the completed-run history:
        clearing the stamps forces every platform to full-fetch. (library/reporter.py)
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
