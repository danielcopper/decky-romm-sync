"""In-memory ``UnitOfWork`` composing the nine fake repositories, for service tests.

Mirrors the real UoW's context-manager shape — a clean ``__exit__`` commits and
flips ``committed``; an exceptional one truly rolls back (discards every write
made inside the block) before flipping ``rolled_back`` and re-raising. Rollback
is snapshot/restore: ``__enter__`` snapshots each repo's store, an exceptional
``__exit__`` restores them. This matches the real ``SqliteUnitOfWork``, which
discards uncommitted writes via SQL ``ROLLBACK``.

The clean-commit path also models the schema's ``rom_id`` foreign key: the real
``SqliteUnitOfWork`` runs with ``PRAGMA foreign_keys=ON``, so committing a
per-rom child aggregate whose ``rom_id`` has no matching ``roms`` row raises
``sqlite3.IntegrityError``. The fake reproduces that at commit (see
``_PER_ROM_FK_CHILD_REPOS``) and, like the real failed COMMIT, rolls the
uncommitted writes back rather than leaving the orphan observable. ON DELETE
CASCADE is intentionally **not** modeled — no consumer deliberately purges a
``roms`` row yet (ADR-0007); only the orphan-child-on-commit check exists.

The repositories persist across clean ``with`` blocks (the fake's storage is the
fake's identity), so a service test can open the unit several times and see
prior committed writes. ``FakeUnitOfWorkFactory`` returns the same unit each call
so a test can inspect state after the service closes it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fakes.fake_bios_file_repository import FakeBiosFileRepository
from fakes.fake_firmware_cache_repository import FakeFirmwareCacheRepository
from fakes.fake_kv_config_repository import FakeKvConfigRepository
from fakes.fake_playtime_repository import FakePlaytimeRepository
from fakes.fake_rom_install_repository import FakeRomInstallRepository
from fakes.fake_rom_metadata_repository import FakeRomMetadataRepository
from fakes.fake_rom_repository import FakeRomRepository
from fakes.fake_rom_save_state_repository import FakeRomSaveStateRepository
from fakes.fake_sync_run_repository import FakeSyncRunRepository

if TYPE_CHECKING:
    from types import TracebackType

    from domain.bios_file import BiosFile
    from domain.firmware_cache import FirmwareCacheEntry
    from domain.playtime import Playtime
    from domain.rom import Rom
    from domain.rom_install import RomInstall
    from domain.rom_metadata import RomMetadata
    from domain.rom_save_state import RomSaveState
    from domain.sync_run import SyncRun


@dataclass(frozen=True)
class _Snapshot:
    """One deep-copied store per fake repo, captured at ``__enter__`` for rollback."""

    roms: dict[int, Rom]
    rom_installs: dict[int, RomInstall]
    rom_metadata: dict[int, RomMetadata]
    playtime: dict[int, Playtime]
    rom_save_states: dict[int, RomSaveState]
    bios_files: dict[tuple[str, str], BiosFile]
    firmware_cache: dict[tuple[str, str], FirmwareCacheEntry]
    sync_runs: dict[str, SyncRun]
    kv_config: dict[str, str]


class FakeUnitOfWork:
    """In-memory unit of work over nine fake repositories with commit/rollback flags."""

    # Child repos whose aggregate carries a ``rom_id`` foreign key onto ``roms``
    # (schema: rom_installs / rom_metadata / rom_playtime / rom_save_states +
    # rom_save_files, the last two backing the one ``rom_save_states`` repo).
    # Each is keyed by ``rom_id``, so ``repo._snapshot().keys()`` are the FK
    # values the commit check validates against ``roms``. Adding a new per-rom
    # vertical means adding its repo attr name here — nothing else.
    _PER_ROM_FK_CHILD_REPOS = ("rom_installs", "rom_metadata", "playtime", "rom_save_states")

    def __init__(self) -> None:
        self.roms = FakeRomRepository()
        self.rom_installs = FakeRomInstallRepository()
        self.rom_metadata = FakeRomMetadataRepository()
        self.playtime = FakePlaytimeRepository()
        self.rom_save_states = FakeRomSaveStateRepository()
        self.bios_files = FakeBiosFileRepository()
        self.firmware_cache = FakeFirmwareCacheRepository()
        self.sync_runs = FakeSyncRunRepository()
        self.kv_config = FakeKvConfigRepository()
        self.committed = False
        self.rolled_back = False
        self.enter_count = 0
        self._snapshot: _Snapshot | None = None

    def __enter__(self) -> FakeUnitOfWork:
        self.enter_count += 1
        # Snapshot every repo's store so an exceptional exit can discard
        # writes made inside the block (the real UoW's SQL ROLLBACK).
        self._snapshot = _Snapshot(
            roms=self.roms._snapshot(),
            rom_installs=self.rom_installs._snapshot(),
            rom_metadata=self.rom_metadata._snapshot(),
            playtime=self.playtime._snapshot(),
            rom_save_states=self.rom_save_states._snapshot(),
            bios_files=self.bios_files._snapshot(),
            firmware_cache=self.firmware_cache._snapshot(),
            sync_runs=self.sync_runs._snapshot(),
            kv_config=self.kv_config._snapshot(),
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        snapshot = self._snapshot
        self._snapshot = None
        if exc_type is None:
            # Mirror PRAGMA foreign_keys=ON: a child row whose rom_id has no
            # matching roms row aborts the commit with sqlite3.IntegrityError.
            try:
                self._enforce_rom_id_foreign_keys()
            except sqlite3.IntegrityError:
                # A failed COMMIT discards the transaction's uncommitted writes;
                # model that rollback before re-raising so the orphaned write is
                # not left observable (the real UoW closes the connection).
                self._restore_snapshot(snapshot)
                self.rolled_back = True
                raise
            self.committed = True
            return
        self._restore_snapshot(snapshot)
        self.rolled_back = True

    def _restore_snapshot(self, snapshot: _Snapshot | None) -> None:
        """Discard every write made inside the block (the real UoW's ``ROLLBACK``)."""
        if snapshot is None:
            return
        self.roms._restore(snapshot.roms)
        self.rom_installs._restore(snapshot.rom_installs)
        self.rom_metadata._restore(snapshot.rom_metadata)
        self.playtime._restore(snapshot.playtime)
        self.rom_save_states._restore(snapshot.rom_save_states)
        self.bios_files._restore(snapshot.bios_files)
        self.firmware_cache._restore(snapshot.firmware_cache)
        self.sync_runs._restore(snapshot.sync_runs)
        self.kv_config._restore(snapshot.kv_config)

    def _enforce_rom_id_foreign_keys(self) -> None:
        """Raise ``sqlite3.IntegrityError`` if any per-rom child row is orphaned.

        Validates every ``rom_id`` in the FK-bearing child repos against the
        ``roms`` repo, matching the real schema's ``REFERENCES roms(rom_id)``.
        On failure ``__exit__`` restores the pre-block snapshot — modelling
        SQLite discarding the uncommitted writes when COMMIT fails the FK check —
        then re-raises, leaving ``committed`` False and ``rolled_back`` True.
        """
        rom_ids = set(self.roms._snapshot())
        for repo_name in self._PER_ROM_FK_CHILD_REPOS:
            repo = getattr(self, repo_name)
            orphans = sorted(set(repo._snapshot()) - rom_ids)
            if orphans:
                raise sqlite3.IntegrityError(
                    f"FOREIGN KEY constraint failed: {repo_name} rom_id(s) {orphans} not in roms"
                )


class FakeUnitOfWorkFactory:
    """Call-shaped factory returning one shared :class:`FakeUnitOfWork`.

    Returning the same unit each call lets a test inspect repository state and
    the commit/rollback flags after the service-under-test has closed it.
    """

    def __init__(self, uow: FakeUnitOfWork | None = None) -> None:
        self.uow = uow if uow is not None else FakeUnitOfWork()
        self.call_count = 0

    def __call__(self) -> FakeUnitOfWork:
        self.call_count += 1
        return self.uow
