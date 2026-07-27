"""Tests for LibraryFetcher — platform/collection roundtrips, ROM fetch pipeline.

Driven end-to-end through :class:`FakeRommApi` so each test seeds
in-memory platforms/ROMs/collections on the fake and asserts on the
observable output of the fetcher (returned ROM lists, mutated state).
Failure paths are exercised with ``fail_on_next`` (one-shot) and the
per-method ``*_side_effect`` attributes (persistent) — no
``run_in_executor`` patching, no ``MagicMock(romm_api)``.
"""

from dataclasses import replace

import pytest

from domain.sync_state import SyncCancelled, SyncState
from domain.work_unit import WorkUnit
from lib.romm_paging import LIST_PAGE_SIZE


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
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
        # The cooperative cancel signal is the dedicated ``SyncCancelled``
        # BaseException — NOT ``asyncio.CancelledError`` — so a cooperative
        # sync cancel is never conflated with a real asyncio task cancel.
        with pytest.raises(SyncCancelled):
            plugin._sync_service._fetcher._check_cancelling()

    def test_noop_when_running(self, plugin):
        plugin._sync_service._box.sync_state = SyncState.RUNNING
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
    async def test_standard_collection_list_failure_continues_with_empty(self, plugin, fake_romm_api):
        """User-collection fetch raises => warning logged, treated as empty."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "standard": {"1": True},
            "smart": {},
            "virtual": {"42": True},
        }

        fake_romm_api.list_collections_side_effect = RuntimeError("standard collections boom")
        fake_romm_api.virtual_collections = {
            "franchise": [
                {"id": "42", "name": "Faves", "slug": "faves", "rom_count": 3},
            ],
        }

        units = await plugin._sync_service._fetcher.build_work_queue()

        # User-collections branch swallowed the failure; virtual collection still listed.
        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_virtual_collection_list_failure_continues_with_empty(self, plugin, fake_romm_api):
        """Virtual-collection fetch raises for every type => warning logged, treated as empty."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "standard": {"7": True},
            "smart": {},
            "virtual": {"100": True},
        }

        fake_romm_api.collections = [{"id": "7", "name": "Faves", "slug": "faves", "rom_count": 4}]
        fake_romm_api.list_virtual_collections_side_effect = RuntimeError("virtual collections boom")

        units = await plugin._sync_service._fetcher.build_work_queue()

        # User collection survives; both virtual-type branches swallowed the failure.
        assert [u.name for u in units] == ["Faves"]

    @pytest.mark.asyncio
    async def test_one_virtual_type_failure_still_lists_the_other(self, plugin, fake_romm_api):
        """AC1 fail-open: a single failing virtual type never drops the other type's collections."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "standard": {},
            "smart": {},
            "virtual": {"fr-1": True, "vc-1": True},
        }
        # The franchise type is down; the collection type answers normally.
        fake_romm_api.virtual_collections = {
            "collection": [{"id": "vc-1", "name": "Series One", "slug": "s1", "rom_count": 2}],
        }
        fake_romm_api.list_virtual_collections_side_effect_by_type = {
            "franchise": RuntimeError("franchise endpoint down"),
        }

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.name for u in units] == ["Series One"]
        assert [u.collection_kind for u in units] == ["virtual"]

    @pytest.mark.asyncio
    async def test_virtual_type_threads_onto_work_units(self, plugin, fake_romm_api):
        """Each virtual unit carries its query virtual_type; standard units carry None (#1539)."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "standard": {"7": True},
            "smart": {},
            "virtual": {"fr-1": True, "vc-1": True},
        }
        fake_romm_api.collections = [{"id": "7", "name": "Faves", "slug": "faves", "rom_count": 4}]
        fake_romm_api.virtual_collections = {
            "franchise": [{"id": "fr-1", "name": "coll-fr", "slug": "coll-fr", "rom_count": 3}],
            "collection": [{"id": "vc-1", "name": "coll-vc", "slug": "coll-vc", "rom_count": 2}],
        }

        units = await plugin._sync_service._fetcher.build_work_queue()

        by_name = {u.name: u for u in units}
        assert by_name["coll-fr"].virtual_type == "franchise"
        assert by_name["coll-vc"].virtual_type == "collection"
        # A standard collection has no virtual sub-type.
        assert by_name["Faves"].virtual_type is None

    @pytest.mark.asyncio
    async def test_smart_collection_list_failure_continues_with_empty(self, plugin, fake_romm_api):
        """Smart-collection fetch raises => warning logged, treated as empty."""
        _wire_fake(plugin, fake_romm_api)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "standard": {"7": True},
            "smart": {"5": True},
            "virtual": {},
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
        # Only the "1" user / "5" smart / "100" virtual collections are enabled.
        plugin.settings["enabled_collections"] = {
            "standard": {"1": True, "2": False},
            "smart": {"5": True, "6": False},
            "virtual": {"100": True, "200": False},
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
        assert kinds == ["standard", "smart", "virtual"]


class TestGetCollectionsVirtualFailOpen:
    """get_collections is per-virtual-type fail-open (#1538)."""

    @pytest.mark.asyncio
    async def test_one_virtual_type_failure_still_lists_the_other(self, plugin, fake_romm_api):
        """AC1 fail-open at the callable: one failing virtual type never fails
        get_collections nor drops the healthy type's collection."""
        _wire_fake(plugin, fake_romm_api)
        # The franchise type is down; the collection type answers normally.
        fake_romm_api.virtual_collections = {
            "collection": [{"id": "vc-1", "name": "Series One", "slug": "s1", "rom_count": 2}],
        }
        fake_romm_api.list_virtual_collections_side_effect_by_type = {
            "franchise": RuntimeError("franchise endpoint down"),
        }

        result = await plugin._sync_service._fetcher.get_collections()

        assert result["success"] is True
        # The healthy collection-type virtual collection is present and tagged...
        by_id = {c["id"]: c for c in result["collections"]}
        assert by_id["vc-1"]["kind"] == "virtual"
        assert by_id["vc-1"]["virtual_type"] == "collection"
        assert by_id["vc-1"]["is_own"] is True
        # ...and the failed franchise type contributes nothing (no franchise-typed row).
        assert not [c for c in result["collections"] if c.get("virtual_type") == "franchise"]


class TestSaveCollectionsSync:
    """save_collections_sync — batch-stamp a filtered subset into one kind bucket (#1539)."""

    def test_stamps_multiple_ids_in_one_write(self, plugin):
        """Every id in the list lands enabled in the kind bucket, one persist."""
        plugin.settings["enabled_collections"] = {"standard": {}, "smart": {}, "virtual": {}}
        recorder = plugin._settings_persister

        result = plugin._sync_service._fetcher.save_collections_sync(["10", "20", "30"], "standard", True)

        assert result == {"success": True}
        bucket = plugin.settings["enabled_collections"]["standard"]
        assert bucket == {"10": True, "20": True, "30": True}
        # A single settings write for the whole batch.
        assert recorder.save_count == 1

    def test_disable_stamps_false_without_touching_other_buckets(self, plugin):
        """Disabling a subset writes False for those ids and leaves siblings intact."""
        plugin.settings["enabled_collections"] = {
            "standard": {},
            "smart": {"5": True, "6": True},
            "virtual": {},
        }

        result = plugin._sync_service._fetcher.save_collections_sync(["5"], "smart", False)

        assert result == {"success": True}
        assert plugin.settings["enabled_collections"]["smart"] == {"5": False, "6": True}

    def test_coerces_non_string_ids_to_string_keys(self, plugin):
        """Integer ids are coerced to string keys (parity with save_collection_sync)."""
        plugin.settings["enabled_collections"] = {"standard": {}, "smart": {}, "virtual": {}}

        plugin._sync_service._fetcher.save_collections_sync([7, 8], "standard", True)

        bucket = plugin.settings["enabled_collections"]["standard"]
        assert bucket == {"7": True, "8": True}

    def test_rejects_invalid_kind_with_failure_shape(self, plugin):
        """An unknown kind is rejected with the canonical failure shape, no write."""
        recorder = plugin._settings_persister

        result = plugin._sync_service._fetcher.save_collections_sync(["1"], "bogus", True)

        assert result["success"] is False
        assert result["reason"] == "invalid_kind"
        assert "Invalid collection kind" in result["message"]
        assert "error" not in result
        assert "error_code" not in result
        assert recorder.save_count == 0

    def test_rejects_non_list_ids_with_failure_shape(self, plugin):
        """A non-list ids argument from the wire is rejected, no write."""
        recorder = plugin._settings_persister

        result = plugin._sync_service._fetcher.save_collections_sync("not-a-list", "standard", True)

        assert result["success"] is False
        assert result["reason"] == "invalid_ids"
        assert isinstance(result["message"], str) and result["message"]
        assert recorder.save_count == 0

    def test_empty_ids_is_a_success_no_op(self, plugin):
        """An empty id list stamps nothing and does not write settings."""
        plugin.settings["enabled_collections"] = {"standard": {"1": True}, "smart": {}, "virtual": {}}
        recorder = plugin._settings_persister

        result = plugin._sync_service._fetcher.save_collections_sync([], "standard", True)

        assert result == {"success": True}
        # Unchanged bucket, no persist.
        assert plugin.settings["enabled_collections"]["standard"] == {"1": True}
        assert recorder.save_count == 0

    def test_materializes_missing_buckets_before_stamping(self, plugin):
        """A settings dict seeded without enabled_collections gets all buckets."""
        plugin.settings.pop("enabled_collections", None)

        plugin._sync_service._fetcher.save_collections_sync(["100"], "virtual", True)

        ec = plugin.settings["enabled_collections"]
        assert ec["virtual"]["100"] is True
        assert ec["standard"] == {}
        assert ec["smart"] == {}


class TestBuildWorkQueueOwnerScope:
    """build_work_queue applies the collection owner-scope filter (#1532)."""

    @staticmethod
    def _seed(fake_romm_api):
        """Two user, two smart (one own / one foreign each), one virtual — all enabled."""
        fake_romm_api.collections = [
            {"id": "1", "name": "Mine", "slug": "mine", "rom_count": 1, "user_id": 7},
            {"id": "2", "name": "Theirs", "slug": "theirs", "rom_count": 1, "user_id": 8},
        ]
        fake_romm_api.smart_collections = [
            {"id": "5", "name": "MySmart", "slug": "ms", "rom_count": 1, "user_id": 7},
            {"id": "6", "name": "TheirSmart", "slug": "ts", "rom_count": 1, "user_id": 8},
        ]
        fake_romm_api.virtual_collections = {
            "franchise": [{"id": "100", "name": "Mario", "slug": "mario", "rom_count": 1}],
        }

    @staticmethod
    def _enable_all(plugin):
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {
            "standard": {"1": True, "2": True},
            "smart": {"5": True, "6": True},
            "virtual": {"100": True},
        }

    @pytest.mark.asyncio
    async def test_own_scope_drops_foreign_keeps_own_and_virtual(self, plugin, fake_romm_api):
        """AC2: a foreign user + foreign smart collection are excluded; own + ALL virtual survive."""
        _wire_fake(plugin, fake_romm_api)
        self._enable_all(plugin)
        self._seed(fake_romm_api)
        plugin.settings["romm_user_id"] = 7
        plugin.settings["collection_owner_scope"] = "own"

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.name for u in units] == ["Mine", "MySmart", "Mario"]

    @pytest.mark.asyncio
    async def test_all_scope_keeps_every_collection(self, plugin, fake_romm_api):
        """AC3: the default "all" scope syncs every collection — today's behaviour, byte-for-byte."""
        _wire_fake(plugin, fake_romm_api)
        self._enable_all(plugin)
        self._seed(fake_romm_api)
        plugin.settings["romm_user_id"] = 7
        plugin.settings["collection_owner_scope"] = "all"

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.name for u in units] == ["Mine", "Theirs", "MySmart", "TheirSmart", "Mario"]

    @pytest.mark.asyncio
    async def test_own_scope_unknown_identity_keeps_every_collection(self, plugin, fake_romm_api):
        """AC4 (load-bearing): "own" with no known identity must NOT filter — degrade to "All"."""
        _wire_fake(plugin, fake_romm_api)
        self._enable_all(plugin)
        self._seed(fake_romm_api)
        plugin.settings.pop("romm_user_id", None)
        plugin.settings["collection_owner_scope"] = "own"

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.name for u in units] == ["Mine", "Theirs", "MySmart", "TheirSmart", "Mario"]

    @pytest.mark.asyncio
    async def test_own_scope_is_default_all_when_setting_absent(self, plugin, fake_romm_api):
        """No collection_owner_scope setting → treated as "all" (no filtering)."""
        _wire_fake(plugin, fake_romm_api)
        self._enable_all(plugin)
        self._seed(fake_romm_api)
        plugin.settings["romm_user_id"] = 7
        plugin.settings.pop("collection_owner_scope", None)

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.name for u in units] == ["Mine", "Theirs", "MySmart", "TheirSmart", "Mario"]


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


