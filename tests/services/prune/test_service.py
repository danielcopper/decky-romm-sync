from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest
import pytest_asyncio
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock, FakeUuidGen
from models.prune import SealedSourceClaims

from domain.fetch_generation import count_rows_for_skip, prune_candidate_ids
from domain.platform_sync_state import PlatformSyncState
from domain.playtime import Playtime
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.version_metadata import VersionMetadata
from lib.errors import OperationAbortedError, RommConnectionError, RommNotFoundError
from services.prune import PruneService, PruneServiceConfig
from services.prune._models import cancellation_state
from services.prune.results import GroupOutcome

if TYPE_CHECKING:
    from models.prune import MutationOutcome, RecoveryArtifact, SourceClaim, SteamRecoverySnapshot


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
        # Backup-phase blocking knobs. ``block_seconds`` stands in for a long
        # artifact copy; it is a bounded wait rather than an unbounded one so a
        # build that never asks the copy to stop finishes the seal and fails the
        # assertions, instead of hanging the suite.
        self.block_seconds = 0.0
        self.seal_entered = threading.Event()
        self.seal_aborted = False
        self.staging_cleaned = False

    def root(self) -> str:
        return "/recovery"

    def free_bytes(self) -> int:
        return 10_000

    def measure_path(self, path: str, safe_root: str) -> int:
        del path, safe_root
        return 123

    def validate_sources(self, bundle_path: str, bundle_digest: str | None = None) -> bool:
        del bundle_path, bundle_digest
        return self.sources_valid

    def source_claims(self, bundle_path: str) -> SealedSourceClaims:
        del bundle_path
        return SealedSourceClaims(claims={}, bundle_digest="sealed-digest")

    def seal_bundle(self, bundle_id, snapshot, artifacts, readme_context, playtime_text, should_abort=None):
        if self.failure is not None:
            raise self.failure
        self.seal_entered.set()
        deadline = time.monotonic() + self.block_seconds
        while time.monotonic() < deadline:
            if should_abort is not None and should_abort():
                self.seal_aborted = True
                # What the real adapter does on the way out: its staging
                # directory is removed before the abort leaves the worker.
                self.staging_cleaned = True
                raise OperationAbortedError("cancelled while writing the bundle")
            time.sleep(0.005)
        self.sealed.append(
            {
                "bundle_id": bundle_id,
                "snapshot": snapshot,
                "artifacts": artifacts,
                "readme_context": readme_context,
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

    def remove(self, rom_ids: list[int], claims: dict[str, SourceClaim] | None = None) -> MutationOutcome:
        del claims
        self.removed.append(rom_ids)
        return {
            "success": True,
            "changed": bool(rom_ids),
            "ambiguous": False,
            "message": "removed",
        }


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

    def remove_state(
        self,
        app_id: int,
        snapshot: SteamRecoverySnapshot,
        claims: dict[str, SourceClaim],
    ) -> MutationOutcome:
        del claims
        assert snapshot["user_id"] == "123"
        self.removed.append(app_id)
        return {"success": True, "changed": True, "ambiguous": False, "message": "removed"}


class FakeSaveCoordinator:
    def __init__(self) -> None:
        self.locked: list[list[int]] = []
        self.quarantined: list[list[dict[str, str]]] = []
        self.inventory_failure_ids: set[int] = set()
        self.lock_id_sequence: list[list[int]] = []
        self.warnings: list[str] = []
        self.absences_valid = True
        self.absence_validations = 0

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
            "source_claims": {},
        }

    def quarantine_prune_saves(
        self, files: list[dict[str, str]], claims: dict[str, SourceClaim] | None = None
    ) -> dict[str, Any]:
        del claims
        self.quarantined.append(files)
        return {"success": True, "moved": []}

    def validate_prune_absences(self, claims: dict[str, SourceClaim]) -> bool:
        del claims
        self.absence_validations += 1
        return self.absences_valid


class FakeInstalledFilesRemover:
    def __init__(self) -> None:
        self.removed: list[int] = []
        self.installed_ids: set[int] = set()
        self.block_ids: set[int] = set()
        self.failure_ids: set[int] = set()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, rom_id: int, claims: dict[str, SourceClaim] | None = None) -> dict[str, Any]:
        del claims
        self.removed.append(rom_id)
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
        assert len(args) == 1
        assert isinstance(args[0], dict)
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
    switch_calls: list[dict[str, Any]]


def _rom(
    rom_id: int,
    *,
    fetch: str | None,
    group: str | None = None,
    app_id: int | None = None,
    fs_name: str | None = None,
    platform: str = "dc",
) -> Rom:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug=platform,
        name=f"Game {rom_id}",
        fs_name=fs_name or f"Game {rom_id}.gdi",
        shortcut_app_id=app_id,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=group, regions=("USA",)),
    )
    if fetch is not None:
        rom.record_fetch_generation(fetch)
    return rom


_CONTROL_ROM_ID = 900001


