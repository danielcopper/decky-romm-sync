"""Contract test — chained RetroDECK home migration over the real plugin (#1042).

Drives the real ``Plugin`` through the real ``bootstrap()`` + real SQLite: an
install is recorded under home A, then the RetroDECK home is changed twice
(A→B→C) through the real ``detect_retrodeck_path_change`` before migrating. The
regression this locks is that the second change used to overwrite the pending
marker, so ``migrate_retrodeck_files`` found nothing under the (now forgotten)
home A, stranded the file, and reported success. After the fix the pending set
accumulates [A, B]; migrating relocates the row's file to C in the real
database, re-emits ``migration_relaunch_options`` with the C path, and clears
both pending markers.

``migrate_retrodeck_files`` is driven frontend-shaped per ``src/api/backend.ts``:
``callable<[string | null], MigrationResult>`` — a single positional argument,
``None`` for the null conflict-strategy.
"""

from __future__ import annotations

import asyncio
import os

from fakes.fake_retrodeck_paths import FakeRetroDeckPaths

from domain.rom import Rom
from domain.rom_install import RomInstall


def _seed_install_at(harness, rom_id: int, *, file_path: str, app_id: int) -> None:
    """Seed a bound Rom + its RomInstall pointing at *file_path* (real SQLite)."""
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=rom_id,
                platform_slug="n64",
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=app_id,
                synced_at="2026-01-01T00:00:00",
            )
        )
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=None,
                platform_slug="n64",
                system="n64",
                installed_at="2026-01-01T00:00:00",
            )
        )


def _relaunch_payload(harness):
    """Return the ``migration_relaunch_options`` payload emitted, or None."""
    for call in harness.emit.await_args_list:
        if call.args and call.args[0] == "migration_relaunch_options":
            return call.args[1]
    return None


async def _detect_at(harness, home: str) -> None:
    """Point the RetroDECK home at *home* and run one detection pass."""
    harness.plugin._migration_service._retrodeck_paths = FakeRetroDeckPaths(home=home)
    harness.plugin._migration_service.detect_retrodeck_path_change()
    await asyncio.sleep(0)  # drain the spawned retrodeck_path_changed emit


async def test_second_home_change_before_migrating_does_not_strand_files(harness):
    """A→B→C then migrate: the row's file reaches C, both markers clear (#1042)."""
    a = str(harness.tmp_path / "A")
    b = str(harness.tmp_path / "B")
    c = str(harness.tmp_path / "C")
    old_rom = os.path.join(a, "roms", "n64", "zelda.z64")
    new_rom = os.path.join(c, "roms", "n64", "zelda.z64")
    for d in (a, b, c):
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.dirname(old_rom))
    with open(old_rom, "w") as f:
        f.write("rom data")

    _seed_install_at(harness, 1, file_path=old_rom, app_id=4242)

    # First detection anchors the stored home at A, then A→B→C before migrating.
    await _detect_at(harness, a)
    await _detect_at(harness, b)
    await _detect_at(harness, c)

    # Pending set accumulated both left-behind homes rather than overwriting.
    with harness.uow_factory() as uow:
        assert uow.kv_config.get("retrodeck_home_path") == c
        assert uow.kv_config.get("retrodeck_home_path_previous") == a
        assert uow.kv_config.get("retrodeck_home_path_hops") == '["' + b + '"]'

    result = await harness.plugin.migrate_retrodeck_files(None)

    # Response shape (MigrationResult) pinned.
    assert result["success"] is True
    assert result["roms_moved"] == 1
    assert result["missing_count"] == 0
    assert isinstance(result["message"], str)
    assert result["errors"] == []

    # The file physically reached C, and the real SQLite row now points at C.
    assert os.path.exists(new_rom)
    assert not os.path.exists(old_rom)
    with harness.uow_factory() as uow:
        assert uow.rom_installs.get(1).file_path == new_rom
        # Both pending markers cleared by the clean migration.
        assert uow.kv_config.get("retrodeck_home_path_previous") is None
        assert uow.kv_config.get("retrodeck_home_path_hops") is None

    # Relaunch options re-emitted with the NEW (C) launch path.
    payload = _relaunch_payload(harness)
    assert payload is not None
    assert len(payload["items"]) == 1
    assert payload["items"][0]["app_id"] == 4242
    assert new_rom in payload["items"][0]["launch_options"]
    assert a not in payload["items"][0]["launch_options"]


async def test_migrate_with_no_pending_returns_canonical_failure_shape(harness):
    """No migration pending → the canonical {success, reason, message} failure shape."""
    result = await harness.plugin.migrate_retrodeck_files(None)
    assert result["success"] is False
    assert result["reason"] == "no_migration_needed"
    assert isinstance(result["message"], str)
