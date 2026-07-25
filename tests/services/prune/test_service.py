from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
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
        self.sources_valid = True

    def root(self) -> str:
        return "/recovery"

    def free_bytes(self) -> int:
        return 10_000

    def measure_path(self, path: str, safe_root: str) -> int:
        del path, safe_root
        return 123

    def validate_sources(self, bundle_path: str) -> bool:
        del bundle_path
        return self.sources_valid

    def source_identities(self, bundle_path: str):
        del bundle_path
        return {}

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

    def remove(self, rom_ids: list[int], identities=None) -> int:
        del identities
        self.removed.append(rom_ids)
        return len(rom_ids)


class FakeSteamRecovery:
    def __init__(self) -> None:
        self.removed: list[int] = []

    def snapshot(self, app_id: int) -> SteamRecoverySnapshot:
        del app_id
        return {
            "user_id": "123",
            "user_dir": "/steam/userdata/123",
            "steam_root": "/steam",
            "controller_setting": "2",
            "artifacts": [],
        }

    def validate_state(self, app_id: int, snapshot: SteamRecoverySnapshot) -> bool:
        del app_id, snapshot
        return True

    def remove_state(self, app_id: int, snapshot: SteamRecoverySnapshot, identities=None) -> int:
        del identities
        assert snapshot["user_id"] == "123"
        self.removed.append(app_id)
        return 1


class FakeSaveCoordinator:
    def __init__(self) -> None:
        self.locked: list[list[int]] = []
        self.quarantined: list[list[dict[str, str]]] = []
        self.inventory_failure_ids: set[int] = set()
        self.lock_id_sequence: list[list[int]] = []
        self.warnings: list[str] = []

    @contextlib.asynccontextmanager
    async def lock_prune_roms(self, rom_ids: list[int]):
        self.locked.append(rom_ids)
        yield

    def inventory_prune_saves(self, purge_rom_ids: list[int]):
        if self.inventory_failure_ids.intersection(purge_rom_ids):
            raise OSError("save inventory failed")
        lock_ids = self.lock_id_sequence.pop(0) if self.lock_id_sequence else purge_rom_ids
        return {
            "artifacts": [],
            "exclusive": [],
            "shared": [],
            "warnings": list(self.warnings),
            "lock_rom_ids": lock_ids,
        }

    def quarantine_prune_saves(self, files: list[dict[str, str]], identities=None):
        del identities
        self.quarantined.append(files)
        return {"success": True, "moved": []}


class FakeInstalledFilesRemover:
    def __init__(self) -> None:
        self.installed_ids: set[int] = set()
        self.block_ids: set[int] = set()
        self.failure_ids: set[int] = set()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, rom_id: int, identities=None) -> dict[str, Any]:
        del identities
        if rom_id in self.block_ids:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release installed-file removal")
        if rom_id in self.installed_ids:
            return {"success": True, "changed": True, "message": "removed"}
        if rom_id in self.failure_ids:
            return {"success": False, "reason": "unknown", "message": "delete failed"}
        return {"success": False, "reason": "not_installed", "message": "not installed"}


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
    saves: FakeSaveCoordinator
    events: EventSink
    active: set[int]
    drifted: set[int]
    installed_remover: FakeInstalledFilesRemover
    clock: FakeClock


def _rom(
    rom_id: int,
    *,
    fetch: str | None,
    group: str | None = None,
    app_id: int | None = None,
    fs_name: str | None = None,
) -> Rom:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug="dc",
        name=f"Game {rom_id}",
        fs_name=fs_name or f"Game {rom_id}.gdi",
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
    saves = FakeSaveCoordinator()
    events = EventSink()
    active: set[int] = set()
    drifted: set[int] = set()
    installed_remover = FakeInstalledFilesRemover()

    async def drift(rom_id: int) -> dict[str, Any]:
        return {"drifted": rom_id in drifted}

    async def switch_version(app_id: int, target_rom_id: int, allow_stranded: bool) -> dict[str, Any]:
        del allow_stranded
        with uow:
            target = uow.roms.get(target_rom_id)
            assert target is not None
            target.bind_shortcut(app_id)
            target.record_applied_launch_options(f"launch-{target_rom_id}")
            uow.roms.save(target)
        return {
            "success": True,
            "app_id": app_id,
            "rom_id": target_rom_id,
            "target_installed": False,
            "launch_options": f"launch-{target_rom_id}",
        }

    clock = FakeClock()
    service = PruneService(
        config=PruneServiceConfig(
            loop=loop,
            logger=logging.getLogger("test-prune"),
            clock=clock,
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
            retrodeck_paths=FakeRetroDeckPaths(saves="/saves", roms="/roms", bios="/bios", home="/retrodeck"),
            save_coordinator=saves,
            active_downloads=lambda: set(active),
            drift_probe=drift,
            remove_installed_files=installed_remover,
            switch_version=switch_version,
            settings={"preferred_region": "USA"},
        )
    )
    return Harness(
        service,
        uow,
        romm,
        recovery,
        artifacts,
        steam_recovery,
        saves,
        events,
        active,
        drifted,
        installed_remover,
        clock,
    )


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


