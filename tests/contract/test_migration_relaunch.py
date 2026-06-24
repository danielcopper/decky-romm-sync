"""Contract test for the migration relaunch-items build over the real nesting.

``MigrationService._build_relaunch_items`` runs on a RetroDECK-home path
migration to re-bake every installed+bound ROM's Steam ``launch_options``
from the relocated path. It resolves each ROM's active core through the
real :class:`ActiveCoreResolver`, which opens its **own** Unit of Work.

The unit tests inject a ``FakeActiveCoreResolver`` (no real UoW), so they
never exercise that nesting. This tier drives the **real** service over the
**real** file-based SQLite UoW the harness wires: every UoW opens with
``BEGIN IMMEDIATE`` (the per-connection write lock), and the lock is not
re-entrant. Resolving the core inside the install-iteration UoW would block
the resolver's own UoW until ``busy_timeout`` then raise
``database is locked`` (#1154) — which is why the build snapshots the rows
in one short read UoW and closes it before resolving.
"""

from __future__ import annotations

from ._seed import seed_install, seed_rom


async def test_build_relaunch_items_no_installs_is_empty(harness):
    """Nothing installed → empty list, no UoW contention."""
    items = harness.plugin._migration_service._build_relaunch_items()
    assert items == []


async def test_build_relaunch_items_installed_bound_rom(harness):
    """An installed+bound ROM resolves its core through a nested real UoW.

    On the unfixed build this deadlocks: the resolver's ``BEGIN IMMEDIATE``
    waits on the iteration UoW's write lock for ``busy_timeout`` then raises
    ``sqlite3.OperationalError: database is locked``. After the fix the rows
    are snapshotted and the iteration UoW is closed before the resolve runs,
    so it returns one ``{app_id, launch_options}`` item with a real command.
    """
    seed_install(harness, 42, system="gba", platform_slug="gba", file_name="pokemon.gba")

    items = harness.plugin._migration_service._build_relaunch_items()

    assert len(items) == 1
    item = items[0]
    assert set(item.keys()) == {"app_id", "launch_options"}
    assert item["app_id"] == 42
    assert isinstance(item["launch_options"], str)
    assert item["launch_options"]  # non-empty — the full launch command


async def test_build_relaunch_items_skips_unbound_rom(harness):
    """A bound-but-uninstalled ROM contributes no item (no install row)."""
    seed_rom(harness, 7, platform_slug="gba", shortcut_app_id=7)
    items = harness.plugin._migration_service._build_relaunch_items()
    assert items == []
