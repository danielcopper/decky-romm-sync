"""Contract tests for the library / sync read-surface callables.

Each callable is driven exactly as the frontend declares it in
``src/api/backend.ts`` — positional, JSON-shaped arguments with the TS arg
types — and the assertions pin the *response shape* (the contract), not the
delegation. Covered here:

- ``get_sync_status`` / ``sync_heartbeat`` / ``get_sync_stats``
- ``get_platforms`` (happy + server-failure)
- ``get_collections`` (happy + server-failure)
- ``get_registry_platforms``

Note on the failure shape: ``get_platforms`` / ``get_collections`` now return
the canonical ``{success: False, reason, message}`` shape used across the
callable surface. The ``reason`` slug is ``"server_unreachable"`` for a
``RommConnectionError``. The earlier legacy divergence (``error_code`` under a
separate key) has been collapsed onto the unified shape, so these assertions
pin ``reason``, not ``error_code``.
"""

from __future__ import annotations

from lib.errors import RommConnectionError

from ._seed import seed_rom

# ── get_sync_status ──────────────────────────────────────────────────────


async def test_get_sync_status_idle_shape(harness):
    """Idle: every progress field present; running is False."""
    result = await harness.plugin.get_sync_status()
    assert result == {
        "running": False,
        "stage": "",
        "current": 0,
        "total": 0,
        "message": "",
        "step": 0,
        "totalSteps": 0,
    }
    assert result["running"] is False
    for key in ("current", "total", "step", "totalSteps"):
        assert isinstance(result[key], int)


# ── sync_heartbeat ───────────────────────────────────────────────────────


async def test_sync_heartbeat_shape(harness):
    result = await harness.plugin.sync_heartbeat()
    assert result == {"success": True}


# ── get_sync_stats ───────────────────────────────────────────────────────


async def test_get_sync_stats_shape(harness):
    """Stats dict: every count key present and an int; last_sync None when never synced."""
    result = await harness.plugin.get_sync_stats()
    assert set(result.keys()) == {"last_sync", "platforms", "collections", "roms", "total_shortcuts"}
    assert result["last_sync"] is None
    for key in ("platforms", "collections", "roms", "total_shortcuts"):
        assert isinstance(result[key], int)


async def test_get_sync_stats_counts_bound_roms(harness):
    """A bound ROM row lifts the roms / total_shortcuts counts."""
    seed_rom(harness, 11, platform_slug="snes")
    result = await harness.plugin.get_sync_stats()
    assert result["roms"] == 1
    assert result["total_shortcuts"] == 1


# ── get_platforms ────────────────────────────────────────────────────────


async def test_get_platforms_happy_shape(harness):
    harness.romm.platforms = [
        {"id": 1, "name": "Super Nintendo", "slug": "snes", "rom_count": 3},
        {"id": 2, "name": "Empty", "slug": "empty", "rom_count": 0},  # filtered out
    ]
    result = await harness.plugin.get_platforms()
    assert result["success"] is True
    assert isinstance(result["platforms"], list)
    # rom_count==0 platform is filtered out
    assert [p["slug"] for p in result["platforms"]] == ["snes"]
    p = result["platforms"][0]
    assert set(p.keys()) == {"id", "name", "slug", "rom_count", "sync_enabled"}
    assert p["id"] == 1
    assert isinstance(p["sync_enabled"], bool)


async def test_get_platforms_server_failure_shape(harness):
    """Server unreachable → canonical failure shape: success False + reason + message."""
    harness.romm.list_platforms_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_platforms()
    assert result["success"] is False
    assert "platforms" not in result
    assert isinstance(result["message"], str)
    assert result["message"]  # non-empty
    # reason is the canonical slug for a RommConnectionError.
    assert result["reason"] == "server_unreachable"
    assert "error_code" not in result
    assert "error" not in result


# ── get_collections ──────────────────────────────────────────────────────


async def test_get_collections_happy_shape(harness):
    harness.romm.collections = [
        {"id": 7, "name": "Favorites", "rom_count": 2, "is_favorite": True},
    ]
    result = await harness.plugin.get_collections()
    assert result["success"] is True
    assert isinstance(result["collections"], list)
    assert len(result["collections"]) == 1
    c = result["collections"][0]
    assert c["id"] == "7"  # stringified
    assert c["name"] == "Favorites"
    assert c["kind"] == "user"
    assert isinstance(c["sync_enabled"], bool)