def _seed(uow: FakeUnitOfWork, *rows: Rom, stamp_count: int = 1, control: bool = True) -> None:
    """Seed the given rows plus, by default, one the stamped fetch is recorded as returning.

    A completed fetch stamp asserts the server served that fetch, so a real
    library always holds at least one row carrying its generation — seeding a
    stamp with none of its rows is a state the fetch path cannot produce.
    Cleanup now leans on exactly that row: with nothing the server is known to
    have served, a 404 cannot be told apart from a misrouted request.
    ``control=False`` seeds that impossible-but-testable state deliberately.
    """
    with uow:
        for row in rows:
            uow.roms.save(row)
        if control:
            # On its own platform on purpose: outside every seeded group, since a
            # row inside the probed group is not available as a control (it is the
            # thing being questioned), and outside every ``dc``-scoped count.
            uow.roms.save(_rom(_CONTROL_ROM_ID, fetch="ctl", platform="ctl", fs_name="Control.gdi"))
            uow.platform_sync_state.save(
                PlatformSyncState.stamp(platform_slug="ctl", at="now", rom_count=1, fetch_id="ctl")
            )
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
    switch_calls: list[dict[str, Any]] = []

    async def drift(rom_id: int) -> dict[str, Any]:
        return {"drifted": rom_id in drifted}

    async def switch_version(app_id: int, target_rom_id: int, allow_stranded: bool) -> dict[str, Any]:
        switch_calls.append({"app_id": app_id, "target_rom_id": target_rom_id, "allow_stranded": allow_stranded})
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
        switch_calls,
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


def _active_run_task(harness: Harness) -> asyncio.Task[None]:
    task = harness.service._task
    assert task is not None
    return task


async def _assert_task_cancelled(task: asyncio.Task[None]) -> None:
    assert task.cancelled()
    with pytest.raises(asyncio.CancelledError):
        await task


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
    # Three re-proof rounds, and because no candidate answered live in any of
    # them, each round also asks one control whether the endpoint answers at all.
    # One extra request per round — never per candidate.
    assert harness.romm.calls == [1, _CONTROL_ROM_ID, 1, _CONTROL_ROM_ID, 1, _CONTROL_ROM_ID]
    assert harness.saves.locked == [[1]]
    frames = [payload for name, payload in harness.events.events if name in {"prune_progress", "prune_complete"}]
    assert frames
    assert all(frame["preview_id"] == preview["preview_id"] for frame in frames)


@pytest.mark.asyncio
async def test_a_total_misroute_removes_nothing_and_says_so(harness):
    """404s from something that is not answering about ROMs are not deletion authority.

    The realistic trigger is a reverse-proxy path misroute: every ROM request
    answers 404 while the row is perfectly alive on the server. Indistinguishable
    from a real 404 one request at a time — which is why the run asks a control
    the server is known to have served, and refuses when that 404s too.
    """
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("misrouted")] * 5
    harness.romm.outcomes[_CONTROL_ROM_ID] = [RommNotFoundError("misrouted")] * 5
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert harness.uow.roms.get(1) is not None, "a misrouted 404 may never delete a row"
    assert complete["removed_rom_ids"] == []
    assert complete["results"][0]["reason"] == "unconfirmed_server"
    assert "could not be confirmed" in complete["results"][0]["message"]
    assert harness.installed_remover.removed == []
    assert harness.artifacts.removed == []


@pytest.mark.asyncio
async def test_a_confirmed_endpoint_still_removes_a_genuine_404(harness):
    """The converse: with the control answering, a 404 is authority exactly as before."""
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 5
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert harness.uow.roms.get(1) is None
    assert complete["removed_rom_ids"] == [1]


@pytest.mark.asyncio
async def test_a_misroute_may_not_reclassify_a_repoint_group_as_fully_dead(harness):
    """The amplifier: a misroute 404s the LIVE sibling too.

    Believed, that turns "one version vanished, repoint to the other" into
    "the whole game is gone" — taking the Steam shortcut and any unselected
    installed content with it, and fully-vanished removal is on by default.
    """
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", group="g", app_id=app_id), _rom(2, fetch="new", group="g"))
    harness.romm.outcomes[1] = [RommNotFoundError("misrouted")] * 5
    harness.romm.outcomes[2] = [RommNotFoundError("misrouted")] * 5
    harness.romm.outcomes[_CONTROL_ROM_ID] = [RommNotFoundError("misrouted")] * 5
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2) is not None, "the live sibling survives a lying 404"
    assert complete["removed_rom_ids"] == []
    assert complete["results"][0]["reason"] == "unconfirmed_server"
    assert harness.steam_recovery.removed == [], "no shortcut was removed"
    assert harness.switch_calls == [], "and no repoint was attempted either"


@pytest.mark.asyncio
async def test_a_live_answer_in_the_round_is_its_own_proof(harness):
    """A group with a live member already holds the proof; no control is asked for.

    A correct answer about one ROM is exactly what a control would be asked to
    demonstrate, so paying for a second request would prove nothing new.
    """
    _seed(harness.uow, _rom(1, fetch="old", group="g"), _rom(2, fetch="new", group="g"), control=False)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 5
    harness.romm.outcomes[2] = [{"id": 2}] * 5
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])

    complete = await _finish(harness)

    assert complete["removed_rom_ids"] == [1]
    assert harness.uow.roms.get(2) is not None
    assert _CONTROL_ROM_ID not in harness.romm.calls, "no control request was needed"


