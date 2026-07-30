"""Unit tests for ``GameProcessService`` — the stop-game escalation ladder.

The ladder's safety rule is that a still-alive process is NEVER asked to stop a
second time (emulators skip their save flush on the repeat and destroy the save
file); the only escalation is the force kill after the grace window. The
exactly-once test in :class:`TestNeverRepeatsTheStopRequest` is the guard for
that rule and must not be weakened.
"""

from __future__ import annotations

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


def _make_service(
    control: FakeGameProcessControlAdapter,
    sleeper: FakeSleeper | None = None,
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
        # The process survived the stop request but vanished before the force
        # kill (or belongs to another user) — the run still succeeds.
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}
        control.unsignalable = {101}

        result = await _make_service(control).stop_running_game()

        assert result == {"success": True, "stopped": 0, "force_killed": 0}


class TestNeverRepeatsTheStopRequest:
    """Guard for the save-safety invariant: one stop request per process, ever.

    A second request makes RetroArch (``unix_sighandler_quit == 2`` → ``exit(1)``
    with no ``atexit``), DuckStation/PCSX2 (``quick_exit``) and Dolphin
    (``SA_RESETHAND``) skip the save flush the first one started, destroying the
    save file. Only the force kill may escalate.
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
