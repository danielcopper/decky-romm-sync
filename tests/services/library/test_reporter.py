"""Tests for SyncReporter — post-apply roms upserts, finalisation, registry queries."""

import json
import os

import pytest
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore

from domain.rom import Rom

# conftest.py patches decky before this import


def _seed_rom(uow, rom_id, *, app_id, platform_slug, name="Game", cover_path=None, sgdb_id=None, igdb_id=None):
    """Insert a bound (or unbound when app_id is None) ROM into the shared fake UoW."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
        cover_path=cover_path,
        sgdb_id=sgdb_id,
        igdb_id=igdb_id,
    )
    with uow:
        uow.roms.save(rom)


def _seed_platform_names(uow, names: dict[str, str]) -> None:
    """Seed the offline ``platform_slug → display_name`` cache."""
    with uow:
        uow.kv_config.set("platform_names", json.dumps(names))


class TestGetSyncStats:
    @pytest.mark.asyncio
    async def test_computes_from_registry(self, plugin):
        from domain.sync_run import SyncRun

        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(uow, 20, app_id=1002, platform_slug="n64", name="Game B")
        _seed_rom(uow, 30, app_id=1003, platform_slug="snes", name="Game C")
        run = SyncRun.start(id="run-1", at="2025-01-01T00:00:00", platforms_planned=2, roms_planned=3)
        run.complete("2025-01-01T00:00:00", ["N64", "SNES"], [])
        with uow:
            uow.sync_runs.save(run)
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}
        plugin.settings["enabled_collections"] = {
            "user": {"3": True},
            "smart": {"5": True},
            "franchise": {"abc": False},  # disabled — not counted
        }

        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 2
        # 3 enabled across two buckets (user["3"], smart["5"]); franchise["abc"] is False.
        assert stats["collections"] == 2
        assert stats["roms"] == 3
        assert stats["total_shortcuts"] == 3
        assert stats["last_sync"] == "2025-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 0
        assert stats["roms"] == 0
        assert stats["total_shortcuts"] == 0
        assert stats["last_sync"] is None

    @pytest.mark.asyncio
    async def test_excludes_unbound_roms_from_count(self, plugin):
        """Stats count only bound ROMs — unbound (stale) rows do not inflate the total."""
        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(uow, 20, app_id=None, platform_slug="snes", name="Game B (stale)")

        stats = await plugin.get_sync_stats()
        assert stats["roms"] == 1
        assert stats["total_shortcuts"] == 1

    @pytest.mark.asyncio
    async def test_report_removal_unbinds_roms_so_stats_drop(self, plugin):
        """report_removal_results unbinds the ROMs; derived get_sync_stats then counts zero."""
        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(uow, 20, app_id=1002, platform_slug="snes", name="Game B")

        await plugin.report_removal_results([10, 20])

        stats = await plugin.get_sync_stats()
        assert stats["roms"] == 0
        assert stats["total_shortcuts"] == 0
        # Rows survive (ADR-0007): they're unbound, not deleted.
        with uow:
            assert uow.roms.get(10).shortcut_app_id is None
            assert uow.roms.get(20).shortcut_app_id is None


class TestGetRegistryPlatforms:
    @pytest.mark.asyncio
    async def test_returns_platforms_from_registry(self, plugin):
        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        _seed_rom(uow, 20, app_id=1002, platform_slug="n64", name="Zelda OOT")
        _seed_rom(uow, 30, app_id=1003, platform_slug="snes", name="DKC")
        # Live name cache resolves slugs → display names.
        _seed_platform_names(uow, {"n64": "Nintendo 64", "snes": "Super Nintendo"})

        result = await plugin.get_registry_platforms()
        assert len(result["platforms"]) == 2
        # Sorted by display name
        assert result["platforms"][0]["name"] == "Nintendo 64"
        assert result["platforms"][0]["slug"] == "n64"
        assert result["platforms"][0]["count"] == 2
        assert result["platforms"][1]["name"] == "Super Nintendo"
        assert result["platforms"][1]["slug"] == "snes"
        assert result["platforms"][1]["count"] == 1

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        result = await plugin.get_registry_platforms()
        assert result["platforms"] == []

    @pytest.mark.asyncio
    async def test_excludes_unbound_roms(self, plugin):
        """Unbound (stale) rows are not surfaced as registry platforms."""
        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Bound")
        _seed_rom(uow, 20, app_id=None, platform_slug="snes", name="Unbound")
        _seed_platform_names(uow, {"n64": "Nintendo 64", "snes": "Super Nintendo"})

        result = await plugin.get_registry_platforms()
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["slug"] == "n64"

    @pytest.mark.asyncio
    async def test_degrades_to_slug_when_name_cache_absent(self, plugin):
        """Offline / no cache → the display name degrades to the slug."""
        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")

        result = await plugin.get_registry_platforms()
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["name"] == "n64"
        assert result["platforms"][0]["slug"] == "n64"
        assert result["platforms"][0]["count"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blob", ["not json at all {", '"a json string, not a dict"', "[1, 2, 3]"])
    async def test_degrades_to_slug_when_name_cache_corrupt(self, plugin, blob):
        """A corrupt / non-dict ``platform_names`` blob decodes to ``{}`` so the
        display name degrades to the slug (bad-path for the decode guard)."""
        uow = plugin._uow
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        with uow:
            uow.kv_config.set("platform_names", blob)

        result = await plugin.get_registry_platforms()
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["name"] == "n64"
        assert result["platforms"][0]["slug"] == "n64"


class TestGetRomBySteamAppId:
    @pytest.mark.asyncio
    async def test_finds_rom_by_app_id_installed(self, plugin):
        from domain.rom_install import RomInstall

        uow = plugin._uow
        _seed_rom(uow, 42, app_id=100001, platform_slug="n64", name="Zelda")
        _seed_platform_names(uow, {"n64": "Nintendo 64"})
        with uow:
            uow.rom_installs.save(
                RomInstall.mark_installed(
                    rom_id=42,
                    file_path="/roms/n64/zelda.z64",
                    rom_dir=None,
                    platform_slug="n64",
                    system="n64",
                    installed_at="2025-01-01T00:00:00",
                )
            )
        result = plugin._sync_service.get_rom_by_steam_app_id(100001)
        assert result is not None
        assert result["rom_id"] == 42
        assert result["name"] == "Zelda"
        assert result["platform_name"] == "Nintendo 64"
        assert result["platform_slug"] == "n64"
        assert result["installed"] is True

    @pytest.mark.asyncio
    async def test_finds_rom_by_app_id_not_installed(self, plugin):
        """A bound ROM with no install record reports ``installed`` False."""
        uow = plugin._uow
        _seed_rom(uow, 42, app_id=100001, platform_slug="n64", name="Zelda")
        _seed_platform_names(uow, {"n64": "Nintendo 64"})

        result = plugin._sync_service.get_rom_by_steam_app_id(100001)
        assert result is not None
        assert result["installed"] is False

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown(self, plugin):
        result = plugin._sync_service.get_rom_by_steam_app_id(999999)
        assert result is None


class TestFinalizeCoverPath:
    """Tests for _finalize_cover_path() — lines 699-712."""

    def test_renames_staging_to_final(self, plugin, tmp_path):
        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("cover data")

        result = plugin._sync_service._reporter._finalize_cover_path(grid, str(staging), 100001, "1")
        expected = os.path.join(grid, "100001p.png")
        assert result == expected
        assert not staging.exists()
        assert os.path.exists(expected)

    def test_returns_existing_final(self, plugin, tmp_path):
        grid = str(tmp_path)
        final = tmp_path / "100001p.png"
        final.write_text("final data")

        result = plugin._sync_service._reporter._finalize_cover_path(grid, "/nonexistent/path.png", 100001, "1")
        assert result == str(final)

    def test_returns_cover_path_when_no_grid(self, plugin):
        result = plugin._sync_service._reporter._finalize_cover_path(None, "/some/path.png", 100001, "1")
        assert result == "/some/path.png"

    def test_returns_cover_path_when_empty(self, plugin, tmp_path):
        result = plugin._sync_service._reporter._finalize_cover_path(str(tmp_path), "", 100001, "1")
        assert result == ""

    def test_handles_rename_os_error(self, plugin, tmp_path):
        grid = str(tmp_path)
        staging_path = os.path.join(grid, "romm_1_cover.png")

        # Inject OSError on rename through the CoverArtFileStore Protocol —
        # mirrors the Wave 3 fake-adapter failure-injection pattern instead
        # of patching ``os.replace`` globally.
        fake_store = FakeCoverArtFileStore(files={staging_path: b"data"})
        fake_store.rename_failures.add(staging_path)
        plugin._artwork_service._cover_art_file_store = fake_store

        result = plugin._sync_service._reporter._finalize_cover_path(grid, staging_path, 100001, "1")
        # Should return original path on error
        assert result == staging_path


class TestCommitUnitResults:
    """Tests for _commit_unit_results_io — per-unit ``roms`` upsert via ``Rom.synced``."""

    def test_commit_upserts_rom_from_pending(self, plugin):
        """A unit's acked ROM is upserted into ``uow.roms`` from its pending entry."""
        uow = plugin._uow
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_name": "Game Boy",
            "platform_slug": "gb",
            "cover_path": "",
            "igdb_id": 555,
            "sgdb_id": 999,
            "ra_id": 777,
        }

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [])

        assert uow.committed is True
        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.shortcut_app_id == 100001
        assert rom.name == "Game"
        assert rom.fs_name == "game.z64"
        assert rom.platform_slug == "gb"
        assert rom.igdb_id == 555
        assert rom.sgdb_id == 999
        assert rom.ra_id == 777

    def test_commit_stamps_cover_path_when_present(self, plugin):
        """A finalized cover path is recorded on the upserted ROM row."""
        uow = plugin._uow
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_slug": "gb",
            "cover_path": "/covers/staging.png",
        }

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [])

        with uow:
            rom = uow.roms.get(42)
        # No grid dir in the test stub → finalize returns the path unchanged.
        assert rom.cover_path == "/covers/staging.png"

    def test_commit_skips_invalid_rom_keeps_rest(self, plugin):
        """An invariant ValueError (missing platform_slug) skips one ROM; the rest still commit."""
        uow = plugin._uow
        plugin._sync_service._box.pending_sync[10] = {
            "name": "Bad",
            "fs_name": "bad.z64",
            "platform_slug": "",  # invalid — Rom.synced raises ValueError
            "cover_path": "",
        }
        plugin._sync_service._box.pending_sync[20] = {
            "name": "Good",
            "fs_name": "good.z64",
            "platform_slug": "gb",
            "cover_path": "",
        }

        plugin._sync_service._reporter._commit_unit_results_io({"10": 1010, "20": 1020}, [])

        assert uow.committed is True
        with uow:
            assert uow.roms.get(10) is None
            assert uow.roms.get(20) is not None

    def test_commit_preserves_out_of_band_sgdb_id_on_resync(self, plugin):
        """An sgdb_id resolved out-of-band (e.g. IGDB cross-ref) survives a re-sync
        whose pending entry has sgdb_id=None — the live RomM fetch never carries it.

        Regression of #746's _merge_optional_id contract: a blind upsert would
        NULL the resolved id and revert SGDB artwork to "needs pick"."""
        uow = plugin._uow
        # Existing row carries a plugin-resolved sgdb_id + ra_id + cover_path.
        _seed_rom(
            uow,
            42,
            app_id=100001,
            platform_slug="gb",
            name="Game",
            sgdb_id=4242,
            cover_path="/covers/42p.png",
        )
        with uow:
            existing = uow.roms.get(42)
            existing.assign_ra_id(7777)
            uow.roms.save(existing)

        # The re-sync's pending entry (live RomM fetch) lacks sgdb_id / ra_id /
        # cover_path entirely.
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_slug": "gb",
            "cover_path": "",
            "igdb_id": 555,
            "sgdb_id": None,
            "ra_id": None,
        }

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [])

        with uow:
            rom = uow.roms.get(42)
        # Out-of-band ids + cover preserved; RomM-native igdb_id overwritten.
        assert rom.sgdb_id == 4242
        assert rom.ra_id == 7777
        assert rom.cover_path == "/covers/42p.png"
        assert rom.igdb_id == 555

    def test_commit_new_value_overwrites_existing_id(self, plugin):
        """A fresh non-None sgdb_id in pending wins over the existing row's value."""
        uow = plugin._uow
        _seed_rom(uow, 42, app_id=100001, platform_slug="gb", name="Game", sgdb_id=4242)

        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_slug": "gb",
            "cover_path": "",
            "sgdb_id": 9999,
        }

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [])

        with uow:
            assert uow.roms.get(42).sgdb_id == 9999


