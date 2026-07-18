"""Contract tests for the ``finalize_game_session`` end-of-session callable.

Driven frontend-shaped per ``src/api/backend.ts``: ``finalize_game_session``
takes ONE positional arg — the RomM ROM id — and returns the
``SessionFinalizeResult`` dict (``total_seconds`` / ``sync`` / ``migration``).

The suspend-exclusion cases (#1148) are the regression guard: the counted
``total_seconds`` is the AWAKE span derived from the monotonic clock — wall time
that elapsed while the device was suspended (the monotonic clock pauses) is not
counted. The zero-suspend case is the control proving the exclusion is a real,
monotonic-driven difference.

A session is opened via the real ``record_session_start`` callable (which stamps
both the wall and monotonic starts from the deterministic ``FakeClock``); the
clock is then advanced before finalize stamps the end — ``advance`` for awake
time (both clocks) and ``advance_wall`` for suspend (wall only, monotonic frozen).
"""

from __future__ import annotations

from ._seed import seed_rom


async def test_finalize_excludes_suspend_via_monotonic(harness):
    """180s awake + 120s suspended (300s wall) → only the 180s awake span counts."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance(180)  # 180s awake (both clocks)
    harness.clock.advance_wall(120)  # 120s suspended (wall only, monotonic paused)

    result = await harness.plugin.finalize_game_session(1)

    assert result["total_seconds"] == 180


async def test_finalize_zero_suspend_counts_full_span(harness):
    """Control: 300s elapsed with no suspend → the full 300s counts."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance(300)

    result = await harness.plugin.finalize_game_session(1)

    assert result["total_seconds"] == 300


async def test_finalize_fully_suspended_session_counts_zero(harness):
    """A session suspended the whole time (monotonic never advanced) counts 0."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance_wall(600)  # 10 min of wall, monotonic frozen throughout

    result = await harness.plugin.finalize_game_session(1)

    assert result["total_seconds"] == 0


async def test_finalize_no_active_session_leaves_total_none(harness):
    """No open session → playtime record fails → ``total_seconds`` is ``None``."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False

    result = await harness.plugin.finalize_game_session(1)

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
    await harness.plugin.finalize_game_session(1)

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
    result = await harness.plugin.finalize_game_session(1)

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
    result = await harness.plugin.finalize_game_session(1)

    assert result["total_seconds"] == 300  # counted locally
    assert harness.romm.play_sessions == {}  # nothing ingested


async def test_finalize_sync_payload_carries_counts_not_toast_keys(harness):
    """The ``sync`` payload carries per-direction counts + ``failure_toast``; the
    removed backend-rendered ``toast_title`` / ``toast_body`` keys are gone (#1481)."""
    seed_rom(harness, 1)
    harness.plugin.settings["save_sync_enabled"] = False
    await harness.plugin.record_session_start(1)
    harness.clock.advance(60)

    result = await harness.plugin.finalize_game_session(1)

    sync = result["sync"]
    assert set(sync.keys()) == {
        "offline",
        "success",
        "synced",
        "uploaded",
        "downloaded",
        "conflicts",
        "failure_toast",
        "conflicts_toast",
    }
    # The directional copy is rendered frontend-side now — no backend toast strings.
    assert "toast_title" not in sync
    assert "toast_body" not in sync
