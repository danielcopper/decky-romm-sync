"""Tests for SyncReporter — post-apply roms upserts, finalisation, registry queries."""

import json
import os

import pytest
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore

from domain.rom import Rom
from domain.sync_diff import BIND_ROM_ID_KEY

# conftest.py patches decky before this import


def _seed_rom(
    uow, rom_id, *, app_id, platform_slug, name="Game", cover_path=None, sgdb_id=None, igdb_id=None, group_key=None
):
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
        sibling_group_key=group_key,
    )
    with uow:
        uow.roms.save(rom)


def _seed_platform_names(uow, names: dict[str, str]) -> None:
    """Seed the offline ``platform_slug → display_name`` cache."""
    with uow:
        uow.kv_config.set("platform_names", json.dumps(names))


def _stage(box, rom_id, entry, *, emitted=True):
    """Stage a fetched ROM's built entry for the group-aware per-unit commit.

    ``pending_all_roms`` is the identity + version source for EVERY fetched ROM;
    ``pending_sync`` holds the emitted representatives (cover-path + bind_rom_id
    marker). A bound representative appears in both; a non-representative sibling
    is staged only in ``pending_all_roms`` (``emitted=False``) so the commit
    persists it unbound.
    """
    box.pending_all_roms[rom_id] = entry
    if emitted:
        box.pending_sync[rom_id] = entry


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
        # Only a completed run exists — no separate attempt to surface.
        assert stats["last_attempt"] is None

    @pytest.mark.asyncio
    async def test_empty_registry(self, plugin):
        stats = await plugin.get_sync_stats()
        assert stats["platforms"] == 0
        assert stats["roms"] == 0
        assert stats["total_shortcuts"] == 0
        assert stats["last_sync"] is None
        assert stats["last_attempt"] is None

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


