"""Tests for the typed save-sync state aggregate (``domain.save_state``)."""

from __future__ import annotations

from domain.save_state import (
    FileSyncState,
    PlaytimeEntry,
    RomSaveState,
    SaveSyncSettings,
    SaveSyncState,
)

# ---------------------------------------------------------------------------
# FileSyncState
# ---------------------------------------------------------------------------


class TestFileSyncState:
    def test_defaults(self) -> None:
        fs = FileSyncState()
        assert fs.tracked_save_id is None
        assert fs.last_sync_hash is None
        assert fs.last_sync_at == ""
        assert fs.last_sync_server_updated_at == ""
        assert fs.last_sync_server_save_id is None
        assert fs.last_sync_server_size is None
        assert fs.last_sync_local_mtime is None
        assert fs.last_sync_local_size is None

    def test_from_dict_round_trip(self) -> None:
        payload = {
            "tracked_save_id": 7,
            "last_sync_hash": "abc",
            "last_sync_at": "2026-05-01T00:00:00",
            "last_sync_server_updated_at": "2026-05-01T00:00:01",
            "last_sync_server_save_id": 7,
            "last_sync_server_size": 1024,
            "last_sync_local_mtime": 12345.6,
            "last_sync_local_size": 1024,
        }
        fs = FileSyncState.from_dict(payload)
        assert fs.to_dict() == payload

    def test_from_dict_drops_legacy_dismissed_newer_save_id(self) -> None:
        payload = {
            "tracked_save_id": 1,
            "dismissed_newer_save_id": 99,
        }
        fs = FileSyncState.from_dict(payload)
        out = fs.to_dict()
        assert "dismissed_newer_save_id" not in out
        assert out["tracked_save_id"] == 1

    def test_from_dict_handles_non_dict_input(self) -> None:
        fs = FileSyncState.from_dict("garbage")  # type: ignore[arg-type]
        assert fs == FileSyncState()

    def test_mutation(self) -> None:
        fs = FileSyncState()
        fs.tracked_save_id = 42
        fs.last_sync_hash = "deadbeef"
        assert fs.tracked_save_id == 42
        assert fs.last_sync_hash == "deadbeef"


# ---------------------------------------------------------------------------
# RomSaveState
# ---------------------------------------------------------------------------


class TestRomSaveState:
    def test_defaults(self) -> None:
        rs = RomSaveState()
        assert rs.active_slot is None
        assert rs.slot_confirmed is False
        assert rs.emulator == "retroarch"
        assert rs.system == ""
        assert rs.last_synced_core is None
        # None distinguishes "uploader attribution unknown" (legacy / fresh)
        # from "we uploaded nothing" (explicitly empty list).
        assert rs.own_upload_ids is None
        assert rs.slots == {}
        assert rs.files == {}
        assert rs.last_sync_check_at is None
        assert rs.extra == {}

    def test_from_dict_round_trip(self) -> None:
        payload = {
            "active_slot": "default",
            "slot_confirmed": True,
            "emulator": "retroarch",
            "system": "gba",
            "last_synced_core": "mgba_libretro",
            "own_upload_ids": [1, 2, 3],
            "slots": {"default": {"source": "server", "count": 2}},
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 5,
                    "last_sync_hash": "h",
                    "last_sync_at": "t",
                    "last_sync_server_updated_at": "u",
                    "last_sync_server_save_id": 5,
                    "last_sync_server_size": 1024,
                    "last_sync_local_mtime": 100.0,
                    "last_sync_local_size": 1024,
                },
            },
        }
        rs = RomSaveState.from_dict(payload)
        out = rs.to_dict()
        assert out["files"]["pokemon.srm"]["tracked_save_id"] == 5
        for key in (
            "active_slot",
            "slot_confirmed",
            "emulator",
            "system",
            "last_synced_core",
            "own_upload_ids",
            "slots",
        ):
            assert out[key] == payload[key]

    def test_from_dict_migrates_active_core_to_last_synced_core(self) -> None:
        rs = RomSaveState.from_dict({"active_core": "snes9x_libretro"})
        assert rs.last_synced_core == "snes9x_libretro"
        out = rs.to_dict()
        assert "active_core" not in out
        assert out["last_synced_core"] == "snes9x_libretro"

    def test_last_synced_core_wins_over_active_core(self) -> None:
        rs = RomSaveState.from_dict(
            {"last_synced_core": "winner", "active_core": "loser"},
        )
        assert rs.last_synced_core == "winner"

    def test_unknown_keys_preserved_in_extra(self) -> None:
        rs = RomSaveState.from_dict({"future_field": "keep-me"})
        assert rs.extra == {"future_field": "keep-me"}
        out = rs.to_dict()
        assert out["future_field"] == "keep-me"

    def test_last_sync_check_at_round_trip(self) -> None:
        rs = RomSaveState.from_dict({"last_sync_check_at": "2026-05-14T00:00:00"})
        assert rs.last_sync_check_at == "2026-05-14T00:00:00"
        assert rs.to_dict()["last_sync_check_at"] == "2026-05-14T00:00:00"

    def test_last_sync_check_at_omitted_when_none(self) -> None:
        rs = RomSaveState()
        assert "last_sync_check_at" not in rs.to_dict()

    def test_malformed_files_defaults_to_empty(self) -> None:
        rs = RomSaveState.from_dict({"files": "not-a-dict"})
        assert rs.files == {}

    def test_malformed_slots_defaults_to_empty(self) -> None:
        rs = RomSaveState.from_dict({"slots": ["not", "a", "dict"]})
        assert rs.slots == {}

    def test_malformed_own_upload_ids_defaults_to_none(self) -> None:
        rs = RomSaveState.from_dict({"own_upload_ids": "not-a-list"})
        assert rs.own_upload_ids is None

    def test_from_dict_drops_legacy_dismissed_newer_save_id_in_files(self) -> None:
        rs = RomSaveState.from_dict(
            {"files": {"f.srm": {"tracked_save_id": 1, "dismissed_newer_save_id": 99}}},
        )
        assert "dismissed_newer_save_id" not in rs.to_dict()["files"]["f.srm"]


