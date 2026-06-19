"""Contract tests for the platform-toggle callables (#1007).

Drives the real ``Plugin`` over the real ``bootstrap`` to pin the
end-to-end behavior the data-loss fix restores: opening the Platforms page
(``get_platforms``) materializes the full per-platform enabled map into the
real ``settings.json``, and un-toggling exactly ONE platform
(``save_platform_sync``) leaves every other platform enabled — both on disk
and in the sync-time platform filter.

Each callable is driven exactly as the frontend declares it in
``src/api/backend.ts``: ``get_platforms()`` zero-arg, ``save_platform_sync``
with ``(platform_id: number, enabled: boolean)``.
"""

from __future__ import annotations

import json
import os
from typing import Any


def _read_settings(harness) -> dict[str, Any]:
    """Read the real on-disk ``settings.json`` the bootstrap wrote under tmp_path."""
    path = os.path.join(str(harness.tmp_path / "settings"), "settings.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def test_get_platforms_materializes_full_map_to_disk(harness):
    """get_platforms over the empty default persists the full all-True map."""
    harness.romm.platforms = [
        {"id": 1, "name": "Super Nintendo", "slug": "snes", "rom_count": 3},
        {"id": 2, "name": "Nintendo 64", "slug": "n64", "rom_count": 4},
        {"id": 3, "name": "Empty", "slug": "empty", "rom_count": 0},  # filtered out
    ]

    result = await harness.plugin.get_platforms()

    assert result["success"] is True
    assert [p["slug"] for p in result["platforms"]] == ["snes", "n64"]
    assert all(p["sync_enabled"] is True for p in result["platforms"])

    # The full all-True map round-trips through the real crash-safe write.
    on_disk = _read_settings(harness)
    assert on_disk["enabled_platforms"] == {"1": True, "2": True}


async def test_one_off_toggle_leaves_other_platforms_enabled(harness):
    """#1007: get_platforms → save_platform_sync(one, False) → others stay enabled.

    Pins the fix across the real settings round-trip and the sync-time filter
    that drives stale-ROM removal: before the fix, the single OFF write turned
    the empty sentinel into a one-entry map and the filter dropped every other
    platform, marking their bound ROMs stale.
    """
    harness.romm.platforms = [
        {"id": 1, "name": "Super Nintendo", "slug": "snes", "rom_count": 3},
        {"id": 2, "name": "Nintendo 64", "slug": "n64", "rom_count": 4},
        {"id": 3, "name": "Game Boy Advance", "slug": "gba", "rom_count": 5},
    ]

    # 1. Platforms page mount materializes the full map.
    await harness.plugin.get_platforms()

    # 2. Un-toggle exactly one platform (frontend: number id, bool enabled).
    toggle_result = await harness.plugin.save_platform_sync(2, False)
    assert toggle_result == {"success": True}

    # 3. The on-disk map is a true partial update — only id 2 flipped.
    on_disk = _read_settings(harness)
    assert on_disk["enabled_platforms"] == {"1": True, "2": False, "3": True}

    # 4. The sync-time filter keeps every OTHER platform.
    filtered = await harness.plugin._sync_service._fetcher._fetch_enabled_platforms()
    kept_slugs = {p["slug"] for p in filtered}
    assert kept_slugs == {"snes", "gba"}
    assert "n64" not in kept_slugs


async def test_get_platforms_idempotent_after_materialization(harness):
    """A second get_platforms over a now-explicit map does not re-write or clobber."""
    harness.romm.platforms = [
        {"id": 1, "name": "Super Nintendo", "slug": "snes", "rom_count": 3},
        {"id": 2, "name": "Nintendo 64", "slug": "n64", "rom_count": 4},
    ]

    await harness.plugin.get_platforms()
    await harness.plugin.save_platform_sync(1, False)

    # Re-opening the page reads the explicit map literally — no re-materialize.
    result = await harness.plugin.get_platforms()
    by_id = {p["id"]: p["sync_enabled"] for p in result["platforms"]}
    assert by_id == {1: False, 2: True}

    on_disk = _read_settings(harness)
    assert on_disk["enabled_platforms"] == {"1": False, "2": True}
