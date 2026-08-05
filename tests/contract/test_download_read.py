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

import asyncio

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
        "launchable",
    }
    assert result["rom_id"] == 42
    assert result["file_name"] == "pokemon.gba"
    assert result["platform_slug"] == "gba"
    assert result["launchable"] is True
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


async def test_cancel_no_active_download_failure_shape(harness):
    """Cancelling a ROM with no active or paused download → canonical failure shape."""
    result = await harness.plugin.cancel_download(999)
    assert result == {
        "success": False,
        "reason": "no_active_download",
        "message": "No active download for this ROM",
    }


async def test_cancel_paused_download_evicts_and_get_queue_omits_it(harness):
    """Cancelling a paused download (no live task) evicts its entry, and a
    following ``get_download_queue`` no longer lists it (#149 downloads-round).

    The pre-fix bug: ``cancel_download`` required a live task, so a paused
    download's cancel silently no-op'd and its row lingered.
    """
    queue = harness.plugin._download_service._download_queue
    queue[7] = {
        "rom_id": 7,
        "rom_name": "Paused",
        "platform_name": "N64",
        "file_name": "game.z64",
        "status": "paused",
        "progress": 0.5,
        "bytes_downloaded": 500,
        "total_bytes": 1000,
        "resumable": True,
    }

    result = await harness.plugin.cancel_download(7)
    await asyncio.sleep(0)  # let the scheduled cancelled-frame emit run + drain

    assert result == {"success": True, "message": "Download cancelled"}
    after = await harness.plugin.get_download_queue()
    assert all(d["rom_id"] != 7 for d in after["downloads"])


# ── clear_completed_downloads (#149) ─────────────────────────────────────


async def test_clear_completed_downloads_empty_queue_shape(harness):
    """Clearing an empty queue → ``{success: True, cleared: 0}``.

    ``clearCompletedDownloads = callable<[], {success, cleared}>`` — the success
    payload carries the eviction count.
    """
    result = await harness.plugin.clear_completed_downloads()
    assert result == {"success": True, "cleared": 0}


async def test_clear_completed_downloads_evicts_terminal_and_get_queue_omits_them(harness):
    """Terminal entries evict; a following ``get_download_queue`` no longer lists them.

    This is the #149 contract: the backend queue is what ``get_download_queue``
    re-seeds the frontend from on every mount, so a cleared entry must be gone
    from the queue — not merely hidden client-side. Active/queued/paused/
    extracting entries survive.
    """
    queue = harness.plugin._download_service._download_queue
    queue[1] = {"rom_id": 1, "rom_name": "Done", "status": "completed"}
    queue[2] = {"rom_id": 2, "rom_name": "Broke", "status": "failed", "error": "boom"}
    queue[3] = {"rom_id": 3, "rom_name": "Stopped", "status": "cancelled"}
    queue[4] = {"rom_id": 4, "rom_name": "Live", "status": "downloading"}

    result = await harness.plugin.clear_completed_downloads()
    assert result == {"success": True, "cleared": 3}

    after = await harness.plugin.get_download_queue()
    assert [d["rom_id"] for d in after["downloads"]] == [4]
    assert after["downloads"][0]["status"] == "downloading"
