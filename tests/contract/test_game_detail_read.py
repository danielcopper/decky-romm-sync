"""Contract tests for the game-detail read callables over the real nesting.

Drives the real ``main.py`` callables through the real ``bootstrap()`` + SQLite,
pinning the response shapes the frontend consumes — the version-metadata keys
added in #1295 (ADR-0019), and the BIOS answer both game-detail reads ship. The
version dimensions are server-derived facts persisted on the ``Rom`` aggregate
and surfaced read-only in the play-section "Version" row.
"""

from __future__ import annotations

from domain.rom import Rom
from domain.version_metadata import VersionMetadata


def _seed_versioned_rom(harness, **overrides):
    """Seed a bound ``Rom`` carrying version metadata; return its app_id."""
    app_id = 5000
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=42,
                platform_slug=overrides.get("platform_slug", "snes"),
                name="Chrono Trigger",
                fs_name="ct.sfc",
                shortcut_app_id=app_id,
                synced_at="2026-01-01T00:00:00Z",
                version=VersionMetadata(
                    sibling_group_key="igdb:3404:57",
                    regions=overrides.get("regions", ("USA", "Europe")),
                    languages=overrides.get("languages", ("En", "Fr")),
                    revision=overrides.get("revision", "1"),
                    tags=overrides.get("tags", ("Demo",)),
                    is_main_sibling=overrides.get("is_main_sibling", True),
                ),
                fs_size_bytes=overrides.get("fs_size_bytes", 3_145_728),
            )
        )
    return app_id


async def test_cached_game_detail_carries_version_metadata(harness):
    """The payload pins the version-metadata keys as JSON-native shapes."""
    app_id = _seed_versioned_rom(harness)

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["found"] is True
    assert result["rom_id"] == 42
    # Version metadata: tuples flattened to JSON arrays, revision a string,
    # is_main_sibling a bool.
    assert result["regions"] == ["USA", "Europe"]
    assert result["languages"] == ["En", "Fr"]
    assert result["revision"] == "1"
    assert result["tags"] == ["Demo"]
    assert result["is_main_sibling"] is True


async def test_cached_game_detail_empty_version_metadata_is_empty_shapes(harness):
    """A ROM with no version metadata still ships the keys as empty JSON shapes,
    so the frontend can hide the row without probing for undefined."""
    app_id = _seed_versioned_rom(harness, regions=(), languages=(), revision="", tags=(), is_main_sibling=False)

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["found"] is True
    assert result["regions"] == []
    assert result["languages"] == []
    assert result["revision"] == ""
    assert result["tags"] == []
    assert result["is_main_sibling"] is False


async def test_cached_game_detail_carries_fs_size_bytes(harness):
    """The payload pins the server-reported ROM size as an int (#1395)."""
    app_id = _seed_versioned_rom(harness, fs_size_bytes=3_145_728)

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["found"] is True
    assert result["fs_size_bytes"] == 3_145_728


async def test_cached_game_detail_null_fs_size_bytes_rides_as_none(harness):
    """A ROM whose size is unknown (NULL) ships the key as ``None`` so the
    frontend hides it without probing for undefined."""
    app_id = _seed_versioned_rom(harness, fs_size_bytes=None)

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["found"] is True
    assert result["fs_size_bytes"] is None


async def test_cached_game_detail_unknown_app_id_is_not_found(harness):
    """An app_id with no ROM returns the ``{found: False}`` sentinel — no version keys."""
    result = await harness.plugin.get_cached_game_detail(999999)

    assert result == {"found": False}


# ── The BIOS answer both reads ship (#1693) ──────────────────────────────────
# An absent ``bios_status`` means "the active core needs no BIOS" and clears a
# shown requirement (#1690); ``bios_status_unknown`` marks the payload that has
# no answer at all, which must not. Both callables are pinned in both directions.
# The uncovered platform (psvita) is the one where the BIOS registry cannot stand
# in for a failed firmware fetch.


async def test_cached_game_detail_cold_firmware_cache_is_unknown(harness):
    """A never-filled firmware cache carries no BIOS answer and says so."""
    app_id = _seed_versioned_rom(harness, platform_slug="psvita")

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["bios_status"] is None
    assert result["bios_status_unknown"] is True


async def test_cached_game_detail_warm_cache_without_firmware_is_answered(harness):
    """A warm cache finding no firmware is the real negative — unflagged."""
    app_id = _seed_versioned_rom(harness, platform_slug="psvita")
    # The System-page read fills the in-memory firmware cache the game-detail
    # path reads; the fake server has no firmware files.
    await harness.plugin.get_firmware_status()

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["bios_status"] is None
    assert result["bios_status_unknown"] is False


async def test_bios_status_unreachable_server_is_unknown(harness):
    """A failed firmware fetch with no registry coverage answers "unknown"."""
    _seed_versioned_rom(harness, platform_slug="psvita")
    harness.romm.list_firmware_side_effect = RuntimeError("offline")

    result = await harness.plugin.get_bios_status(42)

    assert result["bios_status"] is None
    assert result["bios_level"] is None
    assert result["bios_label"] is None
    assert result["bios_status_unknown"] is True


async def test_bios_status_reachable_server_without_firmware_is_answered(harness):
    """A reachable server with no firmware for the platform is the real negative."""
    _seed_versioned_rom(harness, platform_slug="psvita")

    result = await harness.plugin.get_bios_status(42)

    assert result["bios_status"] is None
    assert result["bios_status_unknown"] is False
