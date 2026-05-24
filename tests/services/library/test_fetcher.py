"""Tests for LibraryFetcher — platform/collection roundtrips, ROM fetch pipeline.

Driven end-to-end through :class:`FakeRommApi` so each test seeds
in-memory platforms/ROMs/collections on the fake and asserts on the
observable output of the fetcher (returned ROM lists, mutated state).
Failure paths are exercised with ``fail_on_next`` (one-shot) and the
per-method ``*_side_effect`` attributes (persistent) — no
``run_in_executor`` patching, no ``MagicMock(romm_api)``.
"""

import asyncio

import pytest

from domain.sync_state import SyncState
from domain.work_unit import WorkUnit


def _wire_fake(plugin, fake_romm_api):
    """Point the fetcher at the shared ``FakeRommApi``.

    The ``plugin`` fixture wires the LibraryService with a bare
    ``MagicMock`` romm_api; tests that drive end-to-end need to swap
    that for the seeded fake on the fetcher's captured ref.
    """
    plugin._sync_service._fetcher._romm_api = fake_romm_api


class TestCheckCancelling:
    """Tests for _check_cancelling() — pure state check, no API surface."""

    def test_raises_when_cancelling(self, plugin):
        plugin._sync_service._sync_state = SyncState.CANCELLING
        with pytest.raises(asyncio.CancelledError):
            plugin._sync_service._fetcher._check_cancelling()

    def test_noop_when_running(self, plugin):
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._fetcher._check_cancelling()  # should not raise

    def test_noop_when_idle(self, plugin):
        plugin._sync_service._fetcher._check_cancelling()  # should not raise


class TestFetchEnabledPlatforms:
    """Tests for _fetch_enabled_platforms() — list_platforms + enabled-filter."""

    @pytest.mark.asyncio
    async def test_filters_by_enabled(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64"},
            {"id": 2, "name": "SNES", "slug": "snes"},
            {"id": 3, "name": "GBA", "slug": "gba"},
        ]
        plugin.settings["enabled_platforms"] = {"1": True, "2": False, "3": True}

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert len(result) == 2
        names = [p["name"] for p in result]
        assert "N64" in names
        assert "GBA" in names
        assert "SNES" not in names

    @pytest.mark.asyncio
    async def test_all_enabled_when_no_prefs(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64"},
            {"id": 2, "name": "SNES", "slug": "snes"},
        ]
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_non_list_response(self, plugin, fake_romm_api):
        """When ``list_platforms`` returns a non-list, treat as empty."""
        _wire_fake(plugin, fake_romm_api)
        # Override ``list_platforms`` to return a dict (the real adapter
        # might surface error envelopes shaped this way).
        fake_romm_api.list_platforms = lambda: {"error": "bad response"}  # type: ignore[method-assign]

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert result == []


class TestReconstructPlatformFromRegistry:
    """Tests for _reconstruct_platform_from_registry() — pure registry walk."""

    def test_reconstructs_matching_entries(self, plugin):
        plugin._state["shortcut_registry"] = {
            "1": {
                "name": "Game A",
                "fs_name": "a.z64",
                "platform_name": "N64",
                "igdb_id": 100,
                "sgdb_id": 200,
                "ra_id": 300,
            },
            "2": {"name": "Game B", "fs_name": "b.z64", "platform_name": "N64"},
            "3": {"name": "Game C", "fs_name": "c.z64", "platform_name": "SNES"},
        }
        result = plugin._sync_service._fetcher._reconstruct_platform_from_registry(
            plugin._state["shortcut_registry"], "N64", "n64"
        )
        assert len(result) == 2
        ids = {r["id"] for r in result}
        assert ids == {1, 2}
        # Check fields
        game_a = next(r for r in result if r["id"] == 1)
        assert game_a["name"] == "Game A"
        assert game_a["platform_name"] == "N64"
        assert game_a["platform_slug"] == "n64"
        assert game_a["igdb_id"] == 100

    def test_empty_when_no_match(self, plugin):
        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "SNES"},
        }
        result = plugin._sync_service._fetcher._reconstruct_platform_from_registry(
            plugin._state["shortcut_registry"], "N64", "n64"
        )
        assert result == []

    def test_empty_registry(self, plugin):
        result = plugin._sync_service._fetcher._reconstruct_platform_from_registry({}, "N64", "n64")
        assert result == []


