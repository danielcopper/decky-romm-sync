"""Seeding helpers for contract tests — write real relational + server state.

These helpers write through the harness's *real* SQLite Unit of Work (the
same one the wired services read) and onto the *real* settings dict, so a
contract test seeds state the way production accumulates it. The server
side is seeded directly on the :class:`FakeRommApi` public attributes.

The real ``rom_save_sync_states`` / ``rom_installs`` tables carry a ``rom_id``
foreign key to ``roms`` (``PRAGMA foreign_keys=ON``), so any per-ROM child
seed must seed the parent ``Rom`` row first — :func:`seed_rom` does that and
is called by the child seeders.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.rom_save_sync_state import RomSaveSyncState
from domain.sync_run import SyncRun, SyncRunStatus
from domain.version_metadata import VersionMetadata

if TYPE_CHECKING:
    from tests.contract._harness import ContractHarness


def enable_save_sync(harness: ContractHarness, *, device_id: str = "device-1") -> None:
    """Flip on save sync and bind a server device id (matches FakeRommApi seeds)."""
    harness.plugin.settings["save_sync_enabled"] = True
    with harness.uow_factory() as uow:
        uow.kv_config.set("device_id", device_id)


#: ``seed_rom``'s default: bind the row to its own ``rom_id``. Not a real appId
#: — Steam's non-Steam shortcut ids live in ``[0x80000000, 0xFFFFFFFF]`` — so it
#: can never collide with an id a caller means literally.
BIND_TO_ROM_ID = 0


def seed_rom(
    harness: ContractHarness,
    rom_id: int,
    *,
    platform_slug: str = "gba",
    shortcut_app_id: int | None = BIND_TO_ROM_ID,
) -> None:
    """Seed a ``Rom`` registry row (the FK anchor for per-ROM child writes).

    ``shortcut_app_id`` defaults to :data:`BIND_TO_ROM_ID`, binding the row to
    its own ``rom_id`` so it counts as a bound shortcut in registry/stat reads.
    Pass an explicit id to bind it elsewhere, or ``None`` for an **unbound** ROM.

    Unbound is ``NULL``, never ``0``: the ``003`` partial unique index is
    ``WHERE shortcut_app_id IS NOT NULL`` and ADR-0007 reads NULL as the unbound
    state, so a literal ``0`` would still be a binding — one that no bound-row
    read would skip.
    """
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=rom_id if shortcut_app_id == BIND_TO_ROM_ID else shortcut_app_id,
                synced_at="2026-01-01T00:00:00",
            )
        )


def seed_sync_run(
    harness: ContractHarness,
    run_id: str,
    *,
    started_at: str,
    status: SyncRunStatus = "completed",
    finished_at: str = "2026-01-01T01:00:00",
    platforms_planned: int = 1,
    roms_planned: int = 1,
    platforms_completed: list[str] | None = None,
    collections_completed: list[str] | None = None,
    reason: str = "seeded",
) -> None:
    """Seed one ``SyncRun`` history row in *status*, built through the aggregate.

    The terminal transition is taken by the aggregate's own verb, so a seeded
    row carries exactly what a real run of that status would: a ``running`` run
    keeps every terminal field NULL, a ``completed`` one records the two lists,
    and a stopped one records *reason* as its error and no lists.
    """
    run = SyncRun.start(id=run_id, at=started_at, platforms_planned=platforms_planned, roms_planned=roms_planned)
    if status == "completed":
        run.complete(finished_at, platforms_completed or [], collections_completed or [])
    elif status == "cancelled":
        run.mark_cancelled(finished_at, reason)
    elif status == "interrupted":
        run.mark_interrupted(finished_at, reason)
    elif status == "paused":
        run.mark_paused(finished_at, reason)
    elif status == "errored":
        run.mark_errored(finished_at, reason)
    with harness.uow_factory() as uow:
        uow.sync_runs.save(run)


def seed_platform_stamp(
    harness: ContractHarness,
    platform_slug: str,
    *,
    rom_count: int,
    completed_at: str = "2026-01-01T00:00:00",
) -> None:
    """Seed a ``PlatformSyncState`` completion stamp (ADR-0023).

    The stamp exists iff the platform's local mirror is complete — it gates the
    incremental-skip reconstruct and the ``get_platforms`` collapsed-count
    garnish (#1412), so a contract test that means "this platform was synced"
    must seed it.
    """
    with harness.uow_factory() as uow:
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug=platform_slug, at=completed_at, rom_count=rom_count)
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
    file_path = os.path.join(harness.retrodeck_paths.roms_path(), system, file_name)
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


def seed_group_member(
    harness: ContractHarness,
    rom_id: int,
    *,
    group_key: str,
    shortcut_app_id: int | None = None,
    platform_slug: str = "gba",
    system: str = "gba",
    name: str | None = None,
    regions: tuple[str, ...] = (),
    is_main_sibling: bool = False,
    installed: bool = False,
    file_name: str = "game.gba",
) -> str | None:
    """Seed one ``Rom`` row in a sibling group, optionally installed.

    Unlike :func:`seed_rom` / :func:`seed_install` (which default to a solo,
    self-bound ROM), this sets ``sibling_group_key`` and takes an explicit
    ``shortcut_app_id`` so a version-switch / #1298 supersede contract test can
    build a real multi-version group (bound representative / unbound install /
    grandfathered separate shortcut). Returns the install file path when
    ``installed`` (its stem is the save-file basename the drift/sync discovery
    derives — ``find_save_files`` keys off the install path, not the roms row),
    else ``None``.
    """
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=name or f"rom-{rom_id}",
                fs_name=name or f"rom-{rom_id}",
                shortcut_app_id=shortcut_app_id,
                synced_at="2026-01-01T00:00:00",
                version=VersionMetadata(
                    sibling_group_key=group_key,
                    regions=regions,
                    is_main_sibling=is_main_sibling,
                ),
            )
        )
    if not installed:
        return None
    file_path = os.path.join(harness.retrodeck_paths.roms_path(), system, file_name)
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
    state: RomSaveSyncState,
    *,
    platform_slug: str = "gba",
) -> None:
    """Seed a ``RomSaveSyncState`` aggregate (seeds the ``Rom`` FK first)."""
    seed_rom(harness, rom_id, platform_slug=platform_slug)
    with harness.uow_factory() as uow:
        uow.rom_save_sync_states.save(rom_id, state)


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
    state = RomSaveSyncState()
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


def _retrodeck_marker_path(harness: ContractHarness) -> str:
    """``retrodeck.json`` under the harness ``user_home`` — the installation marker."""
    return os.path.join(
        str(harness.tmp_path), "home", ".var", "app", "net.retrodeck.retrodeck", "config", "retrodeck", "retrodeck.json"
    )


def seed_retrodeck_marker(harness: ContractHarness) -> None:
    """Write ``retrodeck.json`` so a RetroDECK installation is detected at all.

    The emulator catalogue is resolved per installation, and an installation is
    detected by this file's existence. Its paths are written to exactly what the
    plugin's own path adapter falls back to when the file is absent, so seeding
    it moves the catalogue from "no installation" to "readable" and changes
    nothing else about where a contract test's roots point.
    """
    rd_home = os.path.join(str(harness.tmp_path), "home", "retrodeck")
    dest = _retrodeck_marker_path(harness)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        json.dump(
            {
                "paths": {
                    "rd_home_path": rd_home,
                    "roms_path": os.path.join(rd_home, "roms"),
                    "saves_path": os.path.join(rd_home, "saves"),
                    "states_path": os.path.join(rd_home, "states"),
                    "bios_path": os.path.join(rd_home, "bios"),
                }
            },
            f,
        )


def seed_es_systems(harness: ContractHarness, xml: str | None = None) -> None:
    """Write a real-shaped ``es_systems.xml`` for the real resolver to read.

    The harness roots ``user_home`` at ``tmp_path/home``; the resolver reads the
    catalogue out of the per-user flatpak deployment under it (the contract
    conftest repoints the system root away, so this seed is the only source).
    The file lands at the ``…/systems/linux/es_systems.xml`` path ES-DE ships,
    and the RetroDECK marker rides along because a catalogue is only ever read
    through a detected installation. The default seeds a single ``gba`` system
    with an mGBA default and a VBA Next alternative.
    """
    seed_retrodeck_marker(harness)
    dest = os.path.join(_systems_linux_dir(harness), "es_systems.xml")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        f.write(xml if xml is not None else _DEFAULT_ES_SYSTEMS_XML)


def seed_es_find_rules(harness: ContractHarness, xml: str) -> None:
    """Write ``es_find_rules.xml`` beside the seeded ``es_systems.xml``.

    The sandbox-launcher probe (``resolve_sandbox_launcher``) and the standalone
    existence probe both read this file, which ES-DE ships in the same
    per-flavor ``systems/`` dir as its catalogue.
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
