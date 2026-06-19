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


class TestGetPlatformsMaterialization:
    """Tests for get_platforms() — the #1007 empty-map → full-map self-heal."""

    @pytest.mark.asyncio
    async def test_materializes_full_all_true_map_when_empty(self, plugin, fake_romm_api):
        """Empty map + shown platforms → persisted full all-True map (one save)."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 3},
            {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
        ]
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service._fetcher.get_platforms()

        assert result["success"] is True
        assert plugin.settings["enabled_platforms"] == {"1": True, "2": True}
        assert plugin._settings_persister.save_count == 1
        assert all(p["sync_enabled"] is True for p in result["platforms"])

    @pytest.mark.asyncio
    async def test_excludes_zero_rom_platforms_from_materialized_map(self, plugin, fake_romm_api):
        """A rom_count==0 platform is neither shown nor materialized."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 3},
            {"id": 2, "name": "Empty", "slug": "empty", "rom_count": 0},
        ]
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service._fetcher.get_platforms()

        assert plugin.settings["enabled_platforms"] == {"1": True}
        assert [p["slug"] for p in result["platforms"]] == ["n64"]

    @pytest.mark.asyncio
    async def test_does_not_re_materialize_when_map_non_empty(self, plugin, fake_romm_api):
        """A non-empty stored map is read literally — no re-write, no save."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 3},
            {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
        ]
        plugin.settings["enabled_platforms"] = {"1": False}

        result = await plugin._sync_service._fetcher.get_platforms()

        assert plugin.settings["enabled_platforms"] == {"1": False}
        assert plugin._settings_persister.save_count == 0
        by_id = {p["id"]: p["sync_enabled"] for p in result["platforms"]}
        # Absent id 2 resolves False once any pref exists (the literal-map read).
        assert by_id == {1: False, 2: False}

    @pytest.mark.asyncio
    async def test_does_not_persist_when_no_shown_platforms(self, plugin, fake_romm_api):
        """Empty map + zero shown platforms → sentinel survives, no save."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "Empty", "slug": "empty", "rom_count": 0},
        ]
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service._fetcher.get_platforms()

        assert result["success"] is True
        assert result["platforms"] == []
        assert plugin.settings["enabled_platforms"] == {}
        assert plugin._settings_persister.save_count == 0

    @pytest.mark.asyncio
    async def test_one_off_toggle_after_materialization_keeps_others_enabled(self, plugin, fake_romm_api):
        """#1007 regression: get_platforms → save one OFF → other platforms still sync.

        Reproduces the data-loss path end-to-end at the fetcher seam: open the
        Platforms page (materialize), un-toggle exactly ONE platform, then run
        the sync-time filter and assert every OTHER platform survives.
        """
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 3},
            {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
            {"id": 3, "name": "GBA", "slug": "gba", "rom_count": 7},
        ]
        plugin.settings["enabled_platforms"] = {}

        # 1. Platforms page mount materializes the full all-True map.
        await plugin._sync_service._fetcher.get_platforms()
        assert plugin.settings["enabled_platforms"] == {"1": True, "2": True, "3": True}

        # 2. Un-toggle exactly one platform (single-key write).
        plugin._sync_service._fetcher.save_platform_sync(2, False)

        # 3. Sync-time filter: every OTHER platform must survive (pre-fix this
        #    returned only the never-touched platforms, dropping the rest).
        filtered = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        kept = {p["name"] for p in filtered}
        assert kept == {"N64", "GBA"}
        assert "SNES" not in kept


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