def _seed_completed_run(uow):
    """A completed SyncRun so the skip gate has a ``last_sync`` to diff against."""
    from domain.sync_run import SyncRun

    run = SyncRun.start(id="r1", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=3)
    run.complete("2025-01-01T00:00:00", ["N64"], [])
    with uow:
        uow.sync_runs.save(run)


def _seed_persisted_rom(uow, rom_id, *, app_id, group_key, platform_slug="n64", fetch_id=None):
    """Persist one ``roms`` row (bound when app_id is set, else an unbound sibling).

    ``fetch_id`` is the fetch generation that last saw the row (#1504); ``None``
    leaves it unknown, the state a pre-020 row reads.
    """
    from domain.rom import Rom

    with uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=f"G{rom_id}",
                fs_name=f"g{rom_id}.z64",
                shortcut_app_id=app_id,
                last_synced_at="2025-01-01T00:00:00",
                sibling_group_key=group_key,
                last_fetch_id=fetch_id,
            )
        )


class TestIncrementalSkipGroupParity:
    """Skip-gate parity with sibling groups (#1296 / ADR-0021): the platform
    rom_count is compared against ALL persisted rows, not just the bound reps."""

    @pytest.mark.asyncio
    async def test_skips_when_all_persisted_rows_match_server_count(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        # A 3-sibling group: one bound representative + two unbound siblings.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        # No server rows updated after the stamp → delta total 0. RomM's rom_count
        # (all siblings) matches the 3 persisted rows → skip. Under the old
        # bound-only count this platform full-fetched forever.
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None
        # The reconstructed unit_roms are the bound representatives (the shortcuts).
        assert {r["id"] for r in result} == {10}

    @pytest.mark.asyncio
    async def test_no_skip_when_server_count_differs_from_persisted(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_completed_run(uow)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        # Server reports 3 ROMs but only 2 are persisted → a new sibling appeared.
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_null_group_key_forces_full_fetch_backfill(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_completed_run(uow)
        # A legacy bound row whose sibling_group_key was never captured — even
        # though the count matches, the backfill forces a full fetch.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key=None)
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None


class TestIncrementalSkipZeroBoundRows:
    """Skip-gate guard for a mass-deleted platform (on-device #1025).

    Deleting every RomM shortcut leaves the ``roms`` rows behind as
    unbind-only (their ``shortcut_app_id`` cleared, ADR-0007). A completed
    run, an unchanged server, and matching counts otherwise satisfy the
    skip — but the skip reconstructs the unit's ROMs from the *bound* rows,
    so with zero bindings the reconstructed list is empty and the diff sees
    nothing to re-add. The guard must fall through to a full fetch instead.
    """

    @pytest.mark.asyncio
    async def test_no_skip_when_zero_bound_rows(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_completed_run(uow)
        # Rows persist but every shortcut_app_id was cleared by the mass delete;
        # each carries a group key so the backfill gate would NOT fire — the
        # only reason to full-fetch is the zero-bindings guard under test.
        _seed_persisted_rom(uow, 10, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_platform_unit_full_fetches_when_zero_bound_rows(self, plugin, fake_romm_api):
        """End-to-end: the mass-deleted platform re-paginates its ROMs (skipped=False)."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_completed_run(uow)
        _seed_persisted_rom(uow, 10, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        # The full-fetch path paginates the live server list back into the re-add path.
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(3)}
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        unit_roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)

        assert skipped is False
        assert len(unit_roms) == 3

    @pytest.mark.asyncio
    async def test_skips_when_a_bound_row_survives(self, plugin, fake_romm_api):
        """Guard is scoped to zero bindings: one surviving shortcut still skips."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        # One bound representative survives + two unbound siblings → registry_count > 0.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None
        assert {r["id"] for r in result} == {10}


def _seed_platform_stamp(uow, slug, *, at, rom_count, fetch_id=None):
    """Persist a per-platform completion stamp (ADR-0023) so the skip can honor it.

    ``fetch_id`` is the generation the stamp's fetch marked its rows with (#1504);
    ``None`` is the pre-020 stamp the skip falls back to counting every row for.
    """
    from domain.platform_sync_state import PlatformSyncState

    with uow:
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug=slug, at=at, rom_count=rom_count, fetch_id=fetch_id)
        )


class TestIncrementalSkipSupersededRows:
    """Superseded rows must stop defeating the skip forever (#1504).

    RomM re-creating a ROM under a new id leaves the old row behind unbound, and
    ADR-0007 keeps it as an identity anchor. Counting it inflated the local total
    past the server's ``rom_count``, so such a platform full-fetched on every
    single sync. The stamp's fetch generation is what excludes it — nothing is
    deleted.
    """

    @pytest.mark.asyncio
    async def test_platform_with_superseded_rows_skips_again(self, plugin, fake_romm_api):
        """The headline case: 2 server ROMs, 4 local rows (2 superseded) → skip."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=2, fetch_id="run-new")
        # The two rows the last complete fetch returned.
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key="igdb:100:1", fetch_id="run-new")
        _seed_persisted_rom(uow, 25136, app_id=1002, group_key="igdb:200:1", fetch_id="run-new")
        # Their superseded predecessors: unbound, ids the server has since dropped.
        _seed_persisted_rom(uow, 4375, app_id=None, group_key="igdb:100:1", fetch_id="run-old")
        _seed_persisted_rom(uow, 4376, app_id=None, group_key="igdb:200:1", fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None
        assert {r["id"] for r in result} == {25135, 25136}

    @pytest.mark.asyncio
    async def test_superseded_rows_are_not_deleted_by_the_skip(self, plugin, fake_romm_api):
        """The skip excludes the superseded rows from the count but leaves them on
        disk — ADR-0007's identity anchors survive (no destructive op here)."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1, fetch_id="run-new")
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key="igdb:100:1", fetch_id="run-new")
        _seed_persisted_rom(uow, 4375, app_id=None, group_key="igdb:100:1", fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        assert await plugin._sync_service._fetcher._try_unit_incremental_skip(unit) is not None

        with uow:
            superseded = uow.roms.get(4375)
        assert superseded is not None
        assert superseded.last_fetch_id == "run-old"

    @pytest.mark.asyncio
    async def test_skip_repeats_because_the_reference_point_never_drifts(self, plugin, fake_romm_api):
        """Two consecutive skips, not just the first.

        A skipped unit returns before any apply chunk, so neither the stamp's
        generation nor the rows' generation is rewritten. The skip therefore
        compares against the SAME reference point every run — the failure mode
        that would wedge the skip off after exactly one success.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=2, fetch_id="run-new")
        # A bound representative plus an UNBOUND sibling — both rode the last
        # fetch, so both count. An unbound sibling is absent from the reconstructed
        # list, which is exactly what a re-stamp on skip would lose.
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key="igdb:100:1", fetch_id="run-new")
        _seed_persisted_rom(uow, 25136, app_id=None, group_key="igdb:100:1", fetch_id="run-new")
        _seed_persisted_rom(uow, 4375, app_id=None, group_key="igdb:100:1", fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)

        first = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)
        second = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert first is not None
        assert second is not None
        assert {r["id"] for r in first} == {r["id"] for r in second} == {25135}
        with uow:
            stamp = uow.platform_sync_state.get("n64")
            sibling = uow.roms.get(25136)
        assert stamp is not None
        assert stamp.fetch_id == "run-new"
        assert sibling is not None
        assert sibling.last_fetch_id == "run-new"

    @pytest.mark.asyncio
    async def test_genuine_divergence_still_full_fetches(self, plugin, fake_romm_api):
        """A real local/server gap must NOT be masked by the generation filter: the
        server has 3 ROMs but only 2 rode the last fetch → full fetch."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3, fetch_id="run-new")
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key="igdb:100:1", fetch_id="run-new")
        _seed_persisted_rom(uow, 25136, app_id=1002, group_key="igdb:200:1", fetch_id="run-new")
        # A superseded row must never stand in for the third server ROM.
        _seed_persisted_rom(uow, 4375, app_id=None, group_key="igdb:100:1", fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_row_carries_the_stamps_generation_forces_full_fetch(self, plugin, fake_romm_api):
        """Every row predates the stamp's generation → nothing countable → full fetch."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1, fetch_id="run-new")
        _seed_persisted_rom(uow, 4375, app_id=1001, group_key="igdb:100:1", fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_dropped_null_key_row_no_longer_forces_a_backfill_fetch(self, plugin, fake_romm_api):
        """A dropped row's NULL group key can never be filled in, so it must not
        wedge the platform into a full fetch on every sync forever."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1, fetch_id="run-new")
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key="igdb:100:1", fetch_id="run-new")
        # Superseded by 25135 and dropped by the server before the key was ever
        # captured — no fetch will return it again to backfill it.
        _seed_persisted_rom(uow, 4375, app_id=None, group_key=None, fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None
        assert {r["id"] for r in result} == {25135}

    @pytest.mark.asyncio
    async def test_current_generation_null_key_row_still_forces_a_backfill_fetch(self, plugin, fake_romm_api):
        """The gate is narrowed, not disabled: a row the last fetch returned with
        no group key is backfillable and still costs a full fetch."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1, fetch_id="run-new")
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key=None, fetch_id="run-new")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_pre_migration_stamp_backfills_on_any_null_key_row(self, plugin, fake_romm_api):
        """A stamp with no generation cannot say which rows its fetch saw, so it
        keeps the pre-#1504 behaviour and lets any NULL-key row force the fetch."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=2, fetch_id=None)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1", fetch_id=None)
        _seed_persisted_rom(uow, 11, app_id=None, group_key=None, fetch_id="run-old")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_pre_migration_stamp_counts_every_row(self, plugin, fake_romm_api):
        """A stamp written before the generation contract cannot say what its fetch
        saw, so the pre-#1504 count stands and a clean platform keeps skipping
        straight through the upgrade instead of paying a forced re-fetch."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=2, fetch_id=None)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1", fetch_id=None)
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1", fetch_id=None)
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None
        assert {r["id"] for r in result} == {10}


class TestIncrementalSkipFromPlatformStamp:
    """Per-platform completion stamp drives the skip even without a completed run (ADR-0023 / #1025).

    A run that durably synced a platform but was cancelled/crashed before the
    whole run finished leaves NO completed ``SyncRun`` (so the library-wide
    ``last_sync`` never advances) but DOES leave a per-platform stamp. The skip
    reads that stamp's ``completed_at`` as the platform's effective ``last_sync``.
    """

    @pytest.mark.asyncio
    async def test_skip_fires_from_stamp_without_completed_run(self, plugin, fake_romm_api):
        """The crash-resume scenario: stamp present, NO completed run → still skips."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        # NO completed run is seeded — only the per-platform stamp.
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None
        assert {r["id"] for r in result} == {10}

    @pytest.mark.asyncio
    async def test_stamp_completed_at_is_the_delta_reference(self, plugin, fake_romm_api):
        """The stamp's ``completed_at`` (not the older completed-run last_sync) is the
        delta reference: a ROM updated between the two timestamps decides the skip.

        The completed run is OLD; the stamp is NEWER. A platform ROM updated in the
        window is > last_sync but <= stamp — so skip fires only if the stamp is the
        reference. Also asserts the exact ``updated_after`` argument the fetcher sent.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_completed_run(uow)  # completes at 2025-01-01T00:00:00 (old)
        _seed_platform_stamp(uow, "n64", at="2025-06-01T00:00:00", rom_count=1)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        # A platform ROM updated between last_sync and the stamp: skip-blocking under
        # the old last_sync reference, skip-safe under the newer stamp reference.
        fake_romm_api.roms[10] = {"id": 10, "platform_id": 1, "updated_at": "2025-03-01T00:00:00"}
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is not None  # stamp reference → the window ROM is not "after"
        delta_calls = [c for c in fake_romm_api.call_log if c[0] == "list_roms_updated_after"]
        assert delta_calls, "delta check must have run"
        assert delta_calls[-1][1][1] == "2025-06-01T00:00:00"  # updated_after == stamp.completed_at

    @pytest.mark.asyncio
    async def test_no_skip_when_stamp_rom_count_mismatches_server(self, plugin, fake_romm_api):
        """A server-side platform count change since the stamp invalidates it — full fetch.

        Isolates the stamp-count guard from the persisted-count guard: the persisted
        rows still MATCH the server count (4 == 4) and the delta is empty, so WITHOUT
        the stamp guard the platform would skip. The stamped count (3) no longer
        equals the server count (4), so the guard forces a full fetch.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 13, app_id=None, group_key="igdb:100:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=4)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_skip_when_stamp_and_zero_bound_rows(self, plugin, fake_romm_api):
        """Guard precedence: even with a valid stamp, zero bound rows force a full fetch.

        The zero-bound-rows guard (a mass-deleted platform, ADR-0007) is checked
        before the stamp reference is trusted, so a stamp can never resurrect a
        skip that has no shortcuts to reconstruct.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_stamp_never_skips_even_with_completed_run(self, plugin, fake_romm_api):
        """The stamp is the SOLE skip authority: no stamp → full fetch, run history irrelevant.

        A completed run alone cannot vouch for a platform's completeness — its
        shortcuts may have been locally removed and only partially re-applied
        since (the #1025 silent-gap scenario). With a completed run seeded but
        no stamp, the skip must NOT fire and no delta probe is sent.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_completed_run(uow)  # finished at 2025-01-01T00:00:00, no stamp
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None
        delta_calls = [c for c in fake_romm_api.call_log if c[0] == "list_roms_updated_after"]
        assert not delta_calls  # no stamp → no probe; the guard rejects before any server call


class TestStaleStampPartialGapRegression:
    """#1025 silent-gap: a stale stamp must never skip a platform left partially bound.

    The whole fix — the apply-start stamp clear (``sync_orchestrator``) and the
    local-removal stamp invalidation (``shortcut_removal``) — exists so this exact
    state can't arise with a surviving stamp. This pins the fetcher end of the
    contract: given the partial state, the presence or absence of the stamp is the
    SOLE decider of the wrongful skip, so clearing the stamp is what closes the gap.
    """

    @pytest.mark.asyncio
    async def test_stale_stamp_skips_partial_platform_but_cleared_stamp_refetches(self, plugin, fake_romm_api):
        """The interrupted-re-apply aftermath: 5 persisted rows, only 2 rebound before a
        crash, 3 still unbound. The server is unchanged (delta 0) and its rom_count (5)
        still equals the 5 persisted rows — every count the skip matches on lines up,
        and there is NO completed run, so the stamp alone can drive a skip.

        With the stale stamp present the skip FIRES (the bug: the 3 unbound games are
        never recreated). Once the stamp is cleared — exactly what the apply-start clear
        / local-removal invalidation does — the platform full-fetches and recreates them.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        # Two rebound representatives (chunk 0 committed before the crash) ...
        _seed_persisted_rom(uow, 10, app_id=1010, group_key="igdb:10:1")
        _seed_persisted_rom(uow, 11, app_id=1011, group_key="igdb:11:1")
        # ... and three siblings the crashed run never rebound (still unbound).
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:12:1")
        _seed_persisted_rom(uow, 13, app_id=None, group_key="igdb:13:1")
        _seed_persisted_rom(uow, 14, app_id=None, group_key="igdb:14:1")
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)

        # A surviving stale stamp (rom_count still matches the server) → wrongful skip.
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=5)
        skipped = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)
        assert skipped is not None, "baseline: a stale stamp over a partially-bound platform skips (the bug)"
        assert {r["id"] for r in skipped} == {10, 11}  # only the 2 rebound rows reconstruct — 3 games lost

        # The fix clears that stamp; with no stamp and no completed run there is no
        # time reference at all, so the skip cannot fire and the platform re-fetches.
        with uow:
            uow.platform_sync_state.delete("n64")
        assert await plugin._sync_service._fetcher._try_unit_incremental_skip(unit) is None


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

        # Seed exactly one full page worth of ROMs (500 items at limit=500) so a
        # full page 1 advances the offset and a second request is attempted.
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(LIST_PAGE_SIZE)}

        original_list_roms = fake_romm_api.list_roms
        call_count = {"n": 0}

        def list_roms_with_second_page_failure(platform_id, limit=LIST_PAGE_SIZE, offset=0):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("page 2 boom")
            return original_list_roms(platform_id, limit, offset)

        fake_romm_api.list_roms = list_roms_with_second_page_failure  # type: ignore[method-assign]

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=LIST_PAGE_SIZE + 100)
        with pytest.raises(RuntimeError, match="page 2 boom"):
            await plugin._sync_service._fetcher.fetch_platform_unit(unit)

    @pytest.mark.asyncio
    async def test_paginates_across_multiple_pages(self, plugin, fake_romm_api):
        """A full first page must trigger offset += limit and a second fetch."""
        _wire_fake(plugin, fake_romm_api)

        # One full page + a one-ROM tail at the 500-ROM page size => page 1 fills
        # to the limit, page 2 carries the tail (exercises offset += limit).
        rom_count = LIST_PAGE_SIZE + 1
        fake_romm_api.roms = {i: {"id": i, "platform_id": 1, "name": f"G{i}"} for i in range(rom_count)}

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=rom_count)
        unit_roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)

        assert skipped is False
        assert len(unit_roms) == rom_count
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
        """A full first page must trigger offset += limit and a second fetch."""
        _wire_fake(plugin, fake_romm_api)

        # One full page + a one-ROM tail at the 500-ROM page size => page 1 fills,
        # page 2 carries the tail.
        fake_romm_api.roms = {
            i: {
                "id": i,
                "platform_id": 1,
                "name": f"G{i}",
                "platform_name": "N64",
                "platform_slug": "n64",
                "collection_ids": [7],
            }
            for i in range(LIST_PAGE_SIZE)
        }
        fake_romm_api.roms[999] = {
            "id": 999,
            "platform_id": 1,
            "name": "G999",
            "platform_name": "N64",
            "platform_slug": "n64",
            "collection_ids": [7],
        }

        rom_count = LIST_PAGE_SIZE + 1
        unit = WorkUnit(type="collection", id=7, name="Coll", slug="", rom_count=rom_count, collection_kind="standard")
        synced: set[int] = set()
        new_roms, all_collection_rom_ids, skipped = await plugin._sync_service._fetcher.fetch_collection_unit(
            unit, synced
        )

        assert skipped is False
        assert len(new_roms) == rom_count
        assert len(all_collection_rom_ids) == rom_count
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
        new_roms, ids, _skipped = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert [r["id"] for r in new_roms] == [1, 2]
        assert ids == [1, 2]
        # Verify the smart endpoint was the one consulted, not the user/virtual ones.
        method_calls = [c[0] for c in fake_romm_api.call_log]
        assert "list_roms_by_smart_collection" in method_calls
        assert "list_roms_by_collection" not in method_calls
        assert "list_roms_by_virtual_collection" not in method_calls

    @pytest.mark.asyncio
    async def test_dispatches_virtual_collection_to_virtual_endpoint(self, plugin, fake_romm_api):
        """collection_kind='virtual' routes through list_roms_by_virtual_collection."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.roms = {
            1: {"id": 1, "platform_name": "N64", "virtual_collection_ids": ["100"]},
        }

        unit = WorkUnit(type="collection", id="100", name="Mario", slug="", rom_count=1, collection_kind="virtual")
        synced: set[int] = set()
        new_roms, _ids, _skipped = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert [r["id"] for r in new_roms] == [1]
        method_calls = [c[0] for c in fake_romm_api.call_log]
        assert "list_roms_by_virtual_collection" in method_calls
        assert "list_roms_by_smart_collection" not in method_calls


def _seed_collection_stamp(uow, cid, kind, *, updated_at, completed_at, rom_count, member_rom_ids):
    """Persist a per-collection completion stamp (#742) so the skip can honor it."""
    from domain.collection_sync_state import CollectionSyncState

    with uow:
        uow.collection_sync_state.save(
            CollectionSyncState.stamp(
                collection_id=cid,
                collection_kind=kind,
                updated_at=updated_at,
                completed_at=completed_at,
                rom_count=rom_count,
                member_rom_ids=member_rom_ids,
            )
        )


def _user_collection_unit(cid="7", *, rom_count=2, updated_at: str | None = "2026-01-01T00:00:00"):
    return WorkUnit(
        type="collection",
        id=cid,
        name="Faves",
        slug="",
        rom_count=rom_count,
        collection_kind="standard",
        collection_updated_at=updated_at,
    )


class TestTryCollectionIncrementalSkip:
    """Decision tests for _try_collection_incremental_skip() — the #742 collection skip gate."""

    @pytest.mark.asyncio
    async def test_skips_when_unchanged(self, plugin, fake_romm_api):
        """Stamp present, updated_at equal, scoped updated_after 0, counts equal → skip."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        # Members exist but none updated after the stamp's completed_at → probe total 0.
        fake_romm_api.roms = {
            10: {"id": 10, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
            11: {"id": 11, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
        }
        unit = _user_collection_unit(rom_count=2)

        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(unit)

        assert result == [10, 11]
        # The scoped probe ran with the stamp's completed_at as the reference.
        probe = [c for c in fake_romm_api.call_log if c[0] == "list_collection_roms_updated_after"]
        assert probe, "the scoped updated_after probe must have run"
        assert probe[-1][1] == (7, "standard", "2026-06-01T00:00:00")

    @pytest.mark.asyncio
    async def test_no_skip_when_no_stamp(self, plugin, fake_romm_api):
        """First-ever sync: no stamp → full fetch, no probe."""
        _wire_fake(plugin, fake_romm_api)
        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(_user_collection_unit())
        assert result is None
        assert not [c for c in fake_romm_api.call_log if c[0] == "list_collection_roms_updated_after"]

    @pytest.mark.asyncio
    async def test_no_skip_when_updated_at_changed(self, plugin, fake_romm_api):
        """A membership add/remove bumps the collection's updated_at → full fetch, no probe."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        unit = _user_collection_unit(rom_count=2, updated_at="2026-02-02T00:00:00")  # changed

        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(unit)

        assert result is None
        assert not [c for c in fake_romm_api.call_log if c[0] == "list_collection_roms_updated_after"]

    @pytest.mark.asyncio
    async def test_no_skip_when_member_content_changed(self, plugin, fake_romm_api):
        """A member ROM updated after the stamp (scoped probe > 0) → full fetch."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        fake_romm_api.roms = {
            10: {"id": 10, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
            11: {"id": 11, "collection_ids": [7], "updated_at": "2026-07-01T00:00:00"},  # after completed_at
        }
        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(
            _user_collection_unit(rom_count=2)
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_skip_when_server_count_differs(self, plugin, fake_romm_api):
        """Stamped rom_count != live listing rom_count → full fetch, no probe."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        unit = _user_collection_unit(rom_count=3)  # server now reports 3

        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(unit)

        assert result is None
        assert not [c for c in fake_romm_api.call_log if c[0] == "list_collection_roms_updated_after"]

    @pytest.mark.asyncio
    async def test_no_skip_when_member_set_incomplete(self, plugin, fake_romm_api):
        """A stamp whose stored member set count != rom_count is not trusted → full fetch."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10,),  # only 1 stored, rom_count says 2
        )
        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(
            _user_collection_unit(rom_count=2)
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_skip_when_no_updated_at_on_unit(self, plugin, fake_romm_api):
        """A listing that carried no updated_at (None) can't be compared → full fetch."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        unit = _user_collection_unit(rom_count=2, updated_at=None)
        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(unit)
        assert result is None

    @pytest.mark.asyncio
    async def test_virtual_never_skips(self, plugin, fake_romm_api):
        """A virtual collection has no stamp and never probes."""
        _wire_fake(plugin, fake_romm_api)
        unit = WorkUnit(
            type="collection",
            id="100",
            name="Mario",
            slug="",
            rom_count=1,
            collection_kind="virtual",
            collection_updated_at="2026-01-01T00:00:00",
        )
        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(unit)
        assert result is None
        assert not [c for c in fake_romm_api.call_log if c[0] == "list_collection_roms_updated_after"]

    @pytest.mark.asyncio
    async def test_falls_back_on_probe_exception(self, plugin, fake_romm_api):
        """A scoped-probe server error falls open to a full fetch (warn + None)."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        fake_romm_api.list_collection_roms_updated_after_side_effect = RuntimeError("probe boom")
        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(
            _user_collection_unit(rom_count=2)
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_smart_collection_routes_smart_param(self, plugin, fake_romm_api):
        """A smart collection probes with kind='smart' and is keyed off smart_collection_ids."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "9",
            "smart",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=1,
            member_rom_ids=(20,),
        )
        fake_romm_api.roms = {20: {"id": 20, "smart_collection_ids": [9], "updated_at": "2026-01-01T00:00:00"}}
        unit = WorkUnit(
            type="collection",
            id="9",
            name="Smart",
            slug="",
            rom_count=1,
            collection_kind="smart",
            collection_updated_at="2026-01-01T00:00:00",
        )

        result = await plugin._sync_service._fetcher._try_collection_incremental_skip(unit)

        assert result == [20]
        probe = [c for c in fake_romm_api.call_log if c[0] == "list_collection_roms_updated_after"]
        assert probe[-1][1] == (9, "smart", "2026-06-01T00:00:00")


class TestFetchCollectionUnitSkip:
    """End-to-end fetch_collection_unit() over the #742 skip — reconstruction + accounting."""

    @pytest.mark.asyncio
    async def test_skip_reconstructs_uncovered_members_and_marks_synced(self, plugin, fake_romm_api):
        """A skipped collection reconstructs its not-yet-covered bound members and adds
        every member to synced_rom_ids, without paginating."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_collection_stamp(
            uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 11),
        )
        # Member 10 is on a platform NOT fetched this run (bound in the registry);
        # member 11 was already covered by a platform unit (in synced_rom_ids).
        _seed_persisted_rom(uow, 10, app_id=1010, group_key="igdb:10:1", platform_slug="gba")
        # No live members updated after the stamp → probe total 0.
        fake_romm_api.roms = {
            10: {"id": 10, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
            11: {"id": 11, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
        }
        synced = {11}
        unit = _user_collection_unit(rom_count=2)

        new_roms, all_ids, skipped = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert skipped is True
        assert all_ids == [10, 11]
        assert synced == {10, 11}  # every member marked covered for stale cleanup
        # Only the not-already-synced bound member is reconstructed as a new row.
        assert [r["id"] for r in new_roms] == [10]
        assert new_roms[0]["platform_slug"] == "gba"
        assert new_roms[0]["sibling_group_key"] == "igdb:10:1"
        # No pagination endpoint was consulted.
        assert not [c for c in fake_romm_api.call_log if c[0] == "list_roms_by_collection"]

    @pytest.mark.asyncio
    async def test_skip_omits_unbound_member_from_reconstruction(self, plugin, fake_romm_api):
        """An unbound / absent member is not reconstructed (it is a sibling handled at
        finalize, not a shortcut to rebuild), but is still marked synced."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_collection_stamp(
            uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 12),
        )
        _seed_persisted_rom(uow, 10, app_id=1010, group_key="igdb:10:1", platform_slug="gba")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:10:1", platform_slug="gba")  # unbound sibling
        fake_romm_api.roms = {
            10: {"id": 10, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
            12: {"id": 12, "collection_ids": [7], "updated_at": "2026-01-01T00:00:00"},
        }
        synced: set[int] = set()

        new_roms, all_ids, skipped = await plugin._sync_service._fetcher.fetch_collection_unit(
            unit=_user_collection_unit(rom_count=2), synced_rom_ids=synced
        )

        assert skipped is True
        assert [r["id"] for r in new_roms] == [10]  # 12 omitted (unbound)
        assert all_ids == [10, 12]
        assert synced == {10, 12}

    @pytest.mark.asyncio
    async def test_changed_collection_paginates_normally(self, plugin, fake_romm_api):
        """A changed collection (updated_at differs) falls through to a full fetch."""
        _wire_fake(plugin, fake_romm_api)
        _seed_collection_stamp(
            plugin._uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-06-01T00:00:00",
            rom_count=1,
            member_rom_ids=(10,),
        )
        fake_romm_api.roms = {
            10: {"id": 10, "collection_ids": [7], "platform_name": "GBA", "platform_slug": "gba"},
            11: {"id": 11, "collection_ids": [7], "platform_name": "GBA", "platform_slug": "gba"},
        }
        unit = _user_collection_unit(rom_count=2, updated_at="2026-05-05T00:00:00")  # changed
        synced: set[int] = set()

        new_roms, all_ids, skipped = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

        assert skipped is False
        assert [r["id"] for r in new_roms] == [10, 11]  # full fetch returns the live members
        assert all_ids == [10, 11]
        assert [c for c in fake_romm_api.call_log if c[0] == "list_roms_by_collection"]


def _fetching_frames(decky):
    """Ordered ``sync_progress`` payloads whose stage is ``fetching``."""
    return [
        c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_progress" and c[0][1].get("stage") == "fetching"
    ]


def _seed_pages(fake_romm_api, *, platform_id, count):
    """Seed *count* ROMs on one platform so a full fetch paginates over them."""
    fake_romm_api.roms = {i: {"id": i, "platform_id": platform_id, "name": f"G{i}"} for i in range(count)}


class TestFetchProgressNarration:
    """Per-page ``fetching`` progress frames during the paginated fetch (#1025).

    The QAM's coarse bar sat frozen on "Applying shortcuts" for the minutes a
    large platform's fetch takes; the fetch now narrates page progress under the
    ``fetching`` stage. At the 500-ROM page size a large platform is only a
    handful of pages, so every page emits a frame
    (``_FETCH_PROGRESS_PAGE_INTERVAL`` is 1) — "page 3/7" every few seconds.
    """

    @pytest.mark.asyncio
    async def test_platform_fetch_emits_per_page_fetching_frames(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        _wire_fake(plugin, fake_romm_api)
        # 3084 ROMs at the 500-ROM page size → 7 pages (6 full + an 84-item tail).
        # Every page emits a frame (interval 1).
        rom_count = 3084
        _seed_pages(fake_romm_api, platform_id=1, count=rom_count)

        unit = WorkUnit(type="platform", id=1, name="GBA", slug="gba", rom_count=rom_count)
        await plugin._sync_service._fetcher.fetch_platform_unit(unit, progress_step=3, progress_total_steps=12)

        frames = _fetching_frames(decky)
        assert [f["current"] for f in frames] == [1, 2, 3, 4, 5, 6, 7]
        # Every frame keeps the run's coarse position and names the platform+page,
        # and carries the ``fetch`` sub-stage so the bar fills the fetch sub-slice
        # (#1407).
        for f in frames:
            assert f["stage"] == "fetching"
            assert f["subStage"] == "fetch"
            assert f["step"] == 3
            assert f["totalSteps"] == 12
            assert f["total"] == 7
        assert frames[0]["message"] == "Fetching GBA (page 1/7)"
        assert frames[-1]["message"] == "Fetching GBA (page 7/7)"

    @pytest.mark.asyncio
    async def test_single_page_fetch_emits_one_frame(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        _wire_fake(plugin, fake_romm_api)
        _seed_pages(fake_romm_api, platform_id=1, count=3)

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)
        await plugin._sync_service._fetcher.fetch_platform_unit(unit, progress_step=1, progress_total_steps=1)

        frames = _fetching_frames(decky)
        assert [f["current"] for f in frames] == [1]
        assert frames[0]["message"] == "Fetching N64 (page 1/1)"

    @pytest.mark.asyncio
    async def test_incremental_skip_emits_no_fetching_frame(self, plugin, fake_romm_api):
        """A platform that incremental-skips returns before any page → no frames."""
        import decky

        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        decky.emit.reset_mock()

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        _unit_roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(
            unit, progress_step=1, progress_total_steps=1
        )

        assert skipped is True
        assert _fetching_frames(decky) == []

    @pytest.mark.asyncio
    async def test_collection_fetch_emits_per_page_fetching_frames(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        _wire_fake(plugin, fake_romm_api)
        # 1200 ROMs in collection 7 → 3 pages (500 + 500 + 200) at the 500-ROM
        # page size; every page emits a frame (interval 1).
        rom_count = 1200
        fake_romm_api.roms = {
            i: {"id": i, "platform_id": 1, "name": f"G{i}", "collection_ids": [7]} for i in range(rom_count)
        }

        unit = WorkUnit(
            type="collection", id=7, name="Favorites", slug="", rom_count=rom_count, collection_kind="standard"
        )
        await plugin._sync_service._fetcher.fetch_collection_unit(unit, set(), progress_step=2, progress_total_steps=8)

        frames = _fetching_frames(decky)
        assert [f["current"] for f in frames] == [1, 2, 3]
        assert frames[0]["message"] == "Fetching Favorites (page 1/3)"
        assert frames[0]["step"] == 2
        assert frames[0]["totalSteps"] == 8
        # A collection fetch narrates under the same ``fetch`` sub-stage (#1407).
        assert all(f["subStage"] == "fetch" for f in frames)

    @pytest.mark.asyncio
    async def test_no_step_context_leaves_bar_indeterminate(self, plugin, fake_romm_api):
        """Callers that pass no coarse position (default 0) still narrate pages,
        but leave step/totalSteps at 0 so the main bar stays indeterminate."""
        import decky

        decky.emit.reset_mock()
        _wire_fake(plugin, fake_romm_api)
        _seed_pages(fake_romm_api, platform_id=1, count=3)

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)
        await plugin._sync_service._fetcher.fetch_platform_unit(unit)

        frames = _fetching_frames(decky)
        assert len(frames) == 1
        assert frames[0]["step"] == 0
        assert frames[0]["totalSteps"] == 0


class TestPlanEstimates:
    """build_work_queue's plan-time estimate riders (#1382).

    ``predicted_skip`` / ``collapsed_count`` / ``bound_count`` /
    ``new_shortcut_count`` are
    estimate-only fields that price the ``sync_plan`` payload — the fetch-time
    gate (``_try_unit_incremental_skip``) stays the sole skip authority
    (ADR-0023); see :class:`TestPredictionNeverFeedsSkipGate` for the guard.
    """

    @pytest.mark.asyncio
    async def test_predicts_skip_and_collapsed_count_when_local_conditions_hold(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        # A 3-sibling group: one bound representative + two unbound siblings —
        # collapses to ONE shortcut.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert len(units) == 1
        assert units[0].predicted_skip is True
        assert units[0].collapsed_count == 1

    @pytest.mark.asyncio
    async def test_grandfathered_group_counts_each_bound_sibling(self, plugin, fake_romm_api):
        """A pre-ADR-0021 group with two bound duplicates keeps both shortcuts
        (§5), so the plan estimate prices 2, not one-per-group."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].collapsed_count == 2

    @pytest.mark.asyncio
    async def test_never_synced_platform_predicts_no_skip_and_no_collapsed_count(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is False
        assert units[0].collapsed_count is None

    @pytest.mark.asyncio
    async def test_partial_rows_without_stamp_predict_no_collapsed_count(self, plugin, fake_romm_api):
        """#1412: a never-synced but enabled platform carrying only partial
        collection-sibling rows (no completion stamp) must NOT price the ETA at
        the partial local count — the estimate omits collapsed_count so the
        frontend weights the unit at the full server rom_count instead.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        # A big platform whose only local rows are a couple of favorited siblings
        # (ADR-0021) — no stamp, because it was never synced as a platform.
        fake_romm_api.platforms = [{"id": 1, "name": "SNES", "slug": "snes", "rom_count": 3344}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1", platform_slug="snes")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:101:1", platform_slug="snes")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is False
        assert units[0].collapsed_count is None
        # bound_count is NOT stamp-gated: those two favorited siblings really do
        # have Steam shortcuts, so they are genuinely updates, not creates.
        assert units[0].bound_count == 2

    @pytest.mark.asyncio
    async def test_bound_count_counts_rows_carrying_a_shortcut(self, plugin, fake_romm_api):
        """#1511: the count of rows already bound to a Steam shortcut, which the
        frontend prices at the cheap update rate instead of the create rate."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:101:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:102:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].bound_count == 2

    @pytest.mark.asyncio
    async def test_bound_count_is_zero_for_a_platform_with_no_persisted_rows(self, plugin, fake_romm_api):
        """Zero is knowledge, not absence: a never-synced platform mirrors
        nothing, so every planned item really is a create."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].bound_count == 0

    @pytest.mark.asyncio
    async def test_force_full_sync_shape_keeps_bound_count_so_the_re_apply_prices_as_updates(
        self, plugin, fake_romm_api
    ):
        """The #1511 over-read: a Force Full Sync clears every completion stamp
        but unbinds nothing, so the run is all cheap updates. Without
        bound_count the plan prices it as a fresh import (~4x the real cost)."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 2}]
        plugin.settings["enabled_platforms"] = {"1": True}
        # No stamp — exactly what clear_sync_cache leaves behind — but the rows
        # keep their shortcut_app_id.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:101:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is False
        assert units[0].collapsed_count is None
        assert units[0].bound_count == 2

    @pytest.mark.asyncio
    async def test_force_full_sync_of_a_sibling_heavy_platform_mints_nothing(self, plugin, fake_romm_api):
        """The #1517 over-read: a Force Full Sync drops the collapsed count, so
        the unit is weighed at the pre-collapse rom_count — which counts each
        sibling duplicate. Deriving creates by subtracting the bound rows from
        that weight prices every duplicate as a phantom new shortcut (plus a
        cover download it never performs); new_shortcut_count reports none,
        because the clear takes the stamps and not the bindings.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 4}]
        plugin.settings["enabled_platforms"] = {"1": True}
        # No stamp — what clear_sync_cache leaves behind. Two sibling groups
        # (ADR-0021), each a bound representative plus an unbound duplicate.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=1002, group_key="igdb:200:1")
        _seed_persisted_rom(uow, 13, app_id=None, group_key="igdb:200:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].collapsed_count is None
        assert units[0].bound_count == 2
        assert units[0].new_shortcut_count == 0

    @pytest.mark.asyncio
    async def test_never_synced_partial_platform_counts_the_unmirrored_remainder(self, plugin, fake_romm_api):
        """The safety-critical shape (#1412 / #1517): a never-synced platform
        whose only local rows are collection siblings (ADR-0021). Counting
        creates from the known rows alone would price a handful of items for a
        whole platform — a short read. The unmirrored server ROMs count too, so
        creates + bound rows still cover the full rom_count.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "SNES", "slug": "snes", "rom_count": 100}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1", platform_slug="snes")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:101:1", platform_slug="snes")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:102:1", platform_slug="snes")

        units = await plugin._sync_service._fetcher.build_work_queue()

        # One unbound group + the 97 server ROMs no row is held for.
        assert units[0].new_shortcut_count == 98
        assert units[0].bound_count == 2
        assert units[0].new_shortcut_count + units[0].bound_count == units[0].rom_count

    @pytest.mark.asyncio
    async def test_first_ever_sync_counts_every_server_rom_as_a_create(self, plugin, fake_romm_api):
        """Nothing persisted, nothing bound — the whole platform is new work."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].new_shortcut_count == 3
        assert units[0].bound_count == 0

    @pytest.mark.asyncio
    async def test_partially_applied_platform_mints_only_its_unbound_groups(self, plugin, fake_romm_api):
        """A fetched-but-not-fully-applied platform: the groups that never got a
        shortcut are the creates, the bound rows the updates."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 4}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=4)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:200:1")
        _seed_persisted_rom(uow, 13, app_id=None, group_key="igdb:300:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].new_shortcut_count == 2
        assert units[0].bound_count == 1
        # The two terms partition the collapsed count — no item priced twice.
        assert units[0].new_shortcut_count + units[0].bound_count == units[0].collapsed_count

    @pytest.mark.asyncio
    async def test_stamp_count_mismatch_predicts_no_skip_but_keeps_collapsed_count(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        # Server grew to 4 ROMs since the 3-ROM stamp → the gate will re-fetch,
        # but the persisted rows still collapse to a displayable count.
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 4}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=1002, group_key="igdb:200:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is False
        assert units[0].collapsed_count == 2

    @pytest.mark.asyncio
    async def test_backfill_pending_predicts_no_skip(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1)
        # A legacy row with no group key forces the gate's backfill full fetch;
        # the keyless row is a singleton for the collapsed count.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key=None)

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is False
        assert units[0].collapsed_count == 1

    @pytest.mark.asyncio
    async def test_dropped_null_key_row_predicts_a_skip(self, plugin, fake_romm_api):
        """The estimate replays the gate's generation-gated backfill too, so a
        dropped keyless row no longer prices a phantom full fetch every run."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1, fetch_id="run-new")
        _seed_persisted_rom(uow, 25135, app_id=1001, group_key="igdb:100:1", fetch_id="run-new")
        _seed_persisted_rom(uow, 4375, app_id=None, group_key=None, fetch_id="run-old")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is True

    @pytest.mark.asyncio
    async def test_zero_bound_rows_predicts_no_skip(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 2}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=2)
        # Mass delete left unbind-only rows (ADR-0007) — the gate re-fetches.
        _seed_persisted_rom(uow, 10, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].predicted_skip is False

    @pytest.mark.asyncio
    async def test_unstamped_collection_carries_no_estimate_fields(self, plugin, fake_romm_api):
        """Without a stamp a collection's membership is unknown — every rider
        stays None, and the seed prices the unit exactly as it did pre-#1511."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = []
        fake_romm_api.collections = [{"id": 7, "name": "Faves", "slug": "faves", "rom_count": 4}]
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.type for u in units] == ["collection"]
        assert units[0].predicted_skip is None
        assert units[0].collapsed_count is None
        # None, NOT 0 — the platform side reports 0 for "nothing mirrored", but a
        # collection with no stamp has no member set to count at all (#1511).
        assert units[0].bound_count is None
        # Platform-only: a collection's rows belong to their platform's unit, so
        # counting creates here would price the same shortcuts twice (#1517).
        assert units[0].new_shortcut_count is None

    @pytest.mark.asyncio
    async def test_stamped_collection_counts_its_bound_members(self, plugin, fake_romm_api):
        """#1511: a stamped collection's member set comes from the stamp (no ROM
        fetch), so its already-bound members price at the cheap update rate."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = []
        fake_romm_api.collections = [{"id": 7, "name": "Faves", "slug": "faves", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}
        # Three members; two already hold a Steam shortcut.
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:101:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:102:1")
        _seed_collection_stamp(
            uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:00:00",
            rom_count=3,
            member_rom_ids=(10, 11, 12),
        )

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].bound_count == 2
        assert units[0].new_shortcut_count is None

    @pytest.mark.asyncio
    async def test_stamped_collection_ignores_members_with_no_persisted_row(self, plugin, fake_romm_api):
        """A stale member set can name ROMs no longer persisted locally; those
        are not bound and must not be counted (estimate-only, no freshness probe)."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = []
        fake_romm_api.collections = [{"id": 7, "name": "Faves", "slug": "faves", "rom_count": 2}]
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_collection_stamp(
            uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:00:00",
            rom_count=2,
            member_rom_ids=(10, 999),
        )

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert units[0].bound_count == 1

    @pytest.mark.asyncio
    async def test_virtual_collection_never_carries_a_bound_count(self, plugin, fake_romm_api):
        """Virtual collections are never stampable (CollectionSyncState.stamp
        takes only standard/smart), so they have no member set — the field stays absent."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = []
        fake_romm_api.virtual_collections = {
            "franchise": [{"id": "fr-1", "name": "Zelda", "slug": "zelda", "rom_count": 5}]
        }
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"standard": {}, "smart": {}, "virtual": {"fr-1": True}}
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.collection_kind for u in units] == ["virtual"]
        assert units[0].bound_count is None

    @pytest.mark.asyncio
    async def test_collection_bound_count_read_failure_leaves_the_field_none(self, plugin, fake_romm_api):
        """Fail-open, like the platform sibling: a DB failure degrades the
        estimate to its pre-#1511 reading, never the plan."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = []
        fake_romm_api.collections = [{"id": 7, "name": "Faves", "slug": "faves", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}

        def _boom():
            raise RuntimeError("db down")

        plugin._sync_service._fetcher._uow_factory = _boom

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert len(units) == 1
        assert units[0].bound_count is None

    @pytest.mark.asyncio
    async def test_force_full_sync_clears_stamps_so_plan_predicts_no_skips(self, plugin, fake_romm_api):
        """clear_sync_cache runs BEFORE the run, so a forced plan reads no stamps.

        Pins the PLATFORM-vs-COLLECTION asymmetry the clear exposes (#1511): a
        platform keeps its ``bound_count`` (read straight from the rows, no stamp
        gate) while a collection loses its — a collection's membership is knowable
        only from the stamp the clear just deleted, so its unit reverts to create
        pricing for that run. The estimate reads long, never short.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        # A stamped collection with one bound member. Its member row sits on
        # another platform so it cannot perturb the N64 unit's own counts.
        fake_romm_api.collections = [{"id": 7, "name": "Faves", "slug": "faves", "rom_count": 1}]
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}
        _seed_persisted_rom(uow, 20, app_id=2001, group_key="igdb:200:1", platform_slug="snes")
        _seed_collection_stamp(
            uow,
            "7",
            "standard",
            updated_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:00:00",
            rom_count=1,
            member_rom_ids=(20,),
        )

        before = {u.name: u for u in await plugin._sync_service._fetcher.build_work_queue()}
        assert before["N64"].predicted_skip is True
        assert before["N64"].collapsed_count == 1
        assert before["Faves"].bound_count == 1

        plugin._sync_service.clear_sync_cache()

        after = {u.name: u for u in await plugin._sync_service._fetcher.build_work_queue()}
        assert after["N64"].predicted_skip is False
        # The stamp clear also drops the collapsed count (#1412 gate): the forced
        # re-apply recreates every shortcut, so the ETA is priced at the full
        # server rom_count, not the (now stale) persisted collapse. The rows
        # survive the clear, but without a stamp the count is not emitted.
        assert after["N64"].collapsed_count is None
        # The platform's BINDING survives the clear, and so does its bound_count —
        # it reads the rows directly, so the re-apply is priced as a walk of cheap
        # updates (#1511).
        assert after["N64"].bound_count == 1
        # The collection is the opposite case, and this pairing is the point: its
        # shortcut is just as intact, but the clear deleted the ONLY record of
        # which ROMs are members, so the count is absent — not 0, which would
        # assert a membership the plan can no longer see. Absent means the seed
        # prices the unit's items as creates for this run.
        assert after["Faves"].bound_count is None
        with uow:
            assert uow.roms.get(20).shortcut_app_id == 2001

    @pytest.mark.asyncio
    async def test_estimate_read_failure_leaves_fields_none_and_plan_intact(self, plugin, fake_romm_api):
        """Fail-open: a DB failure degrades the estimate, never the plan."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        plugin.settings["enabled_platforms"] = {"1": True}

        def _boom():
            raise RuntimeError("db down")

        plugin._sync_service._fetcher._uow_factory = _boom

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert len(units) == 1
        assert units[0].predicted_skip is None
        assert units[0].collapsed_count is None
        assert units[0].bound_count is None


class TestGetPlatformsCollapsedCount:
    """get_platforms attaches the persisted post-collapse count per platform,
    gated on the platform's completion stamp (#1382 / #1412).

    The stamp exists iff the platform's local mirror is complete, so it is the
    only condition under which a post-collapse count is meaningful. A
    never-synced platform holds only PARTIAL cross-platform collection siblings
    (ADR-0021), whose count would shadow the true server total (#1412) — so
    without a stamp the field is omitted and the frontend shows ``rom_count``.
    """

    @pytest.mark.asyncio
    async def test_synced_platform_carries_collapsed_count(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 4},
            {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
        ]
        # n64: a synced platform (stamp present) with a 3-sibling group + a keyless
        # singleton → 2 shortcuts. snes: never synced (no rows, no stamp).
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=4)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 13, app_id=1002, group_key=None)

        result = await plugin._sync_service._fetcher.get_platforms()

        assert result["success"] is True
        by_slug = {p["slug"]: p for p in result["platforms"]}
        assert by_slug["n64"]["collapsed_count"] == 2
        assert by_slug["n64"]["rom_count"] == 4
        assert "collapsed_count" not in by_slug["snes"]

    @pytest.mark.asyncio
    async def test_partial_rows_without_stamp_omit_collapsed_count(self, plugin, fake_romm_api):
        """#1412: a never-synced platform carrying only partial collection-sibling
        rows (no completion stamp) must NOT surface a collapsed_count — the label
        falls back to the true server total instead of the partial local count.
        """
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        # SNES has a large server total but only a few favorited siblings persisted
        # locally (ADR-0021) — and no completion stamp, because it was never synced.
        fake_romm_api.platforms = [{"id": 2, "name": "SNES", "slug": "snes", "rom_count": 3344}]
        _seed_persisted_rom(uow, 20, app_id=2001, group_key="igdb:200:1", platform_slug="snes")
        _seed_persisted_rom(uow, 21, app_id=2002, group_key="igdb:201:1", platform_slug="snes")

        result = await plugin._sync_service._fetcher.get_platforms()

        assert result["success"] is True
        entry = result["platforms"][0]
        assert "collapsed_count" not in entry
        assert entry["rom_count"] == 3344

    @pytest.mark.asyncio
    async def test_stamp_deleted_reverts_to_server_count(self, plugin, fake_romm_api):
        """Clearing the stamp (local removal / force sync) drops the collapsed_count
        on the next get_platforms, even while the persisted rows survive."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=None, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")

        before = await plugin._sync_service._fetcher.get_platforms()
        assert before["platforms"][0]["collapsed_count"] == 1

        with uow:
            uow.platform_sync_state.delete("n64")

        after = await plugin._sync_service._fetcher.get_platforms()
        assert "collapsed_count" not in after["platforms"][0]
        assert after["platforms"][0]["rom_count"] == 3

    @pytest.mark.asyncio
    async def test_grandfathered_group_counts_each_bound_sibling(self, plugin, fake_romm_api):
        """The toggle label prices a two-bound-duplicate legacy group at 2 (ADR-0021 §5)."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=3)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 11, app_id=1002, group_key="igdb:100:1")
        _seed_persisted_rom(uow, 12, app_id=None, group_key="igdb:100:1")

        result = await plugin._sync_service._fetcher.get_platforms()

        assert result["platforms"][0]["collapsed_count"] == 2

    @pytest.mark.asyncio
    async def test_collapsed_count_read_failure_falls_back_to_raw_counts(self, plugin, fake_romm_api):
        """Fail-open: a DB failure drops the garnish, never the platform list."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 4}]

        def _boom():
            raise RuntimeError("db down")

        plugin._sync_service._fetcher._uow_factory = _boom

        result = await plugin._sync_service._fetcher.get_platforms()

        assert result["success"] is True
        assert "collapsed_count" not in result["platforms"][0]
        assert result["platforms"][0]["rom_count"] == 4


class TestPredictionNeverFeedsSkipGate:
    """ADR-0023 guard: the plan-time prediction rides the payload only — the
    fetch-time gate's decision is IDENTICAL whatever the unit's estimate
    fields say."""

    @pytest.mark.asyncio
    async def test_skip_eligible_platform_skips_regardless_of_prediction_fields(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")
        base = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)

        results = [
            await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)
            for unit in (
                base,
                replace(base, predicted_skip=False, collapsed_count=None),
                replace(base, predicted_skip=True, collapsed_count=1),
            )
        ]

        assert all(r is not None for r in results)
        assert [{rom["id"] for rom in r} for r in results] == [{10}] * 3

    @pytest.mark.asyncio
    async def test_ineligible_platform_full_fetches_even_when_prediction_says_skip(self, plugin, fake_romm_api):
        """A (wrong) predicted_skip=True can only mis-estimate, never mis-skip."""
        _wire_fake(plugin, fake_romm_api)
        # No stamp, no rows — the gate must full-fetch.
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1, predicted_skip=True)

        result = await plugin._sync_service._fetcher._try_unit_incremental_skip(unit)

        assert result is None
