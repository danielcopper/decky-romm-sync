"""Contract tests for the core-selection callables over the real nesting.

``CoreService.clear_game_core`` (Reset / Follow default) and
``set_system_core`` (per-platform core change fan-out) both re-bake a ROM's
Steam ``launch_options`` from its active core, resolved through the real
:class:`ActiveCoreResolver` — which opens its **own** Unit of Work.

The unit tests inject a ``FakeActiveCoreResolver`` (no real UoW), so they never
exercise the nesting. This tier drives the **real** callables over the
**real** file-based SQLite UoW the harness wires: every UoW opens with
``BEGIN IMMEDIATE`` (the per-connection write lock), and the lock is not
re-entrant. Resolving the active core inside the still-open write UoW would
block the resolver's own UoW until ``busy_timeout`` then raise
``database is locked`` (#1047 / #1134) — which is why the write UoW is closed
before the resolve. ``clear_game_core`` additionally must resolve *after* the
pin-clear commits so the resolver reads the landed NULL and bakes the
POST-clear core, never the just-cleared pin (#1047).
"""

from __future__ import annotations

from ._seed import seed_core_defaults, seed_install


async def test_clear_game_core_bakes_post_clear_core_not_old_pin(harness):
    """Clearing a pin bakes the resolved default, not the just-cleared override.

    A ``gba`` ROM pinned to VBA Next is cleared. The resolver runs after the
    pin-clear commits, so it reads the landed NULL and resolves to the system
    default (mGBA); the re-baked ``launch_options`` must carry the mGBA core,
    never the cleared VBA Next pin. On the unfixed build the resolver's
    ``BEGIN IMMEDIATE`` blocks on the still-open write UoW for ``busy_timeout``
    then raises ``database is locked`` (#1047).
    """
    seed_core_defaults(harness)
    seed_install(harness, 42, system="gba", platform_slug="gba", file_name="pokemon.gba")
    with harness.uow_factory() as uow:
        uow.roms.set_emulator_override(42, "VBA Next")

    result = await harness.plugin.clear_game_core(42)

    assert result["success"] is True
    assert result["app_id"] == 42
    # The POST-clear core (system default mGBA) is baked, NOT the cleared pin.
    assert "mgba_libretro.so" in result["launch_options"]
    assert "vba_next_libretro.so" not in result["launch_options"]
    # The pin is gone (SQL NULL) — read back through a fresh UoW.
    with harness.uow_factory() as uow:
        assert uow.roms.get(42).emulator_override is None


async def test_clear_game_core_unknown_rom_returns_canonical_failure(harness):
    """An unknown ROM returns the canonical ``{success, reason, message}`` failure."""
    result = await harness.plugin.clear_game_core(999)

    assert result["success"] is False
    assert result["reason"] == "not_found"
    assert isinstance(result["message"], str)
    assert result["message"]


async def test_set_system_core_rebakes_only_unpinned_rom(harness):
    """The per-platform fan-out re-bakes each unpinned ROM through the real resolver.

    Two installed+bound ``gba`` ROMs: one plain, one with a per-game pin. Setting
    the platform core to VBA Next must re-bake only the unpinned ROM (the pin
    wins over the platform default for the other), with the VBA Next core baked.
    On the unfixed build the resolve inside the fan-out UoW deadlocks on the
    write lock then raises ``database is locked`` (#1134).
    """
    seed_core_defaults(harness)
    seed_install(harness, 1, system="gba", platform_slug="gba", file_name="a.gba")
    seed_install(harness, 2, system="gba", platform_slug="gba", file_name="b.gba")
    with harness.uow_factory() as uow:
        uow.roms.set_emulator_override(2, "mGBA")

    result = await harness.plugin.set_system_core("gba", "VBA Next")

    assert result["success"] is True
    items = result["rebake_items"]
    # Exactly the unpinned ROM (app_id 1) is re-baked; the pinned ROM is absent.
    assert len(items) == 1
    assert items[0]["app_id"] == 1
    assert "vba_next_libretro.so" in items[0]["launch_options"]
    assert all(item["app_id"] != 2 for item in items)
