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

The resolution reads the live ``es_systems.xml`` the harness seeds under
``tmp_path`` (#1210) — there is no ``core_defaults`` snapshot. The last block
pins the SHAPE of the two emulator-picker payloads (game-detail
``get_platform_core_info`` + System-page ``get_firmware_status``) with the
emulator list present (happy) and absent (emulator data unavailable).
"""

from __future__ import annotations

from ._seed import seed_es_systems, seed_install, seed_rom

_GBA_FIRMWARE = [
    {
        "id": 1,
        "file_name": "gba_bios.bin",
        "file_path": "bios/gba/gba_bios.bin",
        "file_size_bytes": 100,
        "md5_hash": "",
    }
]

_MGBA_ENTRY = {
    "label": "mGBA",
    "kind": "libretro",
    "core_so": "mgba_libretro",
    "is_default": True,
    "bakeable": True,
    "reason": None,
}
_VBA_NEXT_ENTRY = {
    "label": "VBA Next",
    "kind": "libretro",
    "core_so": "vba_next_libretro",
    "is_default": False,
    "bakeable": True,
    "reason": None,
}


async def test_clear_game_core_bakes_post_clear_core_not_old_pin(harness):
    """Clearing a pin bakes the resolved default, not the just-cleared override.

    A ``gba`` ROM pinned to VBA Next is cleared. The resolver runs after the
    pin-clear commits, so it reads the landed NULL and resolves to the live
    es_systems default (mGBA); the re-baked ``launch_options`` must carry the
    mGBA core, never the cleared VBA Next pin.
    """
    seed_es_systems(harness)
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
    """
    seed_es_systems(harness)
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


async def test_set_game_core_unbakeable_label_returns_canonical_failure(harness):
    """A label that does not resolve to a bakeable emulator hard-fails, no write."""
    seed_es_systems(harness)
    seed_install(harness, 5, system="gba", platform_slug="gba", file_name="c.gba")

    result = await harness.plugin.set_game_core(5, "Not A Real Emulator")

    assert result["success"] is False
    assert result["reason"] == "core_unavailable"
    assert isinstance(result["message"], str)
    assert result["message"]
    with harness.uow_factory() as uow:
        assert uow.roms.get(5).emulator_override is None


async def test_get_platform_core_info_payload_shape(harness):
    """The game-detail picker payload pins its keys, emulator list, and active core."""
    seed_es_systems(harness)
    seed_rom(harness, 42, platform_slug="gba")

    result = await harness.plugin.get_platform_core_info(42)

    assert set(result) == {
        "emulators",
        "emulator_data_available",
        "active_core",
        "active_core_label",
        "platform_core_label",
        "has_game_override",
    }
    assert result["emulator_data_available"] is True
    assert result["emulators"] == [_MGBA_ENTRY, _VBA_NEXT_ENTRY]
    assert result["active_core"] == "mgba_libretro"
    assert result["active_core_label"] == "mGBA"
    assert result["platform_core_label"] is None
    assert result["has_game_override"] is False


async def test_get_platform_core_info_unavailable_when_no_es_systems(harness):
    """No es_systems (RetroDECK not detected) → emulator data flagged unavailable."""
    seed_rom(harness, 43, platform_slug="gba")

    result = await harness.plugin.get_platform_core_info(43)

    assert result["emulator_data_available"] is False
    assert result["emulators"] == []


async def test_get_firmware_status_carries_emulators_per_platform(harness):
    """The System-page overview carries the classified emulator list per platform."""
    seed_es_systems(harness)
    seed_rom(harness, 7, platform_slug="gba")  # bound → has_games
    harness.romm.firmware_files = list(_GBA_FIRMWARE)

    result = await harness.plugin.get_firmware_status()

    assert result["success"] is True
    gba = next(p for p in result["platforms"] if p["platform_slug"] == "gba")
    assert gba["emulator_data_available"] is True
    assert gba["emulators"] == [_MGBA_ENTRY, _VBA_NEXT_ENTRY]
    assert gba["active_core"] == "mgba_libretro"


async def test_get_firmware_status_flags_unavailable_emulator_data(harness):
    """No es_systems → each platform entry flags emulator data unavailable."""
    seed_rom(harness, 8, platform_slug="gba")
    harness.romm.firmware_files = list(_GBA_FIRMWARE)

    result = await harness.plugin.get_firmware_status()

    gba = next(p for p in result["platforms"] if p["platform_slug"] == "gba")
    assert gba["emulator_data_available"] is False
    assert gba["emulators"] == []
