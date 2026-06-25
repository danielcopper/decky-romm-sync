"""Contract tests for run-scoped ``cancel_sync`` (#1198).

A Cancel click meant for run A can land after run A finalized to IDLE and run
B started fresh. The argument-less cancel would flip run B to CANCELLING — a
sync the user never cancelled — so it would abort and report cancelled. The
run-scoped cancel ignores a stale run id; an unscoped (falsy) cancel still
cancels unconditionally.

Driven through the real ``cancel_sync`` callable over the real wired plugin,
asserting the response shape and the downstream effect on run B's terminal
``sync_complete`` event.
"""

from __future__ import annotations

from domain.sync_state import SyncState


def _orchestrator(harness):
    return harness.plugin._sync_service._orchestrator


async def _ack_immediately(_unit, event):
    """Stand-in for ``_wait_for_unit_complete``: ack with an empty map.

    The frontend's ``report_unit_results`` callback never runs in the contract
    tier; this lets run B's per-unit pipeline complete deterministically so its
    terminal ``sync_complete`` can be asserted.
    """
    event.set()
    return {}


def _sync_complete_payloads(harness):
    return [c.args[1] for c in harness.emit.call_args_list if c.args and c.args[0] == "sync_complete"]


async def test_cancel_sync_shape_when_idle(harness):
    """Idle: the callable returns the success-shaped no-op (not a failure shape)."""
    result = await harness.plugin.cancel_sync("any-run")
    assert result == {"success": True, "message": "No sync in progress"}


async def test_cancel_sync_stale_run_does_not_abort_fresh_run(harness):
    """The #1198 repro: run-A's cancel must not abort the fresh run-B.

    Start run A and capture its run id; finalize A to IDLE (id nulled); start
    run B with a fresh id; then deliver run A's Cancel click. Run B must run to
    completion with ``cancelled`` absent from its ``sync_complete`` payload.
    """
    # Seed one platform so run B has a real unit to process.
    harness.romm.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
    harness.romm.roms[10] = {
        "id": 10,
        "name": "Game",
        "platform_id": 1,
        "platform_name": "N64",
        "platform_slug": "n64",
    }
    harness.plugin.settings["enabled_platforms"] = {"1": True}

    orch = _orchestrator(harness)
    orch._wait_for_unit_complete = _ack_immediately

    # Run A: start through the real callable. The deterministic FakeUuidGen mints
    # a fixed id, so pin run A's id explicitly to model the cross-run race
    # (run A's id must differ from run B's).
    run_a_id = "run-A"
    start_a = await harness.plugin.start_sync()
    assert start_a["success"] is True
    harness.plugin._sync_service._current_sync_id = run_a_id

    # Finalize run A to IDLE exactly as the lifecycle does (id nulled).
    await orch._finish_sync("Sync cancelled")
    assert harness.plugin._sync_service._sync_state == SyncState.IDLE
    assert harness.plugin._sync_service._current_sync_id is None

    # Run B: a fresh run with a distinct id.
    run_b_id = "run-B"
    start_b = await harness.plugin.start_sync()
    assert start_b["success"] is True
    harness.plugin._sync_service._current_sync_id = run_b_id

    # Run A's Cancel click lands now — it must be ignored as stale.
    cancel = await harness.plugin.cancel_sync(run_a_id)
    assert cancel == {"success": True, "message": "Cancel ignored (stale run)"}
    assert harness.plugin._sync_service._sync_state == SyncState.RUNNING

    # Drive run B to completion. It was never cancelled.
    await orch._do_sync_per_unit()

    completes = _sync_complete_payloads(harness)
    assert completes, "run B must emit a terminal sync_complete"
    assert "cancelled" not in completes[-1]
    assert harness.plugin._sync_service._sync_state == SyncState.IDLE


async def test_cancel_sync_matching_run_aborts_it(harness):
    """A cancel that matches the active run id flips it to CANCELLING."""
    harness.plugin._sync_service._sync_state = SyncState.RUNNING
    harness.plugin._sync_service._current_sync_id = "run-B"

    cancel = await harness.plugin.cancel_sync("run-B")
    assert cancel == {"success": True, "message": "Sync cancelling..."}
    assert harness.plugin._sync_service._sync_state == SyncState.CANCELLING


async def test_cancel_sync_empty_run_id_cancels_unconditionally(harness):
    """The frontend's no-id-yet fallback (empty string) always cancels."""
    harness.plugin._sync_service._sync_state = SyncState.RUNNING
    harness.plugin._sync_service._current_sync_id = "run-B"

    cancel = await harness.plugin.cancel_sync("")
    assert cancel == {"success": True, "message": "Sync cancelling..."}
    assert harness.plugin._sync_service._sync_state == SyncState.CANCELLING
