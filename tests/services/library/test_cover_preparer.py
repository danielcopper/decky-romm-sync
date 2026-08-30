"""Tests for CoverPreparer — one work unit's covers, made ready before the apply.

The preparer is reached through the library façade
(``plugin._sync_service._cover_preparer``) so every test drives the same
instance the orchestrator holds, over the shared state box the cancel signal
lands on.

Two levels are covered. The **delegation** and **attach** tests call the
preparer directly against a mocked download: the first pins what the run binds
into each call — the progress position, the unit label, and an ``is_cancelling``
closure that tracks the live sync state rather than a snapshot of it — and the
second which ROMs a unit's delta asks covers for, what each emitted entry
carries away, and which fingerprints the commit gets back. The **invalidation
pass** is driven end-to-end through ``SyncOrchestrator._do_sync_per_unit``
against the real ``ArtworkService`` (real cover-cache file I/O under
``tmp_path``) and the seeded ``FakeRommApi``, because what the pass is for is
observable only where it lands: the ``cover_refreshes`` list on the unit's first
``sync_apply_unit`` chunk, and the fingerprint persisted beside it.

``_wait_for_unit_complete`` stands in for a frontend ``report_unit_results``
callback no test exercises; ``_download_artwork`` stands in for the SteamGridDB
pipeline wherever the test is about the refresh pass rather than the download.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.sync_diff import BIND_ROM_ID_KEY
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit

# conftest.py patches decky before this import
from tests.services.library._helpers import _fake_wait_set_event, _seed_platform, _seed_rom_row, _use_fake_romm


class TestDownloadArtworkDelegation:
    """Tests for _download_artwork."""

    @pytest.mark.asyncio
    async def test_delegates_to_artwork_manager(self, plugin):
        """When _artwork is bound, the call is forwarded with progress + cancel hooks."""
        fake_download = AsyncMock(return_value={1: "/path/a.png", 2: "/path/b.png"})
        plugin._sync_service._cover_preparer._artwork = MagicMock()
        plugin._sync_service._cover_preparer._artwork.download_artwork = fake_download

        roms = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        result = await plugin._sync_service._cover_preparer._download_artwork(
            roms, progress_step=3, progress_total_steps=7
        )

        assert result == {1: "/path/a.png", 2: "/path/b.png"}
        fake_download.assert_called_once()
        call_kwargs = fake_download.call_args.kwargs
        assert call_kwargs["progress_step"] == 3
        assert call_kwargs["progress_total_steps"] == 7
        # is_cancelling closure reflects the live sync_state.
        is_cancelling = call_kwargs["is_cancelling"]
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        assert is_cancelling() is False
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
        assert is_cancelling() is True

    @pytest.mark.asyncio
    async def test_forwards_unit_label_to_artwork(self, plugin):
        """The unit display name is threaded through as the cover-progress label."""
        fake_download = AsyncMock(return_value={})
        plugin._sync_service._cover_preparer._artwork = MagicMock()
        plugin._sync_service._cover_preparer._artwork.download_artwork = fake_download

        await plugin._sync_service._cover_preparer._download_artwork(
            [{"id": 1, "name": "A"}], progress_step=1, progress_total_steps=1, label="Game Boy Advance"
        )

        assert fake_download.call_args.kwargs["label"] == "Game Boy Advance"


class TestAttachUnitCoverPaths:
    """Tests for attach_unit_cover_paths — what the download is asked for, what
    the emitted entries carry away from it, and what the commit gets back.

    ``_download_artwork`` is mocked here: the question is which ROMs are handed
    to it and how its answer is distributed, not what it fetches.
    """

    _UNIT = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)

    @staticmethod
    def _preparer(plugin, cover_paths):
        preparer = plugin._sync_service._cover_preparer
        preparer._download_artwork = AsyncMock(return_value=cover_paths)
        return preparer

    @pytest.mark.asyncio
    async def test_stamps_each_emitted_entry_with_its_downloaded_cover(self, plugin):
        """Every emitted entry carries the downloaded path; a ROM the download did
        not resolve carries the empty string, never a missing key."""
        preparer = self._preparer(plugin, {10: "/cache/10.png"})
        unit_roms = [{"id": 10, "name": "A"}, {"id": 11, "name": "B"}]
        emitted = [{"rom_id": 10}, {"rom_id": 11}]

        await preparer.attach_unit_cover_paths(self._UNIT, unit_roms, emitted, unit_index=2, total_units=5)

        assert emitted == [{"rom_id": 10, "cover_path": "/cache/10.png"}, {"rom_id": 11, "cover_path": ""}]

    @pytest.mark.asyncio
    async def test_fetches_only_the_roms_that_get_a_shortcut(self, plugin):
        """A fetched ROM with no emitted entry is never downloaded — no eager covers
        for versions with no shortcut — and the unit's progress position rides along."""
        preparer = self._preparer(plugin, {})
        unit_roms = [{"id": 10, "name": "A"}, {"id": 11, "name": "B"}]

        await preparer.attach_unit_cover_paths(self._UNIT, unit_roms, [{"rom_id": 11}], unit_index=2, total_units=5)

        assert preparer._download_artwork.call_args.args[0] == [{"id": 11, "name": "B"}]
        kwargs = preparer._download_artwork.call_args.kwargs
        assert (kwargs["progress_step"], kwargs["progress_total_steps"], kwargs["label"]) == (3, 5, "N64")

    @pytest.mark.asyncio
    async def test_rebind_entry_takes_the_representative_cover(self, plugin):
        """A rebind entry's cover comes from the representative it binds
        (``BIND_ROM_ID_KEY``), which is also the ROM the download is asked for."""
        preparer = self._preparer(plugin, {10: "/cache/10.png"})
        unit_roms = [{"id": 10, "name": "A (USA)"}, {"id": 11, "name": "A (JP)"}]
        emitted = [{"rom_id": 11, BIND_ROM_ID_KEY: 10}]

        await preparer.attach_unit_cover_paths(self._UNIT, unit_roms, emitted, unit_index=0, total_units=1)

        assert preparer._download_artwork.call_args.args[0] == [{"id": 10, "name": "A (USA)"}]
        assert emitted[0]["cover_path"] == "/cache/10.png"

    @pytest.mark.asyncio
    async def test_empty_delta_downloads_nothing(self, plugin):
        preparer = self._preparer(plugin, {})

        assert (
            await preparer.attach_unit_cover_paths(
                self._UNIT, [{"id": 10, "name": "A"}], [], unit_index=0, total_units=1
            )
            == {}
        )
        preparer._download_artwork.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirmed_fingerprints_cover_only_resolved_roms(self, plugin):
        """The commit gets back the source for every ROM the download resolved —
        the small cover when there is no large one — and nothing for a ROM whose
        download failed (#1386)."""
        preparer = self._preparer(plugin, {10: "/cache/10.png", 11: "/cache/11.png"})
        unit_roms = [
            {"id": 10, "name": "A", "path_cover_large": "/big.png?ts=1"},
            {"id": 11, "name": "B", "path_cover_small": "/small.png?ts=2"},
            {"id": 12, "name": "C", "path_cover_large": "/big.png?ts=3"},
        ]
        emitted = [{"rom_id": 10}, {"rom_id": 11}, {"rom_id": 12}]

        confirmed = await preparer.attach_unit_cover_paths(self._UNIT, unit_roms, emitted, unit_index=0, total_units=1)

        assert confirmed == {10: "/big.png?ts=1", 11: "/small.png?ts=2"}

    @pytest.mark.asyncio
    async def test_applied_source_overrides_the_roms_own_cover_source(self, plugin):
        """When ArtworkService applied a different source than the ROM's fresh
        ``path_cover`` — the #1450 ``url_cover`` fallback — the accumulator's value
        is what the commit persists."""
        preparer = plugin._sync_service._cover_preparer

        async def download(_roms, *, applied_sources: dict[int, str], **_kwargs):
            applied_sources[10] = "https://cdn.example/fallback.png"
            return {10: "/cache/10.png"}

        preparer._download_artwork = download
        unit_roms = [{"id": 10, "name": "A", "path_cover_large": "/big.png?ts=1"}]

        confirmed = await preparer.attach_unit_cover_paths(
            self._UNIT, unit_roms, [{"rom_id": 10}], unit_index=0, total_units=1
        )

        assert confirmed == {10: "https://cdn.example/fallback.png"}


