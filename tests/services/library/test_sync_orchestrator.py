"""Tests for SyncOrchestrator — preview/apply/full-sync lifecycle and safety heartbeat.

The migrated layout drives the orchestrator end-to-end through
``FakeRommApi``: tests seed in-memory platforms/ROMs/collections on the
fake, then exercise the public callable surface (``sync_preview``,
``sync_apply_delta``, ``_do_sync_per_unit``, etc.) and assert on the
**observable outputs** — ``decky.emit`` calls, state mutations, persister
counts.

Two production seams remain mockable per test:

* ``_wait_for_unit_complete`` — waits on a frontend ``report_unit_results``
  callback that no test exercises. Replaced with a ``fake_wait`` helper.
* ``_download_artwork`` — delegates to the SteamGridDB pipeline; the
  orchestrator tests do not exercise artwork I/O. Replaced with an
  ``AsyncMock``.

``_emit_progress`` is intentionally **not** mocked when the test asserts on
``decky.emit.call_args_list`` — driving real emissions keeps the
assertions honest. The fetcher's runtime methods (``build_work_queue``,
``fetch_platform_unit``, ``fetch_collection_unit``) are reached through
the real fetcher against the seeded fake — that is the whole point of the
migration.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.persistence import (
    PersistenceAdapter,
)
from domain.preview_delta import PreviewDelta
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit

# conftest.py patches decky before this import


# ── Test helpers ─────────────────────────────────────────────────


def _use_fake_romm(plugin, fake_romm_api):
    """Swap the plugin's MagicMock ``_romm_api`` for the seeded fake.

    The library-suite plugin fixture wires ``_romm_api`` as a
    ``MagicMock()`` (kept for the test_fetcher.py tests that match
    callables by identity). Each orchestrator test that wants the
    end-to-end path drives through this helper, which rebinds the fake
    onto every sub-service holding a stale reference.
    """
    plugin._romm_api = fake_romm_api
    plugin._sync_service._fetcher._romm_api = fake_romm_api
    plugin._artwork_service._romm_api = fake_romm_api
    plugin._shortcut_removal_service._romm_api = fake_romm_api
    return fake_romm_api


def _seed_platform(fake_romm_api, *, platform_id, name, slug, roms):
    """Seed a platform plus its ROMs on the fake.

    ROMs are dicts with at least ``id``/``name``; ``platform_id`` and
    ``platform_slug``/``platform_name`` are stamped automatically so the
    fetcher's enrichment loop sees consistent data.
    """
    fake_romm_api.platforms.append({"id": platform_id, "name": name, "slug": slug, "rom_count": len(roms)})
    for rom in roms:
        rom_id = rom["id"]
        full_rom = {
            "platform_id": platform_id,
            "platform_name": name,
            "platform_slug": slug,
            **rom,
        }
        fake_romm_api.roms[rom_id] = full_rom


def _seed_collection(
    fake_romm_api,
    *,
    collection_id,
    name,
    rom_ids,
    is_favorite=False,
    is_virtual=False,
    virtual_category=None,
):
    """Seed a (real or virtual) collection plus the ``collection_ids`` /
    ``virtual_collection_ids`` lookup arrays on each member ROM."""
    entry = {
        "id": collection_id,
        "name": name,
        "rom_count": len(rom_ids),
        "rom_ids": list(rom_ids),
        "is_favorite": is_favorite,
        "is_virtual": is_virtual,
    }
    if is_virtual:
        assert virtual_category is not None, "virtual collections need a category"
        fake_romm_api.virtual_collections.setdefault(virtual_category, []).append(entry)
        for rid in rom_ids:
            rom = fake_romm_api.roms.setdefault(rid, {"id": rid})
            rom.setdefault("virtual_collection_ids", []).append(collection_id)
    else:
        fake_romm_api.collections.append(entry)
        for rid in rom_ids:
            rom = fake_romm_api.roms.setdefault(rid, {"id": rid})
            rom.setdefault("collection_ids", []).append(collection_id)


async def _fake_wait_set_event(_unit, event):
    """Default ``_wait_for_unit_complete`` stand-in: set the event and
    return an empty rom_id_to_app_id map.

    The frontend's ``report_unit_results`` callback never runs in tests.
    The orchestrator's per-unit driver requires the event to fire and a
    mapping to come back — this helper provides both.
    """
    event.set()
    return {}


class TestShortcutDataFormat:
    """Validate the shortcut data format produced by the backend.

    The backend prepares shortcut data that the frontend uses to create
    Steam shortcuts. These tests ensure the data is well-formed.
    """

    @pytest.mark.asyncio
    async def test_exe_path_points_to_romm_launcher(self, plugin):
        """Exe path must point to bin/romm-launcher inside the plugin directory."""
        import decky

        plugin.settings["romm_url"] = "http://romm.local"
        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")

        assert exe.endswith("/bin/romm-launcher"), f"Exe path should end with /bin/romm-launcher, got: {exe}"
        assert "decky-romm-sync" in exe, f"Exe path should contain plugin name, got: {exe}"

    def test_launch_options_format(self, plugin):
        """Launch options must follow the romm:<rom_id> pattern."""
        import re

        pattern = r"^romm:\d+$"

        # Test valid formats
        for rom_id in [1, 42, 4409, 99999]:
            launch_opt = f"romm:{rom_id}"
            assert re.match(pattern, launch_opt), f"Launch option '{launch_opt}' does not match expected pattern"

    def test_start_dir_is_parent_of_exe(self, plugin):
        """Start dir must be the directory containing the launcher."""
        import decky

        exe = os.path.join(decky.DECKY_PLUGIN_DIR, "bin", "romm-launcher")
        start_dir = os.path.join(decky.DECKY_PLUGIN_DIR, "bin")

        assert start_dir == os.path.dirname(exe), f"start_dir ({start_dir}) should be parent of exe ({exe})"


class TestSyncPreview:
    """Tests for sync_preview().

    Preview is read-only — it paginates every unit, classifies the
    result, and returns the summary. It does NOT mutate the metadata
    cache (that happens per applied unit in the apply phase) and does
    NOT cache the prefetched ROMs (apply re-fetches; this is the
    fix for #738)."""

    @pytest.mark.asyncio
    async def test_returns_correct_summary(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 1, "name": "Game A", "fs_name": "a.z64"},
                {"id": 2, "name": "Game B", "fs_name": "b.z64"},
                {"id": 3, "name": "Game C", "fs_name": "c.z64"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        # Set up registry: rom 1 unchanged, rom 2 changed name
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
            "2": {"app_id": 1002, "name": "Old B", "platform_name": "N64", "platform_slug": "n64", "fs_name": "b.z64"},
        }

        result = await plugin.sync_preview()
        assert result["success"] is True
        summary = result["summary"]
        assert summary["new_count"] == 1  # rom 3 is new
        assert summary["changed_count"] == 1  # rom 2 name changed
        assert summary["unchanged_count"] == 1  # rom 1 unchanged
        assert summary["remove_count"] == 0
        assert "preview_id" in result

    @pytest.mark.asyncio
    async def test_populates_pending_delta(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "Game A", "fs_name": "a.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        result = await plugin.sync_preview()
        assert plugin._sync_service._pending_delta is not None
        assert plugin._sync_service._pending_delta.preview_id == result["preview_id"]
        assert plugin._sync_service._pending_delta.created_at == plugin._sync_service._clock.time()
        assert plugin._sync_service._pending_delta.platforms_count == 1
        assert plugin._sync_service._pending_delta.total_roms == 1

    @pytest.mark.asyncio
    async def test_does_not_write_metadata_cache(self, plugin, fake_romm_api):
        """Preview MUST NOT stamp the metadata cache (#738 regression).

        The bug: preview wrote the cache as a side-effect, and the
        per-unit incremental-skip path produced thin registry ROMs
        without ``metadatum``. Those overwrote populated entries with
        empty ones, corrupting the cache on every delta sync.

        The fix: preview is read-only. The metadata stamp happens per
        applied unit in the apply phase, not at preview time.
        """
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        # Seed populated metadata cache entries from a prior real sync.
        plugin._metadata_cache["1"] = {
            "summary": "Existing populated entry",
            "genres": ("RPG",),
            "companies": (),
            "first_release_date": None,
            "average_rating": None,
            "game_modes": (),
            "player_count": "",
            "cached_at": 100.0,
            "steam_categories": (),
        }
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "Game A", "fs_name": "a.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        # Wrap the metadata service so we can assert no record_unit_metadata
        # call lands during preview.
        record_mock = MagicMock()
        plugin._metadata_service.record_unit_metadata = record_mock  # type: ignore[method-assign]

        await plugin.sync_preview()

        record_mock.assert_not_called()
        # The cached entry is unchanged.
        assert plugin._metadata_cache["1"]["summary"] == "Existing populated entry"

    @pytest.mark.asyncio
    async def test_returns_error_when_sync_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = await plugin.sync_preview()
        assert result["success"] is False
        assert "already in progress" in result["message"]

    @pytest.mark.asyncio
    async def test_resets_sync_running_on_completion(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "Game A", "fs_name": "a.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        await plugin.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE


class TestSyncApplyDelta:
    """Tests for sync_apply_delta().

    Apply dispatches the per-unit pipeline against a live fetch (no
    preview-time prefetch cache — that's the #738 fix). The preview_id
    and 30-min age gate still validate stale apply attempts.
    """

    def _setup_pending_delta(self, plugin, preview_id="test-preview-123"):
        """Helper to populate _pending_delta with valid data."""
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id=preview_id,
            created_at=plugin._sync_service._clock.time(),
            platforms_count=1,
            total_roms=3,
        )

    @pytest.mark.asyncio
    async def test_rejects_wrong_preview_id(self, plugin):
        self._setup_pending_delta(plugin, "correct-id")
        result = await plugin.sync_apply_delta("wrong-id")
        assert result["success"] is False
        assert result["error_code"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_rejects_when_no_pending_delta(self, plugin):
        assert plugin._sync_service._pending_delta is None
        result = await plugin.sync_apply_delta("any-id")
        assert result["success"] is False
        assert result["error_code"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_rejects_when_preview_older_than_max_age(self, plugin):
        """Preview snapshots older than 30 minutes are stale.

        Regression for #345: sync_apply_delta previously only validated
        preview_id, so a user could leave the preview open for hours and
        apply a stale RomM snapshot — silent data corruption.
        """
        self._setup_pending_delta(plugin, "preview-abc")
        # Advance the clock past the 30-minute max age.
        plugin._sync_service._clock.advance(1801)

        result = await plugin.sync_apply_delta("preview-abc")

        assert result["success"] is False
        assert result["error_code"] == "stale_preview"
        assert "30 minutes" in result["message"]
        # Stale delta is cleared so a repeat apply can't pick it up.
        assert plugin._sync_service._pending_delta is None

    @pytest.mark.asyncio
    async def test_accepts_when_preview_just_under_max_age(self, plugin, tmp_path):
        """Snapshots within the TTL window apply normally."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin, "preview-xyz")
        # Apply runs the per-unit pipeline as a fire-and-forget task; stub
        # it out so the test can assert dispatch without driving the full
        # pipeline (the per-unit driver is covered in TestDoSyncPerUnit).
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()
        # Just under the 30-minute window.
        plugin._sync_service._clock.advance(1799)

        result = await plugin.sync_apply_delta("preview-xyz")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_dispatches_per_unit_without_cached_queue(self, plugin, tmp_path):
        """Apply dispatches ``_do_sync_per_unit`` with no prefetched cache (always live fetch)."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        do_sync = AsyncMock()
        plugin._sync_service._orchestrator._do_sync_per_unit = do_sync

        result = await plugin.sync_apply_delta("test-preview-123")
        # Drain the create_task'd dispatch.
        for _ in range(3):
            await asyncio.sleep(0)

        assert result["success"] is True
        # Per-unit dispatch was kicked off without any prefetched cache (live fetch).
        do_sync.assert_called_once()
        # The new signature takes no positional/keyword args.
        assert do_sync.call_args.args == ()
        assert do_sync.call_args.kwargs == {}

    @pytest.mark.asyncio
    async def test_apply_persists_sync_stats(self, plugin, tmp_path):
        """Apply writes the preview's platform/rom counts into ``sync_stats`` so
        ``get_sync_stats`` and the shortcut-removal pass see the apply's counts."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")

        assert plugin._state["sync_stats"]["platforms"] == 1
        assert plugin._state["sync_stats"]["roms"] == 3

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin, tmp_path):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

        plugin._state["shortcut_registry"] = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "N64"},
        }
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()

        await plugin.sync_apply_delta("test-preview-123")
        assert plugin._sync_service._pending_delta is None


class TestSyncCancelPreview:
    """Tests for sync_cancel_preview()."""

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin):
        plugin._sync_service._pending_delta = PreviewDelta(
            preview_id="some-id",
            created_at=plugin._sync_service._clock.time(),
            platforms_count=0,
            total_roms=0,
        )
        result = await plugin.sync_cancel_preview()
        assert plugin._sync_service._pending_delta is None
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_returns_success(self, plugin):
        result = await plugin.sync_cancel_preview()
        assert result == {"success": True}


# ── Tests for uncovered helper methods in library_sync.py ──────────


class TestSyncControl:
    """Tests for start_sync, cancel_sync, sync_heartbeat."""

    def test_start_sync_when_idle(self, plugin):
        result = plugin._sync_service.start_sync()
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.RUNNING

    def test_start_sync_rejects_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = plugin._sync_service.start_sync()
        assert result["success"] is False
        assert "already in progress" in result["message"]

    def test_cancel_sync_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        result = plugin._sync_service.cancel_sync()
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_cancel_sync_when_idle(self, plugin):
        result = plugin._sync_service.cancel_sync()
        assert result["success"] is True
        assert "No sync" in result["message"]

    def test_sync_heartbeat(self, plugin):
        old = plugin._sync_service._sync_last_heartbeat
        # Advance the injected FakeClock so monotonic moves forward.
        plugin._sync_service._clock.advance(0.01)
        result = plugin._sync_service.sync_heartbeat()
        assert result["success"] is True
        assert plugin._sync_service._sync_last_heartbeat > old


class TestFinishSync:
    """Tests for _finish_sync()."""

    @pytest.mark.asyncio
    async def test_sets_cancelled_state(self, plugin):
        import decky

        decky.emit.reset_mock()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True, "current": 5, "total": 10}

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._sync_progress["running"] is False
        assert plugin._sync_service._sync_progress["phase"] == "cancelled"
        assert plugin._sync_service._sync_progress["message"] == "Sync cancelled"

    @pytest.mark.asyncio
    async def test_clears_current_sync_id(self, plugin):
        """_finish_sync invalidates _current_sync_id so generation-guarded
        background work (per-unit heartbeat) sees a stale generation."""
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True}
        plugin._sync_service._current_sync_id = "sync-abc"

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._current_sync_id is None


class TestSyncPreviewErrorHandling:
    """Tests for sync_preview error paths."""

    @pytest.mark.asyncio
    async def test_general_exception_returns_error(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        # Cause the platforms listing to blow up — exception bubbles up
        # through build_work_queue into sync_preview exactly like a
        # mid-paginate RomM failure would in production.
        fake_romm_api.list_platforms_side_effect = RuntimeError("Something broke")
        plugin.settings["enabled_platforms"] = {"1": True}

        result = await plugin._sync_service.sync_preview()
        assert result["success"] is False
        assert "error_code" in result
        assert plugin._sync_service._sync_state == SyncState.IDLE
        # Error path evicts any pending delta.
        assert plugin._sync_service._pending_delta is None

    @pytest.mark.asyncio
    async def test_cancelled_error_reraises(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        fake_romm_api.list_platforms_side_effect = asyncio.CancelledError("cancelled")
        plugin.settings["enabled_platforms"] = {"1": True}

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service.sync_preview()
        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._pending_delta is None


# ──────────────────────────────────────────────────────────────
# Per-unit pipeline tests
# ──────────────────────────────────────────────────────────────


class TestBuildWorkQueue:
    """Phase 0 of the per-unit pipeline: enumerate platforms + collections without fetching ROMs."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_nothing_enabled(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {}

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert units == []

    @pytest.mark.asyncio
    async def test_includes_enabled_platforms(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 12},
            {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 99},
            {"id": 3, "name": "GBA", "slug": "gba", "rom_count": 5},
        ]
        plugin.settings["enabled_platforms"] = {"1": True, "2": False, "3": True}
        plugin.settings["enabled_collections"] = {}

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert [u.name for u in units] == ["N64", "GBA"]
        assert all(u.type == "platform" for u in units)
        assert units[0].rom_count == 12

    @pytest.mark.asyncio
    async def test_includes_enabled_collections_after_platforms(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 4}]
        fake_romm_api.collections = [{"id": 7, "name": "Favorites", "rom_count": 3, "is_favorite": True}]
        fake_romm_api.virtual_collections["franchise"] = [
            {"id": 9, "name": "Metroid", "rom_count": 8, "is_virtual": True}
        ]
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin.settings["enabled_collections"] = {"7": True, "9": True}

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert [(u.type, u.name) for u in units] == [
            ("platform", "N64"),
            ("collection", "Favorites"),
            ("collection", "Metroid"),
        ]
        assert units[2].is_virtual is True


class TestFetchPlatformUnit:
    """Per-unit platform ROM fetch with incremental-skip path."""

    @pytest.mark.asyncio
    async def test_full_fetch_when_no_registry(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is False
        assert [r["id"] for r in roms] == [10, 11]
        assert roms[0]["platform_name"] == "N64"

    @pytest.mark.asyncio
    async def test_skips_when_registry_matches_count(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        # No ROMs seeded on the fake; the platform's listing reports zero
        # updates after last_sync so the incremental-skip path fires.
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "fs_name": "a.z64", "platform_name": "N64", "platform_slug": "n64"},
            "11": {"name": "B", "fs_name": "b.z64", "platform_name": "N64", "platform_slug": "n64"},
        }

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is True
        assert {r["id"] for r in roms} == {10, 11}

    @pytest.mark.asyncio
    async def test_full_fetch_when_count_mismatch(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        # Registry says 1 ROM but the unit reports 3 → incremental-skip
        # check still says zero updated (no updated_at > last_sync), but
        # count mismatch forces a full fetch.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}, {"id": 12, "name": "C"}],
        )
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "platform_name": "N64"},
        }

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is False
        assert len(roms) == 3


