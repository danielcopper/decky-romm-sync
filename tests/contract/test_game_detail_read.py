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
# The harness has no emulator installation, so every answer here is the one a
# machine that cannot state its own scope gives.


async def test_cached_game_detail_cold_firmware_cache_is_unknown(harness):
    """A never-filled firmware cache carries no BIOS answer and says so."""
    app_id = _seed_versioned_rom(harness, platform_slug="psvita")

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["bios_status"] is None
    assert result["bios_status_unknown"] is True


async def test_cached_game_detail_never_carries_a_bios_answer(harness):
    """However warm the caches are, this payload answers "not known here".

    What an emulator wants is read off the machine, and an answer read for a
    previous page open may not stand in for this one — so the page opens
    not-knowing and the live ``get_bios_status`` fills it in a moment later.
    """
    app_id = _seed_versioned_rom(harness, platform_slug="psvita")
    # The System-page read fills every cache the game-detail path could draw on.
    await harness.plugin.get_firmware_status()

    result = await harness.plugin.get_cached_game_detail(app_id)

    assert result["bios_status"] is None
    assert result["bios_status_unknown"] is True
    assert "bios" in result["stale_fields"]


async def test_bios_status_unreachable_server_is_unknown(harness):
    """An unreachable server on a machine that states no emulators answers "unknown"."""
    _seed_versioned_rom(harness, platform_slug="psvita")
    harness.romm.list_firmware_side_effect = RuntimeError("offline")

    result = await harness.plugin.get_bios_status(42)

    assert result["bios_status"] is None
    assert result["bios_level"] is None
    assert result["bios_label"] is None
    assert result["bios_status_unknown"] is True


async def test_bios_status_without_an_emulator_list_is_unknown(harness):
    """A reachable server answers half the question; the machine answers the other.

    The harness has no ``es_systems.xml``, so which emulators the platform offers
    — the scope an absence is judged against — cannot be established. An empty
    result then states nothing: an emulator outside the plugin's view could want
    a file the server has never held, and "needs none" would clear a real
    warning on ignorance (#1693).
    """
    _seed_versioned_rom(harness, platform_slug="psvita")

    result = await harness.plugin.get_bios_status(42)

    assert result["bios_status"] is None
    assert result["bios_status_unknown"] is True
