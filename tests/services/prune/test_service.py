from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock, FakeUuidGen

from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.version_metadata import VersionMetadata
from lib.errors import RommConnectionError, RommNotFoundError
from services.prune import PruneService, PruneServiceConfig

if TYPE_CHECKING:
    from models.prune import RecoveryArtifact, SteamRecoverySnapshot


class FakeRomm:
    def __init__(self) -> None:
        self.outcomes: dict[int, list[object]] = {}
        self.calls: list[int] = []

    def get_rom_once(self, rom_id: int) -> dict[str, Any]:
        self.calls.append(rom_id)
        values = self.outcomes.setdefault(rom_id, [{"id": rom_id}])
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


class FakeRecoveryStore:
    def __init__(self) -> None:
        self.sealed: list[dict[str, object]] = []
        self.failure: Exception | None = None

    def root(self) -> str:
        return "/recovery"

    def free_bytes(self) -> int:
        return 10_000

    def measure_path(self, path: str, safe_root: str) -> int:
        del path, safe_root
        return 123

    def seal_bundle(self, bundle_id, snapshot, artifacts, readme, playtime_text):
        if self.failure is not None:
            raise self.failure
        self.sealed.append(
            {
                "bundle_id": bundle_id,
                "snapshot": snapshot,
                "artifacts": artifacts,
                "readme": readme,
                "playtime": playtime_text,
            }
        )
        return f"/recovery/bundles/{bundle_id}"


class FakePruneArtifacts:
    def __init__(self) -> None:
        self.removed: list[list[int]] = []

    def recovery_artifacts(self, rom_ids: list[int]) -> list[RecoveryArtifact]:
        del rom_ids
        return []

    def remove(self, rom_ids: list[int]) -> None:
        self.removed.append(rom_ids)


class FakeSteamRecovery:
    def __init__(self) -> None:
        self.removed: list[int] = []

    def snapshot(self, app_id: int) -> SteamRecoverySnapshot:
        del app_id
        return {"controller_setting": "2", "artifacts": []}

    def remove_files(self, app_id: int) -> None:
        self.removed.append(app_id)


class FakeSteamConfig:
    def __init__(self) -> None:
        self.reset: list[tuple[list[int], str]] = []

    def set_steam_input_config(self, app_ids: list[int], mode: str = "default") -> None:
        self.reset.append((app_ids, mode))


class FakeSaveCoordinator:
    def __init__(self) -> None:
        self.locked: list[list[int]] = []
        self.quarantined: list[list[dict[str, str]]] = []

    @contextlib.asynccontextmanager
    async def lock_prune_roms(self, rom_ids: list[int]):
        self.locked.append(rom_ids)
        yield

    def inventory_prune_saves(self, purge_rom_ids: list[int]):
        return {
            "artifacts": [],
            "exclusive": [],
            "shared": [],
            "warnings": [],
            "lock_rom_ids": purge_rom_ids,
        }

    def quarantine_prune_saves(self, files: list[dict[str, str]]):
        self.quarantined.append(files)
        return {"success": True, "moved": []}


class EventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event: str, /, *args: object) -> None:
        assert len(args) == 1 and isinstance(args[0], dict)
        payload = cast("dict[str, Any]", args[0])
        self.events.append((event, payload))


@dataclass
class Harness:
    service: PruneService
    uow: FakeUnitOfWork
    romm: FakeRomm
    recovery: FakeRecoveryStore
    artifacts: FakePruneArtifacts
    steam_recovery: FakeSteamRecovery
    steam_config: FakeSteamConfig
    saves: FakeSaveCoordinator
    events: EventSink
    active: set[int]


def _rom(rom_id: int, *, fetch: str | None, group: str | None = None, app_id: int | None = None) -> Rom:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug="dc",
        name=f"Game {rom_id}",
        fs_name=f"Game {rom_id}.gdi",
        shortcut_app_id=app_id,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=group, regions=("USA",)),
    )
    if fetch is not None:
        rom.record_fetch_generation(fetch)
    return rom


def _seed(uow: FakeUnitOfWork, *rows: Rom, stamp_count: int = 1) -> None:
    with uow:
        for row in rows:
            uow.roms.save(row)
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=stamp_count, fetch_id="new")
        )


