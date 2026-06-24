"""Contract tests for the startup launch-options reconcile read callable.

Driven frontend-shaped per ``src/api/backend.ts``:
``getInstalledRelaunchOptions = callable<[], {app_id, launch_options}[]>``.

The frontend pulls this on mount (after backend reachability is proven) and
re-confirms each shortcut's launch command to heal any drift to the empty
placeholder (#1043). The contract is the list SHAPE: zero items when nothing
is installed+bound, and a ``{app_id, launch_options}`` dict per installed+bound
ROM with a non-empty launch command.
"""

from __future__ import annotations

from ._seed import seed_install, seed_rom


async def test_no_installs_returns_empty_list(harness):
    """Nothing installed → empty list (the frontend no-ops on it)."""
    result = await harness.plugin.get_installed_relaunch_options()
    assert result == []
    assert isinstance(result, list)


async def test_installed_bound_rom_shape(harness):
    """An installed+bound ROM → one {app_id, launch_options} item with a real command."""
    seed_install(harness, 42, system="gba", platform_slug="gba", file_name="pokemon.gba")
    result = await harness.plugin.get_installed_relaunch_options()
    assert len(result) == 1
    item = result[0]
    assert set(item.keys()) == {"app_id", "launch_options"}
    # seed_rom defaults shortcut_app_id to rom_id when not bound otherwise.
    assert item["app_id"] == 42
    assert isinstance(item["app_id"], int)
    assert isinstance(item["launch_options"], str)
    assert item["launch_options"]  # non-empty — the full launch command


async def test_bound_rom_without_install_is_excluded(harness):
    """A ROM bound to a shortcut but not installed → no item (no install row)."""
    seed_rom(harness, 7, platform_slug="gba", shortcut_app_id=7)
    result = await harness.plugin.get_installed_relaunch_options()
    assert result == []
