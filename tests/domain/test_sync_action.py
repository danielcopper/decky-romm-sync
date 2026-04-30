"""Unit tests for ``domain.sync_action.compute_sync_action``.

Each test pins a specific (local_file, server_saves_in_slot, files_state,
device_id, local_hash) input shape to the ``SyncAction`` outcome the service
must dispatch. Pure-domain only — no I/O, no service fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime

from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    Upload,
    compute_sync_action,
)

DEVICE_ID = "device-abc"
OTHER_DEVICE_ID = "device-xyz"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _local(filename: str = "save.srm", mtime: float = 1_700_000_000.0) -> dict:
    return {
        "filename": filename,
        "path": f"/tmp/{filename}",
        "size": 8192,
        "mtime": mtime,
    }


def _device_sync(device_id: str, is_current: bool) -> dict:
    return {"device_id": device_id, "is_current": is_current}


def _server_save(
    save_id: int = 1,
    updated_at: str = "2024-01-01T12:00:00+00:00",
    slot: int = 0,
    device_syncs: list[dict] | None = None,
) -> dict:
    return {
        "id": save_id,
        "slot": slot,
        "updated_at": updated_at,
        "device_syncs": device_syncs or [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_server_no_local_returns_skip_nothing_to_sync():
    result = compute_sync_action(
        local_file=None,
        server_saves_in_slot=[],
        files_state={},
        device_id=DEVICE_ID,
        local_hash=None,
    )
    assert result == Skip(reason="nothing_to_sync")


def test_empty_server_with_local_returns_upload_post():
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Upload(target_save_id=None)


def test_synced_state_returns_skip_synced():
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Skip(reason="synced")


def test_is_current_true_local_diverged_returns_upload_put():
    """is_current=true + local diverged from baseline → Upload (PUT) the local
    content against the existing server save id."""
    server = _server_save(save_id=42, device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result == Upload(target_save_id=42)


def test_recovery_no_local_is_current_true_returns_download():
    """Row 4 — local file gone, server still tracks our last upload as current."""
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=None,
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash=None,
    )
    assert result == Download(server_save=server)


def test_no_baseline_is_current_true_returns_skip_with_adopt_baseline():
    """Row 8 — is_current=true + local present + no baseline yet → Skip with
    adopt_baseline=True so the service records the current local_hash as the
    new baseline.
    """
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Skip(reason="synced", adopt_baseline=True)


def test_no_baseline_is_current_false_returns_download():
    """Row 11 — is_current=false + local present + no baseline → Download.
    Without a baseline we cannot claim drift, so the server wins outright
    (no mtime split here)."""
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=False)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Download(server_save=server)


def test_server_changed_local_unchanged_returns_download():
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=False)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Download(server_save=server)


def test_both_changed_returns_conflict():
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=False)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result == Conflict(server_save=server)


def test_no_device_entry_no_local_returns_download():
    server = _server_save(device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=None,
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash=None,
    )
    assert result == Download(server_save=server)


def test_no_device_entry_local_newer_returns_upload_post():
    """Row 6a — no entry for our device + local mtime >= server.updated_at →
    POST our local as a new save (target_save_id=None)."""
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),  # 1 hour newer
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Upload(target_save_id=None)


def test_no_device_entry_local_older_returns_download():
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch - 3600),  # 1 hour older
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Download(server_save=server)


def test_multiple_server_saves_picks_newest():
    older = _server_save(
        save_id=1,
        updated_at="2024-01-01T00:00:00+00:00",
        device_syncs=[_device_sync(DEVICE_ID, is_current=True)],
    )
    newer = _server_save(
        save_id=2,
        updated_at="2024-06-01T00:00:00+00:00",
        device_syncs=[_device_sync(DEVICE_ID, is_current=False)],
    )
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[older, newer],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    # Newest is the one with is_current=false → Download
    assert result == Download(server_save=newer)


def test_first_sync_no_state_no_local_one_server_returns_download():
    server = _server_save(device_syncs=[])  # no device_syncs at all
    result = compute_sync_action(
        local_file=None,
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash=None,
    )
    assert result == Download(server_save=server)


def test_local_hash_none_skips_divergence_check_in_synced_branch():
    """Defensive: when local_hash is None we cannot detect divergence, so
    is_current=true falls through to Skip("synced") instead of Conflict.
    """
    server = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash=None,
    )
    assert result == Skip(reason="synced")


def test_last_sync_hash_none_skips_divergence_check():
    """No `last_sync_hash` baseline → cannot detect divergence on either branch.

    is_current=true: Skip with adopt_baseline=True so the missing baseline
    gets recorded for next time.
    is_current=false: Download (server wins; no claim of divergence possible).
    """
    # is_current=true branch
    server_current = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result_current = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server_current],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result_current == Skip(reason="synced", adopt_baseline=True)

    # is_current=false branch (server moved): silent download, no conflict
    server_moved = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=False)])
    result_moved = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server_moved],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result_moved == Download(server_save=server_moved)


def test_zulu_timestamp_is_parsed_for_local_newer_comparison():
    """`updated_at` ending in Z must be normalized before fromisoformat."""
    server = _server_save(
        updated_at="2024-01-01T12:00:00Z",
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    server_epoch = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 1),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Upload(target_save_id=None)


def test_no_device_entry_local_mtime_equals_server_epoch_returns_upload_post():
    """Boundary: `local_mtime == server_epoch` must satisfy the `>=` semantics
    and result in Upload(target=None) (POST), not Download.
    """
    server_updated_at = "2026-04-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch),  # exactly equal
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Upload(target_save_id=None)


def test_no_device_entry_garbled_server_updated_at_returns_download():
    """Parse-failure path: unparseable server `updated_at` → server effectively
    wins (Download), per the conservative-fallthrough contract.
    """
    server = _server_save(
        updated_at="not a date",
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result = compute_sync_action(
        local_file=_local(mtime=1_700_000_000.0),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Download(server_save=server)


def test_no_device_entry_non_numeric_local_mtime_returns_download():
    """Parse-failure path: local mtime is a string instead of a number → server
    effectively wins (Download).
    """
    server = _server_save(
        updated_at="2024-01-01T12:00:00+00:00",
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    local_file = {
        "filename": "save.srm",
        "path": "/tmp/save.srm",
        "size": 8192,
        "mtime": "garbage",
    }
    result = compute_sync_action(
        local_file=local_file,
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Download(server_save=server)
