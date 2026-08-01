"""Unit tests for ``GameProcessService`` — the stop-game escalation ladder.

Two safety rules are guarded here and neither may be weakened.

A still-alive process is NEVER asked to stop a second time (emulators skip their
save flush on the repeat and destroy the save file); the only escalation is the
force kill after the grace window. That rule has two halves — no retry inside
one call, and no second concurrent call — and
:class:`TestNeverRepeatsTheStopRequest` guards both.

And only the instance running the ROM the stop was pressed for is ever
signalled: RetroDECK can have several live at once, so signalling an unmatched
one ends another game mid-save. :class:`TestTargetsOnlyTheMatchedInstance`
guards that, asserting the OTHER instance's pids reached neither signal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fakes.fake_game_process_control import DEFAULT_LAUNCH_PATH, FakeGameProcessControlAdapter
from fakes.fake_rom_launch_path import FakeRomLaunchPathReader
from fakes.system_time import FakeSleeper

from services.game_process import (
    _GRACE_POLL_SECONDS,
    _GRACE_POLLS,
    GameProcessService,
    GameProcessServiceConfig,
)

if TYPE_CHECKING:
    from domain.game_instance import GameInstance

APP_ID = "net.retrodeck.retrodeck"
ROM_ID = 42


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
    launch_path: FakeRomLaunchPathReader | None = None,
) -> GameProcessService:
    """Build the service. By default ``ROM_ID`` resolves to the fake's own path.

    The single-instance shorthand (``FakeGameProcessControlAdapter(pids=…)``)
    puts :data:`DEFAULT_LAUNCH_PATH` on the instance's command line, so the
    default seam here makes that instance the match and the ladder runs — which
    is what every test that is about the LADDER rather than the match wants.
    """
    return GameProcessService(
        config=GameProcessServiceConfig(
            game_process=control,
            launch_path=launch_path or FakeRomLaunchPathReader({ROM_ID: DEFAULT_LAUNCH_PATH}),
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

        result = await _make_service(control).stop_running_game(ROM_ID)

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

        await _make_service(control).stop_running_game(ROM_ID)

        assert control.stop_calls == []
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_looks_the_game_up_by_the_configured_flatpak_app_id(self) -> None:
        # The service must ask about RetroDECK, not some other flatpak — a
        # mismatched id resolves to nothing on the fake.
        control = FakeGameProcessControlAdapter(pids=[101], app_id="org.videolan.VLC")

        result = await _make_service(control).stop_running_game(ROM_ID)

        assert control.find_calls == [APP_ID]
        assert result["reason"] == "not_running"


class TestStopRequestAlone:
    @pytest.mark.asyncio
    async def test_a_cooperative_process_is_never_force_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102])

        result = await _make_service(control).stop_running_game(ROM_ID)

        assert result == {"success": True, "stopped": 2, "force_killed": 0}
        assert control.stop_calls == [101, 102]
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_the_ladder_stops_polling_as_soon_as_everything_has_exited(self) -> None:
        # A well-behaved emulator costs one poll interval, not the whole window.
        control = FakeGameProcessControlAdapter(pids=[101])
        sleeper = FakeSleeper()

        await _make_service(control, sleeper).stop_running_game(ROM_ID)

        assert sleeper.calls == [_GRACE_POLL_SECONDS]

    @pytest.mark.asyncio
    async def test_processes_are_signalled_in_the_order_the_adapter_reports_them(self) -> None:
        # Deepest-first is the adapter's contract; the service must not reorder
        # it — the emulator has to be asked before its shell wrappers die.
        control = FakeGameProcessControlAdapter(pids=[103, 102, 101])

        await _make_service(control).stop_running_game(ROM_ID)

        assert control.stop_calls == [103, 102, 101]


class TestEscalationToForceKill:
    @pytest.mark.asyncio
    async def test_a_process_that_ignores_the_stop_request_is_force_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}

        result = await _make_service(control).stop_running_game(ROM_ID)

        assert result == {"success": True, "stopped": 1, "force_killed": 1}
        assert control.kill_calls == [101]

    @pytest.mark.asyncio
    async def test_only_the_survivors_are_force_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102, 103])
        control.survive_stop = {102}

        result = await _make_service(control).stop_running_game(ROM_ID)

        assert control.kill_calls == [102]
        assert result["force_killed"] == 1

    @pytest.mark.asyncio
    async def test_the_full_grace_window_is_waited_out_before_forcing(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101])
        control.survive_stop = {101}
        sleeper = FakeSleeper()

        await _make_service(control, sleeper).stop_running_game(ROM_ID)

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

        result = await _make_service(control).stop_running_game(ROM_ID)

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

        result = await _make_service(control).stop_running_game(ROM_ID)

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

        await _make_service(control).stop_running_game(ROM_ID)

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

        await _make_service(control).stop_running_game(ROM_ID)

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
            service.stop_running_game(ROM_ID),
            service.stop_running_game(ROM_ID),
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

        assert (await service.stop_running_game(ROM_ID))["success"] is True

        control.pids = [202]
        control.alive = {202}
        second = await service.stop_running_game(ROM_ID)

        assert second == {"success": True, "stopped": 1, "force_killed": 0}
        assert control.stop_calls == [101, 202]

    @pytest.mark.asyncio
    async def test_the_claim_is_released_when_the_ladder_raises(self) -> None:
        # A leaked flag would make Stop Game permanently unavailable for the rest
        # of the session, so the release must survive an exception.
        control = FakeGameProcessControlAdapter(pids=[101])
        service = _make_service(control)
        real_find = control.find_game_instances
        find_count = 0

        def _boom_on_first_call(app_id: str) -> list[GameInstance]:
            nonlocal find_count
            find_count += 1
            if find_count == 1:
                raise RuntimeError("proc exploded")
            return real_find(app_id)

        control.find_game_instances = _boom_on_first_call  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="proc exploded"):
            await service.stop_running_game(ROM_ID)

        # The next call is admitted normally, not refused with already_stopping.
        assert (await service.stop_running_game(ROM_ID))["success"] is True
        assert control.stop_calls == [101]


class TestTargetsOnlyTheMatchedInstance:
    """Guard for the second safety invariant: never signal an unmatched instance.

    RetroDECK is one flatpak app but can have several live instances — a second
    game launched from another Steam shortcut, ES-DE opened on its own. Before
    the ROM reached the callable every one of them was signalled, so stopping
    one game ended them all. Each test here asserts the OTHER instance's pids
    appear in NEITHER ``stop_calls`` nor ``kill_calls``: asserting only that the
    right pids were signalled would pass just as happily if both were.
    """

    OURS = "/home/deck/retrodeck/roms/psx/ours.chd"
    THEIRS = "/home/deck/retrodeck/roms/snes/theirs.sfc"

    def _two_instances(self) -> FakeGameProcessControlAdapter:
        control = FakeGameProcessControlAdapter()
        control.add_instance([201, 202], self.THEIRS)
        control.add_instance([101, 102], self.OURS)
        return control

    @pytest.mark.asyncio
    async def test_only_the_matching_instances_processes_are_signalled(self) -> None:
        control = self._two_instances()
        service = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: self.OURS}))

        result = await service.stop_running_game(ROM_ID)

        assert result == {"success": True, "stopped": 2, "force_killed": 0}
        assert control.stop_calls == [101, 102]
        # THE assertion: the other game was never touched, on either rung.
        assert 201 not in control.stop_calls
        assert 202 not in control.stop_calls
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_the_unmatched_instance_is_not_force_killed_either(self) -> None:
        # Everything survives the stop request, so the force rung is reached for
        # real — and it too must stay inside the matched tree.
        control = self._two_instances()
        control.survive_stop = {101, 102, 201, 202}
        service = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: self.OURS}))

        result = await service.stop_running_game(ROM_ID)

        assert result == {"success": True, "stopped": 2, "force_killed": 2}
        assert sorted(control.kill_calls) == [101, 102]
        assert control.stop_calls == [101, 102]

    @pytest.mark.asyncio
    async def test_a_re_rooted_sandbox_path_still_matches(self) -> None:
        # The sandbox's command line may expose the ROM under a different
        # absolute path than the host one the launch command was baked from, so
        # the path-tail fallback is what makes the stop work at all there.
        control = FakeGameProcessControlAdapter()
        control.add_instance([201], self.THEIRS)
        control.add_instance([101], "/run/media/mmcblk0p1/roms/psx/ours.chd")
        service = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: self.OURS}))

        result = await service.stop_running_game(ROM_ID)

        assert result == {"success": True, "stopped": 1, "force_killed": 0}
        assert control.stop_calls == [101]

    @pytest.mark.asyncio
    async def test_an_exact_path_match_wins_over_a_path_tail_match(self) -> None:
        # Two instances running the same ROM tail: the one running the exact
        # resolved path is the one that gets signalled.
        control = FakeGameProcessControlAdapter()
        control.add_instance([201], "/run/media/mmcblk0p1/roms/psx/ours.chd")
        control.add_instance([101], self.OURS)
        service = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: self.OURS}))

        await service.stop_running_game(ROM_ID)

        assert control.stop_calls == [101]

    @pytest.mark.asyncio
    async def test_the_same_filename_on_another_platform_is_not_the_match(self) -> None:
        # One game, two platform directories, one filename — ordinary in a
        # multi-platform library. Matching on the filename alone would end the
        # Genesis session while the SNES one was meant.
        control = FakeGameProcessControlAdapter()
        control.add_instance([201], "/run/media/mmcblk0p1/roms/genesis/Aladdin.zip")
        service = _make_service(
            control,
            launch_path=FakeRomLaunchPathReader({ROM_ID: "/home/deck/retrodeck/roms/snes/Aladdin.zip"}),
        )

        result = await service.stop_running_game(ROM_ID)

        assert result["reason"] == "game_not_running"
        assert control.stop_calls == []
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_two_instances_matching_the_same_tail_signal_nothing(self) -> None:
        # An ambiguous fallback must refuse, not resolve the tie by scan order:
        # one of the two is another game, and there is no way to tell which.
        control = FakeGameProcessControlAdapter()
        control.add_instance([201], "/run/media/sd/roms/psx/ours.chd")
        control.add_instance([101], "/home/deck/other/roms/psx/ours.chd")
        service = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: self.OURS}))

        result = await service.stop_running_game(ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "game_not_running"
        assert control.stop_calls == []
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_no_matching_instance_signals_nothing_and_refuses(self) -> None:
        control = self._two_instances()
        service = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: "/roms/gb/other.gb"}))

        result = await service.stop_running_game(ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "game_not_running"
        assert isinstance(result["message"], str)
        assert result["message"]
        assert "error" not in result
        assert "error_code" not in result
        # Killing the wrong emulator is worse than refusing: nothing was signalled.
        assert control.stop_calls == []
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_game_not_running_is_a_distinct_reason_from_not_running(self) -> None:
        # Nothing alive at all is a stale overlay the frontend clears; alive but
        # unmatched is a refusal it must NOT read as "the game ended".
        control = self._two_instances()
        matched_none = _make_service(control, launch_path=FakeRomLaunchPathReader({ROM_ID: "/roms/gb/other.gb"}))
        nothing_alive = _make_service(FakeGameProcessControlAdapter(pids=[]))

        assert (await matched_none.stop_running_game(ROM_ID))["reason"] == "game_not_running"
        assert (await nothing_alive.stop_running_game(ROM_ID))["reason"] == "not_running"

    @pytest.mark.asyncio
    async def test_a_rom_with_no_resolvable_launch_path_signals_nothing(self) -> None:
        # No install row / no bound shortcut → the seam answers None. There is
        # nothing to match on, and "match everything" is the bug being fixed.
        control = self._two_instances()
        seam = FakeRomLaunchPathReader()
        service = _make_service(control, launch_path=seam)

        result = await service.stop_running_game(ROM_ID)

        assert result["reason"] == "game_not_running"
        assert seam.calls == [ROM_ID]
        assert control.stop_calls == []
        assert control.kill_calls == []

    @pytest.mark.asyncio
    async def test_the_launch_path_is_asked_for_the_rom_that_was_passed(self) -> None:
        control = self._two_instances()
        seam = FakeRomLaunchPathReader({7: self.OURS})
        service = _make_service(control, launch_path=seam)

        result = await service.stop_running_game(7)

        assert seam.calls == [7]
        assert result["success"] is True
        assert control.stop_calls == [101, 102]

    @pytest.mark.asyncio
    async def test_nothing_alive_never_consults_the_launch_path_seam(self) -> None:
        # No instances means no match to make; the DB read is skipped entirely.
        seam = FakeRomLaunchPathReader({ROM_ID: self.OURS})
        service = _make_service(FakeGameProcessControlAdapter(pids=[]), launch_path=seam)

        assert (await service.stop_running_game(ROM_ID))["reason"] == "not_running"
        assert seam.calls == []


class TestRacesDuringTheLadder:
    @pytest.mark.asyncio
    async def test_a_process_that_vanished_before_the_stop_request_is_not_polled_or_killed(self) -> None:
        control = FakeGameProcessControlAdapter(pids=[101, 102])
        control.unsignalable = {101}

        result = await _make_service(control).stop_running_game(ROM_ID)

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

        result = await _make_service(control, sleeper).stop_running_game(ROM_ID)

        assert result == {"success": True, "stopped": 0, "force_killed": 0}
        assert control.kill_calls == []
        # With nothing left to wait for, the grace window is skipped entirely.
        assert sleeper.calls == []
