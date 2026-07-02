"""Unit tests for ``domain.sync_action.compute_sync_action``.

Each test pins a specific (local_file, server_saves_in_slot, files_state,
device_id, local_hash) input shape to the ``SyncAction`` outcome the service
must dispatch. Pure-domain only — no I/O, no service fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    Upload,
    compute_sync_action,
    resolve_upload_conflict,
)

DEVICE_ID = "device-abc"
OTHER_DEVICE_ID = "device-xyz"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _local(filename: str = "save.srm", mtime: float = 1_700_000_000.0, size: int = 8192) -> dict[str, Any]:
    return {
        "filename": filename,
        "path": f"/tmp/{filename}",
        "size": size,
        "mtime": mtime,
    }


def _device_sync(device_id: str, is_current: bool) -> dict[str, Any]:
    return {"device_id": device_id, "is_current": is_current}


def _server_save(
    save_id: int = 1,
    updated_at: str = "2024-01-01T12:00:00+00:00",
    slot: int = 0,
    device_syncs: list[dict[str, Any]] | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    save: dict[str, Any] = {
        "id": save_id,
        "slot": slot,
        "updated_at": updated_at,
        "device_syncs": device_syncs or [],
    }
    # Older / migrated server saves may lack ``content_hash``; only stamp it when
    # a caller supplies one, so the missing-key path stays exercisable.
    if content_hash is not None:
        save["content_hash"] = content_hash
    return save


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


def test_is_current_true_zero_byte_local_returns_conflict():
    """Row 9b / #1062 — is_current=true + diverged local that is 0 bytes → Conflict,
    NOT an in-place PUT. RomM versions only on POST, so a PUT of the empty file
    would overwrite the only good server copy with no recoverable version.
    """
    server = _server_save(save_id=42, device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(size=0),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc", "last_sync_local_size": 8192},
        device_id=DEVICE_ID,
        local_hash="EMPTY-HASH",  # diverges from baseline
    )
    assert result == Conflict(server_save=server)


def test_is_current_true_zero_byte_local_no_baseline_size_returns_conflict():
    """Row 9b / #1062 — the 0-byte gate is unconditional: even with no recorded
    ``last_sync_local_size`` baseline, a diverged 0-byte local is a Conflict, not a PUT.
    """
    server = _server_save(save_id=42, device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(size=0),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},  # no last_sync_local_size
        device_id=DEVICE_ID,
        local_hash="EMPTY-HASH",
    )
    assert result == Conflict(server_save=server)


def test_is_current_true_shrunken_local_returns_conflict():
    """Row 9b / #1062 — is_current=true + diverged local dramatically smaller than
    the recorded baseline size (truncated / partial write) → Conflict, not PUT.
    """
    server = _server_save(save_id=42, device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(size=100),  # < 50% of 8192
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc", "last_sync_local_size": 8192},
        device_id=DEVICE_ID,
        local_hash="TRUNCATED",
    )
    assert result == Conflict(server_save=server)


def test_is_current_true_plausible_size_diverged_still_uploads_put():
    """Row 9 / #1062 regression — is_current=true + diverged local of a plausible
    size (no shrink) still PUTs in place. The guard must NOT fire on a normal edit.
    """
    server = _server_save(save_id=42, device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(size=8000),  # ~same size as baseline
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc", "last_sync_local_size": 8192},
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result == Upload(target_save_id=42)


def test_is_current_true_grown_local_uploads_put():
    """Row 9 / #1062 regression — a diverged local that GREW past the baseline is a
    plausible edit → PUT, never Conflict.
    """
    server = _server_save(save_id=42, device_syncs=[_device_sync(DEVICE_ID, is_current=True)])
    result = compute_sync_action(
        local_file=_local(size=16384),  # larger than baseline
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc", "last_sync_local_size": 8192},
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


def test_no_baseline_is_current_false_content_identical_returns_download():
    """Row 11a (#1276) — is_current=false + local present + no baseline, and the
    picked server save's ``content_hash`` equals ``local_hash`` (byte-identical).
    The local bytes already exist on the moved-past server head, so adopting via
    Download is harmless and re-establishes the baseline + is_current — no
    spurious Conflict.
    """
    server = _server_save(
        content_hash="same-hash",
        device_syncs=[_device_sync(DEVICE_ID, is_current=False)],
    )
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="same-hash",  # byte-identical to the server content_hash
    )
    assert result == Download(server_save=server)


def test_no_baseline_is_current_false_content_differs_returns_conflict():
    """Row 11b (#1276) — is_current=false + local present + no baseline, and the
    picked server save's ``content_hash`` differs from ``local_hash``. Without a
    baseline we cannot prove the local is unchanged, and it is NOT byte-identical
    to the moved-past head, so silently downloading would clobber an unbacked
    local edit. This is the "no assumptions, user decides" case → Conflict.
    """
    server = _server_save(
        content_hash="server-hash",
        device_syncs=[_device_sync(DEVICE_ID, is_current=False)],
    )
    result = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="local-hash",  # differs from the server content_hash
    )
    assert result == Conflict(server_save=server)


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


def test_no_device_entry_no_baseline_local_newer_returns_conflict():
    """Branch 6 / #1276 — no entry for our device, a present local with a real
    hash but NO baseline, not byte-identical to the head (the server save carries
    no ``content_hash``). Even though local mtime >= server.updated_at, the
    unknown-provenance local is the "user decides" case → ``Conflict`` (branch-5
    parity), not a silent mtime-based ``Upload(POST)``.
    """
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
    assert result == Conflict(server_save=server)


def test_no_device_entry_no_baseline_local_older_returns_conflict():
    """Branch 6 / #1276 — same no-baseline differing-local slice, local mtime
    older than the head. Mtime direction is irrelevant: the unknown-provenance
    local → ``Conflict`` (branch-5 parity), never a silent ``Download`` that
    would overwrite the unbacked local edit.
    """
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
    assert result == Conflict(server_save=server)


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
    is_current=false (#1276): with no baseline AND no proof the local is
    byte-identical to the moved-past server head (the save here carries no
    ``content_hash``), silently downloading would clobber an unbacked local
    edit — so this is a Conflict, not a silent Download.
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

    # is_current=false branch (server moved), no content_hash to prove identity:
    # cannot silently overwrite an unbacked local → Conflict.
    server_moved = _server_save(device_syncs=[_device_sync(DEVICE_ID, is_current=False)])
    result_moved = compute_sync_action(
        local_file=_local(),
        server_saves_in_slot=[server_moved],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result_moved == Conflict(server_save=server_moved)


def test_zulu_timestamp_is_parsed_for_local_newer_comparison():
    """`updated_at` ending in Z must be normalized before fromisoformat.

    Exercised on the trailing mtime path (``local_hash=None`` — the unknown-local
    case the mtime fallback still handles after #1276): the Z-suffixed server
    ``updated_at`` is parsed so the local-newer comparison lands on ``Upload``.
    """
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
        local_hash=None,
    )
    assert result == Upload(target_save_id=None)


def test_no_device_entry_local_mtime_equals_server_epoch_returns_upload_post():
    """Boundary: `local_mtime == server_epoch` must satisfy the `>=` semantics
    and result in Upload(target=None) (POST), not Download.

    Exercised on the trailing mtime path with ``local_hash=None`` — the
    unknown-local case the mtime fallback still handles after #1276 (a present
    local with a real, differing hash now routes to ``Conflict`` before reaching
    this comparison).
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
        local_hash=None,
    )
    assert result == Upload(target_save_id=None)


def test_no_device_entry_baseline_diverged_server_newer_returns_conflict():
    """Branch 6 / #1059 — no entry for our device on the newest server save, but
    we hold a baseline and local has diverged from it. The chosen head is a save
    we never synced (a NEW save id became the slot head while we played offline),
    so both sides moved → Conflict, mirroring branch 5. Today branch 6 does
    mtime-only and Downloads here, silently replacing the diverged local.
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch - 3600),  # local older → today Downloads
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result == Conflict(server_save=server)


def test_no_device_entry_baseline_diverged_local_newer_returns_conflict():
    """Branch 6 / #1059 — same divergence-from-baseline case, but local mtime is
    newer than the server save. Divergence beats mtime: both sides moved → Conflict,
    not Upload. Today branch 6 does mtime-only and Uploads here.
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),  # local newer → today Uploads
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result == Conflict(server_save=server)


