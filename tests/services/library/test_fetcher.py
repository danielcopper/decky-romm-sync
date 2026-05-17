"""Tests for LibraryFetcher — platform/collection roundtrips, ROM fetch pipeline."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.sync_state import SyncState


class TestCheckCancelling:
    """Tests for _check_cancelling() — lines 505-508."""

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
    """Tests for _fetch_enabled_platforms() — lines 398-411, 402-403."""

    @pytest.mark.asyncio
    async def test_filters_by_enabled(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64"},
                {"id": 2, "name": "SNES", "slug": "snes"},
                {"id": 3, "name": "GBA", "slug": "gba"},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin.settings["enabled_platforms"] = {"1": True, "2": False, "3": True}

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert len(result) == 2
        names = [p["name"] for p in result]
        assert "N64" in names
        assert "GBA" in names
        assert "SNES" not in names

    @pytest.mark.asyncio
    async def test_all_enabled_when_no_prefs(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "name": "N64", "slug": "n64"},
                {"id": 2, "name": "SNES", "slug": "snes"},
            ]
        )
        plugin._sync_service._loop = mock_loop
        plugin.settings["enabled_platforms"] = {}

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_for_non_list_response(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"error": "bad response"})
        plugin._sync_service._loop = mock_loop

        result = await plugin._sync_service._fetcher._fetch_enabled_platforms()
        assert result == []


class TestReconstructPlatformFromRegistry:
    """Tests for _reconstruct_platform_from_registry() — lines 413-429."""

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
    """Tests for _try_incremental_skip() — lines 431-465."""

    @pytest.mark.asyncio
    async def test_skips_unchanged_platform(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 0})
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

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
    async def test_no_skip_on_first_sync(self, plugin):
        from unittest.mock import MagicMock

        mock_loop = MagicMock()
        plugin._sync_service._loop = mock_loop

        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        # last_sync is None => no skip
        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, {}, None, "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_no_skip_when_registry_empty(self, plugin):
        from unittest.mock import MagicMock

        mock_loop = MagicMock()
        plugin._sync_service._loop = mock_loop

        platform = {"id": 1, "rom_count": 5}
        all_roms = []

        # registry has no entries for this platform
        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, {}, "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_no_skip_when_updates_exist(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 3})
        plugin._sync_service._loop = mock_loop

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
    async def test_no_skip_when_count_mismatch(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value={"total": 0})
        plugin._sync_service._loop = mock_loop

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        platform = {"id": 1, "rom_count": 5}  # server has 5, registry has 1
        all_roms = []

        skipped = await plugin._sync_service._fetcher._try_incremental_skip(
            platform, plugin._state["shortcut_registry"], "2025-01-01T00:00:00", "N64", "n64", all_roms, 1, 1
        )
        assert skipped is False

    @pytest.mark.asyncio
    async def test_falls_back_on_api_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Connection failed"))
        plugin._sync_service._loop = mock_loop

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
    """Tests for _full_fetch_platform_roms() — lines 467-503."""

    @pytest.mark.asyncio
    async def test_fetches_single_page(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value={
                "items": [
                    {"id": 1, "name": "Game A", "files": ["f1"]},
                    {"id": 2, "name": "Game B"},
                ]
            }
        )
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 2
        assert all_roms[0]["platform_name"] == "N64"
        assert all_roms[0]["platform_slug"] == "n64"
        # files should be removed
        assert "files" not in all_roms[0]

    @pytest.mark.asyncio
    async def test_fetches_multiple_pages(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        page1 = {"items": [{"id": i, "name": f"G{i}"} for i in range(50)]}
        page2 = {"items": [{"id": 50, "name": "G50"}]}

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=[page1, page2])
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)
        assert len(all_roms) == 51

    @pytest.mark.asyncio
    async def test_raises_on_api_error_to_protect_stale_cleanup(self, plugin):
        """Pagination failure must propagate — silently returning a partial list
        would cause the orchestrator's stale-cleanup pass to wipe every ROM the
        truncated fetch missed. See #630."""
        from unittest.mock import AsyncMock, MagicMock

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=RuntimeError("Server error"))
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        with pytest.raises(RuntimeError, match="Server error"):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)

    @pytest.mark.asyncio
    async def test_raises_on_second_page_failure(self, plugin):
        """Page 1 OK + page 2 raises must propagate — partial accumulation is unsafe."""
        from unittest.mock import AsyncMock, MagicMock

        page1 = {"items": [{"id": i, "name": f"G{i}"} for i in range(50)]}
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=[page1, RuntimeError("page 2 boom")])
        plugin._sync_service._loop = mock_loop
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        with pytest.raises(RuntimeError, match="page 2 boom"):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)

    @pytest.mark.asyncio
    async def test_cancelling_during_fetch(self, plugin):
        from unittest.mock import AsyncMock

        plugin._sync_service._sync_state = SyncState.CANCELLING
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        all_roms = []
        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._fetcher._full_fetch_platform_roms(1, "N64", "n64", all_roms, 1, 1)