class TestFetchCollectionUnit:
    """Per-unit collection ROM fetch with cross-unit deduplication."""

    @pytest.mark.asyncio
    async def test_returns_new_roms_and_member_ids(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        fake_romm_api.roms = {
            1: {"id": 1, "platform_name": "N64", "collection_ids": [7]},
            2: {"id": 2, "platform_name": "SNES", "collection_ids": [7]},
            3: {"id": 3, "platform_name": "GBA", "collection_ids": [7]},
        }
        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=3, is_virtual=False)
        synced: set[int] = set()
        new_roms, ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)
        assert [r["id"] for r in new_roms] == [1, 2, 3]
        assert ids == [1, 2, 3]
        assert synced == {1, 2, 3}

    @pytest.mark.asyncio
    async def test_dedups_against_already_synced(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        fake_romm_api.roms = {
            1: {"id": 1, "platform_name": "N64", "virtual_collection_ids": ["9"]},
            2: {"id": 2, "platform_name": "SNES", "virtual_collection_ids": ["9"]},
        }
        unit = WorkUnit(type="collection", id="9", name="Metroid", slug="", rom_count=2, is_virtual=True)

        # rom_id=1 was already fetched via a platform unit
        synced: set[int] = {1}
        new_roms, ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)
        assert [r["id"] for r in new_roms] == [2]
        # All collection rom_ids reported back even if not in new_roms
        assert ids == [1, 2]