class TestGetSyncStatsLastAttempt:
    """last_attempt — surface a cancelled/crashed run so 'Last sync' isn't 'Never' (#1367-class)."""

    @staticmethod
    def _cancelled(uow, *, id, started, finished, reason="Sync cancelled"):
        from domain.sync_run import SyncRun

        run = SyncRun.start(id=id, at=started, platforms_planned=1, roms_planned=1)
        run.mark_cancelled(finished, reason)
        with uow:
            uow.sync_runs.save(run)

    @staticmethod
    def _completed(uow, *, id, started, finished):
        from domain.sync_run import SyncRun

        run = SyncRun.start(id=id, at=started, platforms_planned=1, roms_planned=1)
        run.complete(finished, ["N64"], [])
        with uow:
            uow.sync_runs.save(run)

    @staticmethod
    def _interrupted(uow, *, id, started, finished, reason="Sync interrupted (Steam UI stopped responding)"):
        from domain.sync_run import SyncRun

        run = SyncRun.start(id=id, at=started, platforms_planned=1, roms_planned=1)
        run.mark_interrupted(finished, reason)
        with uow:
            uow.sync_runs.save(run)

    @staticmethod
    def _paused(uow, *, id, started, finished, reason="Sync paused: Steam's memory is nearly full."):
        from domain.sync_run import SyncRun

        run = SyncRun.start(id=id, at=started, platforms_planned=1, roms_planned=1)
        run.mark_paused(finished, reason)
        with uow:
            uow.sync_runs.save(run)

    @pytest.mark.asyncio
    async def test_no_runs_reports_no_attempt(self, plugin):
        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] is None
        assert stats["last_attempt"] is None

    @pytest.mark.asyncio
    async def test_only_cancelled_run_surfaces_attempt(self, plugin):
        """A cancelled run with no completed run ever → last_sync None, last_attempt set."""
        self._cancelled(plugin._uow, id="run-c", started="2025-06-01T17:00:00", finished="2025-06-01T17:48:00")

        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] is None
        assert stats["last_attempt"] == {"finished_at": "2025-06-01T17:48:00", "status": "cancelled"}

    @pytest.mark.asyncio
    async def test_errored_run_surfaces_attempt_with_errored_status(self, plugin):
        run_uow = plugin._uow
        from domain.sync_run import SyncRun

        run = SyncRun.start(id="run-e", at="2025-06-01T10:00:00", platforms_planned=1, roms_planned=1)
        run.mark_errored("2025-06-01T10:05:00", "boom")
        with run_uow:
            run_uow.sync_runs.save(run)

        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] is None
        assert stats["last_attempt"] == {"finished_at": "2025-06-01T10:05:00", "status": "errored"}

    @pytest.mark.asyncio
    async def test_cancelled_newer_than_completed_surfaces_attempt(self, plugin):
        """A cancelled run newer than the last completed one → both surface."""
        self._completed(plugin._uow, id="run-ok", started="2025-06-01T09:00:00", finished="2025-06-01T09:30:00")
        self._cancelled(plugin._uow, id="run-c", started="2025-06-02T08:00:00", finished="2025-06-02T08:20:00")

        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] == "2025-06-01T09:30:00"
        assert stats["last_attempt"] == {"finished_at": "2025-06-02T08:20:00", "status": "cancelled"}

    @pytest.mark.asyncio
    async def test_interrupted_newer_than_completed_surfaces_attempt(self, plugin):
        """An interrupted run (external death) newer than the last completed one →
        last_attempt carries the 'interrupted' status (get_latest_terminal must
        include interrupted, or this run would be invisible to the hint)."""
        self._completed(plugin._uow, id="run-ok", started="2025-06-01T09:00:00", finished="2025-06-01T09:30:00")
        self._interrupted(plugin._uow, id="run-i", started="2025-06-02T08:00:00", finished="2025-06-02T08:20:00")

        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] == "2025-06-01T09:30:00"
        assert stats["last_attempt"] == {"finished_at": "2025-06-02T08:20:00", "status": "interrupted"}

    @pytest.mark.asyncio
    async def test_completed_newer_than_cancelled_hides_attempt(self, plugin):
        """A clean run after a cancelled one → last_sync only, no stale attempt line."""
        self._cancelled(plugin._uow, id="run-c", started="2025-06-01T08:00:00", finished="2025-06-01T08:20:00")
        self._completed(plugin._uow, id="run-ok", started="2025-06-02T09:00:00", finished="2025-06-02T09:30:00")

        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] == "2025-06-02T09:30:00"
        assert stats["last_attempt"] is None

    @pytest.mark.asyncio
    async def test_paused_newer_than_completed_surfaces_resumable_attempt(self, plugin):
        """A session-budget 'paused' run newer than the last completed one → last_attempt
        carries the 'paused' status (get_latest_terminal must include paused, or the
        run — and the Resume Sync affordance it drives — would be invisible, #1383)."""
        self._completed(plugin._uow, id="run-ok", started="2025-07-11T09:00:00", finished="2025-07-11T09:30:00")
        self._paused(plugin._uow, id="run-p", started="2025-07-11T10:00:00", finished="2025-07-11T10:20:00")

        stats = await plugin.get_sync_stats()
        assert stats["last_sync"] == "2025-07-11T09:30:00"
        assert stats["last_attempt"] == {"finished_at": "2025-07-11T10:20:00", "status": "paused"}


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
    """Tests for _finalize_cover_path() — publishes the cache cover onto the grid."""

    def test_copies_cache_to_final_and_persists_cache_path(self, plugin, tmp_path):
        grid_path = tmp_path / "grid"
        grid_path.mkdir()
        grid = str(grid_path)
        cache = tmp_path / "covers" / "1.png"
        cache.parent.mkdir(parents=True)
        cache.write_text("cover data")

        result = plugin._sync_service._reporter._finalize_cover_path(grid, str(cache), 100001, "1")
        expected_final = os.path.join(grid, "100001p.png")
        # The persisted path is the CACHE path; the grid gets a copy (cache survives).
        assert result == str(cache)
        assert cache.exists()
        assert os.path.exists(expected_final)

    def test_returns_existing_final_when_cover_missing(self, plugin, tmp_path):
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

    def test_handles_copy_os_error(self, plugin, tmp_path):
        grid = str(tmp_path / "grid")
        cache_path = os.path.join(str(tmp_path / "covers"), "1.png")

        # Inject OSError on copy_file through the CoverArtFileStore Protocol —
        # mirrors the Wave 3 fake-adapter failure-injection pattern instead
        # of patching ``shutil.copyfile`` globally.
        fake_store = FakeCoverArtFileStore(files={cache_path: b"data"})
        fake_store.copy_failures.add(cache_path)
        plugin._artwork_service._cover_art_file_store = fake_store

        result = plugin._sync_service._reporter._finalize_cover_path(grid, cache_path, 100001, "1")
        # A failed copy still returns the cache path (persist unchanged).
        assert result == cache_path


