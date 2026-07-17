"""Contract tests for the server-hash baseline identity check (#1468).

Drives the real ``Plugin`` over the real ``bootstrap`` to prove the two identity
routes at the wire:

- **Provenance** (primary): a file with sync history whose stored server hash
  (``last_sync_server_hash``) matches the server head's ``content_hash`` syncs
  cleanly even when the LOCALLY-recomputed parity hash no longer matches the
  server's — the scheme-drift robustness the issue is about. No parity
  agreement is consulted.
- **Parity** (fallback): a fresh-install / copied-SD-card file with NO sync
  history (no stored server hash) whose bytes are byte-identical to the server
  head is still adopted via the direct ``local_hash == content_hash`` check.
"""

from __future__ import annotations

import hashlib
import os

from domain.rom_save_state import FileSyncState, RomSaveState

from ._seed import enable_save_sync, seed_install, seed_save_state, seed_server_save


def _write_local_save(harness, *, system: str, content: bytes, filename: str) -> None:
    saves_dir = os.path.join(harness.plugin._retrodeck_paths.saves_path(), system)
    os.makedirs(saves_dir, exist_ok=True)
    with open(os.path.join(saves_dir, filename), "wb") as fh:
        fh.write(content)


async def test_provenance_syncs_cleanly_under_scheme_drift(harness):
    """A synced file whose local parity recomputation is forced to mismatch the
    server's ``content_hash`` still syncs cleanly via the stored server hash.

    The server head carries a ``content_hash`` (``drifted-server-hash``) that
    does NOT equal the local file's real content hash — the exact situation a
    change in RomM's hashing would produce. Because we stored that server hash at
    the last sync AND the local file is unchanged since then, the provenance
    route proves byte-identity without any parity agreement: the kernel adopts
    (``Skip(adopt_baseline=True)``), so the sync transfers nothing and raises no
    conflict.
    """
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    content = b"unchanged local save bytes"
    _write_local_save(harness, system="gba", content=content, filename="game.srm")
    local_hash = hashlib.md5(content).hexdigest()

    drifted_server_hash = "drifted-server-hash"
    assert drifted_server_hash != local_hash  # parity would fail

    # Sync history: our baseline matches the current local (unchanged) and stores
    # the server's own hash from that sync.
    state = RomSaveState(
        active_slot="default",
        slot_confirmed=True,
        system="gba",
        files={
            "game.srm": FileSyncState(
                tracked_save_id=100,
                last_sync_hash=local_hash,
                last_sync_server_hash=drifted_server_hash,
            )
        },
    )
    seed_save_state(harness, 42, state)

    # The server head: same slot, no device_syncs row for us (branch 6), and a
    # content_hash equal to our stored server hash but NOT the local parity hash.
    entry = seed_server_save(harness, save_id=100, rom_id=42, slot="default", file_name="game.srm")
    entry["content_hash"] = drifted_server_hash

    status = await harness.plugin.get_save_status(42)
    files = {f["filename"]: f for f in status["files"]}
    assert files["game.srm"]["status"] == "synced"
    assert status["conflicts"] == []

    result = await harness.plugin.sync_rom_saves(42)
    assert result["success"] is True
    assert result["synced"] == 0
    assert result["conflicts"] == []
    assert result["errors"] == []

    # Provenance carried it: no transfer despite the parity mismatch.
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
    assert not any(c[0] == "download_save_content" for c in harness.romm.call_log)


async def test_parity_fallback_adopts_fresh_install(harness):
    """A fresh-install byte-identical file with NO sync history adopts via parity.

    No baseline, so no stored server hash — the only route is the direct
    ``local_hash == server.content_hash`` parity check. The byte-identical head
    is adopted (``Skip(adopt_baseline=True)``), never re-POSTed as a duplicate.
    """
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", file_name="game.gba")
    content = b"fresh install identical bytes"
    _write_local_save(harness, system="gba", content=content, filename="game.srm")
    local_hash = hashlib.md5(content).hexdigest()

    # Confirmed slot, but no per-file baseline (never synced this device).
    seed_save_state(harness, 42, RomSaveState(active_slot="default", slot_confirmed=True, system="gba"))

    entry = seed_server_save(harness, save_id=100, rom_id=42, slot="default", file_name="game.srm")
    entry["content_hash"] = local_hash  # byte-identical → parity route

    result = await harness.plugin.sync_rom_saves(42)
    assert result["success"] is True
    assert result["synced"] == 0
    assert result["conflicts"] == []
    assert result["errors"] == []
    assert not any(c[0] == "upload_save" for c in harness.romm.call_log)
    assert not any(c[0] == "download_save_content" for c in harness.romm.call_log)