async def _claim_action(harness: Harness, action: dict[str, Any]) -> dict[str, Any]:
    return await harness.service.report_prune_action(
        {
            "phase": "claim",
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "action": action["action"],
            "app_id": action["app_id"],
            "target_rom_id": action.get("target_rom_id"),
        }
    )


async def _complete_action(
    harness: Harness,
    action: dict[str, Any],
    *,
    success: bool = True,
    message: str = "confirmed",
    snapshot: dict[str, object] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "phase": "complete",
        "run_id": action["run_id"],
        "action_token": action["action_token"],
        "success": success,
        "message": message,
    }
    if snapshot is not None:
        request["snapshot"] = snapshot
    return await harness.service.report_prune_action(request)


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
    assert complete["results"][0]["mutations"] == ["plugin_artifacts", "database_rows"]
    assert harness.romm.calls == [1, 1, 1]
    assert harness.saves.locked == [[1]]


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
    assert (await _claim_action(harness, action))["success"] is True
    accepted = await _complete_action(harness, action)
    assert accepted["success"] is True
    duplicate = await harness.service.report_prune_action(
        {
            "phase": "complete",
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
        {
            "phase": "claim",
            "run_id": "wrong",
            "action_token": action["action_token"],
        }
    )
    assert stale["reason"] == "stale_action"
    await _claim_action(harness, action)
    duplicate_claim = await _claim_action(harness, action)
    assert duplicate_claim["success"] is True
    assert duplicate_claim["ignored"] is True
    await _complete_action(harness, action, message="removed")
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]
    assert harness.steam_recovery.removed == []


@pytest.mark.asyncio
async def test_binding_change_after_shortcut_action_blocks_source_removal(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    await _claim_action(harness, action)
    with harness.uow:
        harness.uow.roms.save(_rom(1, fetch="old", app_id=0x80000002))
    await _complete_action(harness, action, message="removed")

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
    await _claim_action(harness, capture)
    invalid = await harness.service.report_prune_action(
        {
            "phase": "complete",
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
            "phase": "complete",
            "run_id": capture["run_id"],
            "action_token": capture["action_token"],
            "success": True,
            "message": "wrong app",
            "snapshot": _steam_snapshot(app_id + 1),
        }
    )
    assert wrong_app["reason"] == "invalid_snapshot"
    await _complete_action(harness, capture, message="captured", snapshot=_steam_snapshot(app_id))
    removal = await _wait_action(harness, "remove_shortcut")
    await _claim_action(harness, removal)
    await _complete_action(harness, removal, message="removed")
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]