@pytest.mark.asyncio
async def test_without_any_control_a_404_is_never_honoured(harness):
    """Nothing the server is known to have served means nothing to check against."""
    _seed(harness.uow, _rom(1, fetch="old"), control=False)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 5
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert harness.uow.roms.get(1) is not None
    assert complete["results"][0]["reason"] == "unconfirmed_server"


@pytest.mark.asyncio
async def test_cancel_stops_the_named_run_and_reports_it_as_cancelled(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", group="g", app_id=app_id), _rom(2, fetch="new", group="g"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"])
    task = _active_run_task(harness)
    # Park the run on its pending Steam action so the cancellation lands mid-run
    # rather than before the executor body has had a chance to start.
    await _wait_action(harness, "repoint_shortcut")

    cancelled = await harness.service.cancel_prune(started["run_id"])

    assert cancelled == {
        "success": True,
        "run_id": started["run_id"],
        "already_cancelling": False,
        "message": "Cleanup will stop before the next group.",
    }
    with pytest.raises(asyncio.CancelledError):
        await task
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    # Nothing was rolled back and nothing was invented: the shortcut is untouched
    # because its action never completed, and the row survives.
    assert harness.uow.roms.get(1) is not None
    # The claim is released, so the next scan is not locked out by a stopped run.
    assert harness.service.is_active() is False


@pytest.mark.asyncio
async def test_cancel_during_the_backup_phase_abandons_it_without_mutating_anything(harness):
    """Stop must not wait out a multi-hundred-megabyte copy the user gave up on.

    The backup phase runs before anything is committed, so a cancellation
    arriving inside it is told to stop at its next chunk rather than awaited to
    completion. Nothing here is recoverable-by-rollback, so "nothing happened"
    is the only honest outcome: no quarantine, no Steam action, no row removed.
    """
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 5
    harness.recovery.block_seconds = 5.0
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True, create_recovery_bundle=True)
    task = _active_run_task(harness)
    await asyncio.get_running_loop().run_in_executor(None, harness.recovery.seal_entered.wait, 5.0)

    await harness.service.cancel_prune(started["run_id"])
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.recovery.seal_aborted is True, "the copy was asked to stop, not waited out"
    assert harness.recovery.staging_cleaned is True
    assert harness.recovery.sealed == [], "an abandoned backup publishes no bundle"
    # Zero mutation: the row, its files, its saves and its shortcut are untouched.
    assert harness.uow.roms.get(1) is not None
    assert harness.saves.quarantined == []
    assert harness.installed_remover.removed == []
    assert harness.artifacts.removed == []
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert complete["removed_rom_ids"] == []
    assert harness.service.is_active() is False


@pytest.mark.asyncio
async def test_the_abandoned_group_is_reported_as_skipped_not_failed(harness):
    """An obedient stop is not an error — reporting it as one blames the user."""
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 5
    harness.recovery.block_seconds = 5.0
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True, create_recovery_bundle=True)
    task = _active_run_task(harness)
    await asyncio.get_running_loop().run_in_executor(None, harness.recovery.seal_entered.wait, 5.0)

    await harness.service.cancel_prune(started["run_id"])
    with pytest.raises(asyncio.CancelledError):
        await task

    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert [result["status"] for result in complete["results"]] == ["skipped"]
    assert complete["results"][0]["reason"] == "cancelled"
    assert complete["partial"] is False, "nothing was committed, so the run is not partial"


