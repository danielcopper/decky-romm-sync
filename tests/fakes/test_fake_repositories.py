"""Smoke tests for the in-memory repository fakes — Protocol satisfaction + behaviour.

Confirms each ``FakeXxxRepository`` structurally satisfies its Protocol (so
#784's service tests can wire them) and exercises every method so the fakes
carry coverage rather than riding on the adapters'.
"""

from __future__ import annotations

from domain.bios_file import BiosFile
from domain.firmware_cache import FirmwareCacheEntry
from domain.playtime import Playtime
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_metadata import RomMetadata
from domain.rom_save_state import FileSyncState, RomSaveState
from domain.sync_run import SyncRun
from fakes.fake_bios_file_repository import FakeBiosFileRepository
from fakes.fake_firmware_cache_repository import FakeFirmwareCacheRepository
from fakes.fake_kv_config_repository import FakeKvConfigRepository
from fakes.fake_playtime_repository import FakePlaytimeRepository
from fakes.fake_rom_install_repository import FakeRomInstallRepository
from fakes.fake_rom_metadata_repository import FakeRomMetadataRepository
from fakes.fake_rom_repository import FakeRomRepository
from fakes.fake_rom_save_state_repository import FakeRomSaveStateRepository
from fakes.fake_sync_run_repository import FakeSyncRunRepository
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from services.protocols import (
    BiosFileRepository,
    FirmwareCacheRepository,
    KvConfigRepository,
    PlaytimeRepository,
    RomInstallRepository,
    RomMetadataRepository,
    RomRepository,
    RomSaveStateRepository,
    SyncRunRepository,
    UnitOfWork,
    UnitOfWorkFactory,
)


def _rom(rom_id: int) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug="snes",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.sfc",
        shortcut_app_id=1000 + rom_id,
        last_synced_at="2026-01-01T00:00:00Z",
    )


class TestProtocolSatisfaction:
    """basedpyright checks the typed assignments; the asserts keep them live at runtime."""

    def test_fakes_satisfy_their_protocols(self):
        roms: RomRepository = FakeRomRepository()
        installs: RomInstallRepository = FakeRomInstallRepository()
        metadata: RomMetadataRepository = FakeRomMetadataRepository()
        playtime: PlaytimeRepository = FakePlaytimeRepository()
        save_states: RomSaveStateRepository = FakeRomSaveStateRepository()
        bios: BiosFileRepository = FakeBiosFileRepository()
        firmware: FirmwareCacheRepository = FakeFirmwareCacheRepository()
        runs: SyncRunRepository = FakeSyncRunRepository()
        kv: KvConfigRepository = FakeKvConfigRepository()
        assert all(
            obj is not None for obj in (roms, installs, metadata, playtime, save_states, bios, firmware, runs, kv)
        )

    def test_fake_uow_and_factory_satisfy_protocols(self):
        uow: UnitOfWork = FakeUnitOfWork()
        factory: UnitOfWorkFactory = FakeUnitOfWorkFactory()
        assert uow is not None
        assert factory is not None


class TestFakeRomRepository:
    def test_round_trip_app_id_lookup_iter_count_delete(self):
        repo = FakeRomRepository()
        repo.save(_rom(1))
        repo.save(_rom(2))
        assert repo.get(1) is not None
        assert repo.get(99) is None
        assert repo.get_by_app_id(1002) is not None
        assert repo.get_by_app_id(9999) is None
        assert {r.rom_id for r in repo.iter_all()} == {1, 2}
        assert {r.rom_id for r in repo.iter_by_platform("snes")} == {1, 2}
        assert list(repo.iter_by_platform("gba")) == []
        assert repo.count() == 2
        assert repo.save_count == 2
        repo.delete(1)
        assert repo.get(1) is None

    def test_deepcopy_isolates_stored_aggregate(self):
        repo = FakeRomRepository()
        rom = _rom(1)
        repo.save(rom)
        rom.update_cover_path("/mutated.png")
        loaded = repo.get(1)
        assert loaded is not None
        assert loaded.cover_path is None

    def test_get_returns_copy_so_caller_mutations_dont_leak(self):
        repo = FakeRomRepository()
        repo.save(_rom(1))
        first = repo.get(1)
        assert first is not None
        first.update_cover_path("/leaked.png")  # mutate the returned object, no save()
        second = repo.get(1)
        assert second is not None
        assert second.cover_path is None  # stored copy untouched

    def test_iter_all_returns_copies_so_caller_mutations_dont_leak(self):
        repo = FakeRomRepository()
        repo.save(_rom(1))
        for rom in repo.iter_all():
            rom.update_cover_path("/leaked.png")  # mutate yielded object, no save()
        reloaded = repo.get(1)
        assert reloaded is not None
        assert reloaded.cover_path is None


