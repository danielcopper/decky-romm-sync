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

``emit_progress`` is intentionally **not** mocked when the test asserts on
``decky.emit.call_args_list`` — driving real emissions keeps the
assertions honest. The fetcher's runtime methods (``build_work_queue``,
``fetch_platform_unit``, ``fetch_collection_unit``) are reached through
the real fetcher against the seeded fake — that is the whole point of the
migration.
"""

import asyncio
import os
from typing import Any
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


def _seed_rom_row(plugin, rom_id, *, app_id, platform_slug, name="Game", fs_name=None):
    """Insert a bound (or unbound when app_id is None) ROM into the shared fake UoW."""
    from domain.rom import Rom

    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=fs_name if fs_name is not None else f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
    )
    with plugin._uow:
        plugin._uow.roms.save(rom)


def _seed_install(plugin, rom_id, *, file_path, platform_slug="n64"):
    """Insert a ``RomInstall`` record (with its FK-parent ``Rom``) into the shared UoW."""
    from domain.rom import Rom
    from domain.rom_install import RomInstall

    with plugin._uow:
        plugin._uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=f"Game {rom_id}",
                fs_name=f"game_{rom_id}.z64",
                shortcut_app_id=None,
                last_synced_at="2025-01-01T00:00:00",
            )
        )
        plugin._uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=None,
                platform_slug=platform_slug,
                system=platform_slug,
                installed_at="2025-01-01T00:00:00",
            )
        )


def _seed_completed_run(plugin, *, at, platforms=None, collections=None, run_id="run-prev"):
    """Insert a completed ``SyncRun`` so ``last_sync`` / ``last_synced_*`` reads resolve."""
    from domain.sync_run import SyncRun

    run = SyncRun.start(id=run_id, at=at, platforms_planned=1, roms_planned=1)
    run.complete(at, platforms or [], collections or [])
    with plugin._uow:
        plugin._uow.sync_runs.save(run)


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

    def test_exe_path_points_to_rom_launcher(self, plugin):
        """Exe path must point to bin/rom-launcher inside the plugin directory."""
        import decky

        from domain.shortcut_data import build_shortcuts_data

        result = build_shortcuts_data([{"id": 1, "name": "Game"}], decky.DECKY_PLUGIN_DIR, {})
        exe = result[0]["exe"]
        assert exe.endswith("/bin/rom-launcher"), f"Exe path should end with /bin/rom-launcher, got: {exe}"
        assert "decky-romm-sync" in exe, f"Exe path should contain plugin name, got: {exe}"

    def test_installed_rom_gets_launch_command(self, plugin):
        """An installed ROM's launch_options is the full RetroDECK launch command."""
        from domain.shortcut_data import build_shortcuts_data

        result = build_shortcuts_data([{"id": 42, "name": "Game"}], "/plugin", {42: "/roms/n64/game.z64"})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/game.z64"'

    def test_start_dir_is_parent_of_exe(self, plugin):
        """Start dir must be the directory containing the launcher."""
        import decky

        from domain.shortcut_data import build_shortcuts_data

        result = build_shortcuts_data([{"id": 1, "name": "Game"}], decky.DECKY_PLUGIN_DIR, {})
        assert result[0]["start_dir"] == os.path.dirname(result[0]["exe"])


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

        # Baseline in roms: rom 1 unchanged, rom 2 changed name. The
        # display name resolves from the live work-queue (slug n64 → "N64").
        _seed_rom_row(plugin, 1, app_id=1001, platform_slug="n64", name="Game A", fs_name="a.z64")
        _seed_rom_row(plugin, 2, app_id=1002, platform_slug="n64", name="Old B", fs_name="b.z64")

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
        assert plugin._sync_service._pending_delta.created_at == plugin._sync_service._orchestrator._clock.time()
        assert plugin._sync_service._pending_delta.platforms_count == 1
        assert plugin._sync_service._pending_delta.total_roms == 1

    @pytest.mark.asyncio
    async def test_does_not_write_metadata(self, plugin, fake_romm_api):
        """Preview MUST NOT persist ``rom_metadata`` (#738 regression).

        The bug: preview wrote metadata as a side-effect, and the per-unit
        incremental-skip path produced thin registry ROMs without
        ``metadatum``. Those overwrote populated entries with empty ones,
        corrupting the cache on every delta sync.

        The fix: preview is read-only. The metadata stamp happens in the
        reporter's per-unit commit during apply, not at preview time.
        """
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "Game A", "fs_name": "a.z64", "metadatum": {"genres": ["RPG"]}}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        await plugin.sync_preview()

        # Preview never commits — no metadata row was persisted.
        with plugin._uow as uow:
            assert uow.rom_metadata.get(1) is None

    @pytest.mark.asyncio
    async def test_excludes_unbound_rows_from_baseline(self, plugin, fake_romm_api):
        """An unbound (NULL ``shortcut_app_id``) row must NOT enter the
        classify baseline, so it cannot inflate ``remove_count`` (R1xR3).

        Setup: rom 1 is bound and still present on the server (unchanged),
        rom 99 is an unbound leftover that is absent from the live fetch.
        If ``_read_preview_baseline`` leaked rom 99 into the registry, it
        would be classified as stale (not in the current fetch) and reported
        as a removal. The NULL-exclusion guard keeps ``remove_count`` at 0.
        """
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

        # rom 1 bound + present on the server (unchanged); rom 99 unbound
        # leftover on a now-absent platform (would look stale if leaked).
        _seed_rom_row(plugin, 1, app_id=1001, platform_slug="n64", name="Game A", fs_name="a.z64")
        _seed_rom_row(plugin, 99, app_id=None, platform_slug="gba", name="Old Z", fs_name="z.gba")

        result = await plugin.sync_preview()
        assert result["success"] is True
        summary = result["summary"]
        # The unbound row is excluded from the baseline → not counted as stale.
        assert summary["remove_count"] == 0
        assert summary["unchanged_count"] == 1
        assert summary["new_count"] == 0

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
            created_at=plugin._sync_service._orchestrator._clock.time(),
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
        plugin._sync_service._orchestrator._clock.advance(1801)

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
        self._setup_pending_delta(plugin, "preview-xyz")
        # Apply runs the per-unit pipeline as a fire-and-forget task; stub
        # it out so the test can assert dispatch without driving the full
        # pipeline (the per-unit driver is covered in TestDoSyncPerUnit).
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()
        # Just under the 30-minute window.
        plugin._sync_service._orchestrator._clock.advance(1799)

        result = await plugin.sync_apply_delta("preview-xyz")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_dispatches_per_unit_without_cached_queue(self, plugin, tmp_path):
        """Apply dispatches ``_do_sync_per_unit`` with no prefetched cache (always live fetch)."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
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
    async def test_apply_dispatches_per_unit_task(self, plugin, tmp_path):
        """Apply transitions to RUNNING and dispatches the per-unit pipeline.

        The planned platform/rom counts are no longer written to a JSON
        ``sync_stats`` scalar — they land on the ``SyncRun`` record opened
        inside ``_do_sync_per_unit`` (covered in TestDoSyncPerUnit)."""
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)
        self._setup_pending_delta(plugin)
        plugin._sync_service._orchestrator._do_sync_per_unit = AsyncMock()

        result = await plugin.sync_apply_delta("test-preview-123")

        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.RUNNING

    @pytest.mark.asyncio
    async def test_clears_pending_delta(self, plugin, tmp_path):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        plugin._persistence = PersistenceAdapter(str(tmp_path), str(tmp_path), decky.logger)

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
            created_at=plugin._sync_service._orchestrator._clock.time(),
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
        plugin._sync_service._orchestrator._clock.advance(0.01)
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
        assert plugin._sync_service._sync_progress["stage"] == "cancelled"
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


class TestGetSyncStatus:
    """Backend-authoritative sync status query.

    ``get_sync_status`` returns the persisted progress snapshot so a
    freshly remounted QAM can recover in-flight state without waiting on
    a live ``sync_progress`` event.
    """

    def test_returns_idle_default_when_no_sync(self, plugin):
        status = plugin._sync_service.get_sync_status()
        assert status["running"] is False
        assert status["stage"] == ""

    def test_returns_live_snapshot_mid_sync(self, plugin):
        snapshot = {
            "running": True,
            "stage": "applying",
            "current": 3,
            "total": 10,
            "message": "N64 (1/2)",
            "step": 1,
            "totalSteps": 2,
        }
        plugin._sync_service._sync_progress = snapshot

        status = plugin._sync_service.get_sync_status()

        assert status == snapshot
        assert status["running"] is True
        assert status["stage"] == "applying"


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
        fake_romm_api.smart_collections = [{"id": 5, "name": "Filter", "rom_count": 2}]
        fake_romm_api.virtual_collections["franchise"] = [{"id": 9, "name": "Metroid", "rom_count": 8}]
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin.settings["enabled_collections"] = {
            "user": {"7": True},
            "smart": {"5": True},
            "franchise": {"9": True},
        }

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert [(u.type, u.name) for u in units] == [
            ("platform", "N64"),
            ("collection", "Favorites"),
            ("collection", "Filter"),
            ("collection", "Metroid"),
        ]
        assert units[1].collection_kind == "user"
        assert units[2].collection_kind == "smart"
        assert units[3].collection_kind == "franchise"


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
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="B", fs_name="b.z64")

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is True
        assert {r["id"] for r in roms} == {10, 11}

    @pytest.mark.asyncio
    async def test_full_fetch_when_count_mismatch(self, plugin, fake_romm_api):
        _use_fake_romm(plugin, fake_romm_api)
        # roms says 1 ROM but the unit reports 3 → incremental-skip
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
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A")

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
        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=3, collection_kind="user")
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
        unit = WorkUnit(type="collection", id="9", name="Metroid", slug="", rom_count=2, collection_kind="franchise")

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
    async def test_emitted_shortcuts_carry_install_launch_options(self, plugin, fake_romm_api):
        """Installed ROMs get the full launch command; uninstalled ROMs get ``""``.

        The orchestrator builds the ``{rom_id: file_path}`` map from
        ``rom_installs`` and passes it to ``build_shortcuts_data`` so the
        emitted ``sync_apply_unit`` shortcuts carry per-ROM launch options.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Installed"}, {"id": 11, "name": "NotInstalled"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        # rom 10 has an install record; rom 11 does not.
        _seed_install(plugin, 10, file_path="/roms/n64/installed.z64", platform_slug="n64")

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        by_rom = {s["rom_id"]: s for s in unit_events[0]["shortcuts"]}
        assert by_rom[10]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/installed.z64"'
        assert by_rom[11]["launch_options"] == ""

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

        # roms matches platform count + zero updates → incremental skip.
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
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
        assert stale_events[0] == {"remove": []}

        # Blueprint invariant #1: a delta sync must NOT shrink platform
        # collections. The skipped platform's unchanged ROM (app_id 1010)
        # must still appear in the rebuilt ``platform_app_ids`` — the
        # collection is rebuilt from the full ``roms`` table, so a skipped
        # unit's rows survive and are re-emitted under their live name.
        collection_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_collections"]
        assert len(collection_events) == 1
        assert collection_events[0]["platform_app_ids"] == {"N64": [1010]}

    @pytest.mark.asyncio
    async def test_stale_entries_unbound_but_rows_kept_after_finalize(self, plugin, fake_romm_api):
        """End-to-end: a stale ROM (disabled platform) is unbound during finalize —
        its ``shortcut_app_id`` is NULLed while the row survives (ADR-0007), not just
        dropped from the frontend via ``sync_stale``.

        Regression for the inflated ``get_sync_stats`` count: the orchestrator emits
        ``sync_stale`` so the frontend drops the shortcut, and the reporter unbinds the
        same rom_ids in ``uow.roms`` (NULL ``shortcut_app_id``, keep the row) so the
        bound-shortcut count matches the still-synced ROMs.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # rom_id 10 is the live N64 ROM (synced this run). rom_id 99 is a leftover
        # from a now-disabled platform — present in roms but in no enabled unit.
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1000, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 99, app_id=9900, platform_slug="gba", name="Z", fs_name="z.gba")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = AsyncMock(return_value={})
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # Frontend was told to remove rom_id 99, carrying its bound app_id
        # captured before the finalize unbind NULLed the binding.
        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [{"remove": [{"rom_id": 99, "app_id": 9900}]}]

        # rom 99 was unbound (NULL app_id) but its row survives; only the
        # synced ROM is still bound.
        with plugin._uow as uow:
            assert uow.roms.get(99).shortcut_app_id is None
            assert uow.roms.get(10).shortcut_app_id == 1000
            assert {r.rom_id for r in uow.roms.iter_all()} == {10, 99}

        # get_sync_stats reflects the bound count, not the pre-sync inflated count.
        stats = await plugin.get_sync_stats()
        assert stats["roms"] == 1
        assert stats["total_shortcuts"] == 1

    @pytest.mark.asyncio
    async def test_sync_stale_excludes_unbound_roms(self, plugin, fake_romm_api):
        """An already-unbound stale ROM (NULL ``shortcut_app_id``) is excluded
        from the ``sync_stale`` payload — it has no Steam shortcut to remove.

        rom 10 is the live synced ROM, rom 99 is a bound stale ROM (carries its
        app_id), and rom 77 is an unbound leftover (cleared on a prior run). Only
        the bound stale ROM appears in ``remove``, each entry carrying its app_id.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1000, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 99, app_id=9900, platform_slug="gba", name="Z", fs_name="z.gba")
        _seed_rom_row(plugin, 77, app_id=None, platform_slug="snes", name="Y", fs_name="y.sfc")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = AsyncMock(return_value={})
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        # Only the bound stale ROM (99) is emitted; the unbound leftover (77) is excluded.
        assert stale_events == [{"remove": [{"rom_id": 99, "app_id": 9900}]}]

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

    @pytest.mark.asyncio
    async def test_normal_completion_emits_finalizing_running(self, plugin, fake_romm_api):
        """A normal-completion run emits a non-terminal finalizing snapshot
        after the unit loop, before the reporter's terminal done emit."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        finalizing = [
            c.args[1]
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("stage") == "finalizing"
        ]
        assert len(finalizing) == 1
        assert finalizing[0]["running"] is True
        # The terminal done snapshot still follows it (running:false).
        done = [
            c.args[1]
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("stage") == "done"
        ]
        assert len(done) == 1
        assert done[0]["running"] is False

    @pytest.mark.asyncio
    async def test_cancelled_run_does_not_emit_finalizing(self, plugin, fake_romm_api):
        """A cancelled run skips the finalizing snapshot — its terminal emit
        is the reporter's cancelled snapshot."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

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
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        finalizing = [
            c.args[1]
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("stage") == "finalizing"
        ]
        assert finalizing == []