@pytest.mark.asyncio
async def test_a_cancel_after_the_destructive_phase_started_does_not_abort_it(harness):
    """The shield boundary is unchanged: once mutating, the group finishes.

    Cancellation is cooperative only up to the commit point. Past it the group
    runs to its own terminal verdict so what it changed is reported truthfully
    rather than abandoned half-done.
    """
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", group="g", app_id=app_id), _rom(2, fetch="new", group="g"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 6
    harness.romm.outcomes[2] = [{"id": 2}] * 6
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"])
    task = _active_run_task(harness)
    # Parked on the Steam action, the group has already switched the binding —
    # it is past the commit point.
    action = await _wait_action(harness, "repoint_shortcut")

    await harness.service.cancel_prune(started["run_id"])
    await harness.service.report_prune_action(
        {
            "run_id": started["run_id"],
            "action_token": action["action_token"],
            "phase": "claim",
            "action": "repoint_shortcut",
            "app_id": app_id,
            "target_rom_id": 2,
        }
    )
    await harness.service.report_prune_action(
        {
            "run_id": started["run_id"],
            "action_token": action["action_token"],
            "phase": "complete",
            "success": True,
            "message": "done",
        }
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    # The committed repoint is reported, not discarded as if it never happened.
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert any(result.get("committed_action") == "repoint_shortcut" for result in complete["results"])


@pytest.mark.asyncio
async def test_cancel_before_the_run_task_starts_still_releases_the_claim(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    task = _active_run_task(harness)

    # Cancelled in the same tick the task was created, so its coroutine body —
    # and the `finally` that normally releases the claim — never runs at all.
    await harness.service.cancel_prune(started["run_id"])
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    # A claim left set here would refuse Play, downloads and saves for the rest
    # of the plugin's life, with no run to release it.
    assert harness.service.is_active() is False
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_cancel_refuses_an_id_that_is_not_the_running_one(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    stale = await harness.service.cancel_prune("00000000-0000-4000-8000-00000000dead")

    assert stale == {
        "success": False,
        "reason": "stale_run",
        "message": "That cleanup run is not running.",
    }
    # The real run is untouched by a wrong-id request.
    await _finish(harness)
    assert harness.uow.roms.get(1) is None
    assert started["run_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["", None, 7])
async def test_cancel_rejects_a_malformed_run_id(harness, run_id):
    result = await harness.service.cancel_prune(run_id)
    assert result == {
        "success": False,
        "reason": "invalid_run_id",
        "message": "Cleanup run id must be a non-empty string.",
    }


@pytest.mark.asyncio
async def test_cancel_is_idempotent_for_the_same_run(harness):
    _seed(harness.uow, _rom(1, fetch="old"), _rom(2, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    task = _active_run_task(harness)

    first = await harness.service.cancel_prune(started["run_id"])
    second = await harness.service.cancel_prune(started["run_id"])

    # A repeat while the first is still propagating is a success, not an error —
    # a user mashing the button must not see a failure for work already asked for.
    assert (first["success"], first["already_cancelling"]) == (True, False)
    assert (second["success"], second["already_cancelling"]) == (True, True)
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_after_the_run_finished_is_refused_not_crashed(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    await _finish(harness)

    late = await harness.service.cancel_prune(started["run_id"])

    assert late["success"] is False
    assert late["reason"] == "stale_run"


@pytest.mark.asyncio
async def test_bundle_is_named_after_the_game_it_removes(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", group="g", app_id=app_id), _rom(2, fetch="new", group="g"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], create_recovery_bundle=True)
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)
    await _finish(harness)

    sealed = harness.recovery.sealed[0]
    # The recovery root is the manual-restore surface: the folder has to say
    # which game it holds, dated, with a short id carrying the uniqueness.
    assert sealed["bundle_id"].startswith("Game-1_")
    assert re.fullmatch(r"Game-1_\d{4}-\d{2}-\d{2}_[A-Za-z0-9]{8}", sealed["bundle_id"])

    context = sealed["readme_context"]
    roles = {game["rom_id"]: game["role"] for game in context["games"]}
    # Both rows are named, and each says what this run does to it.
    assert roles[1] == "removed by this cleanup"
    assert roles[2] == "kept — recorded for context"
    assert {game["name"] for game in context["games"]} == {"Game 1", "Game 2"}
    # A repoint keeps the shortcut, so no Steam state is captured and the README
    # must not offer rebuild instructions for a shortcut that still exists.
    assert "steam_app_id" not in context


@pytest.mark.asyncio
async def test_bundle_playtime_lines_name_the_game_beside_the_id(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    with harness.uow:
        harness.uow.playtime.save(1, Playtime(total_seconds=894))
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True, create_recovery_bundle=True)
    await _finish(harness)

    context = harness.recovery.sealed[0]["readme_context"]
    line = context["playtime_lines"][0]
    # "894" alone tells a person nothing about which game, or how long that is.
    assert line.startswith("Game 1 (ROM 1): 894 seconds")
    assert "0h 14m 54s" in line


@pytest.mark.asyncio
async def test_successful_run_leaves_an_audit_trail_at_info(harness, caplog):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3

    with caplog.at_level(logging.INFO, logger="test-prune"):
        preview = await _preview(harness)
        await _start(harness, preview["preview_id"], remove_fully_vanished=True)
        await _finish(harness)

    run_id = harness.service._release_run_id
    assert run_id
    audit = [record.message for record in caplog.records if record.levelno == logging.INFO]
    # Every line ties back to the run id, so one grep reconstructs the run.
    assert all(run_id in line for line in audit)

    start = next(line for line in audit if "starting" in line)
    assert "1 group(s), 1 candidate(s)" in start
    assert "remove_fully_vanished=True" in start
    assert "recovery=False" in start

    liveness = next(line for line in audit if "group 1/1 liveness" in line)
    # The verdicts every later decision turns on: without them a skipped group
    # cannot be explained after the fact (#1570 F17).
    assert "gone=[1], still_there=[], unconfirmed=[], candidates=[1]" in liveness

    group = next(line for line in audit if "group 1/1" in line and "group 1/1 liveness" not in line)
    assert "rom_ids=[1]" in group
    assert "status=removed" in group
    assert "removed=[1]" in group

    end = next(line for line in audit if "finished" in line)
    assert "removed=[1]" in end


@pytest.mark.asyncio
async def test_audit_trail_records_why_an_untouched_group_was_skipped(harness, caplog):
    _seed(harness.uow, _rom(1, fetch="old"))
    # RomM cannot confirm the id is gone, so nothing may be removed. Which of
    # the several "nothing happened" verdicts it was is exactly what the log has
    # to preserve — a bare "skipped" would leave the user guessing.
    harness.romm.outcomes[1] = [RommConnectionError("offline")]

    with caplog.at_level(logging.INFO, logger="test-prune"):
        preview = await _preview(harness)
        await _start(harness, preview["preview_id"])
        await _finish(harness)

    audit = [record.message for record in caplog.records if record.levelno == logging.INFO]
    liveness = next(line for line in audit if "group 1/1 liveness" in line)
    assert "gone=[], still_there=[], unconfirmed=[1]" in liveness

    group = next(line for line in audit if "group 1/1" in line and "group 1/1 liveness" not in line)
    assert "status=skipped" in group
    assert "reason=liveness_uncertain" in group
    assert next(line for line in audit if "finished" in line).endswith("removed=[], affected_app_ids=[]")


@pytest.mark.asyncio
@pytest.mark.parametrize("create_recovery_bundle", [False, True])
async def test_save_appearing_after_artifact_cleanup_blocks_final_cascade(harness, monkeypatch, create_recovery_bundle):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    original = harness.artifacts.remove

    def appear_after_cleanup(*args):
        outcome = original(*args)
        harness.saves.absences_valid = False
        return outcome

    monkeypatch.setattr(harness.artifacts, "remove", appear_after_cleanup)
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=create_recovery_bundle,
    )
    complete = await _finish(harness)

    assert complete["results"][0]["reason"] == "save_state_changed"
    assert complete["removed_rom_ids"] == []
    assert harness.saves.absence_validations == 1
    assert harness.uow.roms.get(1) is not None


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
async def test_a_failed_terminal_frame_does_not_escape_the_run_task(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3

    async def failing_completion(*_args, **_kwargs):
        raise RuntimeError("bridge closed")

    monkeypatch.setattr(harness.service._executor._results, "emit_completion", failing_completion)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    task = _active_run_task(harness)

    # The mutations are committed either way; an escaping error would only add
    # an unretrievable exception in an unawaited task on top of the lost frame.
    await task
    assert task.exception() is None
    assert harness.uow.roms.get(1) is None
    # The claim is still released, so cleanup stays reachable afterwards.
    assert harness.service.is_active() is False


@pytest.mark.asyncio
async def test_group_result_leads_with_the_bound_row_s_game_name(harness):
    app_id = 0x80000001
    # The bound row is the group's representative — its name is the one the
    # user sees on the shortcut.
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g"),
        _rom(2, fetch="new", group="g", app_id=app_id),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])
    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["name"] == "Game 2"
    assert result["name_truncated"] is False
    # The metadata key stays on the wire for correlation, but it is no longer
    # what a human-facing line has to lead with.
    assert result["group_id"] == "g"


@pytest.mark.asyncio
async def test_group_result_names_an_unbound_group_from_its_members(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)

    assert complete["results"][0]["name"] == "Game 1"


@pytest.mark.asyncio
async def test_nothing_to_do_blames_liveness_not_the_options_when_romm_was_unsure(harness):
    """An unconfirmed id is not an options problem, and must not read like one.

    Both states leave nothing to do, but only one is answered by changing a
    toggle; reporting the other as "options" sends the user to fiddle with
    settings that cannot help (#1570 F17).
    """
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    # 2 is live, so the group is not "fully uncertain" — but 1 never gets a
    # verdict, which is what leaves delete_ids empty and no repoint target.
    harness.romm.outcomes[1] = [RommConnectionError("offline")]
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])
    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"] == "liveness_uncertain"
    assert "could not confirm" in result["message"]
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_nothing_to_do_still_blames_the_options_when_liveness_was_certain(harness):
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g"),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    # Every verdict is certain; the user's own options are what excluded the row.
    await _start(harness, preview["preview_id"], remove_rows=False, repoint_shortcuts=False)
    complete = await _finish(harness)

    assert complete["results"][0]["reason"] == "options_excluded"


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_fully_vanished", [False, True])
async def test_partially_live_group_repoints_and_removes_under_every_toggle(harness, remove_fully_vanished):
    """A group with a live member must not depend on the whole-game option.

    F6 flipped remove_fully_vanished on by default, so this combination —
    bound vanished row + live sibling, every toggle on — became the feature's
    ordinary path. It was previously untested (#1570 F17).
    """
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=remove_fully_vanished,
    )
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)
    complete = await _finish(harness)

    assert complete["removed_rom_ids"] == [1]
    assert harness.uow.roms.get(1) is None
    retained = harness.uow.roms.get(2)
    assert retained is not None
    assert retained.shortcut_app_id == app_id


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
    assert action["preview_id"] == preview["preview_id"]
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
    assert complete["publication_required"] is True
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
@pytest.mark.parametrize("resume_after_claim", [False, True])
async def test_claimed_action_result_is_attached_before_request_task_reraises_cancellation(harness, resume_after_claim):
    request_task = asyncio.create_task(
        harness.service._request_action("run", "remove_shortcut", {"app_id": None}, None, None, {1})
    )
    action = await _wait_action(harness, "remove_shortcut")
    assert (await _claim_action(harness, action))["success"] is True
    if resume_after_claim:
        await asyncio.sleep(0)
    assert request_task.cancel()
    await asyncio.sleep(0)
    assert (await _complete_action(harness, action))["success"] is True

    with pytest.raises(asyncio.CancelledError) as caught:
        await request_task

    state = cancellation_state(caught.value)
    assert request_task.cancelled()
    assert state.action_result is not None
    assert state.action_result["success"] is True
    assert state.action_result["claimed"] is True


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

    run_task = _active_run_task(harness)
    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0.01)
    assert shutdown.done() is False
    harness.installed_remover.release.set()
    await shutdown
    await _assert_task_cancelled(run_task)

    assert harness.uow.roms.get(1) is None
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert complete["partial"] is True
    assert complete["removed_count"] == 1
    assert complete["results"][0]["status"] == "removed"


