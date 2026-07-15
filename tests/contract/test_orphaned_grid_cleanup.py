"""Contract tests for ``cleanup_orphaned_grid_images`` (#1385).

The Danger Zone's orphaned grid-image cleanup driven frontend-shaped over the
real wired plugin: positional ``(live_app_ids, dry_run)`` exactly as
``src/api/backend.ts`` declares. Pins the dry-run/real success shapes, the
``incomplete_scan`` sanity-guard refusal (a bound ``roms.shortcut_app_id``
missing from the submitted live set deletes nothing), and the
``sync_active`` gate refusal while a library sync is in flight.
"""

from __future__ import annotations

import pytest

from domain.sync_state import SyncState

from ._seed import seed_rom

# In-range non-Steam shortcut appIds (high bit set — Steam's assignment range).
_ORPHAN_APP_ID = 2200000001
_FOREIGN_APP_ID = 2200000002
_BOUND_APP_ID = 2200000003

# A regular Steam game's appId — its custom art must never be a candidate.
_STORE_APP_ID = 570


def _make_grid(harness):
    """Materialise a Steam userdata grid dir under the harness home; return it."""
    grid = harness.tmp_path / "home" / ".steam" / "steam" / "userdata" / "12345" / "config" / "grid"
    grid.mkdir(parents=True)
    return grid


async def test_dry_run_counts_candidates_without_deleting(harness):
    grid = _make_grid(harness)
    orphan = grid / f"{_ORPHAN_APP_ID}p.png"
    foreign = grid / f"{_FOREIGN_APP_ID}p.png"
    store_art = grid / f"{_STORE_APP_ID}p.png"
    for f in (orphan, foreign, store_art):
        f.write_bytes(b"art")

    result = await harness.plugin.cleanup_orphaned_grid_images([_FOREIGN_APP_ID], True)

    assert result == {"success": True, "candidate_count": 1}
    assert orphan.exists()
    assert foreign.exists()
    assert store_art.exists()


async def test_real_run_deletes_only_the_orphan(harness):
    grid = _make_grid(harness)
    seed_rom(harness, 42, shortcut_app_id=_BOUND_APP_ID)
    orphan_portrait = grid / f"{_ORPHAN_APP_ID}p.png"
    orphan_hero = grid / f"{_ORPHAN_APP_ID}_hero.jpg"
    bound_art = grid / f"{_BOUND_APP_ID}p.png"
    foreign = grid / f"{_FOREIGN_APP_ID}.png"
    store_art = grid / f"{_STORE_APP_ID}_hero.png"
    for f in (orphan_portrait, orphan_hero, bound_art, foreign, store_art):
        f.write_bytes(b"art")

    result = await harness.plugin.cleanup_orphaned_grid_images([_BOUND_APP_ID, _FOREIGN_APP_ID], False)

    assert result == {"success": True, "removed_count": 2}
    assert not orphan_portrait.exists()
    assert not orphan_hero.exists()
    assert bound_art.exists()
    assert foreign.exists()
    assert store_art.exists()


async def test_incomplete_scan_refusal_shape_and_no_deletion(harness):
    grid = _make_grid(harness)
    seed_rom(harness, 42, shortcut_app_id=_BOUND_APP_ID)
    orphan = grid / f"{_ORPHAN_APP_ID}p.png"
    orphan.write_bytes(b"art")

    # The live set omits the bound appId — the scan is provably incomplete.
    result = await harness.plugin.cleanup_orphaned_grid_images([_FOREIGN_APP_ID], False)

    assert set(result) == {"success", "reason", "message"}
    assert result["success"] is False
    assert result["reason"] == "incomplete_scan"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert orphan.exists()


async def test_no_grid_dir_failure_shape(harness):
    # No Steam userdata dir under the harness home — grid_dir() resolves None.
    result = await harness.plugin.cleanup_orphaned_grid_images([], True)

    assert result == {
        "success": False,
        "reason": "no_grid_dir",
        "message": "Steam grid directory not found",
    }


@pytest.mark.parametrize("state", [SyncState.RUNNING, SyncState.CANCELLING])
async def test_refused_while_sync_in_flight(harness, state):
    grid = _make_grid(harness)
    orphan = grid / f"{_ORPHAN_APP_ID}p.png"
    orphan.write_bytes(b"art")
    harness.plugin._sync_service._box.sync_state = state

    result = await harness.plugin.cleanup_orphaned_grid_images([], False)

    assert set(result) == {"success", "reason", "message"}
    assert result["success"] is False
    assert result["reason"] == "sync_active"
    assert orphan.exists()
