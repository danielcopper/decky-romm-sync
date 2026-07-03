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

from domain.rom_install import RomInstall

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


async def test_ps3_folder_install_bakes_game_root_not_eboot(harness):
    """A PS3 folder game bakes the game ROOT directory, not the nested EBOOT (#1212).

    The install's ``file_path`` stays the ``…/PS3_GAME/USRDIR/EBOOT.BIN`` launch
    file (the ADR-0008 anchor), but the folder-boot override (ADR-0019) makes the
    baked ``launch_options`` quote the game folder so RPCS3's directory-boot can
    launch it. Drives the real bake seam over real on-disk files under tmp_path.
    """
    rom_id = 55
    seed_rom(harness, rom_id, platform_slug="ps3", shortcut_app_id=rom_id)
    rom_dir = harness.tmp_path / "retrodeck" / "roms" / "ps3" / "MyGame"
    eboot = rom_dir / "PS3_GAME" / "USRDIR" / "EBOOT.BIN"
    eboot.parent.mkdir(parents=True, exist_ok=True)
    eboot.write_bytes(b"\x00" * 16)
    with harness.uow_factory() as uow:
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=str(eboot),
                rom_dir=str(rom_dir),
                platform_slug="ps3",
                system="ps3",
                installed_at="2026-01-01T00:00:00",
            )
        )

    item = await harness.plugin.get_rom_relaunch_options(rom_id)

    assert item is not None
    launch_options = item["launch_options"]
    # The baked path is the game folder (quoted), never the nested EBOOT.
    assert f'"{rom_dir}"' in launch_options
    assert "EBOOT.BIN" not in launch_options
