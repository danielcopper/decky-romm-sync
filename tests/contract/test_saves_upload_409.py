"""Contract tests for the upload-time 409 backstop (ADR-0017, #1276).

Driven frontend-shaped through the real ``Plugin`` / ``bootstrap`` harness.
The automatic save-sync dispatch always POSTs a new save with
``overwrite=false``; RomM answers a stale-current race with a 409 that the
matrix backstops by re-fetching the slot and re-deciding purely from hashes
(``resolve_upload_conflict``): a provably-unchanged local downloads the fresh
head, anything else surfaces a conflict. ``keep_local`` conflict resolution
instead POSTs with ``overwrite=true`` (the user's content wins outright).

These tests stay on the legacy ``compute_sync_action`` matrix path
(``active_slot`` set but ``slot_confirmed=False``) so the POST→409 backstop is
actually exercised — the negotiate transport returns an empty plan under the
``FakeRommApi`` and the engine fork is not collapsed until a later phase.
"""

from __future__ import annotations

import hashlib
import os

from domain.rom_save_state import RomSaveState

from ._seed import enable_save_sync, seed_install, seed_save_state, seed_server_save


def _write_local_save(harness, *, system: str, content: bytes, filename: str) -> str:
    """Write a real local save under the resolved saves dir; return its path."""
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    path = os.path.join(saves_dir, filename)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _tracked_slot_state(*, baseline: str | None) -> RomSaveState:
    """A detected-but-unconfirmed ``default`` slot (keeps sync on the legacy matrix).

    ``baseline`` (when given) records the file's ``last_sync_hash`` so
    ``compute_sync_action`` treats local as unchanged; ``None`` leaves the slot
    without a baseline (never synced).
    """
    state = RomSaveState(active_slot="default", slot_confirmed=False, system="gba")
    if baseline is not None:
        state.adopt_baseline("game.srm", tracked_save_id=10, last_sync_hash=baseline)
    return state


async def test_saves_upload_409_stale_downgrades_to_download(harness):
    """A newer foreign save + local unchanged since baseline: the POST 409s and the
    backstop silently downloads the fresh head (overwrite=false POST, then download)."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"in-sync local", filename="game.srm")
    baseline = hashlib.md5(b"in-sync local").hexdigest()
    seed_save_state(harness, 42, _tracked_slot_state(baseline=baseline))

    # A newer save from another device this device has never synced → the POST 409s.
    foreign = harness.romm.seed_foreign_save(
        42,
        uploaded_by="device-B",
        slot="default",
        filename="game.srm",
        updated_at="2026-03-01T00:00:00Z",
        content=b"newer from device B",
    )

    result = await harness.plugin.sync_rom_saves(42)

    assert result["success"] is True
    assert result["synced"] == 1
    assert result["conflicts"] == []
    # The POST was attempted overwrite=false so RomM's 409 could fire…
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    assert upload_calls[0][2]["overwrite"] is False
    # …then the backstop downloaded the fresh server head into the local file.
    downloads = [c for c in harness.romm.call_log if c[0] == "download_save_content"]
    assert [c[1][0] for c in downloads] == [foreign["id"]]
    local = os.path.join(harness.plugin._retrodeck_paths.saves_path(), "gba", "game.srm")
    with open(local, "rb") as fh:
        assert fh.read() == b"newer from device B"


async def test_saves_upload_409_stale_with_local_edit_surfaces_conflict(harness):
    """A stale-current race: the list_saves snapshot said we were current, so the
    matrix planned an Upload for a locally-edited file — but the POST 409s and the
    backstop, seeing local diverged from both baseline and server, surfaces a
    conflict. Nothing is overwritten on either side."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    local_path = _write_local_save(harness, system="gba", content=b"local diverged edit", filename="game.srm")
    seed_save_state(harness, 42, _tracked_slot_state(baseline=hashlib.md5(b"old baseline").hexdigest()))

    # The snapshot flags us current (explicit device_syncs) but no DeviceSaveSync
    # ledger row backs it, so the server rejects the overwrite=false POST with 409.
    harness.romm.saves[200] = {
        "id": 200,
        "rom_id": 42,
        "file_name": "game.srm",
        "slot": "default",
        "updated_at": "2026-03-01T00:00:00Z",
        "file_size_bytes": 32,
        "emulator": "retroarch",
        "download_path": "/saves/game.srm",
        "device_syncs": [{"device_id": "device-1", "is_current": True, "last_synced_at": "2026-03-01T00:00:00Z"}],
    }

    result = await harness.plugin.sync_rom_saves(42)

    assert result["synced"] == 0
    assert result["errors"] == []
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["type"] == "sync_conflict"
    assert conflict["server_save_id"] == 200
    assert conflict["local_hash"] == hashlib.md5(b"local diverged edit").hexdigest()
    # Nothing overwritten: local untouched, no content downloaded.
    with open(local_path, "rb") as fh:
        assert fh.read() == b"local diverged edit"
    assert not any(c[0] == "download_save_content" for c in harness.romm.call_log)


async def test_saves_upload_never_synced_device_existing_slot_conflicts(harness):
    """A device that has never synced a non-empty slot, with a local edit and no
    baseline to prove local's innocence, surfaces a conflict UP-FRONT — the #1276
    branch-6 guard decides "user decides" from the ``list_saves`` snapshot alone,
    without even attempting the POST (the former mtime-newer POST would have 409'd
    into this same conflict; #1276 just drops the wasted round-trip). Nothing is
    overwritten on either side."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    local_path = _write_local_save(harness, system="gba", content=b"local unsynced edit", filename="game.srm")
    # No baseline — the device has never synced this slot.
    seed_save_state(harness, 42, _tracked_slot_state(baseline=None))

    foreign = harness.romm.seed_foreign_save(
        42,
        uploaded_by="device-B",
        slot="default",
        filename="game.srm",
        updated_at="2026-03-01T00:00:00Z",
        content=b"server content",
    )

    result = await harness.plugin.sync_rom_saves(42)

    assert result["synced"] == 0
    assert result["errors"] == []
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["type"] == "sync_conflict"
    assert conflict["server_save_id"] == foreign["id"]
    # No POST is attempted: the kernel conflicts up-front, so nothing could be
    # overwritten and no 409 round-trip is spent.
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
    with open(local_path, "rb") as fh:
        assert fh.read() == b"local unsynced edit"
    assert not any(c[0] == "download_save_content" for c in harness.romm.call_log)


async def test_saves_resolve_conflict_keep_local_reposts_with_overwrite(harness):
    """``keep_local`` resolution POSTs a new save with overwrite=true — the user's
    content wins RomM's currency gate outright, no PUT to the existing head."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    _write_local_save(harness, system="gba", content=b"local wins", filename="game.srm")
    seed_server_save(harness, save_id=100, rom_id=42, slot="default", file_name="game.srm")
    # Server content differs from local so the adopt-without-upload short-circuit
    # cannot fire — the keep_local upload path runs.
    harness.romm.set_server_save_content(100, b"server content")
    seed_save_state(harness, 42, RomSaveState(active_slot="default", system="gba"))

    result = await harness.plugin.resolve_sync_conflict(42, "game.srm", 100, "keep_local")

    assert result["success"] is True
    assert result["action"] == "keep_local"
    upload_calls = [c for c in harness.romm.call_log if c[0] == "upload_save"]
    assert len(upload_calls) == 1
    assert upload_calls[0][2]["save_id"] is None
    assert upload_calls[0][2]["overwrite"] is True
