"""Seeding helpers for contract tests — write real relational + server state.

These helpers write through the harness's *real* SQLite Unit of Work (the
same one the wired services read) and onto the *real* settings dict, so a
contract test seeds state the way production accumulates it. The server
side is seeded directly on the :class:`FakeRommApi` public attributes.

The real ``rom_save_states`` / ``rom_installs`` tables carry a ``rom_id``
foreign key to ``roms`` (``PRAGMA foreign_keys=ON``), so any per-ROM child
seed must seed the parent ``Rom`` row first — :func:`seed_rom` does that and
is called by the child seeders.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_save_state import RomSaveState

if TYPE_CHECKING:
    from tests.contract._harness import ContractHarness


def enable_save_sync(harness: ContractHarness, *, device_id: str = "device-1") -> None:
    """Flip on save sync and bind a server device id (matches FakeRommApi seeds)."""
    harness.plugin.settings["save_sync_enabled"] = True
    with harness.uow_factory() as uow:
        uow.kv_config.set("device_id", device_id)


def seed_rom(
    harness: ContractHarness,
    rom_id: int,
    *,
    platform_slug: str = "gba",
    shortcut_app_id: int = 0,
) -> None:
    """Seed a ``Rom`` registry row (the FK anchor for per-ROM child writes).

    ``shortcut_app_id`` defaults to ``rom_id`` so the row counts as a bound
    shortcut in registry/stat reads; pass ``0`` to seed a ROM with no Steam
    shortcut bound (it then does not count toward bound-shortcut reads).
    """
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=shortcut_app_id or rom_id,
                synced_at="2026-01-01T00:00:00",
            )
        )


def seed_install(
    harness: ContractHarness,
    rom_id: int,
    *,
    system: str = "gba",
    platform_slug: str = "gba",
    file_name: str = "game.gba",
) -> str:
    """Seed a ``RomInstall`` (seeds the ``Rom`` FK first). Returns the file path."""
    seed_rom(harness, rom_id, platform_slug=platform_slug)
    file_path = str(harness.tmp_path / "retrodeck" / "roms" / system / file_name)
    with harness.uow_factory() as uow:
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=None,
                platform_slug=platform_slug,
                system=system,
                installed_at="2026-01-01T00:00:00",
            )
        )
    return file_path


def seed_save_state(
    harness: ContractHarness,
    rom_id: int,
    state: RomSaveState,
    *,
    platform_slug: str = "gba",
) -> None:
    """Seed a ``RomSaveState`` aggregate (seeds the ``Rom`` FK first)."""
    seed_rom(harness, rom_id, platform_slug=platform_slug)
    with harness.uow_factory() as uow:
        uow.rom_save_states.save(rom_id, state)


def seed_confirmed_slot(
    harness: ContractHarness,
    rom_id: int,
    *,
    slot: str = "main",
    source: str = "server",
    platform_slug: str = "gba",
) -> None:
    """Seed a tracked + confirmed save slot for ``rom_id``.

    Produces ``slot_confirmed=True`` and an ``active_slot``, with the slot
    present in the persisted slots map (the shape ``get_slot_delete_info``
    and ``is_save_tracking_configured`` read).
    """
    state = RomSaveState()
    state.confirm_slot(slot)
    state.refresh_slot_listing({slot: {"source": source, "count": 1, "latest_updated_at": "2026-01-01T00:00:00Z"}})
    seed_save_state(harness, rom_id, state, platform_slug=platform_slug)


def server_save(
    *,
    save_id: int,
    rom_id: int,
    file_name: str = "game.srm",
    slot: str | None = "main",
    updated_at: str = "2026-02-01T00:00:00Z",
    emulator: str = "retroarch",
    file_size_bytes: int = 1024,
) -> dict[str, Any]:
    """Build a server-save dict shaped like RomM's save payload."""
    entry: dict[str, Any] = {
        "id": save_id,
        "rom_id": rom_id,
        "file_name": file_name,
        "updated_at": updated_at,
        "emulator": emulator,
        "file_size_bytes": file_size_bytes,
    }
    if slot is not None:
        entry["slot"] = slot
    return entry


def seed_server_save(harness: ContractHarness, **kwargs: Any) -> dict[str, Any]:
    """Seed one server save on the FakeRommApi and return the stored dict."""
    entry = server_save(**kwargs)
    harness.romm.saves[entry["id"]] = entry
    return entry


# A minimal, real-shaped es_systems.xml: one ``gba`` system with an mGBA
# default (first %CORE_RETROARCH% command) and a VBA Next alternative — two
# bakeable libretro cores a core-selection contract test can pin/clear between.
_DEFAULT_ES_SYSTEMS_XML = """\
<?xml version="1.0"?>
<systemList>
  <system>
    <name>gba</name>
    <command label="mGBA">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/mgba_libretro.so %ROM%</command>
    <command label="VBA Next">%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/vba_next_libretro.so %ROM%</command>
  </system>
</systemList>
"""


def _flatpak_files_dir(harness: ContractHarness) -> str:
    """The per-user RetroDECK flatpak ``files`` tree under the harness ``user_home``."""
    return os.path.join(
        str(harness.tmp_path),
        "home",
        ".local",
        "share",
        "flatpak",
        "app",
        "net.retrodeck.retrodeck",
        "current",
        "active",
        "files",
    )


def _systems_linux_dir(harness: ContractHarness) -> str:
    """The ``…/systems/linux`` dir holding es_systems.xml + es_find_rules.xml."""
    return os.path.join(
        _flatpak_files_dir(harness),
        "retrodeck",
        "components",
        "es-de",
        "share",
        "es-de",
        "resources",
        "systems",
        "linux",
    )


def seed_es_systems(harness: ContractHarness, xml: str | None = None) -> None:
    """Write a real-shaped ``es_systems.xml`` for the real ``CoreResolver`` to read.

    The harness roots ``user_home`` at ``tmp_path/home``; the resolver probes the
    per-user flatpak files tree under it (the contract conftest repoints the
    system root away, so this seed is the only source). The file lands at the
    ``…/systems/linux/es_systems.xml`` path ES-DE ships. The default seeds a
    single ``gba`` system with an mGBA default and a VBA Next alternative.
    """
    dest = os.path.join(_systems_linux_dir(harness), "es_systems.xml")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(xml if xml is not None else _DEFAULT_ES_SYSTEMS_XML)


def seed_es_find_rules(harness: ContractHarness, xml: str) -> None:
    """Write ``es_find_rules.xml`` beside the seeded ``es_systems.xml``.

    The sandbox-launcher probe (``resolve_sandbox_launcher``) and the standalone
    existence probe both read this file as the es_systems sibling.
    """
    dest = os.path.join(_systems_linux_dir(harness), "es_find_rules.xml")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(xml)


def seed_component_launcher(harness: ContractHarness, component: str) -> str:
    """Create a bundled RetroDECK component launcher; return its SANDBOX path.

    Lays down the on-disk launcher under the flatpak files tree so the existence
    probe treats the standalone emulator as installed, and returns the
    ``/app/retrodeck/components/<component>/component_launcher.sh`` sandbox path —
    the value ``resolve_sandbox_launcher`` returns and the direct bake carries.
    """
    host = os.path.join(_flatpak_files_dir(harness), "retrodeck", "components", component, "component_launcher.sh")
    os.makedirs(os.path.dirname(host), exist_ok=True)
    with open(host, "w") as f:
        f.write("#!/bin/sh\n")
    return f"/app/retrodeck/components/{component}/component_launcher.sh"
