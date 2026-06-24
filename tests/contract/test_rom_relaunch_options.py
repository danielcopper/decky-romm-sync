"""Contract test for ``get_rom_relaunch_options`` over the real nesting.

``Plugin.get_rom_relaunch_options(rom_id)`` is the single-ROM re-confirm seam
the Play-button funnel pulls just before launch to heal mid-session
``launch_options`` drift (#1150). It resolves through the real
:class:`RelaunchOptionsResolver`, whose ``active_core_for_rom`` opens its **own**
Unit of Work — the same non-reentrant ``BEGIN IMMEDIATE`` write-lock nesting the
batch path guards against (#1154). The unit tests inject a fake UoW; this tier
drives the real callable over the real file-based SQLite UoW the harness wires.

Called positionally as the frontend does, and pinned against the TS shape:
``{ app_id: number; launch_options: string } | null`` — a literal ``None`` where
the TS union says ``null``.
"""

from __future__ import annotations

from ._seed import seed_install, seed_rom


async def test_installed_bound_rom_returns_item(harness):
    """An installed+bound ROM → ``{app_id, launch_options}`` with a real command."""
    seed_install(harness, 42, system="gba", platform_slug="gba", file_name="pokemon.gba")

    item = await harness.plugin.get_rom_relaunch_options(42)

    assert item is not None
    assert set(item.keys()) == {"app_id", "launch_options"}
    assert item["app_id"] == 42
    assert isinstance(item["launch_options"], str)
    assert item["launch_options"]  # non-empty — the full launch command


async def test_bound_rom_with_no_install_returns_none(harness):
    """A bound-but-uninstalled ROM (no install row) → literal None (TS ``null``)."""
    seed_rom(harness, 7, platform_slug="gba", shortcut_app_id=7)

    item = await harness.plugin.get_rom_relaunch_options(7)

    assert item is None


async def test_unknown_rom_returns_none(harness):
    """A rom_id with no rows at all → None — nothing to re-confirm."""
    item = await harness.plugin.get_rom_relaunch_options(999)
    assert item is None