@pytest.mark.asyncio
async def test_shutdown_during_final_removed_progress_preserves_committed_ids(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    entered = asyncio.Event()
    original = harness.service._executor._results.emit_progress

    async def block_final_progress(run_id, index, total, stage, rows, **kwargs):
        if stage == "removed":
            entered.set()
            await asyncio.Future()
        await original(run_id, index, total, stage, rows, **kwargs)

    monkeypatch.setattr(harness.service._executor._results, "emit_progress", block_final_progress)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    await entered.wait()
    assert harness.uow.roms.get(1) is None

    run_task = _active_run_task(harness)
    await harness.service.shutdown()
    await _assert_task_cancelled(run_task)

    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert complete["partial"] is True
    assert complete["removed_count"] == 1
    assert complete["removed_rom_ids"] == [1]
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
    result = harness.service._executor._results.group_result(
        rows,
        "removed",
        None,
        "x" * 2000,
        GroupOutcome(removed_rom_ids=list(range(1, 61))),
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
    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2) is not None
    assert harness.uow.rom_installs.get(1) is not None
    assert harness.uow.rom_installs.get(2) is not None


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
    run_task = _active_run_task(harness)
    shutdown = asyncio.create_task(harness.service.shutdown())
    while harness.service._task is not None and harness.service._task.cancelling() == 0:
        await asyncio.sleep(0)
    await _complete_action(harness, capture, snapshot=_steam_snapshot(app_id))
    await shutdown
    await _assert_task_cancelled(run_task)

    assert harness.recovery.sealed == []
    assert harness.artifacts.removed == []
    assert harness.uow.roms.get(1) is not None


@pytest.mark.asyncio
async def test_server_switch_during_exact_404_never_authorizes_deletion(harness, monkeypatch):
    harness.service._settings.update(
        {
            "romm_url": "https://server-a.example",
            "romm_api_token_origin": "https://server-a.example",
            "romm_user_id": 10,
        }
    )
    _seed(harness.uow, _rom(1, fetch="old"))
    preview = await _preview(harness)

    def switched_namespace_404(_rom_id):
        harness.service._settings.update(
            {
                "romm_url": "https://server-b.example",
                "romm_api_token_origin": "https://server-b.example",
                "romm_user_id": 20,
            }
        )
        raise RommNotFoundError("server B has no matching numeric id")

    monkeypatch.setattr(harness.romm, "get_rom_once", switched_namespace_404)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    complete = await _finish(harness)

    assert complete["results"][0]["reason"] == "server_namespace_changed"
    assert harness.uow.roms.get(1) is not None
    assert harness.artifacts.removed == []


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
    run_task = _active_run_task(harness)
    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0.01)
    assert shutdown.done() is False
    release.set()
    await shutdown
    await _assert_task_cancelled(run_task)

    assert harness.uow.roms.get(1) is None
    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_shutdown_preserves_cancellation_when_shielded_finalization_faults(harness, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocked_failure(*_args):
        entered.set()
        assert release.wait(timeout=5)
        raise OSError("quarantine failed after cancellation")

    monkeypatch.setattr(harness.saves, "quarantine_prune_saves", blocked_failure)
    _seed(harness.uow, _rom(1, fetch="old"), _rom(2, fetch="old"), stamp_count=2)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)

    run_task = _active_run_task(harness)
    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0)
    release.set()
    await shutdown
    await _assert_task_cancelled(run_task)

    complete = [payload for name, payload in harness.events.events if name == "prune_complete"][-1]
    assert complete["reason"] == "cancelled"
    assert complete["results"][0]["status"] == "failed"
    assert "quarantine failed" in complete["results"][0]["message"]
    assert harness.uow.roms.get(1) is not None
    assert harness.uow.roms.get(2) is not None
    assert 2 not in harness.romm.calls


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

    run_task = _active_run_task(harness)
    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0)
    release.set()
    await shutdown
    await _assert_task_cancelled(run_task)

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

    run_task = _active_run_task(harness)
    await harness.service.shutdown()
    await _assert_task_cancelled(run_task)

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
@pytest.mark.parametrize("boundary", ["save", "artifacts"])
async def test_partial_adapter_outcomes_enter_actual_and_ambiguous_mutation_ledger(harness, monkeypatch, boundary):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    if boundary == "save":
        monkeypatch.setattr(
            harness.saves,
            "quarantine_prune_saves",
            lambda *_args: {
                "success": False,
                "moved": ["/saves/game.srm"],
                "ambiguous": True,
                "message": "save parent fsync failed",
            },
        )
        category = "save_quarantine"
    else:
        monkeypatch.setattr(
            harness.artifacts,
            "remove",
            lambda *_args: {
                "success": False,
                "changed": True,
                "ambiguous": True,
                "message": "artifact parent fsync failed",
            },
        )
        category = "plugin_artifacts"
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert category in result["mutations"]
    assert category in result["ambiguous_mutations"]


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
    assert result["warnings_omitted"] is True