class TestCoverRefreshPass:
    """The #1386 cover-cache invalidation pass wired through the per-unit apply.

    Drives the real ArtworkService (real cover-cache file I/O under tmp_path)
    against the seeded FakeRommApi, and asserts the refresh list rides the
    unit's first ``sync_apply_unit`` chunk while the fingerprints persist.
    """

    _OLD = "/cover/big.png?ts=2026-01-01 00:00:00"
    _NEW = "/cover/big.png?ts=2026-07-11 12:00:00"

    @staticmethod
    def _apply_setup(plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        plugin._sync_service._cover_preparer._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._chunk_dispatcher._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-cover"

    @staticmethod
    def _apply_unit_events():
        import decky

        return [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]

    @staticmethod
    def _cache_file(plugin, rom_id):
        from pathlib import Path

        return Path(plugin._artwork_service._cover_cache_dir) / f"{rom_id}.png"

    @pytest.mark.asyncio
    async def test_changed_cover_on_delta_skipped_rom_rides_first_chunk(self, plugin, fake_romm_api):
        # rom 10 is content-unchanged (delta-skipped: no shortcut emitted) but its
        # server cover source changed. The pass re-downloads the cache, persists
        # the fresh fingerprint, and the {rom_id, app_id} entry rides chunk 0 so
        # the frontend re-applies the tile.
        self._apply_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64", "path_cover_large": self._NEW}],
        )
        fake_romm_api.download_payloads[f"cover:{self._NEW}"] = b"fresh cover bytes"
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=self._OLD
        )

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 1
        assert events[0]["shortcuts"] == [], "the item stays delta-skipped — a cover change never re-applies it"
        assert events[0]["cover_refreshes"] == [{"rom_id": 10, "app_id": 1010}]
        # The cache file holds the fresh bytes and the fingerprint advanced.
        assert self._cache_file(plugin, 10).read_bytes() == b"fresh cover bytes"
        with plugin._uow as uow:
            assert uow.roms.get(10).cover_source == self._NEW

    @pytest.mark.asyncio
    async def test_null_fingerprint_adopts_without_refresh_entry(self, plugin, fake_romm_api):
        # A pre-#1386 row (fingerprint NULL) with an existing cache file adopts
        # the fresh fingerprint silently: no download, no refresh entry.
        self._apply_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64", "path_cover_large": self._NEW}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=None)
        cache = self._cache_file(plugin, 10)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"pre-existing cache")

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 1
        assert events[0]["cover_refreshes"] == []
        assert cache.read_bytes() == b"pre-existing cache", "NULL-adopt never re-downloads"
        assert all(name != "download_cover" for name, _a, _k in fake_romm_api.call_log)
        with plugin._uow as uow:
            assert uow.roms.get(10).cover_source == self._NEW

    @pytest.mark.asyncio
    async def test_refreshes_ride_only_the_first_chunk(self, plugin, fake_romm_api, monkeypatch):
        # Four changed items at chunk size 2 → two chunks; rom 1's cover also
        # changed. The refresh entry rides chunk 0 only; chunk 1 carries [].
        from services.library import chunk_dispatcher

        self._apply_setup(plugin, fake_romm_api)
        monkeypatch.setattr(chunk_dispatcher, "_APPLY_CHUNK_SIZE", 2)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {
                    "id": i,
                    "name": f"New {i}",
                    "fs_name": f"g{i}.z64",
                    **({"path_cover_large": self._NEW} if i == 1 else {}),
                }
                for i in range(1, 5)
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        for i in range(1, 5):
            # Old names → every item classifies "changed" and is emitted.
            _seed_rom_row(
                plugin,
                i,
                app_id=1000 + i,
                platform_slug="n64",
                name=f"Old {i}",
                fs_name=f"g{i}.z64",
                cover_source=self._OLD if i == 1 else None,
            )

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 2
        assert events[0]["cover_refreshes"] == [{"rom_id": 1, "app_id": 1001}]
        assert events[1]["cover_refreshes"] == []

    @pytest.mark.asyncio
    async def test_headroom_clips_refresh_list_before_emit(self, plugin, fake_romm_api):
        # A live RSS reading leaves headroom for exactly ONE transient cover after
        # the (empty) chunk's own cost: two refreshes clip to one — never a pause.
        from domain.session_budget import CLIFF_KB, COVER_TRANSIENT_KB

        self._apply_setup(plugin, fake_romm_api)
        # The run's FIRST chunk projects against the cliff; leave headroom for one
        # cover plus half of another so the allowance floor-divides to exactly 1.
        plugin._renderer_rss.rss_kb = CLIFF_KB - COVER_TRANSIENT_KB - COVER_TRANSIENT_KB // 2
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 1, "name": "A", "fs_name": "a.z64", "path_cover_large": "/a.png?ts=2026-07-11 12:00:00"},
                {"id": 2, "name": "B", "fs_name": "b.z64", "path_cover_large": "/b.png?ts=2026-07-11 12:00:00"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(
            plugin, 1, app_id=1001, platform_slug="n64", name="A", fs_name="a.z64", cover_source="/a.png?ts=old"
        )
        _seed_rom_row(
            plugin, 2, app_id=1002, platform_slug="n64", name="B", fs_name="b.z64", cover_source="/b.png?ts=old"
        )

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 1, "the refreshes must never pause the run"
        assert events[0]["cover_refreshes"] == [{"rom_id": 1, "app_id": 1001}], "clipped to the headroom allowance"
        # Both grid-side caches were still refreshed backend-side; only the
        # in-session tile push was clipped.
        with plugin._uow as uow:
            assert uow.roms.get(1).cover_source == "/a.png?ts=2026-07-11 12:00:00"
            assert uow.roms.get(2).cover_source == "/b.png?ts=2026-07-11 12:00:00"

    # ── delegation ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_refresh_delegates_to_artwork_manager(self, plugin):
        fake_refresh = AsyncMock(return_value=[{"rom_id": 1, "app_id": 10}])
        plugin._sync_service._cover_preparer._artwork = MagicMock()
        plugin._sync_service._cover_preparer._artwork.refresh_changed_covers = fake_refresh

        registry = {"1": {"app_id": 10, "cover_source": "/old.png?ts=1"}}
        result = await plugin._sync_service._cover_preparer.refresh_changed_covers(
            [{"id": 1, "name": "A"}], registry, progress_step=3, progress_total_steps=7, label="N64"
        )

        assert result == [{"rom_id": 1, "app_id": 10}]
        assert fake_refresh.call_args.args == ([{"id": 1, "name": "A"}], registry)
        call_kwargs = fake_refresh.call_args.kwargs
        assert call_kwargs["progress_step"] == 3
        assert call_kwargs["progress_total_steps"] == 7
        assert call_kwargs["label"] == "N64"
        # is_cancelling closure reflects the live sync_state.
        is_cancelling = call_kwargs["is_cancelling"]
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        assert is_cancelling() is False
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
        assert is_cancelling() is True