class TestSyncRunLifecycle:
    """The SyncRun record persisted by ``_do_sync_per_unit`` across its outcomes.

    The lifecycle methods (start/complete/cancel/error) are short write
    UoWs keyed off ``box.current_sync_id``; these tests seed that id and
    assert the persisted ``uow.sync_runs`` row, not just method coverage.
    """

    @pytest.mark.asyncio
    async def test_clean_run_persists_completed_with_platforms(self, plugin, fake_romm_api):
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 9001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "run-clean"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-clean")
        assert run is not None
        assert run.status == "completed"
        assert run.platforms_planned == 1
        assert run.roms_planned == 1
        assert run.finished_at is not None
        assert run.platforms_completed == ["N64"]

    @pytest.mark.asyncio
    async def test_empty_queue_preserves_prior_baseline(self, plugin, fake_romm_api):
        """A zero-unit sync must NOT open or complete a SyncRun — an empty
        completed run would reset the preview baseline (next preview would
        report every platform as 'added'). The prior completed run stays the
        baseline source, matching the JSON era's return-early behaviour."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # A prior real sync completed with N64 synced — this is the baseline
        # the next preview must keep reading.
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z", platforms=["Nintendo 64"], run_id="run-prior")

        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {}
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "run-empty"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            # No empty run was persisted.
            assert uow.sync_runs.get("run-empty") is None
            # The prior completed run is still the latest completed → baseline
            # platforms preserved (not reset to []).
            latest = uow.sync_runs.get_latest_completed()
            assert latest is not None
            assert latest.id == "run-prior"
            assert latest.platforms_completed == ["Nintendo 64"]

    @pytest.mark.asyncio
    async def test_cancelled_run_persists_cancelled(self, plugin, fake_romm_api):
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A"}])
        _seed_platform(fake_romm_api, platform_id=2, name="GBA", slug="gba", roms=[{"id": 20, "name": "B"}])
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            plugin._sync_service._sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "run-cancel"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-cancel")
        assert run is not None
        assert run.status == "cancelled"
        assert run.finished_at is not None
        assert run.error == "Sync cancelled"

    @pytest.mark.asyncio
    async def test_exception_in_unit_loop_persists_errored(self, plugin, fake_romm_api):
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # build_work_queue succeeds, then list_roms raises during the unit fetch.
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        fake_romm_api.list_roms_side_effect = RuntimeError("boom")
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "run-error"

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        for _ in range(3):
            await asyncio.sleep(0)

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-error")
        assert run is not None
        assert run.status == "errored"
        assert run.finished_at is not None
        assert run.error  # carries a human-readable detail

    @pytest.mark.asyncio
    async def test_terminal_write_failure_after_finalize_persists_errored(self, plugin, fake_romm_api):
        """A terminal write that raises AFTER finalize must still mark the run
        ``errored`` — not leave it stuck ``running``.

        Regression: ``finalize_per_unit_run`` nulls ``box.current_sync_id``
        before the terminal write. If the error path read that nulled id it
        would no-op and the run would stay ``running``. The fix captures the
        run id up front so ``_mark_sync_run_errored`` still targets the run.
        """
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 9001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait

        # The terminal completed-write raises (e.g. a SQLite lock during the
        # short write UoW) AFTER finalize has already nulled current_sync_id.
        def boom(*_args, **_kwargs):
            raise RuntimeError("terminal write boom")

        plugin._sync_service._orchestrator._complete_sync_run = boom  # type: ignore[method-assign]
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "run-terminal-fail"

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        for _ in range(3):
            await asyncio.sleep(0)

        # current_sync_id was nulled by finalize, but the run is still recorded errored.
        assert plugin._sync_service._current_sync_id is None
        with plugin._uow as uow:
            run = uow.sync_runs.get("run-terminal-fail")
        assert run is not None
        assert run.status == "errored"
        assert run.finished_at is not None
        assert run.error

    @pytest.mark.asyncio
    async def test_double_terminal_guard_is_noop(self, plugin, fake_romm_api):
        """Terminating an already-terminal run is a silent no-op — no raise, no clobber."""
        from domain.sync_run import SyncRun

        with plugin._uow as uow:
            run = SyncRun.start(id="run-done", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
            run.complete("2025-01-01T01:00:00", ["N64"], [])
            uow.sync_runs.save(run)

        # A second complete-transition on the already-completed run must not
        # raise or overwrite the recorded outcome.
        plugin._sync_service._orchestrator._complete_sync_run("run-done", ["SNES"], ["Faves"])

        with plugin._uow as uow:
            after = uow.sync_runs.get("run-done")
        assert after.status == "completed"
        assert after.platforms_completed == ["N64"]
        assert after.collections_completed == []


class TestWaitForUnitComplete:
    """Heartbeat-based per-unit timeout."""

    @pytest.mark.asyncio
    async def test_returns_results_when_event_set(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        event.set()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._orchestrator._clock.monotonic()
        plugin._sync_service._box.last_unit_results = {"10": 9000}

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results == {"10": 9000}

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._orchestrator._clock.monotonic()

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_on_heartbeat_timeout(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._sync_state = SyncState.RUNNING
        # Heartbeat is way too old — should timeout immediately on first loop check
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._orchestrator._clock.monotonic() - 999.0

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None


class TestReportUnitResults:
    """Per-unit ack signal — frontend callback that signals the orchestrator's wait event.

    The actual ``roms`` + ``rom_metadata`` upsert is driven by the
    orchestrator via ``commit_unit_results`` after this ack returns.
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