class TestFakeRomInstallRepository:
    def test_round_trip_iter_delete(self):
        repo = FakeRomInstallRepository()
        install = RomInstall(
            rom_id=1,
            file_path="/x",
            install_path="/i",
            platform_slug="snes",
            system="snes",
            installed_at="2026-01-01T00:00:00Z",
        )
        repo.save(install)
        assert repo.get(1) == install
        assert repo.get(2) is None
        assert [i.rom_id for i in repo.iter_all()] == [1]
        repo.delete(1)
        assert repo.get(1) is None


class TestFakeRomMetadataRepository:
    def test_round_trip_delete(self):
        repo = FakeRomMetadataRepository()
        meta = RomMetadata(
            summary="s",
            genres=(),
            companies=(),
            first_release_date=None,
            average_rating=None,
            game_modes=(),
            player_count="1",
            cached_at=1.0,
        )
        repo.save(1, meta)
        assert repo.get(1) == meta
        assert repo.get(2) is None
        repo.delete(1)
        assert repo.get(1) is None


class TestFakePlaytimeRepository:
    def test_round_trip_iter_delete(self):
        repo = FakePlaytimeRepository()
        repo.save(1, Playtime(total_seconds=10))
        assert repo.get(1) is not None
        assert repo.get(2) is None
        assert dict(repo.iter_all())[1].total_seconds == 10
        repo.delete(1)
        assert repo.get(1) is None


class TestFakeRomSaveStateRepository:
    def test_round_trip_iter_delete(self):
        repo = FakeRomSaveStateRepository()
        state = RomSaveState(files={"a.srm": FileSyncState(tracked_save_id=1, last_sync_hash="h")})
        repo.save(1, state)
        assert repo.get(1) == state
        assert repo.get(2) is None
        assert set(dict(repo.iter_all())) == {1}
        repo.delete(1)
        assert repo.get(1) is None

    def test_get_returns_deep_copy_so_nested_list_mutations_dont_leak(self):
        repo = FakeRomSaveStateRepository()
        repo.save(1, RomSaveState(own_upload_ids=[7]))
        loaded = repo.get(1)
        assert loaded is not None
        loaded.track_own_upload(99)  # mutate the nested list, no save()
        reloaded = repo.get(1)
        assert reloaded is not None
        assert reloaded.own_upload_ids == [7]  # stored copy's list untouched


class TestFakeBiosFileRepository:
    def test_round_trip_composite_key_iter_delete(self):
        repo = FakeBiosFileRepository()
        bios = BiosFile(
            platform_slug="psx",
            file_name="b.bin",
            file_path="/b",
            downloaded_at="2026-01-01T00:00:00Z",
        )
        repo.save(bios)
        assert repo.get("psx", "b.bin") == bios
        assert repo.get("psx", "missing.bin") is None
        assert [b.file_name for b in repo.iter_all()] == ["b.bin"]
        assert [b.file_name for b in repo.iter_by_platform("psx")] == ["b.bin"]
        assert list(repo.iter_by_platform("saturn")) == []
        repo.delete("psx", "b.bin")
        assert repo.get("psx", "b.bin") is None