# ---------------------------------------------------------------------------
# PlaytimeEntry
# ---------------------------------------------------------------------------


class TestPlaytimeEntry:
    def test_defaults(self) -> None:
        pe = PlaytimeEntry()
        assert pe.total_seconds == 0
        assert pe.session_count == 0
        assert pe.last_session_start is None
        assert pe.last_session_duration_sec is None
        assert pe.offline_deltas == []
        assert pe.note_id is None
        assert pe.extra == {}

    def test_from_dict_round_trip(self) -> None:
        payload = {
            "total_seconds": 3600,
            "session_count": 3,
            "last_session_start": "2026-05-14T00:00:00",
            "last_session_duration_sec": 1200,
            "offline_deltas": [{"delta": 100}],
            "note_id": 42,
        }
        pe = PlaytimeEntry.from_dict(payload)
        out = pe.to_dict()
        assert out == payload

    def test_note_id_omitted_when_none(self) -> None:
        pe = PlaytimeEntry(total_seconds=10)
        assert "note_id" not in pe.to_dict()

    def test_unknown_keys_preserved_in_extra(self) -> None:
        pe = PlaytimeEntry.from_dict({"future": "value"})
        assert pe.extra == {"future": "value"}
        assert pe.to_dict()["future"] == "value"


# ---------------------------------------------------------------------------
# SaveSyncSettings
# ---------------------------------------------------------------------------


class TestSaveSyncSettings:
    def test_defaults(self) -> None:
        s = SaveSyncSettings()
        assert s.save_sync_enabled is False
        assert s.sync_before_launch is True
        assert s.sync_after_exit is True
        assert s.default_slot == "default"
        assert s.autocleanup_limit == 10

    def test_from_dict_round_trip(self) -> None:
        payload = {
            "save_sync_enabled": True,
            "sync_before_launch": False,
            "sync_after_exit": False,
            "default_slot": "main",
            "autocleanup_limit": 5,
        }
        s = SaveSyncSettings.from_dict(payload)
        assert s.to_dict() == payload

    def test_drops_legacy_conflict_mode(self) -> None:
        s = SaveSyncSettings.from_dict({"conflict_mode": "ask"})
        out = s.to_dict()
        assert "conflict_mode" not in out

    def test_drops_legacy_clock_skew(self) -> None:
        s = SaveSyncSettings.from_dict({"clock_skew_tolerance_sec": 60})
        out = s.to_dict()
        assert "clock_skew_tolerance_sec" not in out

    def test_unknown_keys_preserved_in_extra(self) -> None:
        s = SaveSyncSettings.from_dict({"future_toggle": True})
        assert s.extra == {"future_toggle": True}
        assert s.to_dict()["future_toggle"] is True


# ---------------------------------------------------------------------------
# SaveSyncState
# ---------------------------------------------------------------------------


def _default_state_dict() -> dict:
    return {
        "version": 1,
        "device_id": None,
        "device_name": None,
        "server_device_id": None,
        "saves": {},
        "playtime": {},
        "settings": {
            "save_sync_enabled": False,
            "sync_before_launch": True,
            "sync_after_exit": True,
            "default_slot": "default",
            "autocleanup_limit": 10,
        },
    }