class TestTryIncrementalSkip:
    """Tests for _try_incremental_skip() — incremental-fetch fast path."""

    @pytest.mark.asyncio
    async def test_skips_unchanged_platform(self, plugin, fake_romm_api):
        """server_total=0 + registry_count == platform_total => reconstruct + skip."""
        _wire_fake(plugin, fake_romm_api)
        # No ROMs updated => list_roms_updated_after returns total=0
        # (the fake's default behaviour with no seeded ``roms``).
        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
            "2": {"name": "Game B", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 2}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is True
        assert len(all_roms) == 2  # reconstructed from registry

    @pytest.mark.asyncio
    async def test_no_skip_on_first_sync(self, plugin, fake_romm_api):
        """last_sync=None => early return, no API roundtrip."""
        _wire_fake(plugin, fake_romm_api)
        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, {}, None, "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False
        # No API call made (early return before list_roms_updated_after).
        assert not any(c[0] == "list_roms_updated_after" for c in fake_romm_api.call_log)

    @pytest.mark.asyncio
    async def test_no_skip_when_registry_empty(self, plugin, fake_romm_api):
        """registry_count=0 => early return."""
        _wire_fake(plugin, fake_romm_api)
        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, {}, "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_no_skip_when_updates_exist(self, plugin, fake_romm_api):
        """server reports updated rows => fall through to full fetch."""
        _wire_fake(plugin, fake_romm_api)
        # Seed 3 ROMs updated after the cutoff so list_roms_updated_after total=3.
        fake_romm_api.roms = {
            10: {"id": 10, "platform_id": 1, "name": "U1", "updated_at": "2025-06-01T00:00:00"},
            11: {"id": 11, "platform_id": 1, "name": "U2", "updated_at": "2025-06-01T00:00:00"},
            12: {"id": 12, "platform_id": 1, "name": "U3", "updated_at": "2025-06-01T00:00:00"},
        }
        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 1}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False
        assert len(all_roms) == 0

    @pytest.mark.asyncio
    async def test_no_skip_when_count_mismatch(self, plugin, fake_romm_api):
        """No updates but rom_count != registry_count => fall through (registry stale)."""
        _wire_fake(plugin, fake_romm_api)
        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        # server reports 5 ROMs, registry has 1 — mismatch forces refetch.
        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_falls_back_on_api_error(self, plugin, fake_romm_api):
        """Transport failure on the delta check => swallow + force full fetch."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.list_roms_updated_after_side_effect = OSError("connection reset")

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 1}
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False


class TestFullFetchPlatformRoms:
    """Tests for _full_fetch_platform_roms() — paginated fetch loop."""

    @pytest.mark.asyncio
    async def test_fetches_single_page(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.roms = {
            1: {"id": 1, "platform_id": 1, "name": "Game A", "files": ["f1"]},
            2: {"id": 2, "platform_id": 1, "name": "Game B"},
        }

        all_roms: list[dict] = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 2
        assert all_roms[0]["platform_name"] == "N64"
        assert all_roms[0]["platform_slug"] == "n64"
        # files should be removed
        assert "files" not in all_roms[0]

    @pytest.mark.asyncio
    async def test_fetches_multiple_pages(self, plugin, fake_romm_api):
        """51 ROMs => one full page (50) + a tail page (1) => loop exits on short page."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(51)}

        all_roms: list[dict] = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 51

    @pytest.mark.asyncio
    async def test_raises_on_api_error_to_protect_stale_cleanup(self, plugin, fake_romm_api):
        """Pagination failure must propagate — silently returning a partial list
        would cause the orchestrator's stale-cleanup pass to wipe every ROM the
        truncated fetch missed. See #630."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.list_roms_side_effect = RuntimeError("Server error")

        all_roms: list[dict] = []
        with pytest.raises(RuntimeError, match="Server error"):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)

    @pytest.mark.asyncio
    async def test_raises_on_second_page_failure(self, plugin, fake_romm_api):
        """Page 1 OK + page 2 raises must propagate — partial accumulation is unsafe.

        Wraps ``list_roms`` with a counter-closure so the first call returns
        the seeded page and the second call raises after the first page's
        bytes are already consumed; this exercises the pagination-break fix
        from #630.
        """
        _wire_fake(plugin, fake_romm_api)
        # Seed exactly one full page so the loop performs a second call.
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(50)}

        # Arm an OSError to fire on the *second* list_roms call (after the
        # first page has been consumed).
        original_list_roms = fake_romm_api.list_roms
        call_count = {"n": 0}

        def list_roms_with_second_page_failure(platform_id, limit=50, offset=0):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("page 2 boom")
            return original_list_roms(platform_id, limit, offset)

        fake_romm_api.list_roms = list_roms_with_second_page_failure  # type: ignore[method-assign]

        all_roms: list[dict] = []
        with pytest.raises(RuntimeError, match="page 2 boom"):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)

    @pytest.mark.asyncio
    async def test_cancelling_during_fetch(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        plugin._sync_service._sync_state = SyncState.CANCELLING

        all_roms: list[dict] = []
        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)


class TestBuildWorkQueueErrorPaths:
    """Tests for build_work_queue() collection-list failure / filter branches."""

    @pytest.mark.asyncio
    async def test_user_collection_list_failure_continues_with_empty(self, plugin, fake_romm_api):
        """User-collection fetch raises => warning logged, treated as empty."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "user": {"1": True},
            "smart": {},
            "franchise": {"42": True},
        }

        fake_romm_api.list_collections_side_effect = RuntimeError("user collections boom")
        fake_romm_api.virtual_collections = {
            "franchise": [
                {"id": "42", "name": "Faves", "slug": "faves", "rom_count": 3},
            ],
        }

        units = await plugin._sync_service._fetcher.build_work_queue()

        # User-collections branch swallowed the failure; franchise collection still listed.
        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_franchise_collection_list_failure_continues_with_empty(self, plugin, fake_romm_api):
        """Franchise-collection fetch raises => warning logged, treated as empty."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "user": {"7": True},
            "smart": {},
            "franchise": {"100": True},
        }

        fake_romm_api.collections = [{"id": "7", "name": "Faves", "slug": "faves", "rom_count": 4}]
        fake_romm_api.list_virtual_collections_side_effect = RuntimeError("franchise collections boom")

        units = await plugin._sync_service._fetcher.build_work_queue()

        # User collection survives; franchise branch swallowed the failure.
        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_smart_collection_list_failure_continues_with_empty(self, plugin, fake_romm_api):
        """Smart-collection fetch raises => warning logged, treated as empty."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "user": {"7": True},
            "smart": {"5": True},
            "franchise": {},
        }

        fake_romm_api.collections = [{"id": "7", "name": "Faves", "slug": "faves", "rom_count": 4}]
        fake_romm_api.list_smart_collections_side_effect = RuntimeError("smart collections boom")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_skips_disabled_collections_in_all_buckets(self, plugin, fake_romm_api):
        """Collections returned by the API but not in enabled_ids are filtered out."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        # Only the "1" user / "5" smart / "100" franchise collections are enabled.
        plugin.settings["enabled_collections"] = {
            "user": {"1": True, "2": False},
            "smart": {"5": True, "6": False},
            "franchise": {"100": True, "200": False},
        }

        fake_romm_api.collections = [
            {"id": "1", "name": "Enabled User", "slug": "eu", "rom_count": 1},
            {"id": "2", "name": "Disabled User", "slug": "du", "rom_count": 1},
        ]
        fake_romm_api.smart_collections = [
            {"id": "5", "name": "Enabled Smart", "slug": "es", "rom_count": 1},
            {"id": "6", "name": "Disabled Smart", "slug": "ds", "rom_count": 1},
        ]
        fake_romm_api.virtual_collections = {
            "franchise": [
                {"id": "100", "name": "Enabled Franchise", "slug": "ef", "rom_count": 1},
                {"id": "200", "name": "Disabled Franchise", "slug": "df", "rom_count": 1},
            ],
        }

        units = await plugin._sync_service._fetcher.build_work_queue()

        # Only enabled collections survive the cid-not-in-enabled_ids skip.
        assert [u.name for u in units] == ["Enabled User", "Enabled Smart", "Enabled Franchise"]
        kinds = [u.collection_kind for u in units]
        assert kinds == ["user", "smart", "franchise"]


class TestTryUnitIncrementalSkip:
    """Tests for _try_unit_incremental_skip() exception fallback."""

    @pytest.mark.asyncio
    async def test_falls_back_on_delta_api_exception(self, plugin, fake_romm_api):
        """Lines 447-451: delta-fetch raises => warning logged, returns None to force full fetch."""
        _wire_fake(plugin, fake_romm_api)
        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        plugin._state["last_sync"] = "2025-01-01T00:00:00"

        fake_romm_api.list_roms_updated_after_side_effect = RuntimeError("delta boom")

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        # Falls back to "force full fetch" sentinel.
        assert result is None


class TestFetchPlatformUnit:
    """Tests for fetch_platform_unit() — wrong-type guard, error propagation, pagination."""

    @pytest.mark.asyncio
    async def test_raises_on_non_platform_unit(self, plugin):
        """Line 478: fetch_platform_unit must reject collection units."""
        unit = WorkUnit(type="collection", id="1", name="Coll", slug="", rom_count=0)
        with pytest.raises(ValueError, match="non-platform unit"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_first_page_exception_propagates(self, plugin, fake_romm_api):
        """A page-fetch failure must raise so the orchestrator aborts before stale-cleanup.

        Previous behaviour swallowed the exception and returned ``([], False)``
        — which classified every existing ROM as stale and wiped the Steam
        shortcut library. See #630.
        """
        _wire_fake(plugin, fake_romm_api)
        # No prior sync => incremental skip returns None and we fall through to pagination.
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        fake_romm_api.list_roms_side_effect = RuntimeError("page boom")

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=10)
        with pytest.raises(RuntimeError, match="page boom"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_second_page_exception_propagates(self, plugin, fake_romm_api):
        """Page 1 OK + page 2 raises must propagate so partial accumulation never
        reaches the stale-cleanup pass. See #630.

        ``fail_on_next`` arms the first call to raise, which would fire on
        page 1 — instead we wrap ``list_roms`` to raise on the second call
        after the first page's bytes are already consumed by the caller.
        """
        _wire_fake(plugin, fake_romm_api)
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        # Seed exactly one full page worth of ROMs (50 items at limit=50).
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(50)}

        original_list_roms = fake_romm_api.list_roms
        call_count = {"n": 0}

        def list_roms_with_second_page_failure(platform_id, limit=50, offset=0):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("page 2 boom")
            return original_list_roms(platform_id, limit, offset)

        fake_romm_api.list_roms = list_roms_with_second_page_failure  # type: ignore[method-assign]

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=200)
        with pytest.raises(RuntimeError, match="page 2 boom"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_paginates_across_multiple_pages(self, plugin, fake_romm_api):
        """Line 514: a full first page must trigger offset += limit and a second fetch."""
        _wire_fake(plugin, fake_romm_api)
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        # 51 ROMs at limit=50 => page 1 fills to limit, page 2 carries the tail.
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(51)}

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=51)
        unit_roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)

        assert skipped is False
        assert len(unit_roms) == 51
        assert {r["platform_name"] for r in unit_roms} == {"N64"}


class TestFetchCollectionUnit:
    """Tests for fetch_collection_unit() — wrong-type guard, multi-page pagination."""

    @pytest.mark.asyncio
    async def test_raises_on_non_collection_unit(self, plugin):
        """Line 534: fetch_collection_unit must reject platform units."""
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=0)
        with pytest.raises(ValueError, match="non-collection unit"):
            await plugin._sync_service._fetcher.fetch_collection_unit(unit, set())

    @pytest.mark.asyncio
    async def test_paginates_across_multiple_pages(self, plugin, fake_romm_api):
        """Line 566: a full first page must trigger offset += limit and a second fetch."""
        _wire_fake(plugin, fake_romm_api)

        # 51 ROMs in collection id=7 => page 1 fills, page 2 carries the tail.
        fake_romm_api.roms = {
            i: {
                "id": i,
                "platform_id": 1,
                "name": f"G{i}",
                "platform_name": "N64",
                "platform_slug": "n64",
                "collection_ids": [7],
            }
            for i in range(50)
        }
        fake_romm_api.roms[999] = {
            "id": 999,
            "platform_id": 1,
            "name": "G999",
            "platform_name": "N64",
            "platform_slug": "n64",
            "collection_ids": [7],
        }

        unit = WorkUnit(type="collection", id=7, name="Coll", slug="", rom_count=51, collection_kind="user")
        synced: set[int] = set()
        new_roms, all_collection_rom_ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert len(new_roms) == 51
        assert len(all_collection_rom_ids) == 51
        assert 999 in synced

    @pytest.mark.asyncio
    async def test_dispatches_smart_collection_to_smart_endpoint(self, plugin, fake_romm_api):
        """collection_kind='smart' routes through list_roms_by_smart_collection."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.roms = {
            1: {"id": 1, "platform_name": "N64", "smart_collection_ids": [9]},
            2: {"id": 2, "platform_name": "SNES", "smart_collection_ids": [9]},
        }

        unit = WorkUnit(type="collection", id=9, name="Smart Filter", slug="", rom_count=2, collection_kind="smart")
        synced: set[int] = set()
        new_roms, ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert [r["id"] for r in new_roms] == [1, 2]
        assert ids == [1, 2]
        # Verify the smart endpoint was the one consulted, not the user/virtual ones.
        method_calls = [c[0] for c in fake_romm_api.call_log]
        assert "list_roms_by_smart_collection" in method_calls
        assert "list_roms_by_collection" not in method_calls
        assert "list_roms_by_virtual_collection" not in method_calls

    @pytest.mark.asyncio
    async def test_dispatches_franchise_collection_to_virtual_endpoint(self, plugin, fake_romm_api):
        """collection_kind='franchise' routes through list_roms_by_virtual_collection."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.roms = {
            1: {"id": 1, "platform_name": "N64", "virtual_collection_ids": ["100"]},
        }

        unit = WorkUnit(type="collection", id="100", name="Mario", slug="", rom_count=1, collection_kind="franchise")
        synced: set[int] = set()
        new_roms, _ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert [r["id"] for r in new_roms] == [1]
        method_calls = [c[0] for c in fake_romm_api.call_log]
        assert "list_roms_by_virtual_collection" in method_calls
        assert "list_roms_by_smart_collection" not in method_calls