class TestCommitUnitResults:
    """Orchestrator-driven per-unit commit: cover-path finalize + ``roms`` + ``rom_metadata`` upsert."""

    @pytest.mark.asyncio
    async def test_updates_registry_for_unit_roms(self, plugin):
        plugin._sync_service._pending_sync = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
            11: {"rom_id": 11, "name": "B", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001, "11": 9002}, [])

        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id == 9001
            assert uow.roms.get(11).shortcut_app_id == 9002

    @pytest.mark.asyncio
    async def test_commits_roms_for_unit(self, plugin):
        """commit_unit_results lands the unit's ROM upserts in one committed UoW."""
        plugin._sync_service._pending_sync = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001}, [])

        assert plugin._uow.committed is True
        with plugin._uow as uow:
            assert uow.roms.get(10) is not None


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
        progress_stages = [
            c.args[1].get("stage") for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_progress"
        ]
        assert "cancelled" in progress_stages

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
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("stage") == "error"
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
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("stage") == "error"
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
        # The error path was taken instead.
        error_events = [
            c
            for c in decky.emit.call_args_list
            if c.args and c.args[0] == "sync_progress" and c.args[1].get("stage") == "error"
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
        plugin.settings["enabled_collections"] = {"user": {"7": True}, "smart": {}, "franchise": {}}

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
    """Per-unit metadata stamping folded into the reporter's commit (#738/#784)."""

    @pytest.mark.asyncio
    async def test_acked_roms_threaded_to_commit(self, plugin, fake_romm_api):
        """The orchestrator threads the acked ROM dicts into ``commit_unit_results``
        so the reporter can stamp ``rom_metadata`` in the same write UoW as the
        ``roms`` upsert (atomic — no separate metadata hop)."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A", "metadatum": {"genres": ["RPG"]}}],
        )

        commit_calls: list[tuple[Any, Any]] = []
        original_commit = plugin._sync_service._reporter.commit_unit_results

        async def tracked_commit(rid_to_aid, acked_roms):
            commit_calls.append((rid_to_aid, acked_roms))
            await original_commit(rid_to_aid, acked_roms)

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

        # commit_unit_results received the acked ROM dict (carrying metadatum).
        assert len(commit_calls) == 1
        _rid_to_aid, acked = commit_calls[0]
        assert [r["id"] for r in acked] == [10]
        assert acked[0]["metadatum"] == {"genres": ["RPG"]}
        # The metadata row + Rom row landed atomically in the shared UoW.
        with plugin._uow as uow:
            assert uow.roms.get(10) is not None
            meta = uow.rom_metadata.get(10)
        assert meta is not None
        assert meta.genres == ("RPG",)

    @pytest.mark.asyncio
    async def test_skipped_unit_does_not_stamp_metadata(self, plugin, fake_romm_api):
        """Incremental-skip platforms must NOT reach ``commit_unit_results``.

        The skipped short-circuit returns from ``_sync_one_unit`` before the
        per-unit commit, so no ``rom_metadata`` is written for a skipped unit
        (populated metadata from prior real fetches is preserved, #738).
        """
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # roms matches platform rom_count + zero updates → incremental skip.
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")

        commit_mock = AsyncMock()
        plugin._sync_service._reporter.commit_unit_results = commit_mock  # type: ignore[method-assign]
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

        commit_mock.assert_not_called()
        with plugin._uow as uow:
            assert uow.rom_metadata.get(10) is None

    @pytest.mark.asyncio
    async def test_acked_roms_filter(self, plugin, fake_romm_api):
        """Only the ROMs the frontend ack'd are threaded into ``commit_unit_results``."""
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

        commit_calls: list[tuple[Any, Any]] = []

        async def capture_commit(rid_to_aid, acked_roms):
            commit_calls.append((rid_to_aid, acked_roms))

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
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

        assert len(commit_calls) == 1
        _rid_to_aid, acked = commit_calls[0]
        assert {r["id"] for r in acked} == {1, 3, 5}


class TestRegression738CacheCorruption:
    """Regression for #738 — delta sync must not erase populated metadata.

    Before the fix, the per-unit incremental-skip path produced thin
    registry-reconstructed ROMs (no ``metadatum`` field). Those flowed
    through the metadata stamp and overwrote populated entries with empty
    ones. Symptom: 160 populated entries → 62 after one delta sync.
    Post-cutover the equivalent guard lives in the reporter's per-unit
    commit — a skipped unit never reaches it, so its ``rom_metadata`` rows
    survive untouched.
    """

    @pytest.mark.asyncio
    async def test_delta_sync_preserves_populated_metadata(self, plugin, fake_romm_api):
        """Populated ``rom_metadata`` rows survive a per-unit delta sync of unchanged platforms.

        Scenario: ``uow`` has 3 ROMs on platform N64 with populated
        metadata. Server reports zero updated after ``last_sync``, so
        ``fetch_platform_unit`` returns skipped=True. The orchestrator's
        skip-guard short-circuits before the per-unit commit, so the
        populated metadata rows are preserved untouched.
        """
        from domain.rom_metadata import RomMetadata

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Pre-existing populated metadata rows (the "160 entries" scenario
        # boiled down to 3 ROMs), each backed by a bound Rom row (FK parent).
        seeds = {
            1: RomMetadata(
                summary="Game 1 description",
                genres=("RPG",),
                companies=("Square",),
                first_release_date=946684800,
                average_rating=95.0,
                game_modes=("Single player",),
                player_count="1",
                cached_at=100.0,
                steam_categories=(2, 21),
            ),
            2: RomMetadata(
                summary="Game 2 description",
                genres=("Action",),
                companies=("Capcom",),
                first_release_date=1000000000,
                average_rating=88.0,
                game_modes=("Multiplayer",),
                player_count="1-4",
                cached_at=100.0,
                steam_categories=(1, 21),
            ),
            3: RomMetadata(
                summary="Game 3 description",
                genres=("Puzzle",),
                companies=("Nintendo",),
                first_release_date=1100000000,
                average_rating=92.0,
                game_modes=("Single player",),
                player_count="1",
                cached_at=100.0,
                steam_categories=(4,),
            ),
        }
        for rid, meta in seeds.items():
            _seed_rom_row(
                plugin, rid, app_id=1000 + rid, platform_slug="n64", name=f"Game {rid}", fs_name=f"g{rid}.z64"
            )
            with plugin._uow as uow:
                uow.rom_metadata.save(rid, meta)

        # A prior completed run + matching roms count drive the incremental skip.
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        # Server reports the platform exists with 3 ROMs and ZERO updates.
        # No ROMs seeded on the fake → list_roms_updated_after returns total=0.
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"1": 1001, "2": 1002, "3": 1003}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # Post-flight: the 3 populated metadata rows MUST survive untouched.
        # Pre-fix, they would have been overwritten by empty ones.
        with plugin._uow as uow:
            for rid, meta in seeds.items():
                assert uow.rom_metadata.get(rid) == meta


class TestWaitForUnitCompleteCancelled:
    """Tests for asyncio.CancelledError in _wait_for_unit_complete."""

    @pytest.mark.asyncio
    async def test_cancelled_error_during_sleep_is_logged_and_reraised(self, plugin):
        """If the inner sleep is cancelled, log + re-raise so the outer loop sees the cancel."""

        class _CancellingSleeper:
            async def sleep(self, _seconds: float) -> None:
                raise asyncio.CancelledError()

        plugin._sync_service._orchestrator._sleeper = _CancellingSleeper()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._orchestrator._clock.monotonic()

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