def test_no_device_entry_baseline_matches_preserves_mtime_behavior():
    """Branch 6 / #1059 — no entry, baseline present, local_hash == last_sync_hash
    (no divergence). The Conflict guard must NOT fire; the existing mtime split is
    preserved: Download when local older, Upload(None) when local newer-or-equal.
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()

    # local older → Download
    server_a = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result_older = compute_sync_action(
        local_file=_local(mtime=server_epoch - 3600),
        server_saves_in_slot=[server_a],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="abc",  # matches baseline → no divergence
    )
    assert result_older == Download(server_save=server_a)

    # local newer-or-equal → Upload(None) (POST)
    server_b = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result_newer = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),
        server_saves_in_slot=[server_b],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="abc",  # matches baseline → no divergence
    )
    assert result_newer == Upload(target_save_id=None)


def test_no_device_entry_no_baseline_differing_local_returns_conflict_both_directions():
    """Branch 6 / #1276 — no entry, NO baseline, a present local with a real hash
    not byte-identical to the head (no ``content_hash`` to dedup against). The
    unknown-provenance local is the "user decides" case in BOTH mtime directions:
    ``Conflict`` whether local is older or newer than the head — never a silent
    mtime-based Download (older) or Upload (newer). Branch-5 parity.
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()

    # local older → Conflict (was a silent Download before #1276)
    server_a = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result_older = compute_sync_action(
        local_file=_local(mtime=server_epoch - 3600),
        server_saves_in_slot=[server_a],
        files_state={},  # no baseline
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result_older == Conflict(server_save=server_a)

    # local newer → Conflict (was Upload(None) before #1276)
    server_b = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    result_newer = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),
        server_saves_in_slot=[server_b],
        files_state={},  # no baseline
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",
    )
    assert result_newer == Conflict(server_save=server_b)


