"""Contract test for the #1298 sibling-supersede cleanup on download start.

Driven frontend-shaped: ``startDownload = callable<[number], ...>``. Starting a
download for a version whose sibling group already has ANOTHER version on disk
strips that install FIRST (files + ``rom_installs`` row; saves untouched, per
ADR-0007), then the download proceeds — the "at most one downloaded version per
group" rule. A grandfathered sibling (its own Steam shortcut) is never touched.

Drives the real ``DownloadService`` → ``RomRemovalService`` wiring (the real
``LateBinding`` that closes their construction cycle) over real SQLite + real
file stores under ``tmp_path``; only the RomM transport is the fake.
"""

from __future__ import annotations

import asyncio

from ._seed import seed_group_member

_GROUP = "igdb:100:99"
_APP_ID = 42


async def _drain_background_tasks() -> None:
    """Await the fire-and-forget download task(s) `start_download` spawned."""
    for _ in range(6):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _seed_server_rom(harness, rom_id: int, *, fs_name: str) -> None:
    harness.romm.roms[rom_id] = {
        "id": rom_id,
        "name": f"rom-{rom_id}",
        "fs_name": fs_name,
        "fs_size_bytes": 8,
        "platform_slug": "gba",
        "platform_fs_slug": "gba",
        "platform_name": "GBA",
    }
    harness.romm.download_payloads[f"rom:{rom_id}:{fs_name}"] = b"NEWBYTES"


async def test_start_download_supersedes_installed_sibling(harness):
    """Downloading the active version removes the old unbound install first."""
    # rom 1: the old version — unbound, still on disk. rom 2: the active version
    # (bound to the group shortcut) about to be downloaded.
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=None, installed=True, file_name="old.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=_APP_ID)
    _seed_server_rom(harness, 2, fs_name="new.gba")

    result = await harness.plugin.start_download(2)
    assert result["success"] is True

    # The old sibling's install is stripped synchronously, before the transfer.
    with harness.uow_factory() as uow:
        assert uow.rom_installs.get(1) is None

    await _drain_background_tasks()
    with harness.uow_factory() as uow:
        assert uow.rom_installs.get(2) is not None  # the new version downloaded


async def test_start_download_keeps_grandfathered_sibling(harness):
    """A sibling with its own separate Steam shortcut is never removed."""
    # rom 1: grandfathered — bound to its OWN shortcut (99), on disk.
    seed_group_member(harness, 1, group_key=_GROUP, shortcut_app_id=99, installed=True, file_name="old.gba")
    seed_group_member(harness, 2, group_key=_GROUP, shortcut_app_id=_APP_ID)
    _seed_server_rom(harness, 2, fs_name="new.gba")

    result = await harness.plugin.start_download(2)
    assert result["success"] is True

    await _drain_background_tasks()
    with harness.uow_factory() as uow:
        assert uow.rom_installs.get(1) is not None  # grandfathered kept
        assert uow.rom_installs.get(2) is not None