@pytest.mark.asyncio
async def test_concurrent_starts_atomically_consume_one_preview(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    preview = await _preview(harness)
    entered = threading.Event()
    release = threading.Event()
    original = harness.service._preview_builder.build

    def blocked_refresh(*args):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release preview refresh")
        return original(*args)

    monkeypatch.setattr(harness.service._preview_builder, "build", blocked_refresh)
    first = asyncio.create_task(_start(harness, preview["preview_id"]))
    while not entered.is_set():
        await asyncio.sleep(0)
    second = await _start(harness, preview["preview_id"])
    assert second["reason"] == "prune_active"
    release.set()
    assert (await first)["success"] is True
    await _finish(harness)
    complete_events = [event for event, _payload in harness.events.events if event == "prune_complete"]
    assert complete_events == ["prune_complete"]


@pytest.mark.asyncio
async def test_preview_and_start_io_failures_use_canonical_shape(harness, monkeypatch):
    monkeypatch.setattr(
        harness.service._preview_builder, "build", lambda *_args: (_ for _ in ()).throw(OSError("disk"))
    )
    preview = await _preview(harness)
    assert preview == {"success": False, "reason": "unknown", "message": "disk"}

    _seed(harness.uow, _rom(1, fetch="old"))
    monkeypatch.undo()
    valid = await _preview(harness)
    monkeypatch.setattr(harness.service._preview_builder, "build", lambda *_args: (_ for _ in ()).throw(OSError("db")))
    started = await _start(harness, valid["preview_id"])
    assert started == {"success": False, "reason": "unknown", "message": "db"}
    assert harness.service.is_active() is False


@pytest.mark.asyncio
async def test_preview_discloses_non_candidate_group_members(harness):
    _seed(harness.uow, _rom(1, fetch="old", group="g"), _rom(2, fetch="new", group="g"))
    preview = await _preview(harness)
    assert preview["total"] == 2
    assert {item["rom_id"]: item["candidate"] for item in preview["items"]} == {1: True, 2: False}


@pytest.mark.asyncio
async def test_repoint_option_is_independent_of_row_removal(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    harness.romm.outcomes[2] = [{"id": 2}]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_rows=False)
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)
    complete = await _finish(harness)
    assert complete["results"][0]["status"] == "repointed"
    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2).shortcut_app_id == app_id


@pytest.mark.asyncio
async def test_repoint_uses_version_picker_filename_stem_ranking(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(9, fetch="old", group="g", app_id=app_id, fs_name="Gone.gdi"),
        _rom(1, fetch="new", group="g", fs_name="Game (Europe).zip"),
        _rom(2, fetch="new", group="g", fs_name="Game.iso"),
    )
    harness.romm.outcomes[9] = [RommNotFoundError("gone")]
    harness.romm.outcomes[1] = [{"id": 1}]
    harness.romm.outcomes[2] = [{"id": 2}]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_rows=False)
    action = await _wait_action(harness, "repoint_shortcut")
    assert action["target_rom_id"] == 2
    await _claim_action(harness, action)
    await _complete_action(harness, action)
    await _finish(harness)


@pytest.mark.asyncio
async def test_fully_dead_bound_group_with_drift_requires_recovery(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    harness.drifted.add(1)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert complete["results"][0]["reason"] == "unsynced_saves"
    assert not any(name == "prune_action_required" for name, _payload in harness.events.events)
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_action_claim_rejects_binding_drift_before_steam_mutation(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    with harness.uow:
        harness.uow.roms.save(_rom(1, fetch="old", app_id=app_id + 1))
    claim = await _claim_action(harness, action)
    assert claim["reason"] == "local_state_changed"
    await harness.service.shutdown()
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_recovery_source_drift_aborts_before_mutation(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.recovery.sources_valid = False
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    complete = await _finish(harness)
    assert complete["results"][0]["reason"] == "recovery_state_changed"
    assert harness.uow.roms.get(1) is not None
    assert harness.artifacts.removed == []


@pytest.mark.asyncio
async def test_group_exception_is_reported_and_unrelated_group_continues(harness):
    _seed(harness.uow, _rom(1, fetch="old"), _rom(2, fetch="old"), stamp_count=2)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [RommNotFoundError("gone")] * 3
    harness.saves.inventory_failure_ids.add(1)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert [result["status"] for result in complete["results"]] == ["failed", "removed"]
    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2) is None
    assert complete["partial"] is True


@pytest.mark.asyncio
async def test_shutdown_waits_for_inflight_filesystem_finalization(harness):
    row = _rom(1, fetch="old")
    _seed(harness.uow, row)
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
    harness.installed_remover.installed_ids.add(1)
    harness.installed_remover.block_ids.add(1)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    while not harness.installed_remover.entered.is_set():
        await asyncio.sleep(0)

    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0.01)
    assert shutdown.done() is False
    harness.installed_remover.release.set()
    await shutdown

    assert harness.uow.roms.get(1) is None
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert complete["results"][0]["status"] == "removed"


@pytest.mark.asyncio
async def test_completion_results_are_emitted_in_bounded_chunks(harness):
    rows = [_rom(rom_id, fetch="old") for rom_id in range(1, 27)]
    _seed(harness.uow, *rows, stamp_count=26)
    for row in rows:
        harness.romm.outcomes[row.rom_id] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    await _finish(harness)

    completions = [payload for name, payload in harness.events.events if name == "prune_complete"]
    assert sum(len(payload["results"]) for payload in completions) == 26
    assert completions[-1]["final"] is True
    assert all(len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) <= 48 * 1024 for payload in completions)