def test_no_device_entry_identical_content_returns_skip_adopt_baseline():
    """Branch 6 / #1013 — no entry for our device, local present, and the picked
    server save's ``content_hash`` equals ``local_hash`` (copied SD card, restored
    backup, fresh reinstall). The local bytes already exist on the server, so adopt
    that save as the baseline (``Skip(synced, adopt_baseline=True)``) instead of
    POSTing a duplicate — even when local mtime is at-or-after the server's
    ``updated_at`` (today branch 6 does mtime-only and returns ``Upload(None)``).
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    server["content_hash"] = "abc"
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),  # local newer → today Uploads
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",  # byte-identical to the server save
    )
    assert result == Skip(reason="synced", adopt_baseline=True)


def test_no_device_entry_no_baseline_different_content_returns_conflict():
    """Branch 6 / #1013 + #1276 — no entry, local present with NO baseline, server
    ``content_hash`` differs from ``local_hash``, local mtime >= server. The dedup
    guard must NOT fire on a hash mismatch; without a baseline the differing local
    is unknown-provenance → ``Conflict`` (branch-5 parity), not a silent
    mtime-based ``Upload(None)``.
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    server["content_hash"] = "server-hash"
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="local-hash",  # differs → not a duplicate
    )
    assert result == Conflict(server_save=server)


def test_no_device_entry_no_baseline_missing_content_hash_returns_conflict():
    """Branch 6 / #1013 + #1276 — no entry, local present with NO baseline, the
    server save carries NO ``content_hash`` key (older / migrated saves may lack
    it). The dedup check is skipped, but without a baseline the present local of
    unknown provenance can't be silently mtime-picked → ``Conflict`` (branch-5
    parity), even though local mtime >= server. The mtime fallback's remaining
    duplicate-POST gap now applies only when local matches a held baseline (see
    ``test_no_device_entry_baseline_matches_preserves_mtime_behavior``).
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    # No "content_hash" key at all.
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),
        server_saves_in_slot=[server],
        files_state={},
        device_id=DEVICE_ID,
        local_hash="abc",
    )
    assert result == Conflict(server_save=server)


def test_no_device_entry_diverged_baseline_with_different_content_hash_returns_conflict():
    """Branch 6 / #1059 regression with #1013 present — no entry, baseline held and
    local diverged from it (``local_hash != last_sync_hash``), and the server's
    ``content_hash`` differs from ``local_hash`` too. The dedup guard must not
    swallow this: content differs from both baseline and head → ``Conflict``.
    """
    server_updated_at = "2024-01-01T12:00:00+00:00"
    server_epoch = datetime.fromisoformat(server_updated_at).timestamp()
    server = _server_save(
        updated_at=server_updated_at,
        device_syncs=[_device_sync(OTHER_DEVICE_ID, is_current=True)],
    )
    server["content_hash"] = "server-hash"  # != local, so dedup must not fire
    result = compute_sync_action(
        local_file=_local(mtime=server_epoch + 3600),
        server_saves_in_slot=[server],
        files_state={"last_sync_hash": "abc"},
        device_id=DEVICE_ID,
        local_hash="DIFFERENT",  # diverged from baseline AND from server head
    )
    assert result == Conflict(server_save=server)


def test_no_device_entry_garbled_server_updated_at_returns_download():
    """Parse-failure path: unparseable server `updated_at` → server effectively
    wins (Download), per the conservative-fallthrough contract.

    Exercised on the trailing mtime path (``local_hash=None`` — the unknown-local
    case the mtime fallback still handles after #1276; a present local with a
    real hash and no baseline routes to ``Conflict`` before this comparison).
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
        local_hash=None,
    )
    assert result == Download(server_save=server)