class TestCommitUnitMetadataStamp:
    """The metadata stamp folded into the per-unit ``roms`` write UoW.

    The reporter saves each acked ROM's cached ``rom_metadata`` in the same
    write UoW as the ``roms`` upsert (Rom row first, metadata second — the
    FK is satisfied at commit), so a ROM and its metadata land atomically.
    """

    def test_stamps_metadata_alongside_rom(self, plugin):
        """An acked ROM carrying a ``metadatum`` lands both a ``roms`` row and
        a ``rom_metadata`` row in the same commit, with fields mapped + ms→s +
        steam_categories computed."""
        uow = plugin._uow
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_slug": "gb",
            "cover_path": "",
        }
        acked = [
            {
                "id": 42,
                "summary": "A classic",
                "metadatum": {
                    "genres": ["Action", "Puzzle"],
                    "companies": ["Nintendo"],
                    "first_release_date": 946684800000,  # ms
                    "average_rating": 88.5,
                    "game_modes": ["Single player"],
                    "player_count": "1",
                },
            },
        ]

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, acked)

        assert uow.committed is True
        with uow:
            rom = uow.roms.get(42)
            meta = uow.rom_metadata.get(42)
        # Rom row committed.
        assert rom is not None
        assert rom.shortcut_app_id == 100001
        # Metadata row committed, fields mapped.
        assert meta is not None
        assert meta.summary == "A classic"
        assert meta.genres == ("Action", "Puzzle")
        assert meta.companies == ("Nintendo",)
        assert meta.first_release_date == 946684800  # ms → s
        assert meta.average_rating == 88.5
        assert meta.game_modes == ("Single player",)
        # Steam categories derived from genres + modes (28 = full controller).
        assert 28 in meta.steam_categories
        assert 21 in meta.steam_categories  # Action
        assert 4 in meta.steam_categories  # Puzzle
        assert 2 in meta.steam_categories  # Single player

    def test_malformed_metadatum_skips_metadata_keeps_rom(self, plugin, caplog):
        """A malformed ``metadatum`` (non-numeric release date) skips only that
        ROM's metadata — the Rom row still commits and a warning is logged."""
        import logging

        uow = plugin._uow
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_slug": "gb",
            "cover_path": "",
        }
        # first_release_date is non-numeric → int(...) raises ValueError in the
        # mapping, caught per-rom.
        acked = [{"id": 42, "summary": "Bad", "metadatum": {"first_release_date": "not-a-number"}}]

        with caplog.at_level(logging.WARNING):
            plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, acked)

        assert uow.committed is True
        with uow:
            # Rom survives; metadata was skipped.
            assert uow.roms.get(42) is not None
            assert uow.rom_metadata.get(42) is None
        assert any("malformed metadatum" in r.message.lower() for r in caplog.records)

    def test_no_metadatum_writes_no_metadata_row(self, plugin):
        """An acked ROM without a ``metadatum`` field commits the Rom but no
        ``rom_metadata`` row (defensive guard against thin-ROM cache erasure)."""
        uow = plugin._uow
        plugin._sync_service._box.pending_sync[42] = {
            "name": "Game",
            "fs_name": "game.z64",
            "platform_slug": "gb",
            "cover_path": "",
        }
        acked = [{"id": 42, "name": "Thin"}]  # no metadatum

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, acked)

        assert uow.committed is True
        with uow:
            assert uow.roms.get(42) is not None
            assert uow.rom_metadata.get(42) is None

    def test_falsy_metadatum_writes_no_metadata_row(self, plugin):
        """``metadatum: None`` and ``metadatum: {}`` both skip the metadata stamp."""
        uow = plugin._uow
        plugin._sync_service._box.pending_sync[10] = {
            "name": "A",
            "fs_name": "a.z64",
            "platform_slug": "gb",
            "cover_path": "",
        }
        plugin._sync_service._box.pending_sync[20] = {
            "name": "B",
            "fs_name": "b.z64",
            "platform_slug": "gb",
            "cover_path": "",
        }
        acked = [{"id": 10, "metadatum": None}, {"id": 20, "metadatum": {}}]

        plugin._sync_service._reporter._commit_unit_results_io({"10": 1010, "20": 1020}, acked)

        with uow:
            assert uow.rom_metadata.get(10) is None
            assert uow.rom_metadata.get(20) is None

    def test_empty_unit_commits_nothing_extra(self, plugin):
        """An empty unit (no acked ROMs) commits cleanly with no metadata rows."""
        uow = plugin._uow

        plugin._sync_service._reporter._commit_unit_results_io({}, [])

        assert uow.committed is True
        with uow:
            assert list(uow.rom_metadata.iter_all()) == []


