"""In-memory ``UnitOfWork`` composing the nine fake repositories, for service tests.

Mirrors the real UoW's context-manager shape — a clean ``__exit__`` commits and
flips ``committed``; an exceptional one truly rolls back (discards every write
made inside the block) before flipping ``rolled_back`` and re-raising. Rollback
is snapshot/restore: ``__enter__`` snapshots each repo's store, an exceptional
``__exit__`` restores them. This matches the real ``SqliteUnitOfWork``, which
discards uncommitted writes via SQL ``ROLLBACK``.

The repositories persist across clean ``with`` blocks (the fake's storage is the
fake's identity), so a service test can open the unit several times and see
prior committed writes. ``FakeUnitOfWorkFactory`` returns the same unit each call
so a test can inspect state after the service closes it.
"""

from __future__ import annotations

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
            self.committed = True
            return
        if snapshot is not None:
            self.roms._restore(snapshot.roms)
            self.rom_installs._restore(snapshot.rom_installs)
            self.rom_metadata._restore(snapshot.rom_metadata)
            self.playtime._restore(snapshot.playtime)
            self.rom_save_states._restore(snapshot.rom_save_states)
            self.bios_files._restore(snapshot.bios_files)
            self.firmware_cache._restore(snapshot.firmware_cache)
            self.sync_runs._restore(snapshot.sync_runs)
            self.kv_config._restore(snapshot.kv_config)
        self.rolled_back = True


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