async def test_get_collections_server_failure_shape(harness):
    """User-collection fetch failure → canonical failure shape."""
    harness.romm.list_collections_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_collections()
    assert result["success"] is False
    assert "collections" not in result
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["reason"] == "server_unreachable"
    assert "error_code" not in result
    assert "error" not in result


# ── get_registry_platforms ───────────────────────────────────────────────


async def test_get_registry_platforms_empty_shape(harness):
    result = await harness.plugin.get_registry_platforms()
    assert result == {"platforms": []}


async def test_get_registry_platforms_counts_bound_roms(harness):
    """Registry read is offline (no RomM call) and counts bound ROMs per slug."""
    seed_rom(harness, 21, platform_slug="snes")
    seed_rom(harness, 22, platform_slug="snes")
    result = await harness.plugin.get_registry_platforms()
    assert "platforms" in result
    assert len(result["platforms"]) == 1
    entry = result["platforms"][0]
    assert set(entry.keys()) == {"name", "slug", "count"}
    assert entry["slug"] == "snes"
    assert entry["count"] == 2


# ── report_unit_results — late ack after heartbeat-timeout abandon (#1052) ────


async def test_report_unit_results_signal_shape(harness):
    """The happy path (orchestrator still waiting): record + signal, pin the shape."""
    import asyncio

    box = harness.plugin._sync_service._box
    box.current_sync_id = "run-1"
    box.active_unit_id = 1
    box.unit_complete_event = asyncio.Event()

    result = await harness.plugin.report_unit_results({"10": 9001}, "run-1", 1)

    assert result == {"success": True, "count": 1}
    assert box.unit_complete_event.is_set()
    # The orchestrator drives the commit on the happy path — nothing bound yet.
    assert await harness.plugin.get_app_id_rom_id_map() == {}


async def test_report_unit_results_stale_run_ignored(harness):
    """A late ack carrying a CANCELLED run's id is ignored — neither signalled
    nor credited to the active run (#1041). Pins the ``ignored`` shape and that
    the active run's wait event stays unset."""
    import asyncio

    box = harness.plugin._sync_service._box
    # Active run B, waiting on its own unit's event.
    box.current_sync_id = "run-B"
    box.active_unit_id = 7
    box.unit_complete_event = asyncio.Event()

    # Stale ack from the cancelled run A.
    result = await harness.plugin.report_unit_results({"10": 9001}, "run-A", 1)

    assert result == {"success": True, "count": 0, "ignored": True}
    # Run B's wait is untouched and nothing was bound.
    assert not box.unit_complete_event.is_set()
    assert await harness.plugin.get_app_id_rom_id_map() == {}


async def test_report_unit_results_late_ack_binds_orphan(harness):
    """A late ack on an abandoned unit (heartbeat timeout) commits the binding,
    so the frontend-created shortcut becomes a bound row instead of an orphan
    that the next sync re-creates as a duplicate (#1052).

    End-to-end over the real Plugin/bootstrap: set the timeout state the
    orchestrator leaves behind, call the callable frontend-shaped, then assert
    ``get_app_id_rom_id_map`` resolves the appId to the rom_id."""
    box = harness.plugin._sync_service._box
    # The state a heartbeat timeout leaves: pending_sync staged, event already
    # None (the wait returned), unit flagged abandoned with its ROMs stashed.
    # Run + unit identity survives the abandon window so the late ack validates.
    box.current_sync_id = "run-1"
    box.active_unit_id = 1
    box.pending_sync = {
        42: {
            "name": "Orphan Game",
            "fs_name": "orphan.gba",
            "platform_slug": "gba",
            "cover_path": "",
        },
    }
    box.unit_complete_event = None
    box.unit_abandoned = True
    box.pending_unit_roms = [{"id": 42}]

    # Before the ack: the appId is NOT in the map (would be an orphan).
    assert await harness.plugin.get_app_id_rom_id_map() == {}

    result = await harness.plugin.report_unit_results({"42": 100001}, "run-1", 1)

    assert result == {"success": True, "count": 1}
    # The orphan is now a bound row — the next sync's getExistingRomMShortcuts
    # maps it and takes the update branch (no duplicate).
    assert await harness.plugin.get_app_id_rom_id_map() == {"100001": 42}
    # The abandoned-unit stash is cleared so a duplicate ack no-ops.
    assert box.unit_abandoned is False
    assert box.pending_unit_roms == []