class TestDoSyncPerUnit:
    """End-to-end orchestration of the per-unit pipeline."""

    @pytest.mark.asyncio
    async def test_empty_queue_terminates_cleanly(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        # No platforms enabled → empty work queue.
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {}
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        assert plugin._sync_service._sync_state == SyncState.IDLE
        # Sync plan was emitted with empty units
        plan_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_plan"]
        assert len(plan_events) == 1
        assert plan_events[0][0][1]["total_units"] == 0

    @pytest.mark.asyncio
    async def test_emits_sync_plan_with_queue(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Platform with registry-matching count → fetcher takes the
        # incremental-skip branch (skipped=True), no live pagination.
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "platform_name": "N64", "platform_slug": "n64", "fs_name": "a.z64"},
            "11": {"name": "B", "fs_name": "b.z64", "platform_name": "N64", "platform_slug": "n64"},
        }
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 2}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        plan_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_plan"]
        assert len(plan_events) == 1
        payload = plan_events[0][0][1]
        assert payload["total_units"] == 1
        assert payload["units"][0]["name"] == "N64"

    @pytest.mark.asyncio
    async def test_processes_each_unit_in_order(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Live-fetch platforms (no last_sync, empty registry) so both
        # units reach the apply branch and emit ``sync_apply_unit``.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}],
        )
        _seed_platform(
            fake_romm_api,
            platform_id=2,
            name="GBA",
            slug="gba",
            roms=[{"id": 20, "name": "B"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_unit, event):
            event.set()
            return {str(_unit.id * 10): 9000 + int(_unit.id)}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 2
        assert unit_events[0]["unit_name"] == "N64"
        assert unit_events[1]["unit_name"] == "GBA"
        assert unit_events[0]["unit_index"] == 0
        assert unit_events[1]["unit_index"] == 1

    @pytest.mark.asyncio
    async def test_skipped_unit_short_circuits_apply(self, plugin, fake_romm_api):
        """``skipped=True`` from the fetcher short-circuits the whole apply+commit branch.

        For a unit whose registry already matches the server-side ROM
        count and has no updates since ``last_sync``, none of these run:
        artwork download, ``_wait_for_unit_complete``, the
        ``sync_apply_unit`` emit, or the reporter's ``commit_unit_results``.
        The unit's reconstructed ROMs still join ``synced_rom_ids`` so
        the final stale-cleanup pass doesn't mistakenly remove them.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Registry matches platform count + zero updates → incremental skip.
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "fs_name": "a.z64", "platform_name": "N64", "platform_slug": "n64"},
        }
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}

        download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._download_artwork = download_artwork
        wait_mock = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_mock
        commit_mock = AsyncMock()
        plugin._sync_service._reporter.commit_unit_results = commit_mock  # type: ignore[method-assign]
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # Nothing on the apply branch ran.
        download_artwork.assert_not_called()
        wait_mock.assert_not_called()
        apply_events = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_apply_unit"]
        assert apply_events == [], f"sync_apply_unit must not be emitted for a skipped unit, got: {apply_events}"
        commit_mock.assert_not_called()

        # Stale-cleanup still emits with an empty remove list — the
        # skipped unit's reconstructed ROMs joined synced_rom_ids so
        # rom_id 10 is not classified as stale.
        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert len(stale_events) == 1
        assert stale_events[0] == {"remove_rom_ids": []}

    @pytest.mark.asyncio
    async def test_downloads_artwork_when_not_skipped(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # No prior sync → full fetch path → skipped=False → artwork pipeline runs.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}
        plugin.settings["enabled_platforms"] = {"1": True}

        download_artwork = AsyncMock(return_value={10: "/grid/a.png"})
        plugin._sync_service._orchestrator._download_artwork = download_artwork
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        download_artwork.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_between_units_stops_processing(self, plugin, fake_romm_api):
        """Cancel flipped during the first unit's ack stops the queue mid-flight.

        Both platforms take the live-fetch path (no ``last_sync``) so
        each fully traverses ``_sync_one_unit`` rather than short-
        circuiting. The cancel observed between units must produce
        exactly one ``sync_apply_unit`` and a ``cancelled=True``
        ``sync_complete``.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Two live-fetch platforms (no last_sync, empty registry).
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}],
        )
        _seed_platform(
            fake_romm_api,
            platform_id=2,
            name="GBA",
            slug="gba",
            roms=[{"id": 20, "name": "B"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            # Flip to CANCELLING after first unit completes
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1  # cancel observed between units
        complete_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert len(complete_events) == 1
        assert complete_events[0].get("cancelled") is True


class TestWaitForUnitComplete:
    """Heartbeat-based per-unit timeout."""

    @pytest.mark.asyncio
    async def test_returns_results_when_event_set(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        event.set()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic()
        plugin._sync_service._box.last_unit_results = {"10": 9000}

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results == {"10": 9000}

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic()

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_on_heartbeat_timeout(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._sync_state = SyncState.RUNNING
        # Heartbeat is way too old — should timeout immediately on first loop check
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic() - 999.0

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None


class TestReportUnitResults:
    """Per-unit ack signal — frontend callback that signals the orchestrator's wait event.

    The actual registry update + state persist is now driven by the
    orchestrator via ``commit_unit_results`` (split for #738 so the
    metadata-cache stamp lands before the state save).
    """

    @pytest.mark.asyncio
    async def test_signals_unit_complete_event(self, plugin):
        plugin._sync_service._pending_sync = {}
        event = asyncio.Event()
        plugin._sync_service._box.unit_complete_event = event
        assert not event.is_set()

        await plugin.report_unit_results({})

        assert event.is_set()
        assert plugin._sync_service._box.last_unit_results == {}

    @pytest.mark.asyncio
    async def test_records_last_unit_results(self, plugin):
        plugin._sync_service._pending_sync = {}
        plugin._sync_service._box.unit_complete_event = asyncio.Event()

        result = await plugin.report_unit_results({"10": 9001, "11": 9002})

        assert result["success"] is True
        assert result["count"] == 2
        assert plugin._sync_service._box.last_unit_results == {"10": 9001, "11": 9002}

    @pytest.mark.asyncio
    async def test_no_state_save_in_report_path(self, plugin):
        """The frontend callable MUST NOT persist state — the orchestrator
        drives ``commit_unit_results`` after the metadata-cache stamp.

        Persisting from ``report_unit_results`` would put the state save
        before the metadata stamp, restoring the #738 crash-safety bug.
        """
        # Count persister invocations across the callable.
        save_count = [0]
        orig_save_state = plugin._state_persister.save_state

        def counting_save():
            save_count[0] += 1
            orig_save_state()

        plugin._state_persister.save_state = counting_save
        plugin._sync_service._reporter._state_persister.save_state = counting_save
        plugin._sync_service._pending_sync = {}
        plugin._sync_service._box.unit_complete_event = asyncio.Event()

        await plugin.report_unit_results({"10": 9001})

        assert save_count[0] == 0, "report_unit_results must NOT persist state — commit_unit_results does"


class TestCommitUnitResults:
    """Orchestrator-driven per-unit commit: cover-path finalize + registry update + state save."""

    @pytest.mark.asyncio
    async def test_updates_registry_for_unit_roms(self, plugin):
        plugin._sync_service._pending_sync = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
            11: {"rom_id": 11, "name": "B", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001, "11": 9002})

        registry = plugin._state["shortcut_registry"]
        assert "10" in registry
        assert registry["10"]["app_id"] == 9001
        assert "11" in registry
        assert registry["11"]["app_id"] == 9002

    @pytest.mark.asyncio
    async def test_persists_state_after_unit(self, plugin):
        save_count = [0]
        orig_save_state = plugin._state_persister.save_state

        def counting_save():
            save_count[0] += 1
            orig_save_state()

        plugin._state_persister.save_state = counting_save
        plugin._sync_service._reporter._state_persister.save_state = counting_save
        plugin._sync_service._pending_sync = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001})

        assert save_count[0] == 1, "commit_unit_results must checkpoint state to disk"


class TestShutdown:
    """Tests for shutdown().

    Graceful shutdown flips a RUNNING sync into CANCELLING so the
    per-unit loop drops its in-flight work on the next checkpoint.
    """

    def test_shutdown_when_running_marks_cancelling(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_shutdown_when_idle_is_noop(self, plugin):
        plugin._sync_service._sync_state = SyncState.IDLE
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.IDLE

    def test_shutdown_when_cancelling_is_noop(self, plugin):
        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.CANCELLING


class TestDoSyncPerUnitErrors:
    """Tests for error/cancel paths inside _do_sync_per_unit."""

    @pytest.mark.asyncio
    async def test_build_work_queue_cancelled_error_finishes_sync(self, plugin, fake_romm_api):
        """CancelledError during build_work_queue triggers _finish_sync + re-raise."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        # ``list_platforms`` runs in the executor; the fake raises
        # CancelledError exactly like an asyncio cancel would propagate.
        fake_romm_api.list_platforms_side_effect = asyncio.CancelledError()
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "sync-cancel-build"

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._orchestrator._do_sync_per_unit()

        # _finish_sync transitioned to IDLE + cleared sync id.
        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._current_sync_id is None
        progress_phases = [
            c.args[1].get("phase") for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_progress"
        ]
        assert "cancelled" in progress_phases

    @pytest.mark.asyncio
    async def test_build_work_queue_general_exception_emits_error(self, plugin, fake_romm_api):
        """A non-cancellation exception during build_work_queue is logged + surfaced."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        fake_romm_api.list_platforms_side_effect = RuntimeError("RomM down")
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._sync_state = SyncState.RUNNING

        # Should NOT raise — outer flow swallows the exception after emitting an error.
        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # error phase was emitted via sync_progress.
        error_events = [
            c
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("phase") == "error"
        ]
        assert len(error_events) >= 1
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_outer_exception_handler_emits_error_progress(self, plugin, fake_romm_api):
        """An exception raised after build_work_queue (e.g. during a unit) hits the outer except."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # build_work_queue succeeds (platforms listing returns a unit), then
        # list_roms blows up when the unit is fetched.
        plugin._state["last_sync"] = None  # no incremental-skip path
        plugin._state["shortcut_registry"] = {}
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        fake_romm_api.list_roms_side_effect = RuntimeError("boom")
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        # Drain any pending tasks scheduled by the outer handler (loop.create_task).
        for _ in range(3):
            await asyncio.sleep(0)

        # sync_progress with phase=error was scheduled.
        error_events = [
            c
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("phase") == "error"
        ]
        assert len(error_events) >= 1
        assert "Sync failed" in error_events[0].args[1]["message"]
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_pagination_failure_does_not_emit_partial_stale_removal(self, plugin, fake_romm_api):
        """#630 safety invariant: a fetch_platform_unit failure must NOT trigger
        the stale-cleanup pass with a partial ROM set.

        Before the fix, ``fetch_platform_unit`` swallowed pagination exceptions
        and returned ``([], False)``. The orchestrator then ran ``_finalize_per_unit``
        with ``synced_rom_ids == set()`` and the registry's full ROM list was
        emitted via ``sync_stale``, which the frontend turned into a wholesale
        Steam shortcut deletion.

        Now that the fetcher re-raises, the exception hits the outer ``except``
        in ``_do_sync_per_unit`` BEFORE ``_finalize_per_unit`` runs, so no
        ``sync_stale`` event is ever emitted.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Existing registry the frontend would happily delete from if the
        # orchestrator ever emitted a partial sync_stale.
        plugin._state["shortcut_registry"] = {
            "10": {"name": "Game A", "platform_name": "N64", "app_id": 1000},
            "20": {"name": "Game B", "platform_name": "N64", "app_id": 2000},
            "30": {"name": "Game C", "platform_name": "N64", "app_id": 3000},
        }
        plugin._state["last_sync"] = None  # no incremental-skip
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        # Mid-pagination failure — the bug scenario from #630.
        fake_romm_api.list_roms_side_effect = RuntimeError("HTTP 500 on page 2")
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        # Drain any pending tasks scheduled by the outer handler.
        for _ in range(3):
            await asyncio.sleep(0)

        # The load-bearing assertion: sync_stale must never have been emitted.
        stale_events = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [], (
            f"Pagination failure leaked a partial sync_stale event: {stale_events}. "
            "This is the #630 wipe-the-library bug."
        )
        # And the registry is untouched — the orchestrator never reaches the
        # reporter's finalize path when the fetcher raises.
        assert set(plugin._state["shortcut_registry"].keys()) == {"10", "20", "30"}
        # The error path was taken instead.
        error_events = [
            c
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("phase") == "error"
        ]
        assert len(error_events) >= 1
        assert plugin._sync_service._sync_state == SyncState.IDLE

    @pytest.mark.asyncio
    async def test_cancelling_state_before_first_unit_skips_processing(self, plugin, fake_romm_api):
        """If state is CANCELLING when the unit loop starts, no units run."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Two units in the queue; CANCELLING gates the loop before either fires.
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "fs_name": "a.z64", "platform_name": "N64", "platform_slug": "n64"},
            "20": {"name": "B", "fs_name": "b.gba", "platform_name": "GBA", "platform_slug": "gba"},
        }
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 1},
            {"id": 2, "name": "GBA", "slug": "gba", "rom_count": 1},
        ]
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._sync_state = SyncState.CANCELLING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # No units were processed because the CANCELLING check fired before
        # the loop entered the per-unit body — sync_apply_unit is the
        # cleanest observable for "did the unit dispatch run?".
        apply_events = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_apply_unit"]
        assert apply_events == []
        # _finalize_per_unit still ran; sync_complete is emitted with cancelled=True.
        complete = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_complete"]
        assert len(complete) == 1
        assert complete[0].args[1].get("cancelled") is True


class TestSyncOneUnitCollectionAndCancel:
    """Tests for _sync_one_unit branches: collection units + mid-unit cancel."""

    @pytest.mark.asyncio
    async def test_collection_unit_records_membership(self, plugin, fake_romm_api):
        """A collection unit populates collection_memberships with its rom_ids."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Seed a real (non-virtual) collection with two ROMs.
        _seed_collection(
            fake_romm_api,
            collection_id=7,
            name="Faves",
            rom_ids=[1, 2],
            is_favorite=True,
        )
        fake_romm_api.roms[1]["name"] = "A"
        fake_romm_api.roms[1]["platform_name"] = "N64"
        fake_romm_api.roms[2]["name"] = "B"
        fake_romm_api.roms[2]["platform_name"] = "N64"
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"7": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # sync_complete fired (collection_memberships flowed through to finalize).
        complete = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_complete"]
        assert len(complete) == 1

    @pytest.mark.asyncio
    async def test_cancel_after_fetch_returns_zero_applied(self, plugin, fake_romm_api):
        """CANCELLING flipped after fetch_platform_unit → unit returns 0."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Real fetcher will be called for the unit. Wrap list_roms so the
        # post-fetch state is CANCELLING when ``_sync_one_unit`` checks it.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        orig_list_roms = fake_romm_api.list_roms

        def list_roms_then_cancel(platform_id, limit=50, offset=0):
            page = orig_list_roms(platform_id, limit=limit, offset=offset)
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return page

        fake_romm_api.list_roms = list_roms_then_cancel  # type: ignore[method-assign]

        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )
        assert applied == 0

    @pytest.mark.asyncio
    async def test_cancel_after_artwork_returns_zero_applied(self, plugin, fake_romm_api):
        """CANCELLING flipped after the artwork download → unit returns 0."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Real fetcher runs; artwork download is intercepted to flip state mid-flight.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        async def cancel_during_artwork(*_a, **_kw):
            # Trigger CANCELLING in between the post-fetch check and the post-artwork check.
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._download_artwork = cancel_during_artwork
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )
        assert applied == 0

    @pytest.mark.asyncio
    async def test_wait_returning_none_clears_pending_and_cancels(self, plugin, fake_romm_api):
        """When _wait_for_unit_complete returns None, the unit drops state + flips CANCELLING."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Live-fetch path so the unit reaches the apply branch where
        # ``_wait_for_unit_complete`` is called.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Simulate heartbeat timeout / cancel inside _wait_for_unit_complete.
        async def wait_returns_none(_unit, _event):
            return None

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_returns_none
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )
        assert applied == 0
        # pending_sync was cleared, unit event reference dropped, state flipped.
        assert plugin._sync_service._pending_sync == {}
        assert plugin._sync_service._box.unit_complete_event is None
        assert plugin._sync_service._sync_state == SyncState.CANCELLING


class TestPerUnitMetadataStamping:
    """Per-unit metadata-cache stamping after the frontend ack (#738)."""

    @pytest.mark.asyncio
    async def test_metadata_stamp_before_state_commit(self, plugin, fake_romm_api):
        """Order check: ``record_unit_metadata`` runs BEFORE ``commit_unit_results``.

        Crash-safety order: metadata-first means an interrupted apply
        leaves only orphan metadata (harmless). State-first would leave
        registry entries pointing at empty metadata — the bug we're
        fixing.
        """
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A", "metadatum": {"genres": ["RPG"]}}],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        # Track the call order across the two phases.
        call_order: list[str] = []

        record_mock = MagicMock(side_effect=lambda _roms: call_order.append("record_unit_metadata"))
        plugin._metadata_service.record_unit_metadata = record_mock  # type: ignore[method-assign]

        original_commit = plugin._sync_service._reporter.commit_unit_results

        async def tracked_commit(rid_to_aid):
            call_order.append("commit_unit_results")
            await original_commit(rid_to_aid)

        plugin._sync_service._reporter.commit_unit_results = tracked_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 5001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        assert call_order == ["record_unit_metadata", "commit_unit_results"]

    @pytest.mark.asyncio
    async def test_skipped_unit_does_not_stamp_metadata(self, plugin, fake_romm_api):
        """Incremental-skip platforms must NOT call ``record_unit_metadata``.

        The skipped short-circuit returns from ``_sync_one_unit`` before
        the metadata stamp runs, so populated cache entries from prior
        real fetches are preserved (#738).
        """
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Registry matches platform rom_count + zero updates → incremental skip.
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"
        plugin._state["shortcut_registry"] = {
            "10": {"name": "A", "fs_name": "a.z64", "platform_name": "N64", "platform_slug": "n64"},
        }

        record_mock = MagicMock()
        plugin._metadata_service.record_unit_metadata = record_mock  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 5001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        record_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_acked_roms_filter(self, plugin, fake_romm_api):
        """Only the ROMs the frontend ack'd land in record_unit_metadata."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 1, "name": "A", "metadatum": {"genres": ["RPG"]}},
                {"id": 2, "name": "B", "metadatum": {"genres": ["Action"]}},
                {"id": 3, "name": "C", "metadatum": {"genres": ["Puzzle"]}},
                {"id": 4, "name": "D", "metadatum": {"genres": ["Sport"]}},
                {"id": 5, "name": "E", "metadatum": {"genres": ["Strategy"]}},
            ],
        )
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        recorded_roms: list[dict] = []
        plugin._metadata_service.record_unit_metadata = lambda roms: recorded_roms.extend(roms)  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Frontend ack's only 3 out of 5 ROMs.
        async def fake_wait(_u, event):
            event.set()
            return {"1": 5001, "3": 5003, "5": 5005}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        assert {r["id"] for r in recorded_roms} == {1, 3, 5}


class TestRegression738CacheCorruption:
    """Regression for #738 — delta sync must not erase populated metadata.

    Before the fix, the per-unit incremental-skip path produced thin
    registry-reconstructed ROMs (no ``metadatum`` field). Those flowed
    through ``extract_metadata`` and overwrote populated entries with
    empty ones. Symptom: 160 populated entries → 62 after one delta
    sync.
    """

    @pytest.mark.asyncio
    async def test_delta_sync_preserves_populated_metadata(self, plugin, fake_romm_api):
        """Populated entries survive a per-unit delta sync of unchanged platforms.

        Scenario: registry has 3 ROMs on platform N64 with populated
        metadata. Server reports zero updated after ``last_sync``, so
        ``fetch_platform_unit`` returns skipped=True with thin
        registry-reconstructed ROMs. The orchestrator's skip-guard
        prevents the metadata stamp from running for that unit, so the
        populated cache entries are preserved untouched.
        """
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Pre-existing populated metadata entries (the "160 entries"
        # scenario boiled down to 3 ROMs).
        plugin._metadata_cache["1"] = {
            "summary": "Game 1 description",
            "genres": ("RPG",),
            "companies": ("Square",),
            "first_release_date": 946684800,
            "average_rating": 95.0,
            "game_modes": ("Single player",),
            "player_count": "1",
            "cached_at": 100.0,
            "steam_categories": (2, 21),
        }
        plugin._metadata_cache["2"] = {
            "summary": "Game 2 description",
            "genres": ("Action",),
            "companies": ("Capcom",),
            "first_release_date": 1000000000,
            "average_rating": 88.0,
            "game_modes": ("Multiplayer",),
            "player_count": "1-4",
            "cached_at": 100.0,
            "steam_categories": (1, 21),
        }
        plugin._metadata_cache["3"] = {
            "summary": "Game 3 description",
            "genres": ("Puzzle",),
            "companies": ("Nintendo",),
            "first_release_date": 1100000000,
            "average_rating": 92.0,
            "game_modes": ("Single player",),
            "player_count": "1",
            "cached_at": 100.0,
            "steam_categories": (4,),
        }

        # Registry mirrors the populated cache.
        def _entry(name, fs, app_id):
            return {
                "name": name,
                "fs_name": fs,
                "platform_name": "N64",
                "platform_slug": "n64",
                "app_id": app_id,
            }

        plugin._state["shortcut_registry"] = {
            "1": _entry("Game 1", "g1.z64", 1001),
            "2": _entry("Game 2", "g2.z64", 1002),
            "3": _entry("Game 3", "g3.z64", 1003),
        }
        plugin._state["last_sync"] = "2025-01-01T00:00:00Z"

        # Server reports the platform exists with 3 ROMs and ZERO updates.
        # No ROMs seeded → list_roms_updated_after returns total=0.
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"1": 1001, "2": 1002, "3": 1003}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        # Pre-flight: cache has 3 populated entries.
        assert len(plugin._metadata_cache) == 3
        assert plugin._metadata_cache["1"]["summary"] == "Game 1 description"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # Post-flight: cache MUST still have 3 populated entries.
        # Pre-fix, they would have been overwritten by empty ones.
        assert "1" in plugin._metadata_cache
        assert "2" in plugin._metadata_cache
        assert "3" in plugin._metadata_cache
        assert plugin._metadata_cache["1"]["summary"] == "Game 1 description"
        assert plugin._metadata_cache["1"]["genres"] == ("RPG",)
        assert plugin._metadata_cache["2"]["summary"] == "Game 2 description"
        assert plugin._metadata_cache["3"]["summary"] == "Game 3 description"


class TestWaitForUnitCompleteCancelled:
    """Tests for asyncio.CancelledError in _wait_for_unit_complete."""

    @pytest.mark.asyncio
    async def test_cancelled_error_during_sleep_is_logged_and_reraised(self, plugin):
        """If the inner sleep is cancelled, log + re-raise so the outer loop sees the cancel."""

        class _CancellingSleeper:
            async def sleep(self, _seconds: float) -> None:
                raise asyncio.CancelledError()

        plugin._sync_service._sleeper = _CancellingSleeper()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._clock.monotonic()

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()  # never set — wait will enter the sleep path

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)


class TestDownloadArtworkDelegation:
    """Tests for _download_artwork."""

    @pytest.mark.asyncio
    async def test_delegates_to_artwork_manager(self, plugin):
        """When _artwork is bound, the call is forwarded with progress + cancel hooks."""
        fake_download = AsyncMock(return_value={1: "/path/a.png", 2: "/path/b.png"})
        plugin._sync_service._orchestrator._artwork = MagicMock()
        plugin._sync_service._orchestrator._artwork.download_artwork = fake_download

        roms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        result = await plugin._sync_service._orchestrator._download_artwork(
            roms, progress_step=3, progress_total_steps=7
        )

        assert result == {1: "/path/a.png", 2: "/path/b.png"}
        fake_download.assert_called_once()
        call_kwargs = fake_download.call_args.kwargs
        assert call_kwargs["progress_step"] == 3
        assert call_kwargs["progress_total_steps"] == 7
        # is_cancelling closure reflects the live sync_state.
        is_cancelling = call_kwargs["is_cancelling"]
        plugin._sync_service._sync_state = SyncState.RUNNING
        assert is_cancelling() is False
        plugin._sync_service._sync_state = SyncState.CANCELLING
        assert is_cancelling() is True