class TestPrefetchAllUnits:
    """Tests for prefetch_all_units — the Skip Preview OFF upfront fetch."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_units(self, plugin):
        """No enabled platforms + no enabled collections → empty prefetch + empty aggregates."""
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=[])
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        (
            prefetched,
            all_roms,
            shortcuts_data,
            memberships,
            platform_rom_ids,
        ) = await plugin._sync_service._fetcher.prefetch_all_units()

        assert prefetched == []
        assert all_roms == []
        assert shortcuts_data == []
        assert memberships == {}
        assert platform_rom_ids == set()

    @pytest.mark.asyncio
    async def test_fetches_each_platform_unit(self, plugin):
        """Each platform unit goes through fetch_platform_unit and aggregates into all_roms."""
        from domain.work_unit import WorkUnit

        queue = [
            WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1),
            WorkUnit(type="platform", id=2, name="GBA", slug="gba", rom_count=1),
        ]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)

        async def fake_platform(unit):
            return [
                {
                    "id": int(unit.id) * 10,
                    "name": f"Game {unit.name}",
                    "platform_name": unit.name,
                    "platform_slug": unit.slug,
                }
            ], False

        plugin._sync_service._fetcher.fetch_platform_unit = fake_platform
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        (
            prefetched,
            all_roms,
            _shortcuts,
            memberships,
            platform_rom_ids,
        ) = await plugin._sync_service._fetcher.prefetch_all_units()

        assert [pu.unit.name for pu in prefetched] == ["N64", "GBA"]
        assert {r["id"] for r in all_roms} == {10, 20}
        assert platform_rom_ids == {10, 20}
        assert memberships == {}

    @pytest.mark.asyncio
    async def test_fetches_collection_unit_records_membership(self, plugin):
        """Collection units populate collection_memberships with their member ids."""
        from domain.work_unit import WorkUnit

        queue = [WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=2)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)

        async def fake_collection(_unit, synced):
            synced.add(101)
            return [
                {"id": 101, "name": "C101", "platform_name": "N64", "platform_slug": "n64"},
            ], [101, 102]

        plugin._sync_service._fetcher.fetch_collection_unit = fake_collection
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        (
            prefetched,
            _all,
            _shortcuts,
            memberships,
            platform_rom_ids,
        ) = await plugin._sync_service._fetcher.prefetch_all_units()

        assert memberships == {"Faves": [101, 102]}
        # Collection-only ROMs do not land in platform_rom_ids.
        assert platform_rom_ids == set()
        # PrefetchedUnit retains the full membership list for finalize.
        assert prefetched[0].all_collection_rom_ids == [101, 102]

    @pytest.mark.asyncio
    async def test_preserves_skipped_flag_on_platform_unit(self, plugin):
        """``skipped=True`` from fetch_platform_unit flows into the PrefetchedUnit."""
        from domain.work_unit import WorkUnit

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64"}], True)
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        prefetched, *_ = await plugin._sync_service._fetcher.prefetch_all_units()

        assert prefetched[0].skipped is True

    @pytest.mark.asyncio
    async def test_caches_metadata_for_every_rom(self, plugin):
        """Aggregated ROMs run through metadata_service for the dirty-flush before returning."""
        from domain.work_unit import WorkUnit

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 10, "name": "A", "platform_name": "N64"}], True)
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        metadata_service = MagicMock()
        metadata_service.extract_metadata = MagicMock(return_value={"name": "A"})
        plugin._sync_service._fetcher._metadata_service = metadata_service

        await plugin._sync_service._fetcher.prefetch_all_units()

        metadata_service.extract_metadata.assert_called_once()
        metadata_service.mark_metadata_dirty.assert_called_once()
        metadata_service.flush_metadata_if_dirty.assert_called_once()

    @pytest.mark.asyncio
    async def test_collection_with_empty_rom_ids_skips_membership(self, plugin):
        """Collection returning an empty member-id list is not added to memberships."""
        from domain.work_unit import WorkUnit

        queue = [WorkUnit(type="collection", id="9", name="EmptyColl", slug="", rom_count=0)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)

        async def fake_collection(_unit, _synced):
            return [], []

        plugin._sync_service._fetcher.fetch_collection_unit = fake_collection
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        prefetched, all_roms, _shortcuts, memberships, _pids = await plugin._sync_service._fetcher.prefetch_all_units()

        # Branch 613->615: empty all_collection_rom_ids must NOT create a membership entry.
        assert memberships == {}
        assert all_roms == []
        assert prefetched[0].all_collection_rom_ids == []

    @pytest.mark.asyncio
    async def test_skips_metadata_block_when_no_metadata_service(self, plugin):
        """Branch 628->634: when metadata_service is None, the cache-stamping loop is skipped."""
        from domain.work_unit import WorkUnit

        queue = [WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)]
        plugin._sync_service._fetcher.build_work_queue = AsyncMock(return_value=queue)
        plugin._sync_service._fetcher.fetch_platform_unit = AsyncMock(
            return_value=([{"id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64"}], False),
        )
        plugin._sync_service._orchestrator._emit_progress = AsyncMock()

        plugin._sync_service._fetcher._metadata_service = None
        # Tracking dict to confirm the metadata_cache was NOT touched.
        plugin._sync_service._fetcher._metadata_cache = {}

        prefetched, all_roms, *_ = await plugin._sync_service._fetcher.prefetch_all_units()

        assert len(all_roms) == 1
        assert prefetched[0].unit.name == "N64"
        # No metadata service => metadata_cache stays empty.
        assert plugin._sync_service._fetcher._metadata_cache == {}


class TestBuildWorkQueueErrorPaths:
    """Tests for build_work_queue() collection-list failure / filter branches."""

    @pytest.mark.asyncio
    async def test_user_collection_list_failure_continues_with_empty(self, plugin):
        """Lines 375-377: user-collection fetch raises => warning logged, treated as empty."""
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"42": True}

        async def fake_run_in_executor(_executor, fn, *args):
            if fn is plugin._romm_api.list_platforms:
                return []
            if fn is plugin._romm_api.list_collections:
                raise RuntimeError("user collections boom")
            if fn is plugin._romm_api.list_virtual_collections:
                return [
                    {"id": "42", "name": "Faves", "slug": "faves", "rom_count": 3, "is_virtual": True},
                ]
            raise AssertionError(f"unexpected call: {fn}")

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_run_in_executor)
        plugin._sync_service._loop = mock_loop

        units = await plugin._sync_service._fetcher.build_work_queue()

        # User-collections branch swallowed the failure; franchise collection still listed.
        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_franchise_collection_list_failure_continues_with_empty(self, plugin):
        """Lines 382-384: franchise-collection fetch raises => warning logged, treated as empty."""
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"7": True}

        async def fake_run_in_executor(_executor, fn, *args):
            if fn is plugin._romm_api.list_platforms:
                return []
            if fn is plugin._romm_api.list_collections:
                return [{"id": "7", "name": "Faves", "slug": "faves", "rom_count": 4}]
            if fn is plugin._romm_api.list_virtual_collections:
                raise RuntimeError("franchise collections boom")
            raise AssertionError(f"unexpected call: {fn}")

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_run_in_executor)
        plugin._sync_service._loop = mock_loop

        units = await plugin._sync_service._fetcher.build_work_queue()

        # User collection survives; franchise branch swallowed the failure.
        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_skips_disabled_user_and_franchise_collections(self, plugin):
        """Lines 389 + 403: collections returned by the API but not in enabled_ids are filtered out."""
        plugin.settings["enabled_platforms"] = {}
        # Only the "1" user collection and "100" franchise collection are enabled.
        plugin.settings["enabled_collections"] = {"1": True, "100": True}

        async def fake_run_in_executor(_executor, fn, *args):
            if fn is plugin._romm_api.list_platforms:
                return []
            if fn is plugin._romm_api.list_collections:
                return [
                    {"id": "1", "name": "Enabled User", "slug": "eu", "rom_count": 1},
                    {"id": "2", "name": "Disabled User", "slug": "du", "rom_count": 1},
                ]
            if fn is plugin._romm_api.list_virtual_collections:
                return [
                    {"id": "100", "name": "Enabled Franchise", "slug": "ef", "rom_count": 1, "is_virtual": True},
                    {"id": "200", "name": "Disabled Franchise", "slug": "df", "rom_count": 1, "is_virtual": True},
                ]
            raise AssertionError(f"unexpected call: {fn}")

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_run_in_executor)
        plugin._sync_service._loop = mock_loop

        units = await plugin._sync_service._fetcher.build_work_queue()

        # Only enabled collections survive the cid-not-in-enabled_ids skip.
        assert [u.name for u in units] == ["Enabled User", "Enabled Franchise"]


class TestTryUnitIncrementalSkip:
    """Tests for _try_unit_incremental_skip() exception fallback."""

    @pytest.mark.asyncio
    async def test_falls_back_on_delta_api_exception(self, plugin):
        """Lines 447-451: delta-fetch raises => warning logged, returns None to force full fetch."""
        from domain.work_unit import WorkUnit

        plugin._state["shortcut_registry"] = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        plugin._state["last_sync"] = "2025-01-01T00:00:00"

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=RuntimeError("delta boom"))
        plugin._sync_service._loop = mock_loop

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        # Falls back to "force full fetch" sentinel.
        assert result is None


class TestFetchPlatformUnit:
    """Tests for fetch_platform_unit() — wrong-type guard, error break, multi-page pagination."""

    @pytest.mark.asyncio
    async def test_raises_on_non_platform_unit(self, plugin):
        """Line 478: fetch_platform_unit must reject collection units."""
        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="collection", id="1", name="Coll", slug="", rom_count=0)
        with pytest.raises(ValueError, match="non-platform unit"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_first_page_exception_propagates(self, plugin):
        """A page-fetch failure must raise so the orchestrator aborts before stale-cleanup.

        Previous behaviour swallowed the exception and returned ``([], False)``
        — which classified every existing ROM as stale and wiped the Steam
        shortcut library. See #630.
        """
        from domain.work_unit import WorkUnit

        # No prior sync => incremental skip returns None and we fall through to pagination.
        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=RuntimeError("page boom"))
        plugin._sync_service._loop = mock_loop

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=10)
        with pytest.raises(RuntimeError, match="page boom"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_second_page_exception_propagates(self, plugin):
        """Page 1 OK + page 2 raises must propagate so partial accumulation never
        reaches the stale-cleanup pass. See #630."""
        from domain.work_unit import WorkUnit

        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        page1 = {"items": [{"id": i, "name": f"G{i}"} for i in range(50)]}
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=[page1, RuntimeError("page 2 boom")])
        plugin._sync_service._loop = mock_loop

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=200)
        with pytest.raises(RuntimeError, match="page 2 boom"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_paginates_across_multiple_pages(self, plugin):
        """Line 514: a full first page must trigger offset += limit and a second fetch."""
        from domain.work_unit import WorkUnit

        plugin._state["last_sync"] = None
        plugin._state["shortcut_registry"] = {}

        page1 = {"items": [{"id": i, "name": f"G{i}"} for i in range(50)]}
        page2 = {"items": [{"id": 100, "name": "G100"}]}
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=[page1, page2])
        plugin._sync_service._loop = mock_loop

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
        from domain.work_unit import WorkUnit

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=0)
        with pytest.raises(ValueError, match="non-collection unit"):
            await plugin._sync_service._fetcher.fetch_collection_unit(unit, set())

    @pytest.mark.asyncio
    async def test_paginates_across_multiple_pages(self, plugin):
        """Line 566: a full first page must trigger offset += limit and a second fetch."""
        from domain.work_unit import WorkUnit

        page1 = {
            "items": [{"id": i, "name": f"G{i}", "platform_name": "N64", "platform_slug": "n64"} for i in range(50)],
        }
        page2 = {
            "items": [{"id": 999, "name": "G999", "platform_name": "N64", "platform_slug": "n64"}],
        }
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=[page1, page2])
        plugin._sync_service._loop = mock_loop

        unit = WorkUnit(type="collection", id=7, name="Coll", slug="", rom_count=51, is_virtual=False)
        synced: set[int] = set()
        new_roms, all_collection_rom_ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert len(new_roms) == 51
        assert len(all_collection_rom_ids) == 51
        assert 999 in synced