def test_no_device_entry_non_numeric_local_mtime_returns_download():
    """Parse-failure path: local mtime is a string instead of a number → server
    effectively wins (Download).

    Exercised on the trailing mtime path (``local_hash=None``), the unknown-local
    case the mtime fallback still handles after #1276.
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
        local_hash=None,
    )
    assert result == Download(server_save=server)


# ---------------------------------------------------------------------------
# resolve_upload_conflict — the 409 write-time backstop (ADR-0017 / #1276)
# ---------------------------------------------------------------------------


def test_resolve_upload_conflict_local_matches_baseline_downloads():
    """Local unchanged since our own recorded baseline (``local_hash ==
    last_sync_hash``) → nothing of ours to protect, adopt the server via
    ``"download"``.
    """
    assert resolve_upload_conflict("abc", "abc", None) == "download"


def test_resolve_upload_conflict_local_matches_server_content_downloads():
    """Local diverged from our recorded baseline but is byte-identical to what
    the server now holds (``local_hash == server_content_hash``, ``!=
    last_sync_hash``) → still nothing of ours to protect → ``"download"``.
    """
    assert resolve_upload_conflict("abc", "old-baseline", "abc") == "download"


def test_resolve_upload_conflict_diverged_from_both_conflicts():
    """Local matches neither our baseline nor the server's current content →
    two-sided divergence (which the 409 proves) → ``"conflict"``.
    """
    assert resolve_upload_conflict("local", "baseline", "server") == "conflict"


def test_resolve_upload_conflict_local_hash_none_conflicts():
    """Missing ``local_hash`` → we cannot prove the local is unchanged, so the
    safe default under uncertainty is ``"conflict"`` — never ``"download"`` even
    when the baseline and server content agree.
    """
    assert resolve_upload_conflict(None, "abc", "abc") == "conflict"


def test_resolve_upload_conflict_no_baseline_no_server_content_conflicts():
    """``last_sync_hash`` and ``server_content_hash`` both ``None`` → no evidence
    the local is unchanged → ``"conflict"``.
    """
    assert resolve_upload_conflict("abc", None, None) == "conflict"


def test_resolve_upload_conflict_empty_local_hash_does_not_match_none():
    """An empty-string hash (``""``) can never read as "provably unchanged".

    ``""`` is a degenerate value: with the old ``is not None`` guards an empty
    ``local_hash`` against a ``None`` baseline/server stayed ``"conflict"``, but
    an empty ``local_hash`` matching an empty baseline would have read as equal
    and downloaded. The truthiness guards forbid any empty-string match, so every
    combination involving an empty hash stays ``"conflict"``.
    """
    assert resolve_upload_conflict("", None, None) == "conflict"
    assert resolve_upload_conflict("", "abc", "def") == "conflict"


def test_resolve_upload_conflict_empty_local_and_baseline_conflicts():
    """Empty ``local_hash`` AND empty ``last_sync_hash`` must not coincidentally
    match as "unchanged" — the truthiness guard keeps it ``"conflict"`` (the
    old ``is not None`` guards would have equated ``"" == ""`` → ``"download"``).
    """
    assert resolve_upload_conflict("", "", None) == "conflict"


def test_resolve_upload_conflict_empty_local_and_server_content_conflicts():
    """Empty ``local_hash`` AND empty ``server_content_hash`` must not
    coincidentally match — the truthiness guard keeps it ``"conflict"``.
    """
    assert resolve_upload_conflict("", None, "") == "conflict"
