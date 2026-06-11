"""Contract tests for the library / sync read-surface callables.

Each callable is driven exactly as the frontend declares it in
``src/api/backend.ts`` — positional, JSON-shaped arguments with the TS arg
types — and the assertions pin the *response shape* (the contract), not the
delegation. Covered here:

- ``get_sync_status`` / ``sync_heartbeat`` / ``get_sync_stats``
- ``get_platforms`` (happy + server-failure)
- ``get_collections`` (happy + server-failure)
- ``get_registry_platforms``

Note on the failure shape: ``get_platforms`` / ``get_collections`` return
``{success: False, message, error_code}`` — the *legacy* failure shape that
predates the canonical ``{success, reason, message}`` used by the save
callables. These contract tests assert the shape the backend *actually*
returns (``error_code``, not ``reason``); pinning the real divergence is the
point of this tier. ``error_code`` is the ``classify_error`` slug
(``"connection_error"`` for a ``RommConnectionError``). When #972 (collapse
the failure-shape dialects onto the canonical shape) lands, these assertions
will break — that is the intended coupling; update them to the unified shape
as part of that fix.
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
    """Server unreachable → legacy failure shape: success False + message + error_code."""
    harness.romm.list_platforms_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_platforms()
    assert result["success"] is False
    assert "platforms" not in result
    assert isinstance(result["message"], str)
    assert result["message"]  # non-empty
    # error_code is classify_error's slug (NOT a `reason` field) — the legacy shape.
    assert result["error_code"] == "connection_error"
    assert "reason" not in result


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
    """User-collection fetch failure → legacy failure shape."""
    harness.romm.list_collections_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_collections()
    assert result["success"] is False
    assert "collections" not in result
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["error_code"] == "connection_error"
    assert "reason" not in result


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