def test_one_large_group_result_is_explicitly_bounded(harness):
    rows = [_rom(rom_id, fetch="old", group="g") for rom_id in range(1, 61)]
    result = harness.service._executor._group_result(
        rows,
        "removed",
        None,
        "x" * 2000,
        removed_rom_ids=list(range(1, 61)),
    )
    assert len(result["rom_ids"]) == 50
    assert result["rom_count"] == 60
    assert result["rom_ids_truncated"] is True
    assert len(result["removed_rom_ids"]) == 50
    assert result["removed_count"] == 60
    assert result["removed_rom_ids_truncated"] is True
    assert len(result["message"]) == 512
    assert result["message_truncated"] is True


@pytest.mark.asyncio
async def test_save_owner_lock_set_retries_until_stable(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.saves.lock_id_sequence = [[1], [1, 2], [1, 2], [1, 2]]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)
    assert complete["results"][0]["status"] == "removed"
    assert harness.saves.locked == [[1], [1, 2]]


@pytest.mark.asyncio
async def test_installed_file_failure_preserves_all_rows_and_reports_prior_mutation(harness):
    _seed(harness.uow, _rom(1, fetch="old", group="g"), _rom(2, fetch="old", group="g"))
    for rom_id in (1, 2):
        harness.romm.outcomes[rom_id] = [RommNotFoundError("gone")] * 3
        with harness.uow:
            harness.uow.rom_installs.save(
                RomInstall.mark_installed(
                    rom_id=rom_id,
                    file_path=f"/roms/dc/{rom_id}.gdi",
                    rom_dir=None,
                    platform_slug="dc",
                    system="dc",
                    installed_at="now",
                )
            )
    harness.installed_remover.installed_ids.add(1)
    harness.installed_remover.failure_ids.add(2)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert result["reason"] == "rom_removal_failed"
    assert result["mutations"] == ["installed_rom_content"]
    assert harness.uow.roms.get(1) is not None and harness.uow.roms.get(2) is not None
    assert harness.uow.rom_installs.get(1) is not None and harness.uow.rom_installs.get(2) is not None


@pytest.mark.asyncio
async def test_post_seal_database_drift_after_shortcut_removal_is_explicit_partial(harness):
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
    await _claim_action(harness, capture)
    await _complete_action(harness, capture, snapshot=_steam_snapshot(app_id))
    removal = await _wait_action(harness, "remove_shortcut")
    with harness.uow:
        row = harness.uow.roms.get(1)
        assert row is not None
        row.record_fetch_generation("changed-after-seal")
        harness.uow.roms.save(row)
    await _claim_action(harness, removal)
    await _complete_action(harness, removal)
    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert result["reason"] == "recovery_state_changed"
    assert result["committed_action"] == "remove_shortcut"
    assert harness.uow.roms.get(1).shortcut_app_id is None
    assert harness.artifacts.removed == []


