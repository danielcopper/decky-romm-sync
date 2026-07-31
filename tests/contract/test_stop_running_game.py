"""Contract test for the ``stop_running_game`` callable over the real ``Plugin``.

Driven frontend-shaped per ``src/api/backend.ts``:
``stopRunningGame = callable<[], StopGameResult>`` — no arguments.

Pins both response shapes across the whole wire (real bootstrap → real
``wire_services`` → real ``GameProcessService``), with only the process table
itself faked:

* nothing running → the canonical ``{success: False, reason: "not_running",
  message}`` failure, never the forbidden ``error`` / ``error_code`` keys;
* a live process → ``{success: True, stopped, force_killed}``, and the app id
  the wiring asks about is RetroDECK's — proving ``bootstrap`` threaded the
  single domain constant into the service rather than a second copy.
"""

from __future__ import annotations

from domain.shortcut_data import RETRODECK_APP_ID


async def test_stop_running_game_with_nothing_running_returns_the_canonical_failure(harness):
    harness.game_process.pids = []

    result = await harness.plugin.stop_running_game()

    assert result == {
        "success": False,
        "reason": "not_running",
        "message": result["message"],
    }
    assert isinstance(result["message"], str)
    assert result["message"]
    assert "error" not in result
    assert "error_code" not in result


async def test_stop_running_game_stops_the_live_processes(harness):
    harness.game_process.pids = [4101, 4102]
    harness.game_process.alive = {4101, 4102}

    result = await harness.plugin.stop_running_game()

    assert result == {"success": True, "stopped": 2, "force_killed": 0}
    assert harness.game_process.stop_calls == [4101, 4102]
    assert harness.game_process.kill_calls == []


async def test_stop_running_game_forces_a_process_that_ignores_the_request(harness):
    harness.game_process.pids = [4101]
    harness.game_process.alive = {4101}
    harness.game_process.survive_stop = {4101}

    result = await harness.plugin.stop_running_game()

    assert result == {"success": True, "stopped": 1, "force_killed": 1}
    # Exactly one stop request even though the process stayed alive throughout —
    # the save-safety invariant, asserted end-to-end.
    assert harness.game_process.stop_calls == [4101]
    assert harness.game_process.kill_calls == [4101]


async def test_stop_running_game_looks_up_retrodecks_flatpak_app_id(harness):
    await harness.plugin.stop_running_game()

    assert harness.game_process.find_calls == [RETRODECK_APP_ID]