class TestCommitUnitResults:
    """Tests for _commit_unit_results_io — per-unit ``roms`` upsert via ``Rom.synced``."""

    def test_commit_upserts_rom_from_pending(self, plugin):
        """A unit's acked ROM is upserted into ``uow.roms`` from its pending entry."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_name": "Game Boy",
                "platform_slug": "gb",
                "cover_path": "",
                "igdb_id": 555,
                "sgdb_id": 999,
                "ra_id": 777,
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

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

    def test_commit_records_applied_launch_options_for_binding_target(self, plugin):
        """A binding target this cycle records the launch command the frontend wrote
        onto the shortcut, so the next sync skips the now-correct shortcut (#1383)."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_slug": "n64",
                "cover_path": "",
                "launch_options": "flatpak run net.retrodeck.retrodeck /game.z64",
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.applied_launch_options == "flatpak run net.retrodeck.retrodeck /game.z64"

    def test_commit_preserves_applied_for_unacked_row(self, plugin):
        """A row committed this chunk but NOT acked (a skipped-unchanged item riding
        chunk 0's leftover) keeps its binding AND its recorded applied state — save()
        excludes applied_launch_options, so the un-re-acked value is never wiped."""
        uow = plugin._uow
        _seed_rom(uow, 43, app_id=100043, platform_slug="n64", name="Keep")
        with uow:
            uow.roms.set_applied_launch_options(43, "flatpak run … /keep.z64")
        _stage(
            plugin._sync_service._box,
            43,
            {"name": "Keep", "fs_name": "keep.z64", "platform_slug": "n64", "cover_path": "", "launch_options": ""},
            emitted=False,
        )

        # Empty ack — rom 43 is not a binding target this cycle, but its row commits.
        plugin._sync_service._reporter._commit_unit_results_io({}, [{"id": 43}])

        with uow:
            rom = uow.roms.get(43)
        assert rom is not None
        assert rom.shortcut_app_id == 100043
        assert rom.applied_launch_options == "flatpak run … /keep.z64"

    def test_commit_stamps_confirmed_cover_source(self, plugin):
        """A fingerprint the artwork layer confirmed for this unit (staged in
        ``pending_cover_sources``) is persisted on the upserted row (#1386)."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )
        plugin._sync_service._box.pending_cover_sources = {42: "/cover/big.png?ts=2026-07-11 12:00:00"}

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.cover_source == "/cover/big.png?ts=2026-07-11 12:00:00"

    def test_commit_preserves_existing_cover_source_when_unconfirmed(self, plugin):
        """A row whose cover was NOT confirmed this unit (failed download, or a
        sibling the download never touched) keeps its persisted fingerprint —
        the fresh fetch string is never blindly stamped, so the change is
        retried next sync (#1386)."""
        uow = plugin._uow
        _seed_rom(uow, 42, app_id=100001, platform_slug="n64", name="Game")
        with uow:
            rom = uow.roms.get(42)
            rom.adopt_cover_source("/cover/big.png?ts=2026-01-01 00:00:00")
            uow.roms.save(rom)
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )
        assert plugin._sync_service._box.pending_cover_sources == {}

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.cover_source == "/cover/big.png?ts=2026-01-01 00:00:00"

    def test_commit_stamps_confirmed_source_for_unacked_row(self, plugin):
        """The fingerprint records the CACHE state, not applied frontend state:
        a confirmed download whose shortcut was never acked still persists
        (the cache file was written regardless of the ack)."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )
        plugin._sync_service._box.pending_cover_sources = {42: "/cover/big.png?ts=2026-07-11 12:00:00"}

        # Empty ack — rom 42's shortcut never landed, its row commits unbound.
        plugin._sync_service._reporter._commit_unit_results_io({}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.shortcut_app_id is None
        assert rom.cover_source == "/cover/big.png?ts=2026-07-11 12:00:00"

    def test_commit_persists_platform_stamp_atomically(self, plugin):
        """A passed ``platform_stamp`` lands in the SAME committed UoW as the rom
        upsert — the per-platform completion stamp is atomic with the chunk (ADR-0023)."""
        from domain.platform_sync_state import PlatformSyncState

        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )
        stamp = PlatformSyncState.stamp(platform_slug="n64", at="2026-01-01T00:00:00+00:00", rom_count=7)

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}], stamp)

        assert uow.committed is True
        with uow:
            assert uow.roms.get(42) is not None  # chunk rom upserted
            loaded = uow.platform_sync_state.get("n64")
        assert loaded is not None
        assert loaded.rom_count == 7
        assert loaded.completed_at == "2026-01-01T00:00:00+00:00"

    def test_commit_without_stamp_writes_no_platform_state(self, plugin):
        """A commit with the default ``platform_stamp=None`` (non-final chunk, or a
        collection/late-ack path) leaves ``platform_sync_state`` untouched."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            assert uow.platform_sync_state.get("n64") is None

    def test_commit_persists_collection_stamp_atomically(self, plugin):
        """A passed ``collection_stamp`` lands in the SAME committed UoW as the rom
        upsert — the per-collection completion stamp is atomic with the chunk (#742)."""
        from domain.collection_sync_state import CollectionSyncState

        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )
        stamp = CollectionSyncState.stamp(
            collection_id="7",
            collection_kind="user",
            updated_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:05:00+00:00",
            rom_count=1,
            member_rom_ids=(42,),
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}], None, stamp)

        assert uow.committed is True
        with uow:
            assert uow.roms.get(42) is not None  # chunk rom upserted
            loaded = uow.collection_sync_state.get("7", "user")
        assert loaded is not None
        assert loaded.rom_count == 1
        assert loaded.member_rom_ids == (42,)
        assert loaded.completed_at == "2026-01-01T00:05:00+00:00"

    def test_commit_without_collection_stamp_writes_no_collection_state(self, plugin):
        """The default ``collection_stamp=None`` (platform / non-final / late-ack path)
        leaves ``collection_sync_state`` untouched."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "n64", "cover_path": ""},
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            assert uow.collection_sync_state.get("7", "user") is None

    def test_commit_persists_version_metadata_from_pending(self, plugin):
        """The sibling-group key + version dimensions ride the pending entry onto
        the upserted ``Rom`` (#1295)."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_slug": "gb",
                "cover_path": "",
                "sibling_group_key": "igdb:3404:57",
                "regions": ["USA", "Europe"],
                "languages": ["En"],
                "revision": "1",
                "tags": ["Demo"],
                "is_main_sibling": True,
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.sibling_group_key == "igdb:3404:57"
        assert rom.regions == ("USA", "Europe")
        assert rom.languages == ("En",)
        assert rom.revision == "1"
        assert rom.tags == ("Demo",)
        assert rom.is_main_sibling is True

    def test_commit_defaults_version_metadata_when_pending_omits_it(self, plugin):
        """A pending entry with no version fields upserts a Rom carrying the
        aggregate defaults — never raises on the missing keys."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_slug": "gb",
                "cover_path": "",
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.sibling_group_key is None
        assert rom.regions == ()
        assert rom.revision == ""
        assert rom.is_main_sibling is False

    def test_commit_stamps_cover_path_when_present(self, plugin):
        """A finalized cover path is recorded on the upserted ROM row."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_slug": "gb",
                "cover_path": "/covers/staging.png",
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            rom = uow.roms.get(42)
        # No grid dir in the test stub → finalize returns the path unchanged.
        assert rom.cover_path == "/covers/staging.png"

    def test_commit_skips_invalid_rom_keeps_rest(self, plugin):
        """An invariant ValueError (missing platform_slug) skips one ROM; the rest still commit."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            10,
            {
                "name": "Bad",
                "fs_name": "bad.z64",
                "platform_slug": "",  # invalid — Rom.synced raises ValueError
                "cover_path": "",
            },
        )
        _stage(
            plugin._sync_service._box,
            20,
            {
                "name": "Good",
                "fs_name": "good.z64",
                "platform_slug": "gb",
                "cover_path": "",
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"10": 1010, "20": 1020}, [{"id": 10}, {"id": 20}])

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

        # The re-sync's built entry (live RomM fetch) lacks sgdb_id / ra_id /
        # cover_path entirely.
        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_slug": "gb",
                "cover_path": "",
                "igdb_id": 555,
                "sgdb_id": None,
                "ra_id": None,
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

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

        _stage(
            plugin._sync_service._box,
            42,
            {
                "name": "Game",
                "fs_name": "game.z64",
                "platform_slug": "gb",
                "cover_path": "",
                "sgdb_id": 9999,
            },
        )

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, [{"id": 42}])

        with uow:
            assert uow.roms.get(42).sgdb_id == 9999


class TestGroupAwareCommit:
    """Group-aware per-unit commit (ADR-0021): persist every fetched sibling,
    bind only representatives, and move a binding on a rebind."""

    def test_persists_non_representative_sibling_unbound(self, plugin):
        """A fetched sibling that is not the emitted representative lands a ``roms``
        row for its identity + version, but carries no shortcut binding."""
        uow = plugin._uow
        box = plugin._sync_service._box
        rep = {
            "name": "Game (USA)",
            "fs_name": "usa.z64",
            "platform_slug": "n64",
            "cover_path": "",
            "sibling_group_key": "g",
        }
        sibling = {
            "name": "Game (JP)",
            "fs_name": "jp.z64",
            "platform_slug": "n64",
            "cover_path": "",
            "sibling_group_key": "g",
        }
        box.pending_sync = {10: rep}  # only rom 10 is emitted
        box.pending_all_roms = {10: rep, 11: sibling}

        plugin._sync_service._reporter._commit_unit_results_io({"10": 9001}, [{"id": 10}, {"id": 11}])

        with uow:
            rep_row = uow.roms.get(10)
            sibling_row = uow.roms.get(11)
        assert rep_row is not None and rep_row.shortcut_app_id == 9001
        assert sibling_row is not None
        assert sibling_row.shortcut_app_id is None
        assert sibling_row.name == "Game (JP)"
        assert sibling_row.sibling_group_key == "g"

    def test_non_acked_bound_sibling_keeps_its_existing_binding(self, plugin):
        """A bound sibling NOT acked this cycle keeps its existing binding — the
        `_persist_synced_rom` fallback (`existing.shortcut_app_id`) is load-bearing.

        Regression guard for the single most dangerous line in #1296: a
        grandfathered group has rom 11 already bound (app 7777) while rom 10 is the
        emitted representative bound this cycle (app 9001). Only rom 10 is acked, so
        the commit must PRESERVE rom 11's 7777 from the existing row — the broken
        `binding.get(rom_id)` variant (no existing fallback) would silently unbind
        it, orphaning a live shortcut. ``test_persists_non_representative_sibling_unbound``
        seeds no prior row for rom 11, so it passes with or without the fallback;
        this one seeds the prior binding and only passes with it.
        """
        uow = plugin._uow
        # rom 11 already carries a shortcut (grandfathered duplicate of group "g").
        _seed_rom(uow, 11, app_id=7777, platform_slug="n64", name="Game (JP)", group_key="g")
        box = plugin._sync_service._box
        rep = {
            "name": "Game (USA)",
            "fs_name": "usa.z64",
            "platform_slug": "n64",
            "cover_path": "",
            "sibling_group_key": "g",
        }
        sibling = {
            "name": "Game (JP)",
            "fs_name": "jp.z64",
            "platform_slug": "n64",
            "cover_path": "",
            "sibling_group_key": "g",
        }
        box.pending_sync = {10: rep}  # only rom 10 is emitted / acked this cycle
        box.pending_all_roms = {10: rep, 11: sibling}

        plugin._sync_service._reporter._commit_unit_results_io({"10": 9001}, [{"id": 10}, {"id": 11}])

        with uow:
            rep_row = uow.roms.get(10)
            sibling_row = uow.roms.get(11)
        # rom 10 binds the acked appId; rom 11 KEEPS its pre-existing binding.
        assert rep_row is not None and rep_row.shortcut_app_id == 9001
        assert sibling_row is not None and sibling_row.shortcut_app_id == 7777

    def test_rebind_moves_binding_to_representative(self, plugin):
        """A rebind entry (keyed to the vanished bound sibling, carrying
        ``bind_rom_id``) moves the DB binding onto the surviving representative:
        the appId survives, the old sibling is unbound (ADR-0021 §2)."""
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=5000, platform_slug="n64", name="Game (USA)", group_key="g")
        box = plugin._sync_service._box
        # The emitted rebind entry is keyed to the vanished bound sibling (rom 1)
        # and names the representative (rom 2) in bind_rom_id.
        box.pending_sync = {1: {"name": "Game (USA)", "cover_path": "", BIND_ROM_ID_KEY: 2}}
        box.pending_all_roms = {
            2: {
                "name": "Game (JP)",
                "fs_name": "jp.z64",
                "platform_slug": "n64",
                "cover_path": "",
                "sibling_group_key": "g",
            },
        }

        # The frontend reused the old shortcut's appId (5000) under rom_id 1.
        plugin._sync_service._reporter._commit_unit_results_io({"1": 5000}, [{"id": 2}])

        with uow:
            rep = uow.roms.get(2)
            old = uow.roms.get(1)
        # The binding moved onto the representative, which keeps its own real name.
        assert rep is not None
        assert rep.shortcut_app_id == 5000
        assert rep.name == "Game (JP)"
        assert rep.sibling_group_key == "g"
        # The vanished sibling is unbound by the collision-safe save (row survives).
        assert old is not None
        assert old.shortcut_app_id is None


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
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "gb", "cover_path": ""},
        )
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
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "gb", "cover_path": ""},
        )
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
        _stage(
            plugin._sync_service._box,
            42,
            {"name": "Game", "fs_name": "game.z64", "platform_slug": "gb", "cover_path": ""},
        )
        acked = [{"id": 42, "name": "Thin"}]  # no metadatum

        plugin._sync_service._reporter._commit_unit_results_io({"42": 100001}, acked)

        assert uow.committed is True
        with uow:
            assert uow.roms.get(42) is not None
            assert uow.rom_metadata.get(42) is None

    def test_falsy_metadatum_writes_no_metadata_row(self, plugin):
        """``metadatum: None`` and ``metadatum: {}`` both skip the metadata stamp."""
        uow = plugin._uow
        _stage(
            plugin._sync_service._box,
            10,
            {"name": "A", "fs_name": "a.z64", "platform_slug": "gb", "cover_path": ""},
        )
        _stage(
            plugin._sync_service._box,
            20,
            {"name": "B", "fs_name": "b.z64", "platform_slug": "gb", "cover_path": ""},
        )
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


class TestAckMatchesActiveUnit:
    """The reporter's ack identity guard now spans run + unit + chunk (#1025).

    A chunked apply dispatches one chunk at a time; an ack must echo back the
    active chunk index or it is rejected, so a crash-late ack for a superseded
    chunk can never be credited to the chunk in flight.
    """

    def test_matches_when_run_unit_and_chunk_all_agree(self, plugin):
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 5
        box.active_chunk_index = 2
        assert plugin._sync_service._reporter._ack_matches_active_unit("run-1", 5, 2) is True

    def test_rejects_wrong_chunk_index(self, plugin):
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 5
        box.active_chunk_index = 2
        # Run + unit agree, but the ack is for a stale chunk.
        assert plugin._sync_service._reporter._ack_matches_active_unit("run-1", 5, 1) is False

    def test_rejects_when_no_active_chunk(self, plugin):
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 5
        box.active_chunk_index = None  # no chunk in flight (cancelled / committed)
        assert plugin._sync_service._reporter._ack_matches_active_unit("run-1", 5, 0) is False


class TestClearSyncCache:
    """Tests for clear_sync_cache() — Force Full Sync clears the per-platform stamps
    and the recorded launch options but PRESERVES the run history, so the Last-sync
    display stays truthful (#1318)."""

    def test_preserves_completed_run_so_last_sync_survives(self, plugin):
        """After clear, the completed run remains → get_latest_completed is set and last_sync still reads its time."""
        from domain.sync_run import SyncRun

        uow = plugin._uow
        run = SyncRun.start(id="run-1", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
        run.complete("2025-01-01T00:10:00", ["N64"], [])
        with uow:
            uow.sync_runs.save(run)

        result = plugin._sync_service.clear_sync_cache()

        assert result["success"] is True
        with uow:
            assert uow.sync_runs.get_latest_completed() is not None
        # The derived last_sync read still surfaces the completed run — no reset to "Never".
        stats = plugin._sync_service.get_sync_stats()
        assert stats["last_sync"] == "2025-01-01T00:10:00"

    def test_leaves_run_history_untouched(self, plugin):
        """Force Full Sync deletes no runs — a completed run AND a running run both
        survive the reset (it clears stamps + recorded launch options only)."""
        from domain.sync_run import SyncRun

        uow = plugin._uow
        completed = SyncRun.start(id="run-done", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
        completed.complete("2025-01-01T00:10:00", ["N64"], [])
        running = SyncRun.start(id="run-live", at="2025-02-01T00:00:00", platforms_planned=1, roms_planned=1)
        with uow:
            uow.sync_runs.save(completed)
            uow.sync_runs.save(running)

        plugin._sync_service.clear_sync_cache()

        with uow:
            assert uow.sync_runs.get("run-done") is not None
            assert uow.sync_runs.get_running() is not None

    def test_resets_recorded_launch_options_so_the_next_apply_skips_nothing(self, plugin):
        """Force Full Sync must force past the per-item delta skip (ADR-0025).

        The recorded launch command is the skip's evidence; resetting it to NULL
        (never matches a target) makes the next apply re-touch every shortcut —
        the repair path for Steam-side drift the recorded value cannot see.
        """
        uow = plugin._uow
        _seed_rom(uow, 7, app_id=111, platform_slug="n64")
        with uow:
            uow.roms.set_applied_launch_options(7, "flatpak run app 'x.zip'")

        plugin._sync_service.clear_sync_cache()

        with uow:
            assert uow.roms.get(7).applied_launch_options is None

    def test_preserves_last_sync_and_a_newer_cancelled_attempt(self, plugin):
        """After Force Full Sync, BOTH a completed run's last_sync AND a newer
        cancelled run's last-attempt hint survive (#1318).

        The old behaviour deleted every terminal run so the display blanked to
        "Never" right after a reset. Preserving history keeps the honest
        "17:48 (cancelled)"-style display; the force still re-fetches (stamps
        cleared) without touching what the panel shows.
        """
        from domain.sync_run import SyncRun

        uow = plugin._uow
        completed = SyncRun.start(id="run-ok", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
        completed.complete("2025-01-01T00:10:00", ["N64"], [])
        cancelled = SyncRun.start(id="run-x", at="2025-01-01T01:00:00", platforms_planned=1, roms_planned=1)
        cancelled.mark_cancelled("2025-01-01T01:05:00", reason="user")
        with uow:
            uow.sync_runs.save(completed)
            uow.sync_runs.save(cancelled)

        plugin._sync_service.clear_sync_cache()

        stats = plugin._sync_service.get_sync_stats()
        assert stats["last_sync"] == "2025-01-01T00:10:00"
        assert stats["last_attempt"] == {"finished_at": "2025-01-01T01:05:00", "status": "cancelled"}

    def test_preserves_a_lone_failed_attempt_across_the_reset(self, plugin):
        """The #1318 core case: with only a non-completed run (a resume situation),
        Force Full Sync no longer blanks the display to "Never" — the interrupted
        attempt survives so last_attempt still surfaces it.
        """
        from domain.sync_run import SyncRun

        uow = plugin._uow
        interrupted = SyncRun.start(id="run-i", at="2025-01-01T00:00:00", platforms_planned=1, roms_planned=1)
        interrupted.mark_interrupted("2025-01-01T00:05:00", reason="external death")
        with uow:
            uow.sync_runs.save(interrupted)

        plugin._sync_service.clear_sync_cache()

        stats = plugin._sync_service.get_sync_stats()
        assert stats["last_sync"] is None
        assert stats["last_attempt"] == {"finished_at": "2025-01-01T00:05:00", "status": "interrupted"}

    def test_clears_platform_completion_stamps(self, plugin):
        """Force Full Sync also drops the per-platform completion stamps (ADR-0023).

        Each stamp is its own effective ``last_sync``; leaving them would let an
        unchanged platform still skip after the user asked for a full re-fetch.
        """
        from domain.platform_sync_state import PlatformSyncState

        uow = plugin._uow
        with uow:
            uow.platform_sync_state.save(
                PlatformSyncState.stamp(platform_slug="n64", at="2025-01-01T00:00:00", rom_count=100)
            )
            uow.platform_sync_state.save(
                PlatformSyncState.stamp(platform_slug="snes", at="2025-01-01T00:00:00", rom_count=200)
            )

        plugin._sync_service.clear_sync_cache()

        with uow:
            assert uow.platform_sync_state.get("n64") is None
            assert uow.platform_sync_state.get("snes") is None

    def test_clears_collection_completion_stamps(self, plugin):
        """Force Full Sync also drops the per-collection completion stamps (#742).

        Each collection stamp is its own effective ``last_sync``; leaving them would
        let an unchanged collection still skip after the user asked for a full
        re-fetch.
        """
        from domain.collection_sync_state import CollectionSyncState

        uow = plugin._uow
        with uow:
            uow.collection_sync_state.save(
                CollectionSyncState.stamp(
                    collection_id="7",
                    collection_kind="user",
                    updated_at="2025-01-01T00:00:00",
                    completed_at="2025-01-01T00:05:00",
                    rom_count=2,
                    member_rom_ids=(1, 2),
                )
            )
            uow.collection_sync_state.save(
                CollectionSyncState.stamp(
                    collection_id="9",
                    collection_kind="smart",
                    updated_at="2025-01-01T00:00:00",
                    completed_at="2025-01-01T00:05:00",
                    rom_count=1,
                    member_rom_ids=(3,),
                )
            )

        plugin._sync_service.clear_sync_cache()

        with uow:
            assert uow.collection_sync_state.get("7", "user") is None
            assert uow.collection_sync_state.get("9", "smart") is None


class TestFinalizePerUnitRun:
    """SyncReporter.finalize_per_unit_run (stale unbind + sync_collections) and the
    separate emit_sync_complete (terminal sync_complete + progress frame, emitted
    LAST by the orchestrator after the SyncRun write, #39)."""

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
            platform_names={"n64": "Nintendo 64"},
        )

        collections_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_collections"]
        payload = collections_events[0][0][1]
        # rom 2 is unbound AND has no sibling group → excluded; only rom 1 appears.
        assert payload["romm_collection_app_ids"] == {"Faves": [1001]}

    @pytest.mark.asyncio
    async def test_romm_collection_group_fallback_maps_unbound_sibling(self, plugin):
        """A collection membership on an UNBOUND sibling maps to its group's bound
        sibling's appId (ADR-0021) — collecting any version collects the game."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        # A bound representative + an unbound sibling in the SAME group.
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="Game (USA)", group_key="g")
        _seed_rom(uow, 2, app_id=None, platform_slug="n64", name="Game (JP)", group_key="g")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={"Faves": [2]},  # the UNBOUND sibling is collected
            pending_platform_rom_ids={1},
            platform_names={"n64": "Nintendo 64"},
        )

        payload = next(c for c in decky.emit.call_args_list if c[0][0] == "sync_collections")[0][1]
        # rom 2 is unbound, but its group's bound sibling (rom 1 → 1001) stands in.
        assert payload["romm_collection_app_ids"] == {"Faves": [1001]}

    @pytest.mark.asyncio
    async def test_romm_collection_dedups_group_members_onto_one_shortcut(self, plugin):
        """A collection holding several siblings of one group yields the group's
        single shortcut appId once, not duplicated."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="Game (USA)", group_key="g")
        _seed_rom(uow, 2, app_id=None, platform_slug="n64", name="Game (JP)", group_key="g")

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={"Faves": [1, 2]},  # BOTH siblings collected
            pending_platform_rom_ids={1},
            platform_names={"n64": "Nintendo 64"},
        )

        payload = next(c for c in decky.emit.call_args_list if c[0][0] == "sync_collections")[0][1]
        assert payload["romm_collection_app_ids"] == {"Faves": [1001]}

    @pytest.mark.asyncio
    async def test_emit_sync_complete_terminal(self, plugin):
        import decky

        decky.emit.reset_mock()

        await plugin._sync_service._reporter.emit_sync_complete(
            platform_app_ids={},
            romm_collection_app_ids={},
            total_games=0,
            cancelled=False,
            interrupt_reason=None,
            restart_recommended=False,
        )

        complete_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert len(complete_events) == 1
        assert "cancelled" not in complete_events[0][0][1]
        assert "interrupted" not in complete_events[0][0][1]
        # The last-run memory delta is retained in the box and read via
        # get_session_budget_status, NOT ridden on the sync_complete wire (#1383 LOW-3).
        assert "memory_delta_kb" not in complete_events[0][0][1]

    @pytest.mark.asyncio
    async def test_emit_sync_complete_carries_restart_recommended_on_clean_run(self, plugin):
        import decky

        decky.emit.reset_mock()

        await plugin._sync_service._reporter.emit_sync_complete(
            platform_app_ids={},
            romm_collection_app_ids={},
            total_games=0,
            cancelled=False,
            interrupt_reason=None,
            restart_recommended=True,
        )

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete and complete[-1]["restart_recommended"] is True

    @pytest.mark.asyncio
    async def test_emit_sync_complete_cancelled_frame_says_cancelled_when_not_interrupted(self, plugin):
        """A user cancel (box.run_interrupted False) → the terminal CANCELLED frame
        leads with 'Sync cancelled:' and the payload carries no ``interrupted``."""
        import decky

        decky.emit.reset_mock()
        plugin._sync_service._box.run_interrupted = False

        await plugin._sync_service._reporter.emit_sync_complete(
            platform_app_ids={},
            romm_collection_app_ids={},
            total_games=3,
            cancelled=True,
            interrupt_reason=None,
            restart_recommended=False,
        )

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert "interrupted" not in complete[-1]
        progress = plugin._sync_service._sync_progress
        assert progress["stage"] == "cancelled"
        assert progress["message"].startswith("Sync cancelled: ")

    @pytest.mark.asyncio
    async def test_emit_sync_complete_frame_says_interrupted_when_run_interrupted(self, plugin):
        """A heartbeat-timeout run routes through the same cancelled emit; with
        box.run_interrupted set the payload carries ``interrupted: True`` and the
        terminal frame leads with 'Sync interrupted:' (stage stays CANCELLED — no
        new SyncStage). The frame's denominator is the PLANNED total from the
        box, not the bound-ROM count (#1384)."""
        import decky

        decky.emit.reset_mock()
        plugin._sync_service._box.run_interrupted = True
        plugin._sync_service._box.run_total_items = 10

        await plugin._sync_service._reporter.emit_sync_complete(
            platform_app_ids={},
            romm_collection_app_ids={},
            total_games=3,
            cancelled=True,
            interrupt_reason=None,
            restart_recommended=False,
        )

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete[-1]["interrupted"] is True
        progress = plugin._sync_service._sync_progress
        assert progress["stage"] == "cancelled"
        assert progress["message"] == "Sync interrupted: 3 of 10 games processed"
        assert progress["total"] == 10

    @pytest.mark.asyncio
    async def test_emit_sync_complete_uses_interrupt_reason_verbatim(self, plugin):
        """A budget-pause interrupt_reason rides the payload AND becomes the terminal
        frame message verbatim (resume-friendly guidance, #1383)."""
        import decky

        decky.emit.reset_mock()

        await plugin._sync_service._reporter.emit_sync_complete(
            platform_app_ids={},
            romm_collection_app_ids={},
            total_games=2,
            cancelled=True,
            interrupt_reason="Sync paused: restart Steam, then Resume Sync.",
            restart_recommended=False,
        )

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete[-1]["interrupt_reason"] == "Sync paused: restart Steam, then Resume Sync."
        # A budget pause sets run_paused + interrupt_reason, never run_interrupted —
        # the payload must not read as a heartbeat interrupt (#1384).
        assert "interrupted" not in complete[-1]
        assert plugin._sync_service._sync_progress["message"] == "Sync paused: restart Steam, then Resume Sync."

    @pytest.mark.asyncio
    async def test_emit_sync_complete_frame_total_falls_back_to_bound_count(self, plugin):
        """With no planned total in the box (``run_total_items`` None — pre-plan or
        the box wiped by a plugin reload), the terminal frame's denominator falls
        back to the bound-ROM registry count (#1384)."""
        import decky

        decky.emit.reset_mock()
        uow = plugin._uow
        _seed_rom(uow, 1, app_id=1001, platform_slug="n64", name="Game A")
        _seed_rom(uow, 2, app_id=1002, platform_slug="n64", name="Game B")
        box = plugin._sync_service._box
        box.run_interrupted = True
        assert box.run_total_items is None

        await plugin._sync_service._reporter.emit_sync_complete(
            platform_app_ids={},
            romm_collection_app_ids={},
            total_games=1,
            cancelled=True,
            interrupt_reason=None,
            restart_recommended=False,
        )

        progress = plugin._sync_service._sync_progress
        assert progress["message"] == "Sync interrupted: 1 of 2 games processed"
        assert progress["total"] == 2

    @pytest.mark.asyncio
    async def test_finalize_does_not_reset_run_lifecycle(self, plugin):
        """finalize_per_unit_run + emit_sync_complete never touch the run lifecycle (#1202).

        The IDLE/None reset lives in the orchestrator's single run-scoped
        ``finally: box.finish_run(run_id)``; the reporter only unbinds/collects and
        emits, leaving ``sync_state`` / ``current_sync_id`` untouched.
        """
        import decky

        from domain.sync_state import SyncState

        decky.emit.reset_mock()
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "sync-xyz"

        await plugin._sync_service._reporter.finalize_per_unit_run(
            pending_collection_memberships={},
            pending_platform_rom_ids=set(),
            platform_names={},
        )

        assert plugin._sync_service._sync_state == SyncState.RUNNING
        assert plugin._sync_service._current_sync_id == "sync-xyz"

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
            platform_names={"n64": "Nintendo 64"},
            stale_rom_ids=[2, 3],
        )

        stats = await plugin.get_sync_stats()
        assert stats["roms"] == 1
        assert stats["total_shortcuts"] == 1