@pytest.mark.asyncio
async def test_unclaimed_action_timeout_makes_late_claim_harmless(harness, monkeypatch):
    monkeypatch.setattr("services.prune.service._ACTION_TIMEOUT_SECONDS", 0.01)
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    complete = await _finish(harness)
    assert complete["results"][0]["reason"] == "steam_action_failed"

    late = await _claim_action(harness, action)
    assert late["reason"] == "stale_action"
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_action_claim_expiring_during_validation_is_rejected(harness, monkeypatch):
    app_id = 0x80000001
    entered = threading.Event()
    release = threading.Event()
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    original = harness.service._registry.validate_action_state

    def delayed_validation(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(harness.service._registry, "validate_action_state", delayed_validation)
    claim_task = asyncio.create_task(_claim_action(harness, action))
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    harness.clock.advance(61)
    release.set()

    claim = await claim_task
    assert claim["reason"] == "stale_action"
    await harness.service.shutdown()
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_shutdown_after_snapshot_claim_does_not_begin_recovery(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    capture = await _wait_action(harness, "capture_shortcut_snapshot")
    await _claim_action(harness, capture)
    shutdown = asyncio.create_task(harness.service.shutdown())
    while harness.service._task is not None and harness.service._task.cancelling() == 0:
        await asyncio.sleep(0)
    await _complete_action(harness, capture, snapshot=_steam_snapshot(app_id))
    await shutdown

    assert harness.recovery.sealed == []
    assert harness.artifacts.removed == []
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["quarantine", "artifacts", "database"])
async def test_shutdown_waits_for_each_executor_backed_finalization_phase(harness, monkeypatch, phase):
    entered = threading.Event()
    release = threading.Event()
    if phase == "quarantine":
        original = harness.saves.quarantine_prune_saves

        def blocked(*args):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args)

        monkeypatch.setattr(harness.saves, "quarantine_prune_saves", blocked)
    elif phase == "artifacts":
        original = harness.artifacts.remove

        def blocked(*args):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args)

        monkeypatch.setattr(harness.artifacts, "remove", blocked)
    else:
        original = harness.service._registry.delete_rows

        def blocked(*args):
            entered.set()
            assert release.wait(timeout=5)
            return original(*args)

        monkeypatch.setattr(harness.service._registry, "delete_rows", blocked)

    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    while not entered.is_set():
        await asyncio.sleep(0)
    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0.01)
    assert shutdown.done() is False
    release.set()
    await shutdown

    assert harness.uow.roms.get(1) is None
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_during_final_liveness_guard_starts_no_local_mutation_or_later_group(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"), _rom(2, fetch="old"), stamp_count=2)
    for rom_id in (1, 2):
        harness.romm.outcomes[rom_id] = [RommNotFoundError("gone")] * 3
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original = harness.romm.get_rom_once

    def block_final_probe(rom_id):
        nonlocal calls
        if rom_id == 1:
            calls += 1
            if calls == 3:
                entered.set()
                assert release.wait(timeout=5)
        return original(rom_id)

    monkeypatch.setattr(harness.romm, "get_rom_once", block_final_probe)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0)
    release.set()
    await shutdown

    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2) is not None
    assert harness.artifacts.removed == []
    assert 2 not in harness.romm.calls
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_shutdown_cancels_admitted_preview_refresh_without_starting_run(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    preview = await _preview(harness)
    entered = threading.Event()
    release = threading.Event()
    original = harness.service._preview_builder.build

    def blocked_refresh(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(harness.service._preview_builder, "build", blocked_refresh)
    start = asyncio.create_task(_start(harness, preview["preview_id"]))
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    await harness.service.shutdown()
    assert harness.service.is_active() is False
    assert harness.service._task is None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    await asyncio.sleep(0.01)
    assert not any(name == "prune_complete" for name, _payload in harness.events.events)


@pytest.mark.asyncio
async def test_shutdown_of_claimed_uncompleted_action_reports_ambiguity_and_halts_later_groups(harness, monkeypatch):
    monkeypatch.setattr("services.prune.service._ACTION_TIMEOUT_SECONDS", 0.01)
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id), _rom(2, fetch="old"), stamp_count=2)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    harness.romm.outcomes[2] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    assert (await _claim_action(harness, action))["success"] is True

    await harness.service.shutdown()

    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2) is not None
    assert 2 not in harness.romm.calls
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert complete["results"][0]["reason"] == "action_ambiguous"
    assert complete["results"][0]["action_ambiguous"] is True


