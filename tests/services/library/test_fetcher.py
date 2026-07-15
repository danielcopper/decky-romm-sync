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


def _seed_completed_run(uow):
    """A completed SyncRun so the skip gate has a ``last_sync`` to diff against."""
    from domain.sync_run import SyncRun

    run = SyncRun.start(id="r1", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=3)
    run.complete("2025-01-01T00:00:00", ["N64"], [])
    with uow:
        uow.sync_runs.save(run)


def _seed_persisted_rom(uow, rom_id, *, app_id, group_key, platform_slug="n64"):
    """Persist one ``roms`` row (bound when app_id is set, else an unbound sibling)."""
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


def _seed_platform_stamp(uow, slug, *, at, rom_count):
    """Persist a per-platform completion stamp (ADR-0023) so the skip can honor it."""
    from domain.platform_sync_state import PlatformSyncState

    with uow:
        uow.platform_sync_state.save(PlatformSyncState.stamp(platform_slug=slug, at=at, rom_count=rom_count))


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
        unit = WorkUnit(type="collection", id=7, name="Coll", slug="", rom_count=rom_count, collection_kind="user")
        synced: set[int] = set()
        new_roms, all_collection_rom_ids = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)

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
        # Every frame keeps the run's coarse position and names the platform+page.
        for f in frames:
            assert f["stage"] == "fetching"
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

        unit = WorkUnit(type="collection", id=7, name="Favorites", slug="", rom_count=rom_count, collection_kind="user")
        await plugin._sync_service._fetcher.fetch_collection_unit(unit, set(), progress_step=2, progress_total_steps=8)

        frames = _fetching_frames(decky)
        assert [f["current"] for f in frames] == [1, 2, 3]
        assert frames[0]["message"] == "Fetching Favorites (page 1/3)"
        assert frames[0]["step"] == 2
        assert frames[0]["totalSteps"] == 8

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

    ``predicted_skip`` / ``collapsed_count`` are estimate-only fields that
    price the ``sync_plan`` payload — the fetch-time gate
    (``_try_unit_incremental_skip``) stays the sole skip authority
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
    async def test_collection_units_carry_no_estimate_fields(self, plugin, fake_romm_api):
        """Collection membership isn't locally derivable — the scope guard leaves both fields None."""
        _wire_fake(plugin, fake_romm_api)
        fake_romm_api.platforms = []
        fake_romm_api.collections = [{"id": 7, "name": "Faves", "slug": "faves", "rom_count": 4}]
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"user": {"7": True}, "smart": {}, "franchise": {}}

        units = await plugin._sync_service._fetcher.build_work_queue()

        assert [u.type for u in units] == ["collection"]
        assert units[0].predicted_skip is None
        assert units[0].collapsed_count is None

    @pytest.mark.asyncio
    async def test_force_full_sync_clears_stamps_so_plan_predicts_no_skips(self, plugin, fake_romm_api):
        """clear_sync_cache runs BEFORE the run, so a forced plan reads no stamps."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_platform_stamp(uow, "n64", at="2025-01-01T00:00:00", rom_count=1)
        _seed_persisted_rom(uow, 10, app_id=1001, group_key="igdb:100:1")

        before = await plugin._sync_service._fetcher.build_work_queue()
        assert before[0].predicted_skip is True

        plugin._sync_service.clear_sync_cache()

        after = await plugin._sync_service._fetcher.build_work_queue()
        assert after[0].predicted_skip is False
        # The rows survive the cache clear, so the collapsed count still shows.
        assert after[0].collapsed_count == 1

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


class TestGetPlatformsCollapsedCount:
    """get_platforms attaches the persisted post-collapse count per platform (#1382)."""

    @pytest.mark.asyncio
    async def test_synced_platform_carries_collapsed_count(self, plugin, fake_romm_api):
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 4},
            {"id": 2, "name": "SNES", "slug": "snes", "rom_count": 5},
        ]
        # n64: a 3-sibling group + a keyless singleton → 2 shortcuts. snes: never synced.
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
    async def test_grandfathered_group_counts_each_bound_sibling(self, plugin, fake_romm_api):
        """The toggle label prices a two-bound-duplicate legacy group at 2 (ADR-0021 §5)."""
        _wire_fake(plugin, fake_romm_api)
        uow = plugin._uow
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 3}]
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
