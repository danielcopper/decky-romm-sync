from __future__ import annotations

from typing import Any, cast

from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock, FakeUuidGen

from domain.playtime import Playtime
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_metadata import RomMetadata
from domain.rom_save_sync_state import RomSaveSyncState
from services.prune.recovery import RecoveryCoordinator, RecoveryCoordinatorConfig


def _coordinator(uow: FakeUnitOfWork) -> RecoveryCoordinator:
    unused = cast("Any", object())
    return RecoveryCoordinator(
        config=RecoveryCoordinatorConfig(
            uow_factory=FakeUnitOfWorkFactory(uow),
            recovery_store=unused,
            prune_artifacts=unused,
            steam_recovery=unused,
            retrodeck_paths=unused,
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(),
        )
    )


def test_snapshot_is_lossless_for_every_per_rom_child_and_playtime_outbox() -> None:
    uow = FakeUnitOfWork()
    rom = Rom.synced(
        rom_id=7,
        platform_slug="gba",
        name="Removed Game",
        fs_name="Removed Game.gba",
        shortcut_app_id=9001,
        synced_at="2026-01-01T00:00:00Z",
    )
    install = RomInstall.mark_installed(
        rom_id=7,
        file_path="/roms/gba/Removed Game.gba",
        rom_dir=None,
        platform_slug="gba",
        system="gba",
        installed_at="2026-01-01T00:00:00Z",
    )
    metadata = RomMetadata.cached(
        summary="summary",
        genres=("Adventure",),
        companies=("Studio",),
        first_release_date=1,
        average_rating=90.0,
        game_modes=("Single player",),
        player_count="1",
        cached_at=123.0,
    )
    save_state = RomSaveSyncState(system="gba")
    save_state.adopt_baseline("Removed Game.srm", tracked_save_id=4, last_sync_hash="abc")
    playtime = Playtime(total_seconds=3600, session_count=2, last_session_duration_sec=600)
    playtime.begin_session("2026-01-02T10:00:00Z", monotonic=100.0)
    playtime.enqueue_session(
        device_id="device-1",
        start_time="2026-01-01T10:00:00Z",
        end_time="2026-01-01T11:00:00Z",
        duration_ms=3_600_000,
    )
    with uow:
        uow.roms.save(rom)
        uow.rom_installs.save(install)
        uow.rom_metadata.save(7, metadata)
        uow.rom_save_sync_states.save(7, save_state)
        uow.playtime.save(7, playtime)

    snapshot = cast(
        "dict[str, Any]",
        _coordinator(uow).snapshot_state(
            [7],
            {"app_id": 9001, "minutes_playtime_forever": 90},
        ),
    )

    assert snapshot["roms"][0]["rom_id"] == 7
    assert snapshot["installs"][0]["file_path"] == "/roms/gba/Removed Game.gba"
    assert snapshot["metadata"][0]["state"]["summary"] == "summary"
    assert snapshot["save_sync"][0]["state"]["files"]["Removed Game.srm"]["last_sync_hash"] == "abc"
    playtime_state = snapshot["playtime"][0]["state"]
    assert playtime_state["last_session_start"] == "2026-01-02T10:00:00Z"
    assert playtime_state["pending_sessions"]["2026-01-01T10:00:00Z"] == {
        "device_id": "device-1",
        "end_time": "2026-01-01T11:00:00Z",
        "duration_ms": 3_600_000,
        "attempts": 0,
    }
    assert snapshot["steam"] == {"app_id": 9001, "minutes_playtime_forever": 90}


def test_playtime_text_includes_open_and_pending_session_details() -> None:
    text = RecoveryCoordinator._playtime_text(
        {
            "playtime": [
                {
                    "rom_id": 7,
                    "state": {
                        "total_seconds": 3600,
                        "session_count": 2,
                        "last_played": "2026-01-01T11:00:00Z",
                        "last_session_duration_sec": 600,
                        "last_session_start": "2026-01-02T10:00:00Z",
                        "last_session_start_monotonic": 100.0,
                        "pending_sessions": {
                            "2026-01-01T10:00:00Z": {
                                "device_id": "device-1",
                                "end_time": "2026-01-01T11:00:00Z",
                                "duration_ms": 3_600_000,
                                "attempts": 1,
                            }
                        },
                    },
                }
            ],
            "steam": {
                "app_id": 9001,
                "minutes_playtime_forever": 90,
                "minutes_playtime_last_two_weeks": 15,
            },
        }
    )

    assert "open_session_start: 2026-01-02T10:00:00Z" in text
    assert "open_session_monotonic: 100.0" in text
    assert "start_time: 2026-01-01T10:00:00Z" in text
    assert "device_id: device-1" in text
    assert "duration_ms: 3600000" in text
    assert "attempts: 1" in text
    assert "Steam appId: 9001" in text