@pytest.mark.asyncio
async def test_action_claim_binds_discriminant_app_target_and_single_group_binding(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    harness.romm.outcomes[2] = [{"id": 2}]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])
    action = await _wait_action(harness, "repoint_shortcut")

    for field, value in (("action", "remove_shortcut"), ("app_id", app_id + 1), ("target_rom_id", 99)):
        request = {
            "phase": "claim",
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "action": action["action"],
            "app_id": action["app_id"],
            "target_rom_id": action["target_rom_id"],
        }
        request[field] = value
        assert (await harness.service.report_prune_action(request))["reason"] == "action_mismatch"

    with harness.uow:
        harness.uow.roms.save(_rom(3, fetch="new", group="g"))
    assert (await _claim_action(harness, action))["reason"] == "local_state_changed"

    with harness.uow:
        harness.uow.roms.delete(3)
        other = harness.uow.roms.get(1)
        assert other is not None
        other.bind_shortcut(app_id + 1)
        harness.uow.roms.save(other)
    claim = await _claim_action(harness, action)
    assert claim["reason"] == "local_state_changed"
    await harness.service.shutdown()


@pytest.mark.asyncio
async def test_action_claim_adapter_exception_uses_canonical_failure(harness, monkeypatch):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    monkeypatch.setattr(
        harness.service._registry,
        "validate_action_state",
        lambda *_args: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    claim = await _claim_action(harness, action)

    assert claim == {"success": False, "reason": "unknown", "message": "database unavailable"}
    await harness.service.shutdown()


@pytest.mark.asyncio
async def test_post_removal_reconciliation_exception_preserves_committed_action(harness, monkeypatch):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    await _claim_action(harness, action)
    monkeypatch.setattr(
        harness.service._registry,
        "reconcile_removed_shortcut",
        lambda *_args: (_ for _ in ()).throw(OSError("commit failed")),
    )
    await _complete_action(harness, action)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert result["reason"] == "unknown"
    assert result["committed_action"] == "remove_shortcut"
    assert result["app_id"] == app_id


@pytest.mark.asyncio
async def test_post_filesystem_database_exception_preserves_actual_mutation_ledger(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
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
    harness.installed_remover.installed_ids.add(1)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    monkeypatch.setattr(
        harness.service._registry,
        "delete_rows",
        lambda *_args: (_ for _ in ()).throw(OSError("database commit failed")),
    )

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert result["reason"] == "unknown"
    assert result["mutations"] == ["installed_rom_content", "plugin_artifacts", "database_rows_ambiguous"]
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_recovery_warnings_are_bounded_and_visible_without_a_bundle(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.saves.warnings = [f"warning {index}: {'x' * 300}" for index in range(7)]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["warning_count"] == 7
    assert len(result["warnings"]) == 5
    assert all(len(value) <= 256 for value in result["warnings"])
    assert result["warnings_truncated"] is True


@pytest.mark.asyncio
async def test_installed_selection_stages_more_than_256_disclosed_rows(harness):
    rows = [_rom(rom_id, fetch="old") for rom_id in range(1, 301)]
    _seed(harness.uow, *rows, stamp_count=len(rows))
    with harness.uow:
        for row in rows:
            harness.uow.rom_installs.save(
                RomInstall.mark_installed(
                    rom_id=row.rom_id,
                    file_path=f"/roms/dc/{row.rom_id}.gdi",
                    rom_dir=None,
                    platform_slug="dc",
                    system="dc",
                    installed_at="now",
                )
            )
    preview = await _preview(harness)
    selection_id = None
    for offset in range(0, 300, 100):
        staged = await harness.service.stage_prune_installed_selection(
            {
                "preview_id": preview["preview_id"],
                "selection_id": selection_id,
                "rom_ids": list(range(offset + 1, offset + 101)),
                "final": offset == 200,
            }
        )
        assert staged["success"] is True
        selection_id = staged["selection_id"]

    assert staged["selected_count"] == 300
    assert staged["finalized"] is True
    assert harness.service._selection is not None
    assert len(harness.service._selection.rom_ids) == 300


@pytest.mark.asyncio
async def test_shutdown_waits_for_recovery_copy_worker_and_starts_no_mutation(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 2
    entered = threading.Event()
    release = threading.Event()
    original = harness.recovery.seal_bundle

    def blocked_seal(*args):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(harness.recovery, "seal_bundle", blocked_seal)
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0.01)
    assert shutdown.done() is False
    release.set()
    await shutdown

    assert harness.recovery.sealed
    assert harness.uow.roms.get(1) is not None
    assert harness.artifacts.removed == []
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_post_seal_aggregate_drift_aborts_before_shortcut_removal(harness, monkeypatch):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 2
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    capture = await _wait_action(harness, "capture_shortcut_snapshot")
    await _claim_action(harness, capture)

    def mutate_after_seal(_bundle_path):
        with harness.uow:
            row = harness.uow.roms.get(1)
            assert row is not None
            row.record_fetch_generation("changed-after-seal")
            harness.uow.roms.save(row)
        return {}

    monkeypatch.setattr(harness.recovery, "source_identities", mutate_after_seal)
    await _complete_action(harness, capture, snapshot=_steam_snapshot(app_id))
    complete = await _finish(harness)

    assert complete["results"][0]["reason"] == "recovery_state_changed"
    assert not any(
        name == "prune_action_required" and payload["action"] == "remove_shortcut"
        for name, payload in harness.events.events
    )
    assert harness.uow.roms.get(1).shortcut_app_id == app_id


@pytest.mark.asyncio
async def test_controller_state_drift_aborts_before_shortcut_removal(harness, monkeypatch):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 2
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )
    capture = await _wait_action(harness, "capture_shortcut_snapshot")
    await _claim_action(harness, capture)
    monkeypatch.setattr(harness.steam_recovery, "validate_state", lambda *_args: False)
    await _complete_action(harness, capture, snapshot=_steam_snapshot(app_id))

    complete = await _finish(harness)

    assert complete["results"][0]["reason"] == "recovery_state_changed"
    assert not any(
        name == "prune_action_required" and payload["action"] == "remove_shortcut"
        for name, payload in harness.events.events
    )
    assert harness.uow.roms.get(1).shortcut_app_id == app_id


@pytest.mark.asyncio
async def test_claimed_removal_timeout_is_ambiguous_and_absent_retry_reconciles(harness, monkeypatch):
    monkeypatch.setattr("services.prune.service._ACTION_TIMEOUT_SECONDS", 0.01)
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 6
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    first = await _wait_action(harness, "remove_shortcut")
    await _claim_action(harness, first)
    ambiguous = await _finish(harness)

    assert ambiguous["results"][0]["reason"] == "action_ambiguous"
    assert ambiguous["results"][0]["action_ambiguous"] is True
    assert harness.uow.roms.get(1).shortcut_app_id == app_id

    previous_events = len(harness.events.events)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    for _ in range(100):
        actions = [
            payload
            for name, payload in harness.events.events[previous_events:]
            if name == "prune_action_required" and payload["action"] == "remove_shortcut"
        ]
        if actions:
            break
        await asyncio.sleep(0.001)
    assert actions
    second = actions[-1]
    await _claim_action(harness, second)
    accepted = await harness.service.report_prune_action(
        {
            "phase": "complete",
            "run_id": second["run_id"],
            "action_token": second["action_token"],
            "success": True,
            "message": "Steam confirmed the shortcut is already absent.",
            "shortcut_absent": True,
        }
    )
    assert accepted["success"] is True
    complete = await _finish(harness)
    assert complete["removed_rom_ids"] == [1]
    assert harness.uow.roms.get(1) is None


@pytest.mark.asyncio
async def test_repoint_only_reprobes_vanished_source_after_recovery_and_action(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone"), RommNotFoundError("gone"), {"id": 1}]
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_rows=False,
        create_recovery_bundle=True,
    )
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert result["reason"] == "live"
    assert result["committed_action"] == "repoint_shortcut"
    assert result["target_rom_id"] == 2
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_zero_change_final_guard_reports_no_mutation_categories(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    monkeypatch.setattr(harness.artifacts, "remove", lambda *_args: 0)
    monkeypatch.setattr(harness.service._registry, "delete_rows", lambda *_args: False)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"] == "local_state_changed"
    assert "mutations" not in result


@pytest.mark.asyncio
async def test_post_delete_progress_failure_keeps_removed_terminal_result(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3

    async def fail_removed_progress(event: str, payload: dict[str, Any]) -> None:
        if event == "prune_progress" and payload.get("stage") == "removed":
            raise OSError("bridge reset")
        await harness.events(event, payload)

    harness.service._executor._emit = fail_removed_progress
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert complete["removed_rom_ids"] == [1]
    assert complete["results"][0]["status"] == "removed"
    assert harness.uow.roms.get(1) is None