class TestClearSyncCache:
    """Tests for clear_sync_cache() — Force Full Sync resets the completed-run history."""

    def test_deletes_completed_runs_so_last_sync_resets(self, plugin):
        """After clear, no completed run remains → get_latest_completed is None and last_sync resets."""
        from domain.sync_run import SyncRun

        uow = plugin._uow
        run = SyncRun.start(id="run-1", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
        run.complete("2025-01-01T00:00:00", ["N64"], [])
        with uow:
            uow.sync_runs.save(run)

        result = plugin._sync_service.clear_sync_cache()

        assert result["success"] is True
        with uow:
            assert uow.sync_runs.get_latest_completed() is None
        # The derived last_sync read now resets to None.
        stats = plugin._sync_service.get_sync_stats()
        assert stats["last_sync"] is None

    def test_keeps_running_run(self, plugin):
        """A running run is untouched — only completed history is cleared."""
        from domain.sync_run import SyncRun

        uow = plugin._uow
        running = SyncRun.start(id="run-live", at="2025-02-01T00:00:00", platforms_planned=1, roms_planned=1)
        with uow:
            uow.sync_runs.save(running)

        plugin._sync_service.clear_sync_cache()

        with uow:
            assert uow.sync_runs.get_running() is not None


class TestFinalizePerUnitRun:
    """SyncReporter.finalize_per_unit_run — emits sync_collections + sync_complete after the per-unit loop."""

    @pytest.mark.asyncio
    async def test_builds_platform_collections_from_roms(self, plugin):
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="A")
        _seed_rom(uow, 2, app_id=1002, platform_slug="snes", name="B")
        plugin.settings["collection_create_platform_groups"] = True

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1, 2},
            total_games=2,
            platform_names={"n64": "Nintendo 64", "snes": "Super Nintendo"},
        )

        collections_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_collections"]
        assert len(collections_events) == 1
        payload = collections_events[0][0][1]
        # Keyed by live display names; the kv_config cache was refreshed.
        assert set(payload["platform_app_ids"].keys()) == {"Nintendo 64", "Super Nintendo"}
        with uow:
            assert json.loads(uow.kv_config.get("platform_names")) == {
                "n64": "Nintendo 64",
                "snes": "Super Nintendo",
            }

    @pytest.mark.asyncio
    async def test_builds_romm_collection_app_ids_excluding_unbound(self, plugin):
        """RomM collections resolve rom_id→app_id via uow.roms and skip unbound rows."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="A")
        _seed_rom(uow, 2, app_id=None, platform_slug="snes", name="B (unbound)")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={"Faves": [1, 2]},
            pending_platform_rom_ids={1},
            total_games=1,
            platform_names={"n64": "Nintendo 64"},
        )

        collections_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_collections"]
        payload = collections_events[0][0][1]
        # rom 2 is unbound → excluded; only rom 1's app_id appears.
        assert payload["romm_collection_app_ids"] == {"Faves": [1001]}

    @pytest.mark.asyncio
    async def test_emits_sync_complete_terminal(self, plugin):
        import decky

        decky.emit.reset_mock()

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids=set(),
            total_games=0,
            platform_names={},
        )

        complete_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert len(complete_events) == 1
        assert "cancelled" not in complete_events[0][0][1]

    @pytest.mark.asyncio
    async def test_sets_state_to_idle_at_end(self, plugin):
        import decky

        from domain.sync_state import SyncState

        decky.emit.reset_mock()
        plugin._sync_service._sync_state = SyncState.RUNNING
        plugin._sync_service._current_sync_id = "sync-xyz"

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids=set(),
            total_games=0,
            platform_names={},
        )

        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._current_sync_id is None

    @pytest.mark.asyncio
    async def test_unbinds_stale_rom_ids_keeping_rows(self, plugin):
        """stale_rom_ids are UNBOUND (NULL app_id) — the rows survive (ADR-0007), never deleted."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="A")
        _seed_rom(uow, 2, app_id=1002, platform_slug="snes", name="B")
        _seed_rom(uow, 3, app_id=1003, platform_slug="gba", name="C")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1},
            total_games=1,
            platform_names={"n64": "Nintendo 64"},
            stale_rom_ids=[2, 3],
        )

        assert uow.committed is True
        with uow:
            # Rows survive but their shortcut binding is cleared.
            assert uow.roms.get(2).shortcut_app_id is None
            assert uow.roms.get(3).shortcut_app_id is None
            assert uow.roms.get(1).shortcut_app_id == 1001
            assert {r.rom_id for r in uow.roms.iter_all()} == {1, 2, 3}

    @pytest.mark.asyncio
    async def test_stale_unbind_excludes_them_from_collections(self, plugin):
        """Collections built from uow.roms must skip NULL-app_id (just-unbound) rows."""
        import decky

        decky.emit.reset_mock()
        plugin.settings["collection_create_platform_groups"] = True
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="A")
        _seed_rom(uow, 2, app_id=1002, platform_slug="snes", name="B")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1, 2},
            total_games=1,
            platform_names={"n64": "Nintendo 64", "snes": "Super Nintendo"},
            stale_rom_ids=[2],
        )

        collections_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_collections"]
        payload = collections_events[0][0][1]
        assert set(payload["platform_app_ids"].keys()) == {"Nintendo 64"}

    @pytest.mark.asyncio
    async def test_stale_unbind_skips_missing_and_already_unbound(self, plugin):
        """A stale_rom_id with no row (missing) or already-unbound row is skipped
        without error; the genuinely-bound stale rows still unbind."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="Kept")
        _seed_rom(uow, 2, app_id=1002, platform_slug="snes", name="Stale bound")
        _seed_rom(uow, 5, app_id=None, platform_slug="gba", name="Already unbound")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1},
            total_games=1,
            platform_names={"n64": "Nintendo 64"},
            stale_rom_ids=[2, 5, 99],  # 2 bound, 5 already unbound, 99 missing
        )

        assert uow.committed is True
        with uow:
            # rom 2 was genuinely stale → unbound; rom 5 stays unbound (skipped,
            # no error); rom 99 has no row (skipped); rom 1 stays bound.
            assert uow.roms.get(2).shortcut_app_id is None
            assert uow.roms.get(5).shortcut_app_id is None
            assert uow.roms.get(1).shortcut_app_id == 1001
            assert uow.roms.get(99) is None
            assert {r.rom_id for r in uow.roms.iter_all()} == {1, 2, 5}

    @pytest.mark.asyncio
    async def test_no_unbind_when_stale_rom_ids_default(self, plugin):
        """Default stale_rom_ids=None unbinds nothing — every bound row stays bound."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="A")
        _seed_rom(uow, 2, app_id=1002, platform_slug="snes", name="B")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1, 2},
            total_games=2,
            platform_names={},
        )

        with uow:
            assert uow.roms.get(1).shortcut_app_id == 1001
            assert uow.roms.get(2).shortcut_app_id == 1002

    @pytest.mark.asyncio
    async def test_get_sync_stats_reflects_unbound_count(self, plugin):
        """After a normal finalize unbinds stale rows, get_sync_stats counts only bound ones."""
        import decky

        from domain.sync_run import SyncRun

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="A")
        _seed_rom(uow, 2, app_id=1002, platform_slug="snes", name="B")
        _seed_rom(uow, 3, app_id=1003, platform_slug="gba", name="C")
        run = SyncRun.start(id="run-1", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
        run.complete("2025-01-01T00:00:00", ["Nintendo 64"], [])
        with uow:
            uow.sync_runs.save(run)

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids={1},
            total_games=1,
            platform_names={"n64": "Nintendo 64"},
            stale_rom_ids=[2, 3],
        )

        stats = await plugin.get_sync_stats()
        assert stats["roms"] == 1
        assert stats["total_shortcuts"] == 1