@pytest_asyncio.fixture
async def harness() -> Harness:
    loop = asyncio.get_running_loop()
    uow = FakeUnitOfWork()
    romm = FakeRomm()
    recovery = FakeRecoveryStore()
    artifacts = FakePruneArtifacts()
    steam_recovery = FakeSteamRecovery()
    steam_config = FakeSteamConfig()
    saves = FakeSaveCoordinator()
    events = EventSink()
    active: set[int] = set()

    async def drift(rom_id: int) -> dict[str, Any]:
        del rom_id
        return {"drifted": False}

    async def remove_installed(rom_id: int) -> dict[str, Any]:
        del rom_id
        return {"success": False, "reason": "not_installed", "message": "not installed"}

    service = PruneService(
        config=PruneServiceConfig(
            loop=loop,
            logger=logging.getLogger("test-prune"),
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(
                [
                    "00000000-0000-4000-8000-000000000001",
                    "00000000-0000-4000-8000-000000000002",
                    "00000000-0000-4000-8000-000000000003",
                    "00000000-0000-4000-8000-000000000004",
                    "00000000-0000-4000-8000-000000000005",
                ]
            ),
            emit=events,
            uow_factory=FakeUnitOfWorkFactory(uow),
            romm_api=cast("Any", romm),
            recovery_store=recovery,
            prune_artifacts=artifacts,
            steam_recovery=steam_recovery,
            steam_config=cast("Any", steam_config),
            retrodeck_paths=FakeRetroDeckPaths(saves="/saves", roms="/roms", bios="/bios", home="/retrodeck"),
            save_coordinator=saves,
            active_downloads=lambda: set(active),
            drift_probe=drift,
            remove_installed_rom=remove_installed,
            settings={"preferred_region": "USA"},
        )
    )
    return Harness(service, uow, romm, recovery, artifacts, steam_recovery, steam_config, saves, events, active)


async def _preview(harness: Harness, *, scope: str = "bulk", rom_id: int | None = None):
    return await harness.service.get_prune_preview(
        {"scope": scope, "rom_id": rom_id, "preview_id": None, "offset": 0, "limit": 50}
    )


async def _start(harness: Harness, preview_id: str, **overrides):
    request = {
        "preview_id": preview_id,
        "confirmed": True,
        "repoint_shortcuts": True,
        "remove_rows": True,
        "remove_fully_vanished": False,
        "create_recovery_bundle": False,
        "include_installed_rom_ids": [],
        **overrides,
    }
    return await harness.service.start_prune(request)


def _steam_snapshot(app_id: int) -> dict[str, object]:
    return {
        "app_id": app_id,
        "name": "Game",
        "exe": "/plugin/bin/rom-launcher",
        "start_dir": "/plugin",
        "launch_options": "launch",
        "minutes_playtime_forever": 10,
        "minutes_playtime_last_two_weeks": 2,
        "last_played": 123,
        "collections": [],
    }


async def _finish(harness: Harness) -> dict[str, Any]:
    task = harness.service._task
    assert task is not None
    await task
    return [payload for name, payload in harness.events.events if name == "prune_complete"][-1]


async def _wait_action(harness: Harness, action: str) -> dict[str, Any]:
    for _ in range(100):
        for name, payload in harness.events.events:
            if name == "prune_action_required" and payload["action"] == action:
                return payload
        await asyncio.sleep(0.001)
    raise AssertionError(f"action {action} was not emitted")


@pytest.mark.asyncio
async def test_preview_is_generation_gated_paged_and_tokenized(harness):
    _seed(harness.uow, _rom(1, fetch="old"), _rom(2, fetch="new"))
    preview = await _preview(harness)
    assert preview["success"] is True
    assert preview["total"] == 1
    assert preview["items"][0]["rom_id"] == 1
    assert preview["recovery_root"] == "/recovery"

    count_only = await harness.service.get_prune_preview(
        {"scope": "bulk", "rom_id": None, "preview_id": preview["preview_id"], "offset": 0, "limit": 0}
    )
    assert count_only["items"] == []
    assert count_only["total"] == 1
    stale = await harness.service.get_prune_preview(
        {"scope": "bulk", "rom_id": None, "preview_id": "wrong", "offset": 0, "limit": 50}
    )
    assert stale["reason"] == "stale_preview"


@pytest.mark.asyncio
async def test_inline_preview_bypasses_missing_generation_for_exact_row(harness):
    with harness.uow:
        harness.uow.roms.save(_rom(9, fetch=None))
    bulk = await _preview(harness)
    inline = await _preview(harness, scope="rom", rom_id=9)
    assert bulk["total"] == 0
    assert inline["total"] == 1
    assert inline["items"][0]["rom_id"] == 9


@pytest.mark.asyncio
async def test_stale_preview_refuses_start_after_local_change(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    preview = await _preview(harness)
    with harness.uow:
        harness.uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=1,
                file_path="/roms/dc/game.gdi",
                rom_dir=None,
                platform_slug="dc",
                system="dc",
                installed_at="now",
            )
        )
    result = await _start(harness, preview["preview_id"])
    assert result["reason"] == "stale_preview"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,reason",
    [
        ({"id": 1}, "options_excluded"),
        ({"id": 99}, "liveness_uncertain"),
        ({}, "liveness_uncertain"),
        (RommConnectionError("offline"), "liveness_uncertain"),
    ],
)
async def test_only_exact_404_can_authorize_removal(harness, outcome, reason):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [outcome]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])
    complete = await _finish(harness)
    assert harness.uow.roms.get(1) is not None
    assert complete["results"][0]["reason"] == reason


