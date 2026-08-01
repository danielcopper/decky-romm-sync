"""Contract test for the ``stop_running_game`` callable over the real ``Plugin``.

Driven frontend-shaped per ``src/api/backend.ts``:
``stopRunningGame = callable<[number], StopGameResult>`` — the rom id, passed
positionally.

Pins all three response shapes across the whole wire (real bootstrap → real
``wire_services`` → real ``GameProcessService`` → real
``RelaunchOptionsResolver`` over real SQLite), with only the process table
itself faked:

* nothing running → the canonical ``{success: False, reason: "not_running",
  message}`` failure, never the forbidden ``error`` / ``error_code`` keys;
* RetroDECK alive but running another game → ``game_not_running``, with nothing
  signalled;
* the instance running this ROM → ``{success: True, stopped, force_killed}``,
  and the app id the wiring asks about is RetroDECK's — proving ``bootstrap``
  threaded the single domain constant into the service rather than a second copy.

The launch path each instance is matched on comes from the real resolver over
real ``rom_installs`` rows, so this tier also pins that the read path and the
bake path agree: a seeded install's own file path is what identifies its
instance.
"""

from __future__ import annotations

from domain.shortcut_data import RETRODECK_APP_ID

from ._seed import seed_install

_OTHER_GAME = "/home/deck/retrodeck/roms/snes/someone-elses.sfc"


def _add_instance(harness, pids: list[int], launch_path: str) -> None:
    """Put one more live RetroDECK instance, running *launch_path*, on the table."""
    harness.game_process.add_instance(pids, launch_path)


async def test_stop_running_game_with_nothing_running_returns_the_canonical_failure(harness):
    seed_install(harness, 42)

    result = await harness.plugin.stop_running_game(42)

    assert result == {
        "success": False,
        "reason": "not_running",
        "message": result["message"],
    }
    assert isinstance(result["message"], str)
    assert result["message"]
    assert "error" not in result
    assert "error_code" not in result


async def test_stop_running_game_stops_only_the_instance_running_this_rom(harness):
    file_path = seed_install(harness, 42, file_name="ours.gba")
    _add_instance(harness, [4201, 4202], _OTHER_GAME)
    _add_instance(harness, [4101, 4102], file_path)

    result = await harness.plugin.stop_running_game(42)

    assert result == {"success": True, "stopped": 2, "force_killed": 0}
    assert harness.game_process.stop_calls == [4101, 4102]
    assert harness.game_process.kill_calls == []
    # The other live game was never signalled on either rung.
    assert 4201 not in harness.game_process.stop_calls
    assert 4202 not in harness.game_process.stop_calls


async def test_stop_running_game_refuses_when_no_instance_runs_this_rom(harness):
    seed_install(harness, 42, file_name="ours.gba")
    _add_instance(harness, [4201], _OTHER_GAME)

    result = await harness.plugin.stop_running_game(42)

    assert result == {
        "success": False,
        "reason": "game_not_running",
        "message": result["message"],
    }
    assert isinstance(result["message"], str)
    assert result["message"]
    assert "error" not in result
    assert "error_code" not in result
    # Refusing means refusing: the other game's process was left alone.
    assert harness.game_process.stop_calls == []
    assert harness.game_process.kill_calls == []


async def test_stop_running_game_refuses_for_a_rom_with_no_install_row(harness):
    # Nothing was ever launched from an uninstalled ROM, so no live instance can
    # be attributed to it — and "attribute it to whatever is running" is exactly
    # the behaviour this callable's rom_id exists to prevent.
    _add_instance(harness, [4201], _OTHER_GAME)

    result = await harness.plugin.stop_running_game(999)

    assert result["success"] is False
    assert result["reason"] == "game_not_running"
    assert harness.game_process.stop_calls == []


async def test_stop_running_game_forces_a_process_that_ignores_the_request(harness):
    file_path = seed_install(harness, 42, file_name="ours.gba")
    _add_instance(harness, [4101], file_path)
    harness.game_process.survive_stop = {4101}

    result = await harness.plugin.stop_running_game(42)

    assert result == {"success": True, "stopped": 1, "force_killed": 1}
    # Exactly one stop request even though the process stayed alive throughout —
    # the save-safety invariant, asserted end-to-end.
    assert harness.game_process.stop_calls == [4101]
    assert harness.game_process.kill_calls == [4101]


async def test_stop_running_game_looks_up_retrodecks_flatpak_app_id(harness):
    await harness.plugin.stop_running_game(42)

    assert harness.game_process.find_calls == [RETRODECK_APP_ID]
