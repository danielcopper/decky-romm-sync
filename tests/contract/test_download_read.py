"""Contract tests for the download read-surface callables.

Driven frontend-shaped per ``src/api/backend.ts``:
``getDownloadQueue = callable<[], {downloads: DownloadItem[]}>`` and
``getInstalledRom = callable<[number], InstalledRom | null>``.

The ``get_installed_rom`` ``null`` case is the #1004-class shape risk: the
backend must return Python ``None`` (which marshals to JS ``null``), not a
sentinel dict. The test asserts the literal ``None``.

Out of Phase 1 (not tested here): ``start_download`` / ``cancel_download``
mutation + event flows — their event contract is owned by #1017 and lands
with that fix.
"""

from __future__ import annotations

from ._seed import seed_install

# ── get_download_queue ───────────────────────────────────────────────────


async def test_get_download_queue_empty_shape(harness):
    """Empty queue: downloads key present and an empty list."""
    result = await harness.plugin.get_download_queue()
    assert result == {"downloads": []}
    assert isinstance(result["downloads"], list)


# ── get_installed_rom ────────────────────────────────────────────────────


async def test_get_installed_rom_not_installed_is_literal_none(harness):
    """Not installed → literal Python None (→ JS null), NOT a sentinel dict.

    #1004-class guard: the frontend's ``InstalledRom | null`` contract relies
    on the backend returning ``None`` here.
    """
    result = await harness.plugin.get_installed_rom(999)
    assert result is None


async def test_get_installed_rom_installed_shape(harness):
    """Installed → InstalledRom dict with the documented keys."""
    seed_install(harness, 42, system="gba", platform_slug="gba", file_name="pokemon.gba")
    result = await harness.plugin.get_installed_rom(42)
    assert result is not None
    assert set(result.keys()) == {
        "rom_id",
        "file_name",
        "file_path",
        "system",
        "platform_slug",
        "installed_at",
    }
    assert result["rom_id"] == 42
    assert result["file_name"] == "pokemon.gba"
    assert result["platform_slug"] == "gba"
    assert isinstance(result["file_path"], str)


# ── pause_download / resume_download (#1124) ─────────────────────────────


async def test_pause_download_no_active_failure_shape(harness):
    """Pausing a ROM with no active download → canonical failure shape.

    ``pauseDownload = callable<[number], {success, message}>`` — the failure
    branch carries the canonical ``{success: False, reason, message}``.
    """
    result = await harness.plugin.pause_download(999)
    assert result == {
        "success": False,
        "reason": "no_active_download",
        "message": "No active download for this ROM",
    }


async def test_resume_download_not_paused_failure_shape(harness):
    """Resuming a ROM with no paused download → canonical failure shape."""
    result = await harness.plugin.resume_download(999)
    assert result == {
        "success": False,
        "reason": "not_paused",
        "message": "No paused download for this ROM",
    }
