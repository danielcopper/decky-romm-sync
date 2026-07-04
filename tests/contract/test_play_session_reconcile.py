"""Contract tests for ``reconcile_playtime`` — the flush → GET → max() path.

Driven frontend-shaped per ``src/api/backend.ts``: ``reconcile_playtime`` takes
one positional ROM id and returns ``{total_seconds, session_count, server_query_failed}``.

The reconcile drains the local outbox into RomM's native ``/api/play-sessions``
ingest, then reads the ROM's cross-device session union back and folds its summed
duration into the local total via ``max()`` (playtime is monotonic — the pull
never regresses local play). These cases exercise that through the real harness:
the outbox is flushed, a foreign device's history is unioned in, and an
unreachable GET degrades to the local total.
"""

from __future__ import annotations

from domain.playtime import PendingPlaySession, Playtime
from lib.errors import RommApiError, RommForbiddenError

from ._seed import seed_rom


async def _register(harness, device_id: str = "device-1") -> None:
    with harness.uow_factory() as uow:
        uow.kv_config.set("device_id", device_id)


async def test_reconcile_flushes_outbox_then_unions_server(harness):
    """A locally-recorded session is flushed, unioned with a foreign device's, and max'd in."""
    seed_rom(harness, 1)
    await _register(harness)
    harness.plugin.settings["save_sync_enabled"] = False

    # Record a 300s local session (folded + ingested on exit).
    await harness.plugin.record_session_start(1)
    harness.clock.advance(300)
    await harness.plugin.finalize_game_session(1, 0)

    # A foreign device already holds 500s on the server for this ROM.
    harness.romm.play_sessions.setdefault(1, []).append(
        {"id": 9000, "rom_id": 1, "device_id": "device-B", "duration_ms": 500_000}
    )

    result = await harness.plugin.reconcile_playtime(1)

    # Server union is our 300s + the foreign 500s = 800s; max(local 300, 800) = 800.
    assert result["total_seconds"] == 800
    assert result["server_query_failed"] is False
    # session_count folds in as len(server rows) = local + foreign = 2; last_played is
    # the newest server end_time (only our ingested session carries one here).
    local_end = next(s["end_time"] for s in harness.romm.play_sessions[1] if s.get("end_time"))
    assert result["session_count"] == 2
    assert result["last_played"] == local_end


async def test_reconcile_restores_session_count_and_last_played_from_server(harness):
    """A fresh device with no local play restores session_count + last_played from the server union (#903)."""
    seed_rom(harness, 1)
    await _register(harness)
    harness.plugin.settings["save_sync_enabled"] = False

    # The server already holds a prior device's two sessions — the cutover history.
    harness.romm.play_sessions[1] = [
        {"id": 9001, "rom_id": 1, "device_id": "device-B", "duration_ms": 600_000, "end_time": "2026-02-01T10:00:00Z"},
        {"id": 9002, "rom_id": 1, "device_id": "device-B", "duration_ms": 300_000, "end_time": "2026-02-03T10:00:00Z"},
    ]

    result = await harness.plugin.reconcile_playtime(1)

    assert result["total_seconds"] == 900  # 600s + 300s union
    assert result["session_count"] == 2  # restored from the server rows, not 0
    assert result["last_played"] == "2026-02-03T10:00:00Z"  # newest server end_time
    assert result["server_query_failed"] is False


async def test_reconcile_local_ahead_is_not_regressed(harness):
    """When local play exceeds the server union, reconcile never lowers the total."""
    seed_rom(harness, 1)
    await _register(harness)
    harness.plugin.settings["save_sync_enabled"] = False

    await harness.plugin.record_session_start(1)
    harness.clock.advance(900)  # 900s local
    await harness.plugin.finalize_game_session(1, 0)

    # Server only knows about our 900s session (ingested on exit) — no foreign play.
    result = await harness.plugin.reconcile_playtime(1)

    assert result["total_seconds"] == 900
    assert result["server_query_failed"] is False


async def test_reconcile_server_unreachable_keeps_local_total(harness):
    """A failed GET degrades to local-only with the partial-success flag set."""
    seed_rom(harness, 1)
    await _register(harness)
    harness.plugin.settings["save_sync_enabled"] = False

    await harness.plugin.record_session_start(1)
    harness.clock.advance(120)
    await harness.plugin.finalize_game_session(1, 0)  # local total 120s

    harness.romm.list_play_sessions_side_effect = RommApiError("offline")

    result = await harness.plugin.reconcile_playtime(1)

    assert result["server_query_failed"] is True
    assert result["total_seconds"] == 120


async def test_queued_poison_session_is_healed_on_flush(harness):
    """An already-queued sub-second poison row (a legacy pre-filter enqueue) no longer wedges the outbox (#1312).

    RomM 422s the whole batch on the zero-duration entry; the flush drops exactly
    that entry and resubmits the survivor, which ingests and reconciles.
    """
    seed_rom(harness, 1)
    await _register(harness)
    harness.plugin.settings["save_sync_enabled"] = False

    # Seed the outbox directly with a poison + a healthy row (bypassing the
    # recording seam, which now filters sub-second windows at enqueue).
    with harness.uow_factory() as uow:
        uow.playtime.save(
            1,
            Playtime(
                pending_sessions={
                    "2026-07-04T10:00:00Z": PendingPlaySession(
                        device_id="device-1", end_time="2026-07-04T10:00:00Z", duration_ms=0
                    ),
                    "2026-07-04T11:00:00Z": PendingPlaySession(
                        device_id="device-1", end_time="2026-07-04T11:20:00Z", duration_ms=1_200_000
                    ),
                }
            ),
        )
    # RomM rejects the whole POST while any entry is below one second.
    harness.romm.reject_batch_below_duration_ms = 1

    result = await harness.plugin.reconcile_playtime(1)

    # The outbox is fully drained: poison dropped, survivor ingested.
    with harness.uow_factory() as uow:
        entry = uow.playtime.get(1)
    assert entry is not None
    assert entry.pending_sessions == {}
    # Only the healthy 1200s session reached the server; reconcile unions it in.
    assert [s["duration_ms"] for s in harness.romm.play_sessions[1]] == [1_200_000]
    assert result["total_seconds"] == 1200
    assert result["server_query_failed"] is False


async def test_forbidden_reconcile_raises_scope_notice_then_clears(harness):
    """A 403 GET (token lacks roms.user.read) sets the durable notice; a later 200 clears it."""
    seed_rom(harness, 1)
    await _register(harness)
    harness.plugin.settings["save_sync_enabled"] = False

    # A token predating the roms.user.read scope: the reconcile GET is forbidden.
    harness.romm.list_play_sessions_side_effect = RommForbiddenError("token lacks roms.user.read")

    result = await harness.plugin.reconcile_playtime(1)

    assert result["server_query_failed"] is True
    assert await harness.plugin.get_playtime_scope_notice() == {"pending": True}

    # The next sign-in (or scope grant) lets the GET succeed — the notice clears.
    harness.romm.list_play_sessions_side_effect = None
    await harness.plugin.reconcile_playtime(1)

    assert await harness.plugin.get_playtime_scope_notice() == {"pending": False}