class TestFakeFirmwareCacheRepository:
    def test_replace_all_clear_epoch(self):
        repo = FakeFirmwareCacheRepository()
        assert repo.get_cache_epoch() is None
        entry = FirmwareCacheEntry(id=1, name="x.bin", platform_slug="psx", file_size_bytes=10, cached_at=5.0)
        repo.replace_all([entry])
        assert repo.get("psx", "x.bin") == entry
        assert repo.get("psx", "missing") is None
        assert repo.get_cache_epoch() == 5.0
        assert repo.replace_count == 1
        repo.clear()
        assert list(repo.iter_all()) == []
        assert repo.get_cache_epoch() is None


class TestFakeSyncRunRepository:
    def test_latest_completed_and_running(self):
        repo = FakeSyncRunRepository()
        assert repo.get_latest_completed() is None
        assert repo.get_running() is None
        running = SyncRun.start(id="r1", at="2026-01-01T00:00:00Z", platforms_planned=1, roms_planned=1)
        repo.save(running)
        older = SyncRun.start(id="c1", at="2026-01-01T00:00:00Z", platforms_planned=1, roms_planned=1)
        older.complete(at="2026-01-01T01:00:00Z", platforms=[], collections=[])
        newer = SyncRun.start(id="c2", at="2026-02-01T00:00:00Z", platforms_planned=1, roms_planned=1)
        newer.complete(at="2026-02-01T01:00:00Z", platforms=[], collections=[])
        repo.save(older)
        repo.save(newer)
        assert repo.get("c2") is not None
        latest = repo.get_latest_completed()
        assert latest is not None
        assert latest.id == "c2"
        run = repo.get_running()
        assert run is not None
        assert run.id == "r1"


class TestFakeKvConfigRepository:
    def test_set_get_delete(self):
        repo = FakeKvConfigRepository()
        assert repo.get("k") is None
        repo.set("k", "v")
        assert repo.get("k") == "v"
        assert repo.set_count == 1
        repo.delete("k")
        assert repo.get("k") is None


class TestFakeUnitOfWork:
    def test_clean_exit_sets_committed(self):
        uow = FakeUnitOfWork()
        with uow:
            uow.roms.save(_rom(1))
        assert uow.committed is True
        assert uow.rolled_back is False
        assert uow.enter_count == 1
        assert uow.roms.get(1) is not None

    def test_exception_sets_rolled_back_and_re_raises(self):
        uow = FakeUnitOfWork()

        class Boom(Exception):
            pass

        try:
            with uow:
                raise Boom
        except Boom:
            pass
        assert uow.rolled_back is True
        assert uow.committed is False

    def test_exception_rolls_back_writes_made_inside_block(self):
        uow = FakeUnitOfWork()

        class Boom(Exception):
            pass

        try:
            with uow:
                uow.roms.save(_rom(1))
                uow.kv_config.set("k", "v")
                raise Boom
        except Boom:
            pass
        assert uow.rolled_back is True
        assert uow.roms.get(1) is None  # write discarded
        assert uow.kv_config.get("k") is None  # write discarded

    def test_rollback_preserves_committed_writes_from_earlier_block(self):
        uow = FakeUnitOfWork()

        class Boom(Exception):
            pass

        with uow:
            uow.roms.save(_rom(1))  # committed
        assert uow.roms.get(1) is not None

        try:
            with uow:
                uow.roms.save(_rom(2))  # rolled back
                raise Boom
        except Boom:
            pass
        assert uow.roms.get(1) is not None  # earlier commit survives
        assert uow.roms.get(2) is None  # later write discarded

    def test_factory_returns_shared_unit(self):
        unit = FakeUnitOfWork()
        factory = FakeUnitOfWorkFactory(unit)
        assert factory() is unit
        assert factory() is unit
        assert factory.call_count == 2

    def test_factory_builds_default_unit_when_none_given(self):
        factory = FakeUnitOfWorkFactory()
        assert isinstance(factory(), FakeUnitOfWork)
