"""Tests for ``SqliteRomSaveStateRepository`` — the two-table save-state aggregate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.rom import Rom
from domain.rom_save_state import FileSyncState, RomSaveState

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _seed_rom(uow: SqliteUnitOfWork, rom_id: int) -> None:
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug="snes",
            name=f"Game {rom_id}",
            fs_name=f"game_{rom_id}.sfc",
            shortcut_app_id=1000 + rom_id,
            last_synced_at="2026-01-01T00:00:00Z",
        )
    )


class TestRoundTrip:
    def test_scalars_and_multiple_file_children_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        state = RomSaveState(
            active_slot="slot1",
            slot_confirmed=True,
            emulator="retroarch",
            system="snes",
            last_synced_core="snes9x",
            own_upload_ids=[10, 20],
            slots={"slot1": {"source": "local", "count": 2, "latest_updated_at": None}},
            files={
                "save.srm": FileSyncState(
                    tracked_save_id=100,
                    last_sync_hash="hashA",
                    last_sync_at="2026-04-04T00:00:00Z",
                    last_sync_server_updated_at="2026-04-04T01:00:00Z",
                    last_sync_server_save_id=100,
                    last_sync_server_size=2048,
                    last_sync_local_mtime=1700000000.5,
                    last_sync_local_size=2048,
                ),
                "save.state": FileSyncState(
                    tracked_save_id=101,
                    last_sync_hash="hashB",
                ),
            },
            last_sync_check_at="2026-04-04T02:00:00Z",
        )
        uow.rom_save_states.save(5, state)

        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded == state
        assert set(loaded.files) == {"save.srm", "save.state"}

    def test_slot_confirmed_bool_round_trips(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_save_states.save(5, RomSaveState(slot_confirmed=False))
        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded.slot_confirmed is False

        uow.rom_save_states.save(5, RomSaveState(slot_confirmed=True))
        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded.slot_confirmed is True

    def test_never_synced_sentinel_is_empty_string_not_null(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        state = RomSaveState(
            files={"s.srm": FileSyncState(tracked_save_id=1, last_sync_hash="h")},
        )
        uow.rom_save_states.save(5, state)

        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        file = loaded.files["s.srm"]
        assert file.last_sync_at == ""
        assert file.last_sync_server_updated_at == ""


class TestOwnUploadIdsNullVsEmpty:
    """NULL ("attribution unknown") and '[]' ("uploaded nothing") are DISTINCT."""

    def test_none_round_trips_as_none(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_save_states.save(5, RomSaveState(own_upload_ids=None))
        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded.own_upload_ids is None

    def test_empty_list_round_trips_as_empty_list_not_none(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_save_states.save(5, RomSaveState(own_upload_ids=[]))
        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded.own_upload_ids == []
        assert loaded.own_upload_ids is not None

    def test_populated_list_round_trips(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_save_states.save(5, RomSaveState(own_upload_ids=[7, 8, 9]))
        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded.own_upload_ids == [7, 8, 9]


class TestFilesReplacedOnReSave:
    def test_re_save_replaces_child_file_rows(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        first = RomSaveState(
            files={
                "old1.srm": FileSyncState(tracked_save_id=1, last_sync_hash="h1"),
                "old2.srm": FileSyncState(tracked_save_id=2, last_sync_hash="h2"),
            },
        )
        uow.rom_save_states.save(5, first)

        second = RomSaveState(
            files={"new.srm": FileSyncState(tracked_save_id=3, last_sync_hash="h3")},
        )
        uow.rom_save_states.save(5, second)

        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert set(loaded.files) == {"new.srm"}

    def test_re_save_with_no_files_clears_children(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_save_states.save(
            5,
            RomSaveState(files={"a.srm": FileSyncState(tracked_save_id=1, last_sync_hash="h")}),
        )
        uow.rom_save_states.save(5, RomSaveState())

        loaded = uow.rom_save_states.get(5)
        assert loaded is not None
        assert loaded.files == {}


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.rom_save_states.get(999) is None


class TestDelete:
    def test_delete_removes_state_and_files(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.rom_save_states.save(
            5,
            RomSaveState(files={"a.srm": FileSyncState(tracked_save_id=1, last_sync_hash="h")}),
        )
        uow.rom_save_states.delete(5)

        assert uow.rom_save_states.get(5) is None
        # The child rows are gone too, so a fresh save under the same id starts clean.
        uow.rom_save_states.save(5, RomSaveState())
        reloaded = uow.rom_save_states.get(5)
        assert reloaded is not None
        assert reloaded.files == {}

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.rom_save_states.delete(404)
        assert uow.rom_save_states.get(404) is None


class TestIteration:
    def test_iter_all_yields_rom_id_state_pairs(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.rom_save_states.save(1, RomSaveState(active_slot="a"))
        uow.rom_save_states.save(
            2,
            RomSaveState(files={"x.srm": FileSyncState(tracked_save_id=9, last_sync_hash="h")}),
        )

        by_id = dict(uow.rom_save_states.iter_all())
        assert set(by_id) == {1, 2}
        assert by_id[1].active_slot == "a"
        assert set(by_id[2].files) == {"x.srm"}