@pytest.mark.asyncio
async def test_unbound_confirmed_404_row_is_deleted_after_final_reprobes(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert harness.uow.roms.get(1) is None
    assert complete["removed_rom_ids"] == [1]
    assert harness.romm.calls == [1, 1, 1]
    assert harness.saves.locked == [[1], [1]]


@pytest.mark.asyncio
async def test_restore_race_after_initial_404_retains_everything(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone"), {"id": 1}]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert harness.uow.roms.get(1) is not None
    assert complete["results"][0]["reason"] == "live"


@pytest.mark.asyncio
async def test_active_download_and_multiple_bindings_skip_before_mutation(harness):
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=0x80000001),
        _rom(2, fetch="old", group="g", app_id=0x80000002),
    )
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert complete["results"][0]["reason"] == "multiple_bindings"
    assert harness.romm.calls == []

    harness.active.add(1)
    with harness.uow:
        row = harness.uow.roms.get(2)
        assert row is not None
        row.unbind_shortcut()
        harness.uow.roms.save(row)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert complete["results"][0]["reason"] == "download_in_progress"


@pytest.mark.asyncio
async def test_bound_live_group_repoints_then_deletes_old_row(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])
    action = await _wait_action(harness, "repoint_shortcut")
    assert action["target_rom_id"] == 2
    with harness.uow:
        target = harness.uow.roms.get(2)
        assert target is not None
        target.bind_shortcut(app_id)
        harness.uow.roms.save(target)
    accepted = await harness.service.report_prune_action(
        {
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "success": True,
            "message": "confirmed",
        }
    )
    assert accepted["success"] is True
    duplicate = await harness.service.report_prune_action(
        {
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "success": True,
            "message": "late duplicate",
        }
    )
    assert duplicate["ignored"] is True
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]
    assert harness.uow.roms.get(1) is None
    assert harness.uow.roms.get(2).shortcut_app_id == app_id


@pytest.mark.asyncio
async def test_fully_dead_shortcut_requires_confirmed_removal_action(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    stale = await harness.service.report_prune_action(
        {"run_id": "wrong", "action_token": action["action_token"], "success": True, "message": "wrong"}
    )
    assert stale["reason"] == "stale_action"
    await harness.service.report_prune_action(
        {
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "success": True,
            "message": "removed",
        }
    )
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]
    assert harness.steam_recovery.removed == [app_id]
    assert harness.steam_config.reset == [([app_id], "default")]


@pytest.mark.asyncio
async def test_binding_change_after_shortcut_action_blocks_source_removal(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    with harness.uow:
        harness.uow.roms.save(_rom(1, fetch="old", app_id=0x80000002))
    await harness.service.report_prune_action(
        {
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "success": True,
            "message": "removed",
        }
    )

    complete = await _finish(harness)

    assert complete["results"][0]["reason"] == "local_state_changed"
    assert complete["affected_app_ids"] == [app_id]
    assert harness.uow.roms.get(1) is not None
    assert harness.artifacts.removed == []
    assert harness.steam_recovery.removed == []


@pytest.mark.asyncio
async def test_recovery_failure_skips_group_and_success_records_bundle(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    harness.recovery.failure = OSError("disk full")
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    failed = await _finish(harness)
    assert failed["results"][0]["reason"] == "recovery_failed"
    assert harness.uow.roms.get(1) is not None

    harness.recovery.failure = None
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]
    assert harness.recovery.sealed[0]["snapshot"]["roms"][0]["rom_id"] == 1


@pytest.mark.asyncio
async def test_recovery_snapshot_rejects_wire_base64_before_accepting_bounded_json(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    capture = await _wait_action(harness, "capture_shortcut_snapshot")
    invalid = await harness.service.report_prune_action(
        {
            "run_id": capture["run_id"],
            "action_token": capture["action_token"],
            "success": True,
            "message": "bad",
            "snapshot": {"cover_base64": "AAAA"},
        }
    )
    assert invalid["reason"] == "invalid_snapshot"
    wrong_app = await harness.service.report_prune_action(
        {
            "run_id": capture["run_id"],
            "action_token": capture["action_token"],
            "success": True,
            "message": "wrong app",
            "snapshot": _steam_snapshot(app_id + 1),
        }
    )
    assert wrong_app["reason"] == "invalid_snapshot"
    await harness.service.report_prune_action(
        {
            "run_id": capture["run_id"],
            "action_token": capture["action_token"],
            "success": True,
            "message": "captured",
            "snapshot": _steam_snapshot(app_id),
        }
    )
    removal = await _wait_action(harness, "remove_shortcut")
    await harness.service.report_prune_action(
        {
            "run_id": removal["run_id"],
            "action_token": removal["action_token"],
            "success": True,
            "message": "removed",
        }
    )
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]
