"""Contract tests for the ``evaluate_launch`` launch-gate callable.

Driven frontend-shaped per ``src/api/backend.ts``: ``evaluate_launch`` takes a
single positional Steam app id and returns the ``LaunchVerdict`` dict
(``action`` / ``reason`` / ``toast_title`` / ``toast_body``).

The save-sync-disabled cases are the #1056 regression guard: with the feature
off, an installed ROM must launch (``action="allow"``) and the gate must not
perform a RomM ``list_saves`` round-trip — even when a server save exists that
would otherwise surface as a conflict. The enabled case is the control proving
the round-trip is a real, toggle-driven difference, not an unconditional skip.
"""

from __future__ import annotations

from ._seed import enable_save_sync, seed_install, seed_rom, seed_server_save


def _call_names(harness) -> list[str]:
    return [entry[0] for entry in harness.romm.call_log]


async def test_evaluate_launch_disabled_allows_and_skips_round_trip(harness):
    """Save-sync off + installed ROM → allow, and no ``list_saves`` round-trip."""
    harness.plugin.settings["save_sync_enabled"] = False
    seed_install(harness, 1)  # rom_id=1; shortcut_app_id defaults to rom_id, so app id 1 maps
    # A server save that the gate WOULD read (and could surface as a conflict)
    # if it ran the status round-trip — it must not.
    seed_server_save(harness, save_id=10, rom_id=1)

    verdict = await harness.plugin.evaluate_launch(1)

    assert verdict["action"] == "allow"
    assert verdict["reason"] is None
    assert "list_saves" not in _call_names(harness)


async def test_evaluate_launch_disabled_still_blocks_not_installed(harness):
    """The disabled-allow is gated behind the not-installed check — uninstalled still blocks."""
    harness.plugin.settings["save_sync_enabled"] = False
    seed_rom(harness, 1, shortcut_app_id=1)  # bound shortcut, but NOT installed

    verdict = await harness.plugin.evaluate_launch(1)

    assert verdict["action"] == "block"
    assert verdict["reason"] == "not_installed"
    assert "list_saves" not in _call_names(harness)


async def test_evaluate_launch_enabled_performs_round_trip(harness):
    """Control: save-sync on + installed → the gate DOES read save status (list_saves called)."""
    enable_save_sync(harness)
    seed_install(harness, 1)

    verdict = await harness.plugin.evaluate_launch(1)

    assert verdict["action"] in {"allow", "warn", "block"}
    assert "list_saves" in _call_names(harness)