class TestSaveSyncState:
    def test_defaults_match_legacy_default_dict(self) -> None:
        s = SaveSyncState()
        assert s.to_dict() == _default_state_dict()

    def test_round_trip_default(self) -> None:
        s = SaveSyncState.from_dict(_default_state_dict())
        assert s.to_dict() == _default_state_dict()

    def test_round_trip_populated(self) -> None:
        payload = {
            "version": 1,
            "device_id": "abc",
            "device_name": "deck",
            "server_device_id": "server-1",
            "saves": {
                "42": {
                    "active_slot": "default",
                    "slot_confirmed": True,
                    "emulator": "retroarch",
                    "system": "gba",
                    "last_synced_core": "mgba_libretro",
                    "own_upload_ids": [1, 2],
                    "slots": {"default": {"source": "server", "count": 1}},
                    "files": {
                        "pokemon.srm": {
                            "tracked_save_id": 1,
                            "last_sync_hash": "abc",
                            "last_sync_at": "2026-05-14T00:00:00",
                            "last_sync_server_updated_at": "2026-05-14T00:00:01",
                            "last_sync_server_save_id": 1,
                            "last_sync_server_size": 1024,
                            "last_sync_local_mtime": 100.0,
                            "last_sync_local_size": 1024,
                        },
                    },
                    "last_sync_check_at": "2026-05-14T00:00:02",
                },
            },
            "playtime": {
                "42": {
                    "total_seconds": 1800,
                    "session_count": 2,
                    "last_session_start": None,
                    "last_session_duration_sec": 900,
                    "offline_deltas": [],
                    "note_id": 7,
                },
            },
            "settings": {
                "save_sync_enabled": True,
                "sync_before_launch": True,
                "sync_after_exit": True,
                "default_slot": "default",
                "autocleanup_limit": 10,
            },
        }
        s = SaveSyncState.from_dict(payload)
        assert s.to_dict() == payload

    def test_round_trip_stability_via_from_to_from(self) -> None:
        # The contract: from_dict(s.to_dict()) == s for any valid s.
        payload = {
            "version": 1,
            "device_id": "d",
            "device_name": None,
            "server_device_id": None,
            "saves": {"7": {"emulator": "retroarch", "system": "snes"}},
            "playtime": {"7": {"total_seconds": 100}},
            "settings": {"save_sync_enabled": True},
        }
        s = SaveSyncState.from_dict(payload)
        s2 = SaveSyncState.from_dict(s.to_dict())
        assert s == s2

    def test_active_core_migration(self) -> None:
        s = SaveSyncState.from_dict(
            {"saves": {"1": {"active_core": "core_libretro"}}},
        )
        assert s.saves["1"].last_synced_core == "core_libretro"
        assert "active_core" not in s.to_dict()["saves"]["1"]

    def test_legacy_settings_keys_stripped(self) -> None:
        s = SaveSyncState.from_dict(
            {"settings": {"conflict_mode": "ask", "clock_skew_tolerance_sec": 60, "save_sync_enabled": True}},
        )
        out = s.to_dict()["settings"]
        assert "conflict_mode" not in out
        assert "clock_skew_tolerance_sec" not in out
        assert out["save_sync_enabled"] is True

    def test_dismissed_newer_save_id_stripped(self) -> None:
        s = SaveSyncState.from_dict(
            {
                "saves": {
                    "1": {
                        "files": {
                            "f.srm": {
                                "tracked_save_id": 1,
                                "dismissed_newer_save_id": 999,
                            },
                        },
                    },
                },
            },
        )
        out = s.to_dict()
        assert "dismissed_newer_save_id" not in out["saves"]["1"]["files"]["f.srm"]

    def test_from_dict_non_dict_input_yields_defaults(self) -> None:
        assert SaveSyncState.from_dict("garbage") == SaveSyncState()  # type: ignore[arg-type]

    def test_malformed_saves_defaults_to_empty(self) -> None:
        s = SaveSyncState.from_dict({"saves": "not-a-dict"})
        assert s.saves == {}

    def test_malformed_playtime_defaults_to_empty(self) -> None:
        s = SaveSyncState.from_dict({"playtime": []})
        assert s.playtime == {}

    def test_replace_with_mutates_in_place(self) -> None:
        s = SaveSyncState()
        ref = s
        new_state = SaveSyncState(device_id="x", saves={"1": RomSaveState(system="gba")})
        s.replace_with(new_state)
        # The original reference now reflects the new state.
        assert ref is s
        assert s.device_id == "x"
        assert "1" in s.saves
        assert s.saves["1"].system == "gba"

    def test_equality(self) -> None:
        a = SaveSyncState(device_id="abc")
        b = SaveSyncState(device_id="abc")
        assert a == b
        b.device_id = "xyz"
        assert a != b
