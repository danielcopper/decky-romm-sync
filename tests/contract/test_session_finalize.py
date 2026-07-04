"""Contract tests for the ``finalize_game_session`` end-of-session callable.

Driven frontend-shaped per ``src/api/backend.ts``: ``finalize_game_session``
takes TWO positional args — the RomM ROM id and the device-suspend wall-clock
(seconds) the frontend's ``sessionManager`` accumulated during the session —
and returns the ``SessionFinalizeResult`` dict (``total_seconds`` / ``sync`` /
``migration``).

The suspend-subtraction cases (#1051 "decision C") are the regression guard:
the counted ``total_seconds`` is the raw server-clock elapsed span MINUS the
suspended wall-clock, clamped at ``[0, 24h]``. The zero-suspend case is the
control proving the subtraction is a real, arg-driven difference.

A session is opened via the real ``record_session_start`` callable (which
stamps ``last_session_start`` from the deterministic ``FakeClock``); the clock
is then advanced to fix the raw elapsed span before finalize stamps the end.
"""

from __future__ import annotations

from ._seed import seed_rom


async def test_finalize_subtracts_suspended_seconds(harness):
    """Raw elapsed 300s minus 120s suspended -> 180s counted."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance(300)  # 5 min of wall-clock elapsed

    result = await harness.plugin.finalize_game_session(1, 120)

    assert result["total_seconds"] == 180


async def test_finalize_zero_suspend_counts_full_span(harness):
    """Control: 300s elapsed, 0 suspended → full 300s counted."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance(300)

    result = await harness.plugin.finalize_game_session(1, 0)

    assert result["total_seconds"] == 300


async def test_finalize_over_subtraction_clamps_to_zero(harness):
    """Suspend exceeding the elapsed span clamps the counted duration to 0."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance(60)  # 60s elapsed

    result = await harness.plugin.finalize_game_session(1, 600)  # 600s suspended

    assert result["total_seconds"] == 0


async def test_finalize_no_active_session_leaves_total_none(harness):
    """No open session → playtime record fails → ``total_seconds`` is ``None``."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False

    result = await harness.plugin.finalize_game_session(1, 0)

    assert result["total_seconds"] is None


async def test_finalize_registered_device_ingests_play_session(harness):
    """A registered device POSTs the closed session to RomM's native ingest on exit.

    Playtime ingest is decoupled from save-sync (ADR-0018): save_sync stays OFF
    here, yet the session is recorded for the ROM because a device id is bound.
    """
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    with harness.uow_factory() as uow:
        uow.kv_config.set("device_id", "device-1")

    await harness.plugin.record_session_start(1)
    harness.clock.advance(300)
    await harness.plugin.finalize_game_session(1, 0)

    stored = harness.romm.play_sessions.get(1)
    assert stored is not None
    assert stored[0]["device_id"] == "device-1"
    assert stored[0]["duration_ms"] == 300_000


async def test_finalize_server_rejected_session_drains_outbox(harness):
    """A session the server acknowledges but refuses (``skipped``) drains, not retried.

    Regression guard for the infinite-retry loop: a sub-second launch-death gets
    a ``skipped`` verdict; the outbox row must drop (empty outbox = nothing to
    re-flush) rather than being retained and re-POSTed on every heartbeat.
    """
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    with harness.uow_factory() as uow:
        uow.kv_config.set("device_id", "device-1")
    harness.romm.reject_below_duration_ms = 1000  # server refuses sub-1s sessions

    await harness.plugin.record_session_start(1)
    # No clock advance → a 0ms session the server rejects.
    result = await harness.plugin.finalize_game_session(1, 0)

    assert result["total_seconds"] == 0  # still folded locally
    assert harness.romm.play_sessions == {}  # server stored nothing
    with harness.uow_factory() as uow:
        entry = uow.playtime.get(1)
    assert entry is not None
    assert entry.pending_sessions == {}  # drained — no infinite retry


async def test_finalize_unregistered_device_folds_locally_no_ingest(harness):
    """No device id → the session is counted locally but never POSTed (decision #8)."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    # No device_id in kv_config — the device is unregistered.

    await harness.plugin.record_session_start(1)
    harness.clock.advance(300)
    result = await harness.plugin.finalize_game_session(1, 0)

    assert result["total_seconds"] == 300  # counted locally
    assert harness.romm.play_sessions == {}  # nothing ingested
