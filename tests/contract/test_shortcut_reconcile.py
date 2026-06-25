"""Contract tests for the sync-start shortcut reconcile callable.

Driven frontend-shaped per ``src/api/backend.ts``:
``reconcileShortcuts = callable<[number[]], {success, reason?, message, unbound_count?}>``.

The frontend reads Steam's live RomM-shortcut appIds at sync start and passes
them here; the backend unbinds every bound ``roms.shortcut_app_id`` absent from
that live set so the next sync's incremental skip recreates the shortcut the
user deleted via Steam's own UI (#1046). The contract is the response SHAPE and
the binding side effect: dead bindings cleared, live bindings untouched, rows
always kept (ADR-0007).
"""

from __future__ import annotations

from ._seed import seed_rom


async def test_unbinds_dead_appid_keeps_live(harness):
    """A bound appId not in the live set is unbound; a present one is untouched."""
    seed_rom(harness, 1, shortcut_app_id=100)
    seed_rom(harness, 2, shortcut_app_id=200)
    seed_rom(harness, 3, shortcut_app_id=300)

    result = await harness.plugin.reconcile_shortcuts([100, 200])

    assert result["success"] is True
    assert result["unbound_count"] == 1
    assert isinstance(result["message"], str)
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == 100
        assert uow.roms.get(2).shortcut_app_id == 200
        # ROM 3's shortcut was deleted in Steam → unbound, row kept.
        assert uow.roms.get(3).shortcut_app_id is None


async def test_all_present_no_unbind(harness):
    """When the live set covers every binding nothing is unbound."""
    seed_rom(harness, 1, shortcut_app_id=100)
    seed_rom(harness, 2, shortcut_app_id=200)

    result = await harness.plugin.reconcile_shortcuts([100, 200])

    assert result == {
        "success": True,
        "unbound_count": 0,
        "message": "Unbound 0 stale shortcut(s)",
    }
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id == 100
        assert uow.roms.get(2).shortcut_app_id == 200


async def test_empty_live_set_unbinds_all(harness):
    """An empty live set (scan ran, found none) unbinds every binding, keeps rows."""
    seed_rom(harness, 1, shortcut_app_id=100)
    seed_rom(harness, 2, shortcut_app_id=200)

    result = await harness.plugin.reconcile_shortcuts([])

    assert result["success"] is True
    assert result["unbound_count"] == 2
    with harness.uow_factory() as uow:
        assert uow.roms.get(1).shortcut_app_id is None
        assert uow.roms.get(2).shortcut_app_id is None


async def test_unbind_drops_row_from_app_id_rom_id_map(harness):
    """After reconcile the dead appId is gone from the map playtime/launch bakes read.

    ``get_app_id_rom_id_map`` only serves bound rows; unbinding the stale
    appId stops it being served to a nonexistent Steam app, and the same NULL
    binding is what makes the incremental skip re-fetch the platform next sync.
    """
    seed_rom(harness, 1, shortcut_app_id=100)
    seed_rom(harness, 2, shortcut_app_id=200)

    before = await harness.plugin.get_app_id_rom_id_map()
    assert before == {"100": 1, "200": 2}

    await harness.plugin.reconcile_shortcuts([100])

    after = await harness.plugin.get_app_id_rom_id_map()
    assert after == {"100": 1}
    assert "200" not in after
