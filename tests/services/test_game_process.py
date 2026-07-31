"""Unit tests for ``GameProcessService`` — the stop-game escalation ladder.

The ladder's safety rule is that a still-alive process is NEVER asked to stop a
second time (emulators skip their save flush on the repeat and destroy the save
file); the only escalation is the force kill after the grace window. The rule
has two halves — no retry inside one call, and no second concurrent call — and
:class:`TestNeverRepeatsTheStopRequest` guards both. Neither may be weakened.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from fakes.fake_game_process_control import FakeGameProcessControlAdapter
from fakes.system_time import FakeSleeper

from services.game_process import (
    _GRACE_POLL_SECONDS,
    _GRACE_POLLS,
    GameProcessService,
    GameProcessServiceConfig,
)

APP_ID = "net.retrodeck.retrodeck"


class _YieldingSleeper:
    """``Sleeper`` that hands control back to the event loop without real delay.

    ``FakeSleeper`` returns without ever suspending, so two coroutines driven
    through it never interleave — a concurrency test built on it would run the
    two calls strictly one after the other and pass vacuously. This one awaits a
    zero-delay ``asyncio.sleep``, so a second call genuinely arrives while the
    first is parked inside its grace window.
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


def _make_service(
    control: FakeGameProcessControlAdapter,
    sleeper: FakeSleeper | _YieldingSleeper | None = None,
) -> GameProcessService:
    return GameProcessService(
        config=GameProcessServiceConfig(
            game_process=control,
            sleeper=sleeper or FakeSleeper(),
            logger=logging.getLogger("test_game_process"),
            log_debug=MagicMock(),
            flatpak_app_id=APP_ID,
        ),
    )


class TestNothingRunning:
    @pytest.mark.asyncio
    async def test_returns_the_canonical_not_running_failure(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[])

        result = await _make_service(control).stop_running_game()

        assert result["success"] is False
        assert result["reason"] == "not_running"
        assert isinstance(result["message"], str)
        assert result["message"]
        # Canonical failure shape only — never the forbidden legacy keys.
        assert "error" not in result
        assert "error_code" not in result

    @pytest.mark.asyncio
    async def test_signals_nothing_at_all(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[])

        await _make_service(control).stop_running_game()

        assert control.stop_calls == []
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_looks_the_game_up_by_the_configured_flatpak_app_id(self) -> None:
        # The service must ask about RetroDECK, not some other flatpak — a
        # mismatched id resolves to nothing on the fake.
        control = FakeGameProcessControlAdapter(pids=[101], app_id="org.videolan.VLC")

        result = await _make_service(control).stop_running_game()

        assert control.find_calls == [APP_ID]
        assert result["reason"] == "not_running"


class TestStopRequestAlone:
    @pytest.mark.asyncio
    async def test_a_cooperative_process_is_never_force_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102])

        result = await _make_service(control).stop_running_game()

        assert result == {"success": True, "stopped": 2, "force_killed": 0}
        assert control.stop_calls == [101, 102]
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_the_ladder_stops_polling_as_soon_as_everything_has_exited(self) -> None:
        # A well-behaved emulator costs one poll interval, not the whole window.
        control = FakeGameProcessControlAdapter(pids=[101])
        sleeper = FakeSleeper()

        await _make_service(control, sleeper).stop_running_game()

        assert sleeper.calls == [_GRACE_POLL_SECONDS]

    @pytest.mark.asyncio
    async def test_processes_are_signalled_in_the_order_the_adapter_reports_them(self) -> None:
        # Deepest-first is the adapter's contract; the service must not reorder
        # it — the emulator has to be asked before its shell wrappers die.
        control = FakeGameProcessControlAdapter(pids=[103, 102, 101])

        await _make_service(control).stop_running_game()

        assert control.stop_calls == [103, 102, 101]


class TestEscalationToForceKill:
    @pytest.mark.asyncio
    async def test_a_process_that_ignores_the_stop_request_is_force_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}

        result = await _make_service(control).stop_running_game()

        assert result == {"success": True, "stopped": 1, "force_killed": 1}
        assert control.kill_calls == [101]

    @pytest.mark.asyncio
    async def test_only_the_survivors_are_force_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102, 103])
        control.survive_stop = {102}

        result = await _make_service(control).stop_running_game()

        assert control.kill_calls == [102]
        assert result["force_killed"] == 1

    @pytest.mark.asyncio
    async def test_the_full_grace_window_is_waited_out_before_forcing(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}
        sleeper = FakeSleeper()

        await _make_service(control, sleeper).stop_running_game()

        assert sleeper.calls == [_GRACE_POLL_SECONDS] * _GRACE_POLLS

    @pytest.mark.asyncio
    async def test_a_process_that_exits_late_in_the_window_is_not_force_killed(self) -> None:
        # Alive for the first few polls, gone before the window expires — a slow
        # save flush must never be force-killed.
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}
        real_is_alive = control.is_alive

        def _alive_for_three_polls(pid: int) -> bool:
            still = real_is_alive(pid)
            if len(control.alive_calls) >= 3:
                control.alive.discard(pid)
            return still

        control.is_alive = _alive_for_three_polls  # type: ignore[method-assign]

        result = await _make_service(control).stop_running_game()

        assert control.kill_calls == []
        assert result["force_killed"] == 0

    @pytest.mark.asyncio
    async def test_a_force_kill_that_cannot_be_delivered_is_not_counted(self) -> None:
        # The process took the stop request, survived the grace window, and then
        # vanished before the force kill landed (or belongs to another user).
        # ``unkillable`` — not ``unsignalable`` — so the stop request still
        # succeeds and the pid actually reaches the force rung.
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}
        control.unkillable = {101}

        result = await _make_service(control).stop_running_game()

        # The force kill was genuinely attempted and genuinely reported False.
        assert control.kill_calls == [101]
        assert result == {"success": True, "stopped": 1, "force_killed": 0}