@pytest.mark.asyncio
async def test_omitted_short_warnings_are_not_marked_as_display_truncated(harness):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.saves.warnings = [f"warning {index}" for index in range(6)]
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["warning_count"] == 6
    assert len(result["warnings"]) == 5
    assert result["warnings_omitted"] is True
    assert result["warnings_truncated"] is False


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

    run_task = _active_run_task(harness)
    shutdown = asyncio.create_task(harness.service.shutdown())
    await asyncio.sleep(0.01)
    assert shutdown.done() is False
    release.set()
    await shutdown
    await _assert_task_cancelled(run_task)

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
        return {"claims": {}, "bundle_digest": "sealed-digest"}

    monkeypatch.setattr(harness.recovery, "source_claims", mutate_after_seal)
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
async def test_claimed_repoint_timeout_is_an_explicit_ambiguous_partial(harness, monkeypatch):
    monkeypatch.setattr("services.prune.service._ACTION_TIMEOUT_SECONDS", 0.01)
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
    await _claim_action(harness, action)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["reason"] == "action_ambiguous"
    assert result["action_ambiguous"] is True
    assert result["committed_action"] == "repoint_shortcut"
    assert result["target_rom_id"] == 2


@pytest.mark.asyncio
async def test_attempted_unconfirmed_removal_is_an_explicit_ambiguous_partial(harness):
    app_id = 0x80000001
    _seed(harness.uow, _rom(1, fetch="old", app_id=app_id))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    action = await _wait_action(harness, "remove_shortcut")
    await _claim_action(harness, action)
    await harness.service.report_prune_action(
        {
            "phase": "complete",
            "run_id": action["run_id"],
            "action_token": action["action_token"],
            "success": False,
            "reason": "steam_action_failed",
            "message": "Steam removal was attempted but could not be confirmed.",
            "mutation_attempted": True,
        }
    )

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["reason"] == "action_ambiguous"
    assert result["action_ambiguous"] is True
    assert harness.uow.roms.get(1).shortcut_app_id == app_id


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
async def test_mixed_repoint_reprobes_non_candidate_vanished_source_after_action(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="new", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
        _rom(3, fetch="old", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone"), RommNotFoundError("gone"), {"id": 1}]
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    harness.romm.outcomes[3] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"])
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "partial"
    assert result["reason"] == "live"
    assert result["committed_action"] == "repoint_shortcut"
    assert harness.romm.calls.count(1) == 3
    assert harness.uow.roms.get(3) is not None


@pytest.mark.asyncio
async def test_zero_change_final_guard_reports_no_mutation_categories(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    monkeypatch.setattr(
        harness.artifacts,
        "remove",
        lambda *_args: {"success": True, "changed": False, "ambiguous": False, "message": "none"},
    )
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

    harness.service._executor._results._emit = fail_removed_progress
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert complete["removed_rom_ids"] == [1]
    assert complete["results"][0]["status"] == "removed"
    assert harness.uow.roms.get(1) is None


@pytest.mark.asyncio
async def test_purge_without_a_steam_action_validates_sealed_sources_once_before_deletion(harness, monkeypatch):
    _seed(harness.uow, _rom(1, fetch="old"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    order: list[str] = []

    def record_validation(_bundle_path, _bundle_digest=None):
        order.append("validate_sources")
        return True

    def record_removal(rom_ids, _claims=None):
        order.append("artifact_removal")
        return {"success": True, "changed": bool(rom_ids), "ambiguous": False, "message": "removed"}

    monkeypatch.setattr(harness.recovery, "validate_sources", record_validation)
    monkeypatch.setattr(harness.artifacts, "remove", record_removal)
    preview = await _preview(harness)
    await _start(
        harness,
        preview["preview_id"],
        remove_fully_vanished=True,
        create_recovery_bundle=True,
    )

    complete = await _finish(harness)

    assert complete["results"][0]["status"] == "removed"
    assert harness.uow.roms.get(1) is None
    assert order == ["validate_sources", "artifact_removal"]


@pytest.mark.asyncio
async def test_repoint_revalidates_sealed_sources_before_any_steam_action(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    harness.recovery.sources_valid = False
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], create_recovery_bundle=True)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"] == "recovery_state_changed"
    assert not any(name == "prune_action_required" for name, _payload in harness.events.events)
    assert harness.switch_calls == []
    assert harness.uow.roms.get(1).shortcut_app_id == app_id
    assert harness.uow.roms.get(2).shortcut_app_id is None


@pytest.mark.asyncio
async def test_repoint_without_drift_never_bypasses_the_save_stranding_guard(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_rows=False)
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)

    complete = await _finish(harness)

    assert complete["results"][0]["status"] == "repointed"
    assert harness.switch_calls == [{"app_id": app_id, "target_rom_id": 2, "allow_stranded": False}]


@pytest.mark.asyncio
async def test_drifted_repoint_bypasses_the_stranding_guard_only_after_the_bundle_sealed(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    harness.drifted.add(1)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_rows=False, create_recovery_bundle=True)
    action = await _wait_action(harness, "repoint_shortcut")
    await _claim_action(harness, action)
    await _complete_action(harness, action)

    complete = await _finish(harness)

    assert complete["results"][0]["status"] == "repointed"
    assert harness.recovery.sealed
    assert harness.switch_calls == [{"app_id": app_id, "target_rom_id": 2, "allow_stranded": True}]


@pytest.mark.asyncio
async def test_drifted_repoint_is_skipped_when_recovery_is_disabled(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    harness.drifted.add(1)
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_rows=False)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"] == "unsynced_saves"
    assert harness.switch_calls == []
    assert harness.recovery.sealed == []
    assert harness.uow.roms.get(1).shortcut_app_id == app_id


@pytest.mark.asyncio
async def test_drifted_repoint_is_skipped_when_the_recovery_bundle_fails(harness):
    app_id = 0x80000001
    _seed(
        harness.uow,
        _rom(1, fetch="old", group="g", app_id=app_id),
        _rom(2, fetch="new", group="g"),
    )
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [{"id": 2}] * 3
    harness.drifted.add(1)
    harness.recovery.failure = OSError("bundle seal failed")
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_rows=False, create_recovery_bundle=True)

    complete = await _finish(harness)

    result = complete["results"][0]
    assert result["status"] == "failed"
    assert result["reason"] == "recovery_failed"
    assert harness.switch_calls == []
    assert harness.recovery.sealed == []
    assert harness.uow.roms.get(1).shortcut_app_id == app_id


@pytest.mark.asyncio
@pytest.mark.parametrize("fully_dead", [False, True])
async def test_unbound_group_plans_no_steam_action_and_takes_no_steam_path(harness, fully_dead):
    """No planned Steam action must mean no Steam mutation is reachable.

    The early recovery revalidation is skipped precisely when the run plans no
    Steam action, on the grounds that nothing irreversible then happens before
    ``_finish_group``'s own pre-mutation check. That reasoning holds only while
    the skip condition and the two Steam branches stay in step, so this pins
    the correspondence from both sides: with no bound shortcut, neither the
    repoint branch (a live sibling to move to) nor the whole-game branch (every
    member vanished) may reach the version switcher or raise a shortcut action.
    """
    _seed(harness.uow, _rom(1, fetch="old", group="g"), _rom(2, fetch="new", group="g"))
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    harness.romm.outcomes[2] = [RommNotFoundError("gone")] * 3 if fully_dead else [{"id": 2}] * 3
    preview = await _preview(harness)
    await _start(harness, preview["preview_id"], remove_fully_vanished=fully_dead)

    complete = await _finish(harness)

    assert complete["results"][0]["status"] != "failed"
    assert harness.switch_calls == []
    assert [name for name, _ in harness.events.events if name == "prune_action_required"] == []
    assert harness.uow.roms.get(1) is None
    assert (harness.uow.roms.get(2) is None) is fully_dead


@pytest.mark.asyncio
async def test_inline_purge_keeps_the_platform_stamp_and_still_denies_the_incremental_skip(harness):
    _seed(harness.uow, _rom(1, fetch="new"), _rom(2, fetch="new"), stamp_count=2)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness, scope="rom", rom_id=1)
    await _start(harness, preview["preview_id"], remove_fully_vanished=True)

    complete = await _finish(harness)

    assert complete["removed_rom_ids"] == [1]
    assert harness.uow.roms.get(1) is None
    stamp = harness.uow.platform_sync_state.get("dc")
    assert stamp is not None
    assert (stamp.fetch_id, stamp.rom_count) == ("new", 2)
    # Scoped to the stamped platform, because that is what the stamp describes.
    remaining = list(harness.uow.roms.iter_by_platform("dc"))
    # The purge removed a row carrying the stamp's generation, so the fetcher's
    # own conditions must still deny the skip: the stamped server count no longer
    # matches the count the server now reports (1), which forces the full fetch.
    assert count_rows_for_skip(remaining, stamp.fetch_id) == 1
    assert stamp.rom_count != 1
    # The retained stamp keeps bulk discovery available and claims nothing new.
    assert prune_candidate_ids(remaining, stamp) == set()


@pytest.mark.asyncio
async def test_release_wait_times_out_while_a_run_still_holds_the_claim(harness, monkeypatch):
    monkeypatch.setattr("services.prune.service._RELEASE_TIMEOUT_SECONDS", 0.01)
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
    harness.installed_remover.block_ids.add(1)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    while not harness.installed_remover.entered.is_set():
        await asyncio.sleep(0)

    assert await harness.service.wait_for_prune_release(started["run_id"]) == {
        "success": False,
        "reason": "release_timeout",
        "message": "Cleanup claim release was not observed in time.",
    }

    harness.installed_remover.release.set()
    await _finish(harness)


@pytest.mark.asyncio
async def test_release_wait_returns_once_the_run_releases_the_claim(harness):
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
    harness.installed_remover.block_ids.add(1)
    harness.romm.outcomes[1] = [RommNotFoundError("gone")] * 3
    preview = await _preview(harness)
    started = await _start(harness, preview["preview_id"], remove_fully_vanished=True)
    while not harness.installed_remover.entered.is_set():
        await asyncio.sleep(0)

    waiter = asyncio.create_task(harness.service.wait_for_prune_release(started["run_id"]))
    await asyncio.sleep(0)
    assert waiter.done() is False

    harness.installed_remover.release.set()
    assert await waiter == {"success": True, "message": "Cleanup claim is released."}
    assert harness.service.is_active() is False
    await _finish(harness)
