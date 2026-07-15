"""Contract test for the unstamped-platform re-run (#1416).

After a heartbeat-timeout run's late ack recovers a chunk (#1367 / #1409), the
platform is complete but carries no ``PlatformSyncState`` stamp — the late-ack
path never stamps (ADR-0023). The next preview finds a zero shortcut delta, so
without a re-run signal the frontend short-circuits: the platform never
re-stamps (every future sync full-fetches it) and "Last sync: interrupted"
lingers indefinitely.

This drives the real Plugin through the real bootstrap: it builds that
complete-but-unstamped residue with real verbs (the repository stamp delete a
timeout leaves un-rewritten, plus a ``SyncRun.mark_interrupted`` newest attempt),
asserts the preview surfaces ``restamp_platform_count``, then asserts the gated
apply re-stamps the platform and records a fresh completed ``SyncRun`` that heals
the run status.
"""

from __future__ import annotations

import asyncio

from domain.sync_run import SyncRun
from domain.sync_state import SyncState

_ONE_DAY_SEC = 86400


def _orchestrator(harness):
    return harness.plugin._sync_service._orchestrator


def _ack_with(bindings):
    """A ``_wait_for_unit_complete`` stand-in acking with *bindings*."""

    async def _wait(_unit, event):
        event.set()
        return dict(bindings)

    return _wait


def _make_grid_resolvable(harness) -> None:
    """Materialise a Steam userdata dir so ``grid_dir()`` resolves for cover finalise."""
    (harness.tmp_path / "home" / ".steam" / "steam" / "userdata" / "12345").mkdir(parents=True)


def _seed_library(harness) -> None:
    harness.romm.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
    harness.romm.roms[10] = {
        "id": 10,
        "name": "Game",
        "fs_name": "game.z64",
        "platform_id": 1,
        "platform_name": "N64",
        "platform_slug": "n64",
    }
    harness.plugin.settings["enabled_platforms"] = {"1": True}


async def _run_sync(harness, run_id: str) -> None:
    assert harness.plugin._sync_service._box.try_begin_run(run_id) is True
    await _orchestrator(harness)._do_sync_per_unit()


async def _drain_apply(harness, tries: int = 5000) -> None:
    for _ in range(tries):
        if harness.plugin._sync_service._sync_state is SyncState.IDLE:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("sync_apply_delta's background apply task never finished")


async def test_unstamped_platform_rerun_restamps_and_heals_run_status(harness):
    _make_grid_resolvable(harness)
    _seed_library(harness)

    # Seeding run at t0: the platform is stamped and the ROM bound.
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({"10": 7777})
    await _run_sync(harness, "run-seed")
    with harness.uow_factory() as uow:
        assert uow.platform_sync_state.get("n64") is not None

    # Reproduce the ADR-0023 late-ack residue: the apply-start stamp clear a
    # heartbeat timeout then leaves un-rewritten (bound rows, no stamp), plus the
    # timed-out run surfaced as the newest 'interrupted' attempt. Both are built
    # with real verbs — the repository delete and SyncRun.mark_interrupted.
    harness.clock.advance(_ONE_DAY_SEC)  # t1
    with harness.uow_factory() as uow:
        uow.platform_sync_state.delete("n64")
    interrupted = SyncRun.start(id="run-timeout", at="2026-01-01T00:00:00", platforms_planned=1, roms_planned=1)
    interrupted.mark_interrupted(harness.clock.now().isoformat(), "Sync interrupted (Steam UI stopped responding)")
    with harness.uow_factory() as uow:
        uow.sync_runs.save(interrupted)

    # Pre-state: no stamp, and the lingering interrupted attempt.
    with harness.uow_factory() as uow:
        assert uow.platform_sync_state.get("n64") is None
    stats = await harness.plugin.get_sync_stats()
    assert stats["last_attempt"]["status"] == "interrupted"

    # The next preview surfaces the re-stamp need with an otherwise-empty delta,
    # so the frontend offers Apply instead of short-circuiting on "no changes".
    harness.clock.advance(_ONE_DAY_SEC)  # t2 — the heal run becomes the newest terminal
    preview = await harness.plugin.sync_preview()
    assert preview["success"] is True
    summary = preview["summary"]
    assert summary["restamp_platform_count"] == 1
    assert summary["new_count"] == 0
    assert summary["changed_count"] == 0
    assert summary["remove_count"] == 0
    assert summary["cover_refresh_count"] == 0

    # The gated apply's empty chunk re-stamps the platform and records a fresh
    # completed SyncRun. The empty chunk acks nothing (no shortcut to bind).
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({})
    apply_result = await harness.plugin.sync_apply_delta(preview["preview_id"])
    assert apply_result == {"success": True, "message": "Applying changes"}
    await _drain_apply(harness)

    with harness.uow_factory() as uow:
        stamp = uow.platform_sync_state.get("n64")
        assert stamp is not None
        assert stamp.rom_count == 1
        completed = uow.sync_runs.get_latest_completed()
        assert completed is not None
        assert completed.id not in ("run-seed", "run-timeout")

    # The interrupted attempt no longer lingers — the fresh completed run heals it.
    healed = await harness.plugin.get_sync_stats()
    assert healed["last_attempt"] is None