class TestNeverRepeatsTheStopRequest:
    """Guard for the save-safety invariant: one stop request per process, ever.

    A second request makes RetroArch (``unix_sighandler_quit == 2`` → ``exit(1)``
    with no ``atexit``), DuckStation/PCSX2 (``quick_exit``) and Dolphin
    (``SA_RESETHAND``) skip the save flush the first one started, destroying the
    save file. Only the force kill may escalate.

    "Ever" is literal and covers both halves: no retry loop inside a single
    ladder, AND no second ladder running concurrently with the first. The
    in-call half alone is not the guarantee — the grace window yields the event
    loop for seconds, which is ample time for a second callable to arrive.
    """

    @pytest.mark.asyncio
    async def test_a_surviving_process_is_asked_exactly_once_across_the_whole_window(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}

        await _make_service(control).stop_running_game()

        # Exactly once — not once per poll, not a retry ladder. The process was
        # alive for every one of the polls, which is precisely the state a naive
        # retry loop would react to.
        assert control.stop_calls == [101]
        assert control.alive_calls  # it really did stay alive through the window
        assert control.kill_calls == [101]

    @pytest.mark.asyncio
    async def test_every_process_is_asked_exactly_once_even_when_all_survive(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102, 103])
        control.survive_stop = {101, 102, 103}

        await _make_service(control).stop_running_game()

        assert control.stop_calls == [101, 102, 103]
        assert sorted(control.kill_calls) == [101, 102, 103]

    @pytest.mark.asyncio
    async def test_a_concurrent_second_call_never_re_asks_the_same_process(self) -> None:
        """The cross-CALL half of the guarantee — the save-destroying scenario.

        The user presses Stop, the emulator starts flushing a large memory card,
        the UI shows nothing changing for seconds, and the user presses Stop
        again. Without the single-flight claim the second call rediscovers the
        same still-alive pids and sends a second stop request, which is exactly
        what makes the emulator abandon the flush.
        """
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}
        # A yielding sleeper, so the second call really does land while the first
        # is parked in its grace window (see _YieldingSleeper).
        service = _make_service(control, _YieldingSleeper())

        first, second = await asyncio.gather(
            service.stop_running_game(),
            service.stop_running_game(),
        )

        # THE assertion: one stop request for that pid across BOTH calls.
        assert control.stop_calls == [101]
        # The loser is refused with the canonical shape rather than silently
        # no-oping, so the frontend can say something true about it.
        outcomes = {first["success"], second["success"]}
        assert outcomes == {True, False}
        refused = first if first["success"] is False else second
        assert refused["reason"] == "already_stopping"
        assert isinstance(refused["message"], str)
        assert refused["message"]
        assert "error" not in refused
        assert "error_code" not in refused
        # The refusal never touched the process table at all.
        assert control.find_calls == [APP_ID]

    @pytest.mark.asyncio
    async def test_the_claim_is_released_so_a_later_stop_still_works(self) -> None:
        # The guard is single-FLIGHT, not once-per-session: once the ladder ends,
        # a fresh Stop for a new session must be admitted normally.
        control = FakeGameProcessControlAdapter(pids=[101])
        service = _make_service(control)

        assert (await service.stop_running_game())["success"] is True

        control.pids = [202]
        control.alive = {202}
        second = await service.stop_running_game()

        assert second == {"success": True, "stopped": 1, "force_killed": 0}
        assert control.stop_calls == [101, 202]

    @pytest.mark.asyncio
    async def test_the_claim_is_released_when_the_ladder_raises(self) -> None:
        # A leaked flag would make Stop Game permanently unavailable for the rest
        # of the session, so the release must survive an exception.
        control = FakeGameProcessControlAdapter(pids=[101])
        service = _make_service(control)
        real_find = control.find_game_pids
        find_count = 0

        def _boom_on_first_call(app_id: str) -> list[int]:
            nonlocal find_count
            find_count += 1
            if find_count == 1:
                raise RuntimeError("proc exploded")
            return real_find(app_id)

        control.find_game_pids = _boom_on_first_call  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="proc exploded"):
            await service.stop_running_game()

        # The next call is admitted normally, not refused with already_stopping.
        assert (await service.stop_running_game())["success"] is True
        assert control.stop_calls == [101]


class TestRacesDuringTheLadder:
    @pytest.mark.asyncio
    async def test_a_process_that_vanished_before_the_stop_request_is_not_polled_or_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102])
        control.unsignalable = {101}

        result = await _make_service(control).stop_running_game()

        # 101 was still asked once (that IS the discovery of its death) but is
        # carried no further; 102 is the only one counted.
        assert control.stop_calls == [101, 102]
        assert 101 not in control.alive_calls
        assert control.kill_calls == []
        assert result == {"success": True, "stopped": 1, "force_killed": 0}

    @pytest.mark.asyncio
    async def test_every_process_vanishing_between_discovery_and_signal_still_succeeds(self) -> None:
        # The requested end state (nothing running) is already true, so this is a
        # success with a zero count — not a failure the user has to act on.
        control = FakeGameProcessControlAdapter(pids=[101, 102])
        control.unsignalable = {101, 102}
        sleeper = FakeSleeper()

        result = await _make_service(control, sleeper).stop_running_game()

        assert result == {"success": True, "stopped": 0, "force_killed": 0}
        assert control.kill_calls == []
        # With nothing left to wait for, the grace window is skipped entirely.
        assert sleeper.calls == []
