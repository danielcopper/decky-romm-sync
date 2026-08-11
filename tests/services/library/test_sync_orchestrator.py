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
from domain.shortcut_data import EmulatorInvocation
from domain.sync_diff import BIND_ROM_ID_KEY
from domain.sync_stage import SyncStage
from domain.sync_state import SyncState
from domain.work_unit import WorkUnit
from lib.romm_paging import LIST_PAGE_SIZE

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


def _seed_rom_row(
    plugin,
    rom_id,
    *,
    app_id,
    platform_slug,
    name="Game",
    fs_name=None,
    sibling_group_key: str | None = "romm:seed:1",
    applied_launch_options: str | None = "",
    cover_source: str | None = None,
):
    """Insert a bound (or unbound when app_id is None) ROM into the shared fake UoW.

    ``sibling_group_key`` defaults to a non-null value so the incremental-skip
    path treats the registry as already backfilled (#1295); pass ``None`` to
    seed a pre-migration row that must force a full fetch for backfill.

    ``applied_launch_options`` defaults to ``""`` — the recorded uninstalled
    placeholder — so an uninstalled bound baseline (built launch_options "")
    reads as unchanged by the delta-restricted classify (#1383); pass ``None`` to
    seed a pre-migration-015 row (unknown → always "changed").

    ``cover_source`` is the persisted cover-cache fingerprint (#1386); defaults
    to ``None`` (a pre-migration-016 row — the NULL-adopt path).
    """
    from domain.rom import Rom

    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=fs_name if fs_name is not None else f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
        sibling_group_key=sibling_group_key,
        cover_source=cover_source,
    )
    with plugin._uow:
        plugin._uow.roms.save(rom)
        plugin._uow.roms.set_applied_launch_options(rom_id, applied_launch_options)


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


def _seed_platform_stamp(plugin, slug, *, at, rom_count):
    """Persist a per-platform completion stamp (ADR-0023) into the shared UoW."""
    from domain.platform_sync_state import PlatformSyncState

    with plugin._uow:
        plugin._uow.platform_sync_state.save(PlatformSyncState.stamp(platform_slug=slug, at=at, rom_count=rom_count))


async def _fake_wait_set_event(_unit, event):
    """Default ``_wait_for_unit_complete`` stand-in: set the event and
    return an empty rom_id_to_app_id map.

    The frontend's ``report_unit_results`` callback never runs in tests.
    The orchestrator's per-unit driver requires the event to fire and a
    mapping to come back — this helper provides both.
    """
    event.set()
    return {}


class _ClockAdvancingSleeper:
    """A ``Sleeper`` that advances a ``FakeClock`` on each sleep, so the real
    heartbeat-clocked ``_wait_for_unit_complete`` times out deterministically
    without any wall-clock wait (#1367)."""

    def __init__(self, clock, step: float) -> None:
        self._clock = clock
        self._step = step
        self.calls: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(self._step)


def _stash_abandoned_and_wind_down(plugin, *, run_id, unit_id, chunk_index, pending, chunk_rows):
    """Drive the box into the production post-heartbeat-timeout state via the
    real box verbs, then wind the run down — no hand-forced internals (#1367).

    Mirrors exactly what ``_abandon_active_chunk`` (stash the chunk: null the
    event, clear the dispatch identity) followed by the run's terminal
    ``finally: finish_run(run_id)`` (null ``current_sync_id``) leave behind, so a
    late ``report_unit_results`` arriving now must recover the binding through
    the ``abandoned_chunk`` stash rather than the (dead) active-unit path.
    """
    box = plugin._sync_service._box
    box.try_begin_run(run_id)
    box.active_unit_id = unit_id
    box.active_chunk_index = chunk_index
    box.pending_sync = dict(pending)
    box.pending_all_roms = dict(pending)
    box.stash_abandoned_chunk(list(chunk_rows))
    box.finish_run(run_id)
    return box


class TestShortcutDataFormat:
    """Validate the shortcut data format produced by the backend.

    The backend prepares shortcut data that the frontend uses to create
    Steam shortcuts. These tests ensure the data is well-formed.
    """

    def test_exe_path_points_to_rom_launcher(self, plugin):
        """Exe path must point to bin/rom-launcher inside the plugin directory."""
        import decky

        from domain.shortcut_data import build_shortcuts_data

        result = build_shortcuts_data([{"id": 1, "name": "Game"}], decky.DECKY_PLUGIN_DIR, {}, {})
        exe = result[0]["exe"]
        assert exe.endswith("/bin/rom-launcher"), f"Exe path should end with /bin/rom-launcher, got: {exe}"
        # Anchored to the directory it was handed, not to a name: DECKY_PLUGIN_DIR
        # is the checkout root under test, so asserting a literal name here pins
        # whatever the working copy happens to be called.
        assert exe.startswith(f"{decky.DECKY_PLUGIN_DIR}/"), (
            f"Exe path should sit inside the plugin directory it was given, got: {exe}"
        )

    def test_installed_rom_gets_launch_command(self, plugin):
        """An installed ROM's launch_options is the full RetroDECK launch command."""
        from domain.shortcut_data import build_shortcuts_data

        result = build_shortcuts_data([{"id": 42, "name": "Game"}], "/plugin", {42: "/roms/n64/game.z64"}, {})
        assert result[0]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/game.z64"'

    def test_start_dir_is_parent_of_exe(self, plugin):
        """Start dir must be the directory containing the launcher."""
        import decky

        from domain.shortcut_data import build_shortcuts_data

        result = build_shortcuts_data([{"id": 1, "name": "Game"}], decky.DECKY_PLUGIN_DIR, {}, {})
        assert result[0]["start_dir"] == os.path.dirname(result[0]["exe"])


class TestBuildCoreOverrides:
    """The ``core_overrides`` map both preview and apply pass to ``build_shortcuts_data``.

    Maps ``rom_id -> resolved core_so`` for every ROM in the unit that carries a
    still-valid ``emulator_override``; NULL pins never enter the map, and a stale
    LABEL is omitted with a WARNING so the bake degrades to the plain launch.
    """

    def test_resolved_override_included_null_omitted(self, plugin):
        """A resolvable pin maps to its libretro EmulatorInvocation; an unpinned ROM is absent."""
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        _seed_install(plugin, 10, file_path="/roms/psx/a.chd", platform_slug="psx")
        _seed_install(plugin, 11, file_path="/roms/psx/b.chd", platform_slug="psx")
        with plugin._uow:
            plugin._uow.roms.set_emulator_override(10, "PCSX ReARMed")

        roms = [{"id": 10, "platform_slug": "psx"}, {"id": 11, "platform_slug": "psx"}]
        result = plugin._sync_service._orchestrator._build_core_overrides(roms)

        assert result == {10: EmulatorInvocation.libretro("pcsx_rearmed_libretro", "PCSX ReARMed")}
        assert 11 not in result

    def test_stale_override_omitted_with_warning(self, plugin, caplog):
        """A pin whose LABEL no longer resolves is omitted and a WARNING is logged."""
        import logging

        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        _seed_install(plugin, 10, file_path="/roms/psx/a.chd", platform_slug="psx")
        with plugin._uow:
            plugin._uow.roms.set_emulator_override(10, "Removed Core")

        roms = [{"id": 10, "platform_slug": "psx"}]
        with caplog.at_level(logging.WARNING):
            result = plugin._sync_service._orchestrator._build_core_overrides(roms)

        assert result == {}
        assert "Removed Core" in caplog.text
        assert "no longer resolves" in caplog.text

    def test_no_overrides_returns_empty(self, plugin):
        """No pins anywhere → empty map (no available-cores lookups needed)."""
        _seed_install(plugin, 10, file_path="/roms/n64/a.z64", platform_slug="n64")
        result = plugin._sync_service._orchestrator._build_core_overrides([{"id": 10, "platform_slug": "n64"}])
        assert result == {}


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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
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


class TestPreviewCoverRefreshCount:
    """The preview's cover-only work count (#1386 flow gap).

    A cover-only server change yields an empty shortcut delta, so the frontend
    would short-circuit on "no changes" and the apply-time invalidation pass
    would never run. The preview therefore counts fingerprint mismatches with
    the SAME kernel the apply pass refreshes by, over the registry projection
    it already reads — side-effect-free: no downloads, no DB writes.
    """

    _OLD = "/cover/big.png?ts=2026-01-01 00:00:00"
    _NEW = "/cover/big.png?ts=2026-07-11 12:00:00"

    @staticmethod
    def _preview_setup(plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

    @pytest.mark.asyncio
    async def test_platform_unit_mismatch_counted_without_downloads_or_writes(self, plugin, fake_romm_api):
        # A bound, content-unchanged platform ROM whose server cover changed:
        # the shortcut delta is empty but the cover count is 1 — and the
        # preview stays read-only (no cover download, fingerprint untouched).
        self._preview_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64", "path_cover_large": self._NEW}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=self._OLD
        )

        result = await plugin.sync_preview()

        assert result["success"] is True
        summary = result["summary"]
        assert summary["cover_refresh_count"] == 1
        assert summary["new_count"] == 0
        assert summary["changed_count"] == 0
        assert summary["remove_count"] == 0
        # Side-effect-free: the fake saw no cover download and the persisted
        # fingerprint did not advance (the APPLY run owns both).
        assert all(name != "download_cover" for name, _a, _k in fake_romm_api.call_log)
        with plugin._uow as uow:
            assert uow.roms.get(10).cover_source == self._OLD

    @pytest.mark.asyncio
    async def test_collection_unit_mismatch_is_counted(self, plugin, fake_romm_api):
        # The hardware repro was a collection-unit ROM: its platform is NOT
        # enabled, so the ROM only enters the preview union via the enabled
        # collection — the count must still see it.
        self._preview_setup(plugin, fake_romm_api)
        fake_romm_api.roms[20] = {
            "id": 20,
            "name": "CGame",
            "fs_name": "cgame.gba",
            "platform_id": 2,
            "platform_name": "GBA",
            "platform_slug": "gba",
            "path_cover_large": self._NEW,
        }
        _seed_collection(fake_romm_api, collection_id=7, name="Favorites", rom_ids=[20], is_favorite=True)
        plugin.settings["enabled_platforms"] = {}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}}
        _seed_rom_row(
            plugin, 20, app_id=2020, platform_slug="gba", name="CGame", fs_name="cgame.gba", cover_source=self._OLD
        )

        result = await plugin.sync_preview()

        assert result["success"] is True
        assert result["summary"]["cover_refresh_count"] == 1
        assert result["summary"]["new_count"] == 0
        assert result["summary"]["changed_count"] == 0

    @pytest.mark.asyncio
    async def test_unchanged_and_null_fingerprints_count_zero(self, plugin, fake_romm_api):
        # An unchanged fingerprint is no work; a NULL fingerprint is the silent
        # adopt path (or the apply path's own download) — neither is user-visible
        # cover work, so the pure no-changes preview keeps its shape.
        self._preview_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 1, "name": "Same", "fs_name": "same.z64", "path_cover_large": self._NEW},
                {"id": 2, "name": "Null", "fs_name": "null.z64", "path_cover_large": self._NEW},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(
            plugin, 1, app_id=1001, platform_slug="n64", name="Same", fs_name="same.z64", cover_source=self._NEW
        )
        _seed_rom_row(plugin, 2, app_id=1002, platform_slug="n64", name="Null", fs_name="null.z64", cover_source=None)

        result = await plugin.sync_preview()

        assert result["success"] is True
        assert result["summary"]["cover_refresh_count"] == 0
        assert result["summary"]["new_count"] == 0
        assert result["summary"]["changed_count"] == 0

    def test_apply_registry_projection_carries_cover_source(self, plugin):
        # Round-trip: the bound-row projection the apply scan (and its group
        # collapse) reads must surface the persisted fingerprint.
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=self._OLD
        )
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="Null", fs_name="null.z64", cover_source=None)

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        registry = plugin._sync_service._orchestrator._read_apply_registry(unit)

        assert registry["10"]["cover_source"] == self._OLD
        assert registry["11"]["cover_source"] is None

    def test_preview_baseline_projection_carries_cover_source(self, plugin):
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=self._OLD
        )

        registry, _platforms, _collections = plugin._sync_service._orchestrator._read_preview_baseline({"n64": "N64"})

        assert registry["10"]["cover_source"] == self._OLD


class TestPreviewRestampPlatformCount:
    """The preview's unstamped-platform re-run count (#1416).

    A late-ack-recovered platform is complete but carries no ``PlatformSyncState``
    stamp, so its shortcut delta is empty yet its apply must still run once to
    re-stamp it. The preview counts enabled platforms without a completion stamp
    (``restamp_platform_count``) so the frontend offers Apply on an otherwise-empty
    delta instead of short-circuiting — a fully-stamped library keeps counting 0
    and short-circuits exactly as before.
    """

    @staticmethod
    def _preview_setup(plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

    @pytest.mark.asyncio
    async def test_unstamped_platform_counted_with_empty_delta(self, plugin, fake_romm_api):
        # A bound, content-unchanged platform ROM with NO completion stamp: the
        # shortcut delta is empty (0 new / changed / removed) but the platform
        # needs a re-stamp run, so restamp_platform_count is 1.
        self._preview_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")

        result = await plugin.sync_preview()

        assert result["success"] is True
        summary = result["summary"]
        assert summary["restamp_platform_count"] == 1
        assert summary["new_count"] == 0
        assert summary["changed_count"] == 0
        assert summary["remove_count"] == 0
        assert summary["cover_refresh_count"] == 0

    @pytest.mark.asyncio
    async def test_stamped_platform_not_counted(self, plugin, fake_romm_api):
        # The same unchanged platform WITH a matching completion stamp: no
        # re-stamp is owed, so the count stays 0 and the "up to date"
        # short-circuit is preserved.
        self._preview_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00", rom_count=1)

        result = await plugin.sync_preview()

        assert result["success"] is True
        summary = result["summary"]
        assert summary["restamp_platform_count"] == 0
        assert summary["new_count"] == 0
        assert summary["changed_count"] == 0

    @pytest.mark.asyncio
    async def test_counts_only_unstamped_across_mixed_platforms(self, plugin, fake_romm_api):
        # Two enabled platforms, one stamped and one not: only the unstamped one
        # is counted (the count is not a whole-library boolean).
        self._preview_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A", "fs_name": "a.z64"}]
        )
        _seed_platform(
            fake_romm_api, platform_id=2, name="GBA", slug="gba", roms=[{"id": 20, "name": "B", "fs_name": "b.gba"}]
        )
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 20, app_id=2020, platform_slug="gba", name="B", fs_name="b.gba")
        # Only n64 carries a stamp; gba is the unstamped late-ack survivor.
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00", rom_count=1)

        result = await plugin.sync_preview()

        assert result["success"] is True
        assert result["summary"]["restamp_platform_count"] == 1


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
        assert result["reason"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_rejects_when_no_pending_delta(self, plugin):
        assert plugin._sync_service._pending_delta is None
        result = await plugin.sync_apply_delta("any-id")
        assert result["success"] is False
        assert result["reason"] == "stale_preview"

    @pytest.mark.asyncio
    async def test_rejected_when_run_in_flight_preserves_delta(self, plugin):
        """RC-OVERLAP (#1202): an apply landing while a run is already in flight
        is rejected by the admission guard WITHOUT consuming the staged delta or
        disturbing the active run id.
        """
        self._setup_pending_delta(plugin, "pv-1")
        box = plugin._sync_service._box
        assert box.try_begin_run("active-run") is True

        result = await plugin.sync_apply_delta("pv-1")

        assert result == {"success": False, "reason": "sync_in_progress", "message": "Sync already in progress"}
        # The active run is untouched and the staged delta survives for the
        # legitimate apply.
        assert box.current_sync_id == "active-run"
        assert box.pending_delta is not None
        assert box.pending_delta.preview_id == "pv-1"

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
        assert result["reason"] == "stale_preview"
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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        result = plugin._sync_service.start_sync()
        assert result["success"] is False
        assert "already in progress" in result["message"]

    def test_cancel_sync_when_running(self, plugin):
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        result = plugin._sync_service.cancel_sync()
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_cancel_sync_when_idle(self, plugin):
        result = plugin._sync_service.cancel_sync()
        assert result["success"] is True
        assert "No sync" in result["message"]

    def test_cancel_sync_with_matching_run_id_sets_cancelling(self, plugin):
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-B"
        result = plugin._sync_service.cancel_sync("run-B")
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_cancel_sync_with_stale_run_id_is_noop(self, plugin):
        """A cancel meant for run-A must NOT abort the fresh run-B (#1198).

        The regression case: run-A finalized to IDLE and run-B started fresh,
        then run-A's Cancel click lands. The argument-less cancel would flip
        run-B to CANCELLING; the run-scoped cancel ignores the stale id.
        """
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-B"
        result = plugin._sync_service.cancel_sync("run-A")
        assert result["success"] is True
        assert "stale" in result["message"].lower()
        assert plugin._sync_service._sync_state == SyncState.RUNNING

    def test_cancel_sync_with_none_run_id_cancels_unconditionally(self, plugin):
        """A falsy run_id (legacy caller / no id captured yet) always cancels."""
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-B"
        result = plugin._sync_service.cancel_sync(None)
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_cancel_sync_with_empty_run_id_cancels_unconditionally(self, plugin):
        """An empty-string run_id (the frontend's no-id-yet fallback) cancels."""
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-B"
        result = plugin._sync_service.cancel_sync("")
        assert result["success"] is True
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_cancel_sync_stale_run_id_when_idle_is_no_op(self, plugin):
        """Idle short-circuits before the run-id check — no sync, plain no-op."""
        plugin._sync_service._box.current_sync_id = "run-B"
        result = plugin._sync_service.cancel_sync("run-A")
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
    """Tests for _finish_sync().

    ``_finish_sync`` emits the terminal CANCELLED progress snapshot only; the
    IDLE/None reset of the run-lifecycle pair is owned by the caller's
    ``finally: box.finish_run(run_id)`` (#1202), so this method leaves
    ``sync_state`` / ``current_sync_id`` untouched.
    """

    @pytest.mark.asyncio
    async def test_emits_cancelled_progress_snapshot(self, plugin):
        import decky

        decky.emit.reset_mock()
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True, "current": 5, "total": 10}

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._sync_progress["running"] is False
        assert plugin._sync_service._sync_progress["stage"] == "cancelled"
        assert plugin._sync_service._sync_progress["message"] == "Sync cancelled"

    @pytest.mark.asyncio
    async def test_does_not_reset_run_lifecycle(self, plugin):
        """_finish_sync only emits — the terminal ``finally`` owns the reset.

        Leaving ``current_sync_id`` set here is what lets the run-scoped
        ``finish_run(run_id)`` later decide whether this run still owns the slot.
        """
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._sync_progress = {"running": True}
        plugin._sync_service._box.current_sync_id = "sync-abc"

        await plugin._sync_service._orchestrator._finish_sync("Sync cancelled")

        assert plugin._sync_service._sync_state == SyncState.RUNNING
        assert plugin._sync_service._current_sync_id == "sync-abc"


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

    @pytest.mark.asyncio
    async def test_emit_progress_sub_stage_rides_event_and_status(self, plugin):
        """The ``sub_stage`` kwarg rides the payload as the camelCase ``subStage``
        key (matching ``totalSteps`` / ``runId``) on BOTH the emitted
        ``sync_progress`` event and the persisted snapshot that
        ``get_sync_status`` re-seeds a remounted QAM from (#1407)."""
        import decky

        decky.emit.reset_mock()
        await plugin._sync_service._orchestrator.emit_progress(
            SyncStage.FETCHING,
            current=2,
            total=7,
            message="Fetching GBA (page 2/7)",
            step=3,
            total_steps=8,
            sub_stage="fetch",
        )

        event_payloads = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_progress"]
        assert event_payloads, "emit_progress must emit a sync_progress event"
        assert event_payloads[-1]["subStage"] == "fetch"
        # Same value re-seeds a remounted QAM through get_sync_status.
        assert plugin._sync_service.get_sync_status()["subStage"] == "fetch"

    @pytest.mark.asyncio
    async def test_emit_progress_defaults_sub_stage_empty(self, plugin):
        """A frame that names no phase carries an empty ``subStage`` — the bar
        reads it as "rest at the unit floor", never a stale phase (#1407)."""
        import decky

        decky.emit.reset_mock()
        await plugin._sync_service._orchestrator.emit_progress(
            SyncStage.FETCHING, message="Fetching GBA", step=3, total_steps=8
        )

        assert plugin._sync_service.get_sync_status()["subStage"] == ""


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
        assert "reason" in result
        assert plugin._sync_service._sync_state == SyncState.IDLE
        # Error path evicts any pending delta.
        assert plugin._sync_service._pending_delta is None

    @pytest.mark.asyncio
    async def test_cancelled_error_returns_canonical_failure(self, plugin, fake_romm_api):
        """A cooperative cancel during sync_preview RETURNS the canonical failure
        shape — it does NOT re-raise out of the Decky callable (#1035).

        sync_preview is awaited by the frontend; re-raising would leave that
        promise unsettled. The cooperative cancel — now the dedicated
        ``SyncCancelled`` BaseException, matching the production signal raised
        by ``fetcher._check_cancelling`` and the per-unit checkpoint — must
        surface as ``{success: False, reason: "cancelled", message: ...}`` and
        leave sync_state IDLE with no pending delta. ``SyncCancelled`` skips the
        generic ``except Exception`` and lands in ``except SyncCancelled``.
        """
        import decky

        from domain.sync_state import SyncCancelled

        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)

        fake_romm_api.list_platforms = MagicMock(side_effect=SyncCancelled("Sync cancelled"))
        plugin.settings["enabled_platforms"] = {"1": True}

        # sync_preview only runs from IDLE — guard against a leaked non-IDLE state.
        assert plugin._sync_service._sync_state == SyncState.IDLE

        result = await plugin._sync_service.sync_preview()

        assert result == {"success": False, "reason": "cancelled", "message": "Sync cancelled"}
        assert plugin._sync_service._sync_state == SyncState.IDLE
        assert plugin._sync_service._pending_delta is None
        # The cooperative signal genuinely originated from the fetch.
        fake_romm_api.list_platforms.assert_called()


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
            "standard": {"7": True},
            "smart": {"5": True},
            "virtual": {"9": True},
        }

        units = await plugin._sync_service._fetcher.build_work_queue()
        assert [(u.type, u.name) for u in units] == [
            ("platform", "N64"),
            ("collection", "Favorites"),
            ("collection", "Filter"),
            ("collection", "Metroid"),
        ]
        assert units[1].collection_kind == "standard"
        assert units[2].collection_kind == "smart"
        assert units[3].collection_kind == "virtual"


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
        # updates after the completion stamp so the incremental-skip path fires.
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=2)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="B", fs_name="b.z64")

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is True
        assert {r["id"] for r in roms} == {10, 11}

    @pytest.mark.asyncio
    async def test_null_group_key_forces_full_fetch_for_backfill(self, plugin, fake_romm_api):
        # A pre-#1295 registry row (NULL sibling_group_key) must NOT skip even
        # when count matches + zero server updates — the platform is re-fetched so
        # the commit backfills its version metadata. One bound row un-backfilled
        # is enough to force the whole platform's fetch.
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64", sibling_group_key=None)
        _seed_rom_row(
            plugin, 11, app_id=1011, platform_slug="n64", name="B", fs_name="b.z64", sibling_group_key="igdb:5:1"
        )

        roms, skipped = await plugin._sync_service._fetcher.fetch_platform_unit(unit)
        assert skipped is False
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
        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=3, collection_kind="standard")
        synced: set[int] = set()
        new_roms, ids, _skipped = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)
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
        unit = WorkUnit(type="collection", id="9", name="Metroid", slug="", rom_count=2, collection_kind="virtual")

        # rom_id=1 was already fetched via a platform unit
        synced: set[int] = {1}
        new_roms, ids, _skipped = await plugin._sync_service._fetcher.fetch_collection_unit(unit, synced)
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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-plan"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        plan_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_plan"]
        assert len(plan_events) == 1
        payload = plan_events[0][0][1]
        assert payload["total_units"] == 1
        assert payload["units"][0]["name"] == "N64"
        # The frontend captures this run_id to scope a later Cancel click (#1198).
        assert payload["run_id"] == "run-plan"

    @pytest.mark.asyncio
    async def test_sync_plan_carries_skip_aware_estimate_fields(self, plugin, fake_romm_api):
        """#1382: platform units ride predicted_skip / collapsed_count, the raw
        ``total_roms`` stays untouched (backward compat), and the additive
        ``total_estimated_items`` zero-weights predicted skips and prices the
        rest at their collapsed count (raw fallback)."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # N64: skip-eligible — stamp matches the server count, 2 persisted rows
        # in one sibling group with a bound representative → predicted skip,
        # collapsed count 1. GBA: never synced → no skip, raw-count fallback.
        fake_romm_api.platforms = [
            {"id": 1, "name": "N64", "slug": "n64", "rom_count": 2},
            {"id": 2, "name": "GBA", "slug": "gba", "rom_count": 5},
        ]
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00", rom_count=2)
        _seed_rom_row(plugin, 10, app_id=1001, platform_slug="n64", sibling_group_key="igdb:100:1")
        _seed_rom_row(plugin, 11, app_id=None, platform_slug="n64", sibling_group_key="igdb:100:1")
        # A collection unit, left UNSTAMPED here: its membership is knowable only
        # from a completion stamp, so it carries no estimate field at all and
        # weighs its raw rom_count. (A stamped collection does ride bound_count,
        # #1511 — covered in test_fetcher.)
        _seed_collection(fake_romm_api, collection_id=7, name="Faves", rom_ids=[20, 21, 22])
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-est"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        plan_events = [c for c in decky.emit.call_args_list if c[0][0] == "sync_plan"]
        assert len(plan_events) == 1
        payload = plan_events[0][0][1]
        # Raw planned total is untouched: 2 + 5 + 3.
        assert payload["total_roms"] == 10
        # Skip-aware: N64 predicted-skip → 0, GBA raw 5, collection raw 3.
        assert payload["total_estimated_items"] == 8
        units = {u["name"]: u for u in payload["units"]}
        assert units["N64"]["predicted_skip"] is True
        assert units["N64"]["collapsed_count"] == 1
        assert units["GBA"]["predicted_skip"] is False
        assert "collapsed_count" not in units["GBA"]
        assert "predicted_skip" not in units["Faves"]
        assert "collapsed_count" not in units["Faves"]
        assert "bound_count" not in units["Faves"]

    @pytest.mark.asyncio
    async def test_unstamped_zero_delta_platform_restamps_and_records_run(self, plugin, fake_romm_api):
        """#1416: an unstamped platform with a 0 shortcut delta still runs the apply.

        A late-ack recovery leaves the platform complete but unstamped. The next
        apply must NOT wholesale-skip it (no stamp): its delta-restricted apply
        emits a single empty final chunk whose commit re-writes the completion
        stamp, and the run records a fresh completed ``SyncRun`` — so the platform
        stops full-fetching forever and the "interrupted" status heals.
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
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        # Bound + content-unchanged → empty delta. NO completion stamp: the
        # complete-but-unstamped residue a heartbeat-timeout's late ack leaves.
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")
        with plugin._uow as uow:
            assert uow.platform_sync_state.get("n64") is None

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-restamp"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # The apply ran despite the empty delta: a single empty final chunk fired.
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        assert unit_events[0]["shortcuts"] == []
        assert unit_events[0]["chunk_count"] == 1

        # The empty chunk's commit re-wrote the platform stamp, and the run
        # recorded a fresh completed SyncRun.
        with plugin._uow as uow:
            stamp = uow.platform_sync_state.get("n64")
            assert stamp is not None
            assert stamp.rom_count == 1
            completed = uow.sync_runs.get_latest_completed()
            assert completed is not None
            assert completed.id == "run-restamp"

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 2
        assert unit_events[0]["unit_name"] == "N64"
        assert unit_events[1]["unit_name"] == "GBA"
        assert unit_events[0]["unit_index"] == 0
        assert unit_events[1]["unit_index"] == 1

    @pytest.mark.asyncio
    async def test_emitted_unit_carries_run_id(self, plugin, fake_romm_api):
        """Each ``sync_apply_unit`` payload carries the run's ``current_sync_id``.

        The frontend keys its once-per-run existing-shortcut scan cache off
        ``run_id``, so every unit emitted within a run must carry the same id.
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
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-abc"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 2
        assert all(e["run_id"] == "run-abc" for e in unit_events)

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        by_rom = {s["rom_id"]: s for s in unit_events[0]["shortcuts"]}
        assert by_rom[10]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/n64/installed.z64"'
        assert by_rom[11]["launch_options"] == ""

    @pytest.mark.asyncio
    async def test_apply_bakes_emulator_override_into_launch_options(self, plugin, fake_romm_api):
        """A pinned ``emulator_override`` bakes the ``-e`` form; a NULL pin stays plain (R6).

        Two installed ROMs on the same platform: rom 10 carries a resolvable
        override (``-e`` baked), rom 11 has none (plain launch). Proves the
        sync-apply ``core_overrides`` map drives ``build_shortcuts_data`` per-ROM.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="PSX",
            slug="psx",
            roms=[{"id": 10, "name": "Pinned"}, {"id": 11, "name": "Plain"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_install(plugin, 10, file_path="/roms/psx/pinned.chd", platform_slug="psx")
        _seed_install(plugin, 11, file_path="/roms/psx/plain.chd", platform_slug="psx")
        with plugin._uow:
            plugin._uow.roms.set_emulator_override(10, "PCSX ReARMed")

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        by_rom = {s["rom_id"]: s for s in unit_events[0]["shortcuts"]}
        assert by_rom[10]["launch_options"] == (
            "flatpak run net.retrodeck.retrodeck "
            '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%" '
            '"/roms/psx/pinned.chd"'
        )
        assert by_rom[11]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/psx/plain.chd"'
        assert "-e" not in by_rom[11]["launch_options"]

    @pytest.mark.asyncio
    async def test_apply_stale_override_bakes_plain_with_warning(self, plugin, fake_romm_api, caplog):
        """A stale override LABEL (no longer in available_cores) bakes PLAIN + WARNs (B4)."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        # The options no longer carry the pinned label → label_to_invocation → None.
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="PSX",
            slug="psx",
            roms=[{"id": 10, "name": "Stale"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_install(plugin, 10, file_path="/roms/psx/stale.chd", platform_slug="psx")
        with plugin._uow:
            plugin._uow.roms.set_emulator_override(10, "Removed Core")

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        import logging

        with caplog.at_level(logging.WARNING):
            await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        by_rom = {s["rom_id"]: s for s in unit_events[0]["shortcuts"]}
        # Stale → PLAIN launch, never -e with a bogus core.
        assert by_rom[10]["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/psx/stale.chd"'
        assert "-e" not in by_rom[10]["launch_options"]
        assert "Removed Core" in caplog.text
        assert "no longer resolves" in caplog.text

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
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}

        download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._download_artwork = download_artwork
        wait_mock = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_mock
        commit_mock = AsyncMock()
        plugin._sync_service._reporter.commit_unit_results = commit_mock  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1000, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 99, app_id=9900, platform_slug="gba", name="Z", fs_name="z.gba")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = AsyncMock(return_value={})
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1000, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 99, app_id=9900, platform_slug="gba", name="Z", fs_name="z.gba")
        _seed_rom_row(plugin, 77, app_id=None, platform_slug="snes", name="Y", fs_name="y.sfc")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = AsyncMock(return_value={})
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        # Only the bound stale ROM (99) is emitted; the unbound leftover (77) is excluded.
        assert stale_events == [{"remove": [{"rom_id": 99, "app_id": 9900}]}]

    @pytest.mark.asyncio
    async def test_appid_reuse_collision_excluded_from_sync_stale(self, plugin, fake_romm_api):
        """A new server-issued rom_id reusing an old appId must NOT be wiped (#1036).

        Old row (rom 1, app 5000) survives a server switch / re-import; the new
        ROM (rom 2) for the same game produces the SAME appId (unchanged
        exe+name). The frontend re-acks app 5000 for rom 2; the real commit
        binds rom 2 and records app 5000 in ``committed_app_ids``. The stale
        scan flags old rom 1 — but ``select_stale_removals`` excludes app 5000
        (bound this run), so ``sync_stale`` carries NO removal and the live
        shortcut survives."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Old colliding row from before the reassignment: rom 1 bound to app 5000.
        # No completed run is seeded so the platform full-fetches (no incremental
        # skip), exercising the real commit path for the new rom_id.
        _seed_rom_row(plugin, 1, app_id=5000, platform_slug="n64", name="A", fs_name="a.z64")
        # The server now serves the same game under a NEW rom_id (2).
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 2, "name": "A"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # The frontend re-uses the same appId (CRC32 of unchanged exe+name) and
        # acks it for the new rom_id. The REAL commit runs so committed_app_ids
        # is populated and the repo unbinds the colliding sibling.
        async def ack_same_appid(_unit, event):
            event.set()
            return {"2": 5000}

        plugin._sync_service._orchestrator._wait_for_unit_complete = ack_same_appid
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # The load-bearing assertion: app 5000 is NOT emitted for removal.
        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [{"remove": []}], (
            f"appId-reuse collision leaked a removal that would wipe the live shortcut: {stale_events}"
        )
        # The new row holds the binding; the old row is unbound (ADR-0007 — kept).
        with plugin._uow as uow:
            assert uow.roms.get(2).shortcut_app_id == 5000
            assert uow.roms.get(1).shortcut_app_id is None
            assert {r.rom_id for r in uow.roms.iter_all()} == {1, 2}

    @pytest.mark.asyncio
    async def test_genuinely_stale_still_removed_alongside_collision(self, plugin, fake_romm_api):
        """A genuinely-stale ROM (its appId NOT bound this run) is still removed,
        even while a colliding appId is excluded — the fix narrows removals, it
        does not disable the stale path (#1036)."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # rom 1 collides (app 5000 re-bound to rom 2 this run); rom 99 is a
        # genuinely-removed ROM on a now-disabled platform (app 9900, not re-bound).
        # No completed run seeded → full fetch (no skip) so the real commit runs.
        _seed_rom_row(plugin, 1, app_id=5000, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 99, app_id=9900, platform_slug="gba", name="Z", fs_name="z.gba")
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 2, "name": "A"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def ack_same_appid(_unit, event):
            event.set()
            return {"2": 5000}

        plugin._sync_service._orchestrator._wait_for_unit_complete = ack_same_appid
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        # rom 99 (app 9900) is removed; the colliding app 5000 is excluded.
        assert stale_events == [{"remove": [{"rom_id": 99, "app_id": 9900}]}]

    @pytest.mark.asyncio
    async def test_group_emits_one_shortcut_per_sibling_group(self, plugin, fake_romm_api):
        """A platform with a 3-version sibling group emits ONE shortcut for the
        game (ADR-0021); the non-representative dumps are persisted unbound."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                # Three dumps of one game (shared igdb_id) + one unrelated game.
                {
                    "id": 10,
                    "name": "Zelda (USA)",
                    "igdb_id": 100,
                    "fs_name_no_ext": "zelda_usa",
                    "rom_user": {"is_main_sibling": True},
                },
                {"id": 11, "name": "Zelda (JP)", "igdb_id": 100, "fs_name_no_ext": "zelda_jp"},
                {"id": 12, "name": "Zelda (EU)", "igdb_id": 100, "fs_name_no_ext": "zelda_eu"},
                {"id": 20, "name": "Mario", "igdb_id": 200, "fs_name_no_ext": "mario"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def ack_reps(_unit, event):
            event.set()
            return {str(rid): 9000 + rid for rid in plugin._sync_service._box.pending_sync}

        plugin._sync_service._orchestrator._wait_for_unit_complete = ack_reps
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        emitted_ids = {sd["rom_id"] for sd in unit_events[0]["shortcuts"]}
        # ONE shortcut for the Zelda group (rep = the RomM default, rom 10) + Mario.
        assert emitted_ids == {10, 20}
        with plugin._uow as uow:
            # All four siblings are persisted; only the representatives bind.
            assert uow.roms.get(10).shortcut_app_id == 9010
            assert uow.roms.get(20).shortcut_app_id == 9020
            assert uow.roms.get(11).shortcut_app_id is None
            assert uow.roms.get(12).shortcut_app_id is None
            assert uow.roms.get(11).sibling_group_key == "igdb:100:1"

    async def _apply_group_and_get_shortcuts(self, plugin, fake_romm_api, roms):
        """Seed a single-platform sibling group, run one apply unit, return the
        emitted ``sync_apply_unit`` shortcut dicts. Shared by the region tests."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=roms)
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def ack_reps(_unit, event):
            event.set()
            return {str(rid): 9000 + rid for rid in plugin._sync_service._box.pending_sync}

        plugin._sync_service._orchestrator._wait_for_unit_complete = ack_reps
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        await plugin._sync_service._orchestrator._do_sync_per_unit()

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        return unit_events[0]["shortcuts"]

    @pytest.mark.asyncio
    async def test_region_priority_picks_and_names_representative(self, plugin, fake_romm_api):
        """No default/installed/bound: region priority binds the USA dump AND
        names the shortcut after it, even though Japan sorts first alphabetically
        (ADR-0021 §3 region leg + canonical naming)."""
        shortcuts = await self._apply_group_and_get_shortcuts(
            plugin,
            fake_romm_api,
            [
                {"id": 10, "name": "Zelda (JP)", "igdb_id": 100, "fs_name_no_ext": "zelda_jp", "regions": ["Japan"]},
                {"id": 11, "name": "Zelda (USA)", "igdb_id": 100, "fs_name_no_ext": "zelda_usa", "regions": ["USA"]},
            ],
        )
        assert len(shortcuts) == 1
        assert shortcuts[0]["rom_id"] == 11
        assert shortcuts[0]["name"] == "Zelda (USA)"

    @pytest.mark.asyncio
    async def test_preferred_region_setting_threads_into_apply_collapse(self, plugin, fake_romm_api):
        """Setting ``preferred_region`` re-heads the ranking: with Japan preferred,
        the Japanese dump becomes the representative + shortcut name — proving the
        setting is threaded into the apply collapse call site."""
        plugin.settings["preferred_region"] = "Japan"
        shortcuts = await self._apply_group_and_get_shortcuts(
            plugin,
            fake_romm_api,
            [
                {"id": 10, "name": "Zelda (JP)", "igdb_id": 100, "fs_name_no_ext": "zelda_jp", "regions": ["Japan"]},
                {"id": 11, "name": "Zelda (USA)", "igdb_id": 100, "fs_name_no_ext": "zelda_usa", "regions": ["USA"]},
            ],
        )
        assert len(shortcuts) == 1
        assert shortcuts[0]["rom_id"] == 10
        assert shortcuts[0]["name"] == "Zelda (JP)"

    @pytest.mark.asyncio
    async def test_preview_new_names_follow_region_canonical(self, plugin, fake_romm_api):
        """The preview collapse call site also applies region priority: the new
        game's reported name is the region-canonical (USA) name."""
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
                {"id": 10, "name": "Zelda (JP)", "igdb_id": 100, "fs_name_no_ext": "zelda_jp", "regions": ["Japan"]},
                {"id": 11, "name": "Zelda (USA)", "igdb_id": 100, "fs_name_no_ext": "zelda_usa", "regions": ["USA"]},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        result = await plugin.sync_preview()
        assert result["success"] is True
        assert result["summary"]["new_count"] == 1  # one game, not two dumps
        assert result["new_names"] == ["Zelda (USA)"]

    @pytest.mark.asyncio
    async def test_vanished_bound_sibling_rebinds_without_stale_removal(self, plugin, fake_romm_api):
        """A bound sibling that disappears while its group survives rebinds to a
        surviving sibling — the appId is preserved (no sync_stale removal), and the
        binding moves onto the representative (ADR-0021 §2)."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        # rom 1 was the bound USA dump (app 5000); it is GONE from the server now.
        _seed_rom_row(
            plugin,
            1,
            app_id=5000,
            platform_slug="n64",
            name="Zelda (USA)",
            fs_name="zelda_usa.z64",
            sibling_group_key="igdb:100:1",
        )
        # The server still serves the JP + EU dumps of the same game.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {
                    "id": 2,
                    "name": "Zelda (JP)",
                    "igdb_id": 100,
                    "fs_name_no_ext": "zelda_jp",
                    "rom_user": {"is_main_sibling": True},
                },
                {"id": 3, "name": "Zelda (EU)", "igdb_id": 100, "fs_name_no_ext": "zelda_eu"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def ack_reuse(_unit, event):
            event.set()
            # The frontend reuses the vanished sibling's shortcut (app 5000) under
            # its rom_id — the emitted rebind entry is keyed to rom 1.
            return {str(rid): 5000 for rid in plugin._sync_service._box.pending_sync}

        plugin._sync_service._orchestrator._wait_for_unit_complete = ack_reuse
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # ONE shortcut emitted, keyed to the vanished sibling (rom 1) for reuse,
        # rebinding to the RomM default (rom 2).
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        emitted = unit_events[0]["shortcuts"]
        assert len(emitted) == 1
        assert emitted[0]["rom_id"] == 1
        # The rebind target is backend-internal — it drives the commit below via
        # pending_sync, and is stripped from the wire (the frontend reuses the
        # shortcut by rom_id). The binding move is verified by the roms rows below.
        assert BIND_ROM_ID_KEY not in emitted[0]

        # No stale removal — the appId is preserved, not wiped.
        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [{"remove": []}]

        # The binding moved onto the surviving representative (rom 2); the vanished
        # sibling row is unbound (kept per ADR-0007).
        with plugin._uow as uow:
            assert uow.roms.get(2).shortcut_app_id == 5000
            assert uow.roms.get(1).shortcut_app_id is None

    @pytest.mark.asyncio
    async def test_skipped_platform_collection_unbound_sibling_never_rebinds(self, plugin, fake_romm_api):
        """#1296 CRITICAL: a skipped platform + a collection holding an UNBOUND
        sibling of a group must NEVER rebind the live installed game.

        Worked failure: the platform incremental-SKIPS, so only its BOUND rows
        enter ``synced_rom_ids`` and the collection fetches the group's unbound
        sibling un-deduped. The collection is a PARTIAL group view — the bound
        installed representative (rom 10) is absent from its fetch but alive on the
        server — so the collapse must grandfather the group untouched, not read the
        binding as "vanished" and rebind it onto the uninstalled sibling (which
        would blank the launch options and orphan the installed ROM).
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # A 2-version Zelda group on N64: rom 10 bound + installed (active
        # version), rom 11 the unbound JP sibling. Both persisted + backfilled so
        # the platform incremental-skips this run.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Zelda (USA)", "igdb_id": 100, "fs_name_no_ext": "zelda_usa"},
                {"id": 11, "name": "Zelda (JP)", "igdb_id": 100, "fs_name_no_ext": "zelda_jp"},
            ],
        )
        # The collection holds ONLY the unbound sibling.
        _seed_collection(fake_romm_api, collection_id=7, name="Faves", rom_ids=[11], is_favorite=True)
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}}

        _seed_completed_run(plugin, at="2025-01-01T00:00:00Z")
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=2)
        # Install first so the binding survives the _seed_rom_row overwrite.
        _seed_install(plugin, 10, file_path="/roms/n64/zelda_usa.z64", platform_slug="n64")
        _seed_rom_row(
            plugin,
            10,
            app_id=5000,
            platform_slug="n64",
            name="Zelda (USA)",
            fs_name="zelda_usa.z64",
            sibling_group_key="igdb:100:1",
        )
        _seed_rom_row(
            plugin,
            11,
            app_id=None,
            platform_slug="n64",
            name="Zelda (JP)",
            fs_name="zelda_jp.z64",
            sibling_group_key="igdb:100:1",
        )

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # Prove the exact worked scenario ran: the platform incremental-SKIPPED
        # (never paginated ``list_roms``), so its bound rows — but NOT the unbound
        # sibling — entered ``synced_rom_ids`` and the collection fetched rom 11
        # un-deduped, exercising the partial-view collapse branch.
        call_names = [c[0] for c in fake_romm_api.call_log]
        assert "list_roms" not in call_names
        assert "list_roms_by_collection" in call_names

        # The platform skipped its apply; the collection emitted but grandfathered
        # the group → NO shortcut entry, and above all NO rebind entry.
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        all_emitted = [sd for e in unit_events for sd in e["shortcuts"]]
        assert all(BIND_ROM_ID_KEY not in sd for sd in all_emitted)
        assert all(sd["rom_id"] != 11 for sd in all_emitted)

        # No stale removal of the live binding.
        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [{"remove": []}]

        # Binding + version untouched: rom 10 still bound to app 5000, rom 11 stays
        # an unbound tracked sibling.
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id == 5000
            assert uow.roms.get(11).shortcut_app_id is None

    @pytest.mark.asyncio
    async def test_disabled_platform_bound_row_stale_removed_collection_does_not_rebind(self, plugin, fake_romm_api):
        """Disabled-platform variant of #1296: the bound row is stale-removed by the
        normal path; the collection still does NOT rebind in the same run.

        The group's platform is disabled, so no platform unit re-affirms rom 10 —
        it is correctly stale-removed (unbound, shortcut torn down). An enabled
        collection fetches the group's unbound sibling (rom 11), but as a partial
        view it grandfathers rather than rebinding rom 11 onto the just-freed appId.
        The result is eventually-consistent: re-enabling the platform on a later
        sync re-establishes the group's shortcut; nothing is rebound off a partial
        view here.
        """
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # rom 11 (the unbound sibling) lives on N64 + in the collection; rom 10 is
        # a bound DB row on the (now disabled) platform.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 11, "name": "Zelda (JP)", "igdb_id": 100, "fs_name_no_ext": "zelda_jp"}],
        )
        _seed_collection(fake_romm_api, collection_id=7, name="Faves", rom_ids=[11], is_favorite=True)
        plugin.settings["enabled_platforms"] = {"1": False}
        plugin.settings["enabled_collections"] = {"standard": {"7": True}}

        _seed_rom_row(
            plugin,
            10,
            app_id=5000,
            platform_slug="n64",
            name="Zelda (USA)",
            fs_name="zelda_usa.z64",
            sibling_group_key="igdb:100:1",
        )
        _seed_rom_row(
            plugin,
            11,
            app_id=None,
            platform_slug="n64",
            name="Zelda (JP)",
            fs_name="zelda_jp.z64",
            sibling_group_key="igdb:100:1",
        )

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # rom 10 is stale-removed by the normal path (its platform is gone).
        stale_events = [c.args[1] for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_stale"]
        assert stale_events == [{"remove": [{"rom_id": 10, "app_id": 5000}]}]

        # The collection did NOT rebind onto the freed appId — no rebind entry, no
        # shortcut for the unbound sibling.
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        all_emitted = [sd for e in unit_events for sd in e["shortcuts"]]
        assert all(BIND_ROM_ID_KEY not in sd for sd in all_emitted)
        assert all(sd["rom_id"] != 11 for sd in all_emitted)

        # rom 10 ends unbound (stale path); rom 11 stays unbound (grandfathered).
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id is None
            assert uow.roms.get(11).shortcut_app_id is None

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-clean"

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-empty"

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
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-cancel"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-cancel")
        assert run is not None
        assert run.status == "cancelled"
        assert run.finished_at is not None
        assert run.error == "Sync cancelled"

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_run_persists_interrupted(self, plugin, fake_romm_api):
        """A heartbeat timeout (the frontend stopped responding, not a user cancel)
        ends the run as ``interrupted`` — the terminal write branches on
        ``box.run_interrupted`` so a crash is never blamed on the user's Cancel."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Heartbeat timeout: the wait gives up (None) while the box is still
        # RUNNING — _sync_one_unit flags run_interrupted and requests the cancel.
        async def wait_timeout(_u, _event):
            return

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_timeout
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-interrupted"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-interrupted")
        assert run is not None
        assert run.status == "interrupted"
        assert run.finished_at is not None
        assert run.error == sync_orchestrator._SYNC_INTERRUPTED

    @pytest.mark.asyncio
    async def test_exception_in_unit_loop_persists_errored(self, plugin, fake_romm_api):
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # build_work_queue succeeds, then list_roms raises during the unit fetch.
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        fake_romm_api.list_roms_side_effect = RuntimeError("boom")
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-error"

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-terminal-fail"

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._orchestrator._clock.monotonic()
        plugin._sync_service._box.last_unit_results = {"10": 9000}

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results == {"10": 9000}

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
        plugin._sync_service._sync_last_heartbeat = plugin._sync_service._orchestrator._clock.monotonic()

        results = await plugin._sync_service._orchestrator._wait_for_unit_complete(unit, event)
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_on_heartbeat_timeout(self, plugin):
        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        event = asyncio.Event()
        plugin._sync_service._box.sync_state = SyncState.RUNNING
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
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 1
        box.active_chunk_index = 0
        event = asyncio.Event()
        box.unit_complete_event = event
        assert not event.is_set()

        await plugin.report_unit_results({}, "run-1", 1, 0)

        assert event.is_set()
        assert box.last_unit_results == {}

    @pytest.mark.asyncio
    async def test_records_last_unit_results(self, plugin):
        plugin._sync_service._pending_sync = {}
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 1
        box.active_chunk_index = 0
        box.unit_complete_event = asyncio.Event()

        result = await plugin.report_unit_results({"10": 9001, "11": 9002}, "run-1", 1, 0)

        assert result["success"] is True
        assert result["count"] == 2
        assert box.last_unit_results == {"10": 9001, "11": 9002}

    @pytest.mark.asyncio
    async def test_stale_run_id_ack_is_ignored_not_credited_to_new_run(self, plugin):
        """A late ack from a cancelled run A, arriving while run B is in flight,
        is ignored — not signalled, not recorded, not committed (#1041).

        Run B's wait event must stay UNSET and its registry untouched: the late
        ack carries run A's id, which no longer matches ``current_sync_id``."""
        plugin._sync_service._pending_sync = {}
        box = plugin._sync_service._box
        # Run B is the active run, waiting on its own unit's event.
        box.current_sync_id = "run-B"
        box.active_unit_id = 7
        box.active_chunk_index = 0
        event = asyncio.Event()
        box.unit_complete_event = event

        # Late ack from the cancelled run A (stale run id) for the old unit.
        result = await plugin.report_unit_results({"10": 9001}, "run-A", 1, 0)

        assert result == {"success": True, "count": 0, "ignored": True}
        # Run B is untouched: its event stays unset and no result was recorded.
        assert not event.is_set()
        assert box.last_unit_results is None
        # Nothing committed to run B's registry.
        assert plugin._uow.committed is False
        with plugin._uow as uow:
            assert uow.roms.get(10) is None

    @pytest.mark.asyncio
    async def test_mismatched_unit_id_same_run_is_ignored(self, plugin):
        """An ack for a different unit within the SAME run is ignored — the
        unit_id guard rejects it even though the run id matches (#1041)."""
        plugin._sync_service._pending_sync = {}
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 5  # unit 5 is the active one
        box.active_chunk_index = 0
        event = asyncio.Event()
        box.unit_complete_event = event

        result = await plugin.report_unit_results({"10": 9001}, "run-1", 99, 0)

        assert result == {"success": True, "count": 0, "ignored": True}
        assert not event.is_set()
        assert box.last_unit_results is None

    @pytest.mark.asyncio
    async def test_stale_chunk_index_same_unit_is_ignored(self, plugin):
        """An ack for a superseded chunk of the ACTIVE unit is ignored — the
        chunk-index guard rejects it even though run + unit match. A crash-late
        ack for chunk 0 must never be credited to chunk 1 in flight."""
        plugin._sync_service._pending_sync = {}
        box = plugin._sync_service._box
        box.current_sync_id = "run-1"
        box.active_unit_id = 1
        box.active_chunk_index = 1  # chunk 1 is the active one
        event = asyncio.Event()
        box.unit_complete_event = event

        result = await plugin.report_unit_results({"10": 9001}, "run-1", 1, 0)

        assert result == {"success": True, "count": 0, "ignored": True}
        assert not event.is_set()
        assert box.last_unit_results is None

    @pytest.mark.asyncio
    async def test_late_ack_after_abandon_commits_binding(self, plugin):
        """A late ack for a heartbeat-timed-out chunk commits the delivered
        bindings itself instead of discarding them (#1052 / #1367).

        The box is driven into the real production post-timeout state (chunk
        stashed via ``stash_abandoned_chunk``, run wound down via ``finish_run``
        so ``current_sync_id`` is None). The late ack — whose active-unit check
        can no longer match — recovers the ``abandoned_chunk`` stash, drives
        ``commit_unit_results`` directly, persists the ``roms`` binding +
        metadata, and clears the stash."""
        _entry = {"name": "Game", "fs_name": "game.z64", "platform_slug": "gb", "cover_path": ""}
        box = _stash_abandoned_and_wind_down(
            plugin,
            run_id="run-1",
            unit_id=1,
            chunk_index=0,
            pending={42: _entry},
            chunk_rows=[{"id": 42, "metadatum": {"genres": ["RPG"]}}],
        )
        # The run has wound down: the active-unit identity is gone.
        assert box.current_sync_id is None
        assert box.active_unit_id is None

        result = await plugin.report_unit_results({"42": 100001}, "run-1", 1, 0)

        assert result == {"success": True, "count": 1}
        # The binding was committed (not discarded).
        with plugin._uow as uow:
            rom = uow.roms.get(42)
            meta = uow.rom_metadata.get(42)
        assert rom is not None
        assert rom.shortcut_app_id == 100001
        # Metadata stamped from the stashed chunk rows.
        assert meta is not None
        assert meta.genres == ("RPG",)
        # The stash is cleared so a duplicate late ack no longer recovers it.
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_duplicate_late_ack_after_recovery_is_ignored(self, plugin):
        """The first late ack pops the stash and commits; a second identical late
        ack finds no stash and is ignored, so nothing is double-committed
        (#1367)."""
        _entry = {"name": "Game", "fs_name": "game.z64", "platform_slug": "gb", "cover_path": ""}
        box = _stash_abandoned_and_wind_down(
            plugin,
            run_id="run-1",
            unit_id=1,
            chunk_index=0,
            pending={42: _entry},
            chunk_rows=[{"id": 42}],
        )

        first = await plugin.report_unit_results({"42": 100001}, "run-1", 1, 0)
        second = await plugin.report_unit_results({"42": 100001}, "run-1", 1, 0)

        assert first == {"success": True, "count": 1}
        assert second == {"success": True, "count": 0, "ignored": True}
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_late_ack_with_wrong_identity_is_ignored_and_stash_intact(self, plugin):
        """A late ack whose chunk index does not match the stash is ignored and
        leaves the stash untouched, so the genuine late ack can still recover it
        (#1367)."""
        _entry = {"name": "Game", "fs_name": "game.z64", "platform_slug": "gb", "cover_path": ""}
        box = _stash_abandoned_and_wind_down(
            plugin,
            run_id="run-1",
            unit_id=1,
            chunk_index=0,
            pending={42: _entry},
            chunk_rows=[{"id": 42}],
        )

        # Wrong chunk index → no stash match.
        result = await plugin.report_unit_results({"42": 100001}, "run-1", 1, 9)

        assert result == {"success": True, "count": 0, "ignored": True}
        # Nothing committed and the stash survives for the real late ack.
        with plugin._uow as uow:
            assert uow.roms.get(42) is None
        assert box.abandoned_chunk is not None

    @pytest.mark.asyncio
    async def test_late_ack_binds_stashed_rom_without_metadatum_stamps_no_metadata(self, plugin):
        """A stashed ROM carrying no ``metadatum`` still binds via the late ack,
        but stamps no metadata — the binding is load-bearing, metadata is
        best-effort (#1052)."""
        _entry = {"name": "A", "fs_name": "a.z64", "platform_slug": "gb", "cover_path": ""}
        _stash_abandoned_and_wind_down(
            plugin,
            run_id="run-1",
            unit_id=1,
            chunk_index=0,
            pending={42: _entry},
            # The stash (the chunk fetch) carries rom 42 without a metadatum.
            chunk_rows=[{"id": 42}],
        )

        result = await plugin.report_unit_results({"42": 100001}, "run-1", 1, 0)

        assert result == {"success": True, "count": 1}
        with plugin._uow as uow:
            rom = uow.roms.get(42)
            meta = uow.rom_metadata.get(42)
        # Binding still committed.
        assert rom is not None
        assert rom.shortcut_app_id == 100001
        # No metadata: rom 42 carried no metadatum.
        assert meta is None

    @pytest.mark.asyncio
    async def test_stray_ack_on_active_chunk_without_live_wait_is_noop(self, plugin):
        """An ack for the ACTIVE chunk whose event was already consumed (a stray
        duplicate) records the mapping but commits nothing — the active-unit
        branch signals no event and never double-commits (#1052)."""
        box = plugin._sync_service._box
        box.try_begin_run("run-1")
        box.active_unit_id = 1
        box.active_chunk_index = 0
        box.unit_complete_event = None
        box.pending_sync = {}

        result = await plugin.report_unit_results({"42": 100001}, "run-1", 1, 0)

        assert result == {"success": True, "count": 1}
        # The mapping is still recorded, but NOTHING is committed.
        assert box.last_unit_results == {"42": 100001}
        assert plugin._uow.committed is False
        with plugin._uow as uow:
            assert uow.roms.get(42) is None

    @pytest.mark.asyncio
    async def test_late_ack_of_timed_out_chunk_commits_only_that_chunk(self, plugin):
        """A late ack for an abandoned CHUNK commits only that chunk's stashed rows
        (the chunk subset), validated by run + unit + chunk index (#1025/#1052).

        Models the state a chunk-1 heartbeat timeout leaves behind: the chunk's
        two rows stashed under identity (run-1, unit 1, chunk 1). The ack for
        chunk 1 recovers them while an ack for any other chunk would be
        ignored."""
        entries = {
            3: {"name": "C", "fs_name": "c.z64", "platform_slug": "n64", "cover_path": ""},
            4: {"name": "D", "fs_name": "d.z64", "platform_slug": "n64", "cover_path": ""},
        }
        box = _stash_abandoned_and_wind_down(
            plugin,
            run_id="run-1",
            unit_id=1,
            chunk_index=1,
            pending=entries,
            chunk_rows=[{"id": 3}, {"id": 4}],
        )

        result = await plugin.report_unit_results({"3": 7003, "4": 7004}, "run-1", 1, 1)

        assert result == {"success": True, "count": 2}
        with plugin._uow as uow:
            assert uow.roms.get(3).shortcut_app_id == 7003
            assert uow.roms.get(4).shortcut_app_id == 7004
        assert box.abandoned_chunk is None


class TestLateAckReconciliationWithStaleScan:
    """#1052 ↔ #1036 reconciliation: a binding committed via the late-ack path
    must be excluded from a stale scan, exactly like a happy-path binding.

    ``committed_app_ids`` accumulates from EVERY commit — both the orchestrator's
    in-loop ack and the reporter's late-ack commit (#1052). If it only captured
    the happy path, a late-committed binding could still be wiped by a later
    stale scan, re-opening the #1036 data-loss bug."""

    @pytest.mark.asyncio
    async def test_late_ack_appid_excluded_from_subsequent_stale_scan(self, plugin):
        """A unit times out → its binding commits late via report_unit_results →
        a subsequent stale scan does NOT remove that appId.

        The late ack both binds the row (app 5000) AND records it in
        committed_app_ids; the stale scan then excludes app 5000 even though the
        old colliding row (rom 1) looks stale (#1036 collision via the #1052
        late-ack path)."""
        # Old colliding bound row (a prior server's rom_id for the same game).
        _seed_rom_row(plugin, 1, app_id=5000, platform_slug="n64", name="A", fs_name="a.z64")

        # The production post-timeout state for the NEW rom_id (2), which the
        # frontend acks with the SAME reused appId, run wound down. The fresh box
        # starts with an empty committed_app_ids (the real reset lives in
        # _do_sync_per_unit); the late-ack commit accumulates app 5000 into it.
        _entry = {"name": "A", "fs_name": "a.z64", "platform_slug": "n64", "cover_path": ""}
        box = _stash_abandoned_and_wind_down(
            plugin,
            run_id="run-1",
            unit_id=1,
            chunk_index=0,
            pending={2: _entry},
            chunk_rows=[{"id": 2}],
        )

        # Late ack: commits the binding AND records app 5000 in committed_app_ids.
        await plugin.report_unit_results({"2": 5000}, "run-1", 1, 0)

        assert 5000 in box.committed_app_ids
        # rom 2 now holds app 5000; rom 1 was unbound by the collision-safe save.
        with plugin._uow as uow:
            assert uow.roms.get(2).shortcut_app_id == 5000
            assert uow.roms.get(1).shortcut_app_id is None

        # A subsequent stale scan (rom 1 not in synced_rom_ids) must NOT emit
        # app 5000 for removal — it's a freshly-committed binding.
        stale = await plugin.loop.run_in_executor(
            None,
            plugin._sync_service._orchestrator._scan_stale_roms,
            set(),  # synced_rom_ids — neither rom counts as synced for this scan
            set(box.committed_app_ids),
        )
        # rom 1 is already unbound (Layer 2), so it's not even a candidate; and
        # if it were, app 5000 is in committed_app_ids (Layer 1) → excluded.
        assert all(app_id != 5000 for _rid, app_id in stale)
        assert stale == []


class TestRealOrchestratorLateAckRecovery:
    """The #1367 acceptance path end-to-end through the REAL orchestrator.

    Drives a real ``_do_sync_per_unit`` whose only frontend ack never arrives, so
    the real heartbeat-clocked wait times out, the real ``_abandon_active_chunk``
    stashes the chunk, and the run's terminal ``finally`` winds the run down
    (``finish_run`` nulls ``current_sync_id``). Then the real
    ``report_unit_results`` — carrying the identity captured from the emitted
    ``sync_apply_unit`` event — recovers the stash and commits the binding. No box
    state is hand-set; the commit is observed through the shared UoW."""

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_then_late_ack_commits_binding(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A", "fs_name": "a.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        # The frontend never acks, so the REAL _wait_for_unit_complete runs; the
        # clock-advancing sleeper pushes it past the heartbeat timeout on the
        # second poll — a genuine timeout, no cancel, no stubbed wait.
        orch = plugin._sync_service._orchestrator
        orch._sleeper = _ClockAdvancingSleeper(orch._clock, 999.0)

        # Real run start (claims the slot + stamps current_sync_id).
        assert plugin._sync_service._box.try_begin_run("run-headline") is True
        plugin._sync_service._box.sync_last_heartbeat = orch._clock.monotonic()

        await orch._do_sync_per_unit()

        box = plugin._sync_service._box
        # The run wound down: the slot is free and a chunk is stashed for recovery.
        assert box.current_sync_id is None
        assert box.abandoned_chunk is not None
        assert box.run_interrupted is True
        # The chunk timed out before its commit, so rom 1 has no row yet.
        with plugin._uow as uow:
            assert uow.roms.get(1) is None
        # The run was persisted as interrupted (not cancelled) by the real teardown.
        with plugin._uow as uow:
            terminal = uow.sync_runs.get_latest_terminal()
        assert terminal is not None
        assert terminal.status == "interrupted"

        # Capture the identity the frontend echoes back from the emitted event.
        apply_events = [c.args[1] for c in decky.emit.call_args_list if c.args[0] == "sync_apply_unit"]
        assert len(apply_events) == 1
        ev = apply_events[0]

        # The late ack arrives AFTER the run wound down — the exact production
        # timing #1367 fixes. It recovers the stash and commits the binding.
        result = await plugin.report_unit_results({"1": 90001}, ev["run_id"], ev["unit_id"], ev["chunk_index"])

        assert result == {"success": True, "count": 1}
        with plugin._uow as uow:
            rom = uow.roms.get(1)
        assert rom is not None
        assert rom.shortcut_app_id == 90001
        # The stash is consumed so a duplicate late ack can't double-commit.
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_next_run_start_drops_an_unacked_stash(self, plugin, fake_romm_api):
        """If the frontend crash never acks, the abandoned chunk is inert until the
        next run's ``try_begin_run`` drops it — bounded lifetime (#1367)."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A", "fs_name": "a.z64"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        orch = plugin._sync_service._orchestrator
        orch._sleeper = _ClockAdvancingSleeper(orch._clock, 999.0)

        assert plugin._sync_service._box.try_begin_run("run-1") is True
        plugin._sync_service._box.sync_last_heartbeat = orch._clock.monotonic()
        await orch._do_sync_per_unit()

        box = plugin._sync_service._box
        assert box.abandoned_chunk is not None

        # A fresh run starts before any late ack — the stale stash is dropped.
        assert box.try_begin_run("run-2") is True
        assert box.abandoned_chunk is None

        # A late ack for the old run now finds nothing and is ignored.
        result = await plugin.report_unit_results({"1": 90001}, "run-1", 1, 0)
        assert result == {"success": True, "count": 0, "ignored": True}
        with plugin._uow as uow:
            assert uow.roms.get(1) is None


class TestCommitUnitResults:
    """Orchestrator-driven per-unit commit: cover-path finalize + ``roms`` + ``rom_metadata`` upsert."""

    @pytest.mark.asyncio
    async def test_updates_registry_for_unit_roms(self, plugin):
        box = plugin._sync_service._box
        entries = {
            10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
            11: {"rom_id": 11, "name": "B", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""},
        }
        box.pending_sync = entries
        box.pending_all_roms = entries

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001, "11": 9002}, [{"id": 10}, {"id": 11}])

        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id == 9001
            assert uow.roms.get(11).shortcut_app_id == 9002

    @pytest.mark.asyncio
    async def test_commits_roms_for_unit(self, plugin):
        """commit_unit_results lands the unit's ROM upserts in one committed UoW."""
        box = plugin._sync_service._box
        entries = {10: {"rom_id": 10, "name": "A", "platform_name": "N64", "platform_slug": "n64", "cover_path": ""}}
        box.pending_sync = entries
        box.pending_all_roms = entries

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001}, [{"id": 10}])

        assert plugin._uow.committed is True
        with plugin._uow as uow:
            assert uow.roms.get(10) is not None

    @pytest.mark.asyncio
    async def test_persists_unbound_non_representative_siblings(self, plugin):
        """Group-aware persist (ADR-0021): every fetched ROM gets an identity row,
        but a sibling absent from the ack lands UNBOUND — only representatives
        carry a binding."""
        box = plugin._sync_service._box
        entries = {
            10: {"rom_id": 10, "name": "A (USA)", "platform_slug": "n64", "cover_path": "", "sibling_group_key": "g"},
            11: {"rom_id": 11, "name": "A (JP)", "platform_slug": "n64", "cover_path": "", "sibling_group_key": "g"},
        }
        # Only rom 10 is the representative (emitted + acked); rom 11 is a sibling.
        box.pending_sync = {10: entries[10]}
        box.pending_all_roms = entries

        await plugin._sync_service._reporter.commit_unit_results({"10": 9001}, [{"id": 10}, {"id": 11}])

        with plugin._uow as uow:
            rep = uow.roms.get(10)
            sibling = uow.roms.get(11)
        assert rep is not None and rep.shortcut_app_id == 9001
        # The non-representative sibling is persisted for its identity + version,
        # but carries no shortcut binding.
        assert sibling is not None
        assert sibling.shortcut_app_id is None
        assert sibling.sibling_group_key == "g"


class TestShutdown:
    """Tests for shutdown().

    Graceful shutdown flips a RUNNING sync into CANCELLING so the
    per-unit loop drops its in-flight work on the next checkpoint.
    """

    def test_shutdown_when_running_marks_cancelling(self, plugin):
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.CANCELLING

    def test_shutdown_when_idle_is_noop(self, plugin):
        plugin._sync_service._box.sync_state = SyncState.IDLE
        plugin._sync_service.shutdown()
        assert plugin._sync_service._sync_state == SyncState.IDLE

    def test_shutdown_when_cancelling_is_noop(self, plugin):
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "sync-cancel-build"

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
    async def test_cancel_mid_unit_fetch_finalizes_gracefully(self, plugin, fake_romm_api):
        """A cooperative cancel delivered MID per-unit fetch recovers all state (#1035).

        The cancel arrives while ``_sync_one_unit`` is fetching the unit's
        ROMs (``fetcher._check_cancelling`` raising ``SyncCancelled`` from
        inside ``list_roms``) — NOT at an ``is_cancelling()`` checkpoint and
        NOT during ``build_work_queue``. ``SyncCancelled`` is a
        ``BaseException`` (like ``asyncio.CancelledError``), so it unwinds
        through the fetcher's ``except Exception`` re-raise around ``list_roms``
        untouched and lands in ``_do_sync_per_unit``'s dedicated
        ``except SyncCancelled``. On the un-fixed code (raising
        ``asyncio.CancelledError`` and catching it) sonar's S7497 would flag the
        swallow; the refactor uses a distinct cooperative type so the swallow is
        scoped to the cooperative signal only.

        The handler routes that mid-fetch SyncCancelled into the same graceful
        finalize the checkpoint break uses. This asserts all three recovery
        post-conditions AND that ``_do_sync_per_unit`` does NOT propagate the
        cooperative cancel (contrast with the build_work_queue path, which
        re-raises a real asyncio cancel).
        """
        from domain.sync_state import SyncCancelled

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # One live-fetch platform (no last_sync, empty registry) so the unit
        # takes the real per-unit fetch rather than the incremental-skip path.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}

        # The per-unit ROM fetch raises SyncCancelled mid-flight — exactly how
        # ``fetcher._check_cancelling`` now signals a cooperative cancel that
        # landed after the platform listing but before the unit ack. A tracked
        # MagicMock (not just ``list_roms_side_effect``) lets us pin that the
        # signal was raised FROM the per-unit fetch — not bypassed by an early
        # ``is_cancelling()`` checkpoint or the incremental-skip path, which would
        # finalize gracefully for the WRONG reason.
        fake_romm_api.list_roms = MagicMock(side_effect=SyncCancelled("Sync cancelled"))

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-mid-fetch-cancel"

        # Guard against cross-test state leakage: a stale CANCELLING at entry
        # would break the unit loop before the fetch and pass for the wrong reason.
        assert plugin._sync_service._sync_state == SyncState.RUNNING

        # Must NOT propagate the cooperative cancel — awaiting returns normally.
        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # The cooperative signal genuinely originated from the per-unit fetch.
        fake_romm_api.list_roms.assert_called()

        # 1. sync_state restored to IDLE (not stuck CANCELLING).
        assert plugin._sync_service._sync_state == SyncState.IDLE
        # 2. The SyncRun row is marked cancelled (not left ``running``).
        with plugin._uow as uow:
            run = uow.sync_runs.get("run-mid-fetch-cancel")
        assert run is not None
        assert run.status == "cancelled"
        assert run.finished_at is not None
        # 3. The persisted progress snapshot is no longer running.
        assert plugin._sync_service._orchestrator.get_sync_status()["running"] is False

    @pytest.mark.asyncio
    async def test_real_asyncio_cancel_mid_fetch_is_not_swallowed(self, plugin, fake_romm_api):
        """A REAL ``asyncio.CancelledError`` mid per-unit fetch PROPAGATES (#1035).

        This is the key safety guard the SyncCancelled split buys: the
        cooperative cancel signal is now a DISTINCT type (``SyncCancelled``),
        so the unit-loop ``except SyncCancelled`` does NOT catch a genuine
        ``asyncio.CancelledError`` (e.g. the sync task being cancelled by the
        runtime). Were the handler still ``except asyncio.CancelledError`` (the
        S7497-flagged pre-refactor shape), this real cancel would be swallowed
        into the graceful finalize and the run wrongly marked ``cancelled`` —
        masking a real task cancellation.

        The real cancel is injected at the ``list_roms`` layer, unwinds through
        the fetcher's ``except Exception`` re-raise, skips the unit-loop
        ``except SyncCancelled`` AND the outer ``except Exception`` (both narrower
        than ``BaseException``), and propagates straight out of
        ``_do_sync_per_unit``. The SyncRun is left ``running`` — it is NOT marked
        cancelled by the cooperative swallow path.
        """
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

        # A genuine asyncio task cancellation lands mid-fetch — NOT the
        # cooperative SyncCancelled signal. A tracked MagicMock guarantees a
        # 'DID NOT RAISE' can never be a silent fetch bypass: list_roms.assert_called()
        # below pins that the real cancel originated from the per-unit fetch.
        fake_romm_api.list_roms = MagicMock(side_effect=asyncio.CancelledError("real task cancel"))

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-real-cancel"

        # Guard against cross-test state leakage (a stale CANCELLING would break
        # the loop before the fetch and the cancel would never fire).
        assert plugin._sync_service._sync_state == SyncState.RUNNING

        # The real cancel must PROPAGATE OUT — the cooperative handler does not
        # catch it.
        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service._orchestrator._do_sync_per_unit()

        # The cancel genuinely fired from the per-unit fetch (not a bypass).
        fake_romm_api.list_roms.assert_called()

        # The run is NOT marked cancelled by the swallow path — a real task
        # cancel leaves the SyncRun ``running`` (never routed through the
        # graceful cooperative finalize).
        with plugin._uow as uow:
            run = uow.sync_runs.get("run-real-cancel")
        assert run is not None
        assert run.status == "running"
        assert run.finished_at is None

    @pytest.mark.asyncio
    async def test_real_asyncio_cancel_mid_preview_is_not_swallowed(self, plugin, fake_romm_api):
        """A REAL ``asyncio.CancelledError`` mid sync_preview PROPAGATES (#1035).

        Symmetric to the per-unit guard: ``sync_preview``'s
        ``except SyncCancelled`` catches only the cooperative signal. A genuine
        ``asyncio.CancelledError`` injected at the fetch layer skips it (and the
        generic ``except Exception``) and propagates straight out of the
        callable — it is NOT mapped onto the canonical ``cancelled`` failure
        dict. The ``finally`` still restores sync_state to IDLE.
        """
        _use_fake_romm(plugin, fake_romm_api)

        fake_romm_api.list_platforms = MagicMock(side_effect=asyncio.CancelledError("real task cancel"))
        plugin.settings["enabled_platforms"] = {"1": True}

        # sync_preview only runs from IDLE — guard against a leaked non-IDLE state
        # that would short-circuit it to "sync_in_progress" before the fetch.
        assert plugin._sync_service._sync_state == SyncState.IDLE

        with pytest.raises(asyncio.CancelledError):
            await plugin._sync_service.sync_preview()

        # The cancel genuinely fired from the fetch, not a bypass.
        fake_romm_api.list_platforms.assert_called()

        # The ``finally`` block always restores IDLE, even on a propagated cancel.
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
        plugin._sync_service._box.sync_state = SyncState.CANCELLING

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
        plugin.settings["enabled_collections"] = {"standard": {"7": True}, "smart": {}, "virtual": {}}

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        # sync_complete fired (collection_memberships flowed through to finalize).
        complete = [c for c in decky.emit.call_args_list if c.args and c.args[0] == "sync_complete"]
        assert len(complete) == 1

    @pytest.mark.asyncio
    async def test_sync_collection_unit_threads_kind_and_virtual_type(self, plugin):
        """_sync_collection_unit stamps the unit's kind + virtual_type onto the membership (#1539)."""
        orch = plugin._sync_service._orchestrator
        orch._fetcher.fetch_collection_unit = AsyncMock(return_value=([{"id": 1}], [1, 2], False))  # type: ignore[method-assign]
        unit = WorkUnit(
            type="collection",
            id="vc-1",
            name="coll-fr",
            slug="coll-fr",
            rom_count=2,
            collection_kind="virtual",
            virtual_type="franchise",
        )
        memberships: dict[tuple[str, str], Any] = {}

        await orch._sync_collection_unit(unit, synced_rom_ids=set(), collection_memberships=memberships)

        membership = memberships[("virtual", "vc-1")]
        assert membership.name == "coll-fr"
        assert membership.rom_ids == [1, 2]
        assert membership.kind == "virtual"
        assert membership.virtual_type == "franchise"

    @pytest.mark.asyncio
    async def test_sync_collection_unit_standard_has_no_virtual_type(self, plugin):
        """A standard collection membership carries kind='standard' and virtual_type=None (#1539)."""
        orch = plugin._sync_service._orchestrator
        orch._fetcher.fetch_collection_unit = AsyncMock(return_value=([{"id": 1}], [1], False))  # type: ignore[method-assign]
        unit = WorkUnit(
            type="collection",
            id="7",
            name="Faves",
            slug="faves",
            rom_count=1,
            collection_kind="standard",
        )
        memberships: dict[tuple[str, str], Any] = {}

        await orch._sync_collection_unit(unit, synced_rom_ids=set(), collection_memberships=memberships)

        membership = memberships[("standard", "7")]
        assert membership.kind == "standard"
        assert membership.virtual_type is None

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

        def list_roms_then_cancel(platform_id, limit=LIST_PAGE_SIZE, offset=0):
            page = orig_list_roms(platform_id, limit=limit, offset=offset)
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return page

        fake_romm_api.list_roms = list_roms_then_cancel  # type: ignore[method-assign]

        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._download_artwork = cancel_during_artwork
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
    async def test_cancel_after_artwork_still_counts_delta_skips(self, plugin, fake_romm_api):
        """A mid-unit cancel AFTER the delta is computed returns the delta-skip
        count: those ROMs were verified already-correct in Steam, so they are
        processed — same as a wholesale-skipped unit's ROMs — and the terminal
        frame's "N of M games processed" numerator must not drop them."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # rom 10 is content-unchanged (delta-skipped); rom 11 is brand-new, so
        # the unit reaches the artwork step where the cancel lands.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Keep", "fs_name": "keep.z64"},
                {"id": 11, "name": "Fresh", "fs_name": "fresh.z64"},
            ],
        )
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")

        async def cancel_during_artwork(*_a, **_kw):
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return {}

        plugin._sync_service._orchestrator._download_artwork = cancel_during_artwork
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        processed = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )
        assert processed == 1, "the delta-skipped ROM is processed; the cancelled apply is not"

    @pytest.mark.asyncio
    async def test_cancel_after_cover_refresh_still_counts_delta_skips(self, plugin, fake_romm_api):
        """Same invariant at the sibling guard: a cancel landing during the
        cover-refresh pass — after the delta is computed, before the artwork
        download — still returns the delta-skip count."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # rom 10 is content-unchanged (delta-skipped); the cancel lands inside
        # the cover-refresh pass, so the guard right after it fires.
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Keep", "fs_name": "keep.z64"}],
        )
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")

        async def cancel_during_cover_refresh(*_a, **_kw):
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return []

        plugin._sync_service._orchestrator._refresh_changed_covers = cancel_during_cover_refresh
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        processed = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )
        assert processed == 1, "the delta-skipped ROM is processed even on a pre-apply cancel"

    @pytest.mark.asyncio
    async def test_user_cancel_clears_pending_and_drops_event(self, plugin, fake_romm_api):
        """A user cancel during the wait discards in-flight work: pending_sync
        cleared, unit event nulled, no abandoned-chunk stash.

        ``_wait_for_unit_complete`` returns None while the box is already
        CANCELLING (the cancel branch), so the unit's in-flight state is
        intentionally dropped and a stray late ack can't commit it (#1052)."""
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

        # The wait observes a user cancel: flip CANCELLING, then give up (None).
        async def wait_user_cancel(_unit, _event):
            plugin._sync_service._box.sync_state = SyncState.CANCELLING
            return

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_user_cancel
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        # User cancel: pending_sync cleared, unit event dropped, state CANCELLING.
        assert plugin._sync_service._pending_sync == {}
        assert plugin._sync_service._box.unit_complete_event is None
        assert plugin._sync_service._sync_state == SyncState.CANCELLING
        # No abandoned-chunk stash — a cancel intentionally discards the work.
        assert plugin._sync_service._box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_retains_pending_and_stashes_roms(self, plugin, fake_romm_api):
        """A heartbeat timeout (not a cancel) stashes the abandoned chunk so a
        late ``report_unit_results`` can still commit the delivered bindings.

        The wait returns None while the box is still RUNNING (the timeout
        branch): the chunk is moved into ``abandoned_chunk`` (its rows captured),
        the whole-unit ``pending_sync`` staging stays live for the late-ack
        commit to read, and the dispatch identity (``unit_complete_event`` +
        ``active_unit_id`` + ``active_chunk_index``) is cleared. The box flips
        CANCELLING so the outer loop stops (#1052 / #1367)."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}],
        )

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Heartbeat timeout: the wait gives up (None) WITHOUT a user cancel.
        async def wait_timeout(_unit, _event):
            return

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_timeout
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        box = plugin._sync_service._box
        # Timeout: whole-unit staging RETAINED so the late-ack commit can read it,
        # but the dispatch identity is cleared (moved into the stash).
        assert plugin._sync_service._pending_sync != {}
        assert box.unit_complete_event is None
        assert box.active_unit_id is None
        assert box.active_chunk_index is None
        assert plugin._sync_service._sync_state == SyncState.CANCELLING
        assert box.run_interrupted is True
        # The chunk is stashed (identity + rows) for the late-ack commit.
        assert box.abandoned_chunk is not None
        assert (box.abandoned_chunk.unit_id, box.abandoned_chunk.chunk_index) == (1, 0)
        assert [r["id"] for r in box.abandoned_chunk.chunk_rows] == [1]


class TestApplyChunking:
    """A unit's apply is split into durable commit chunks (#1025).

    Each chunk is emitted → acked → committed on its own, so a mid-unit CEF
    crash forfeits only the in-flight chunk. These tests drive ``_sync_one_unit``
    directly with a shrunk ``_APPLY_CHUNK_SIZE`` so a handful of singleton ROMs
    exercises the multi-chunk loop; the exact partition maths is pinned in
    ``tests/domain/test_sync_chunking.py``.
    """

    @pytest.mark.asyncio
    async def test_large_unit_emits_one_event_and_commit_per_chunk(self, plugin, fake_romm_api, monkeypatch):
        """Five singletons at chunk size 2 → three ``sync_apply_unit`` events with
        continuous unit-wide chunk fields, and one commit per chunk carrying only
        that chunk's rows."""
        import decky

        from services.library import sync_orchestrator

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"Game {i}"} for i in range(1, 6)],
        )

        commit_rows: list[list[int]] = []

        async def capture_commit(_rid_to_aid, chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_rows.append([r["id"] for r in chunk_rows])

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 3
        assert [e["chunk_index"] for e in unit_events] == [0, 1, 2]
        assert all(e["chunk_count"] == 3 for e in unit_events)
        assert [e["chunk_offset"] for e in unit_events] == [0, 2, 4]
        assert all(e["unit_total"] == 5 for e in unit_events)
        assert [len(e["shortcuts"]) for e in unit_events] == [2, 2, 1]
        # One commit per chunk, each with only its chunk's rows.
        assert commit_rows == [[1, 2], [3, 4], [5]]

    @pytest.mark.asyncio
    async def test_small_unit_emits_exactly_one_chunk(self, plugin, fake_romm_api):
        """A unit under the chunk size emits a single chunk — regression guard that
        the chunk fields collapse to the today's one-shot behaviour."""
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 4)],
        )

        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-single"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=3)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        event = unit_events[0]
        assert event["chunk_index"] == 0
        assert event["chunk_count"] == 1
        assert event["chunk_offset"] == 0
        assert event["unit_total"] == 3
        assert len(event["shortcuts"]) == 3

    @pytest.mark.asyncio
    async def test_user_cancel_between_chunks_keeps_committed_chunks(self, plugin, fake_romm_api, monkeypatch):
        """A user cancel during chunk 1's wait discards the rest but leaves chunk 0
        committed — the whole point of chunking (#1025)."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        commit_rows: list[list[int]] = []

        async def capture_commit(_rid_to_aid, chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_rows.append([r["id"] for r in chunk_rows])

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        box = plugin._sync_service._box

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            box.sync_state = SyncState.CANCELLING  # user cancel during chunk 1
            return None

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-cancel-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # Only chunk 0 committed; the cancel discarded chunk 1 onward.
        assert commit_rows == [[1, 2]]
        # Staging + chunk identity cleared so a stray late ack can't commit.
        assert box.pending_sync == {}
        assert box.unit_complete_event is None
        assert box.active_chunk_index is None
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_cancel_in_inter_chunk_window_never_emits_next_chunk(self, plugin, fake_romm_api, monkeypatch):
        """A cancel landing AFTER chunk 0's commit but BEFORE chunk 1's emit stops
        the unit at the top of the loop: chunk 1 is never emitted, chunk 0's commit
        persists, staging cleared. Complements
        ``test_user_cancel_between_chunks_keeps_committed_chunks`` (cancel DURING
        the wait) — this is the inter-chunk window, where an un-guarded loop would
        still emit chunk 1 and leave ~200 shortcuts orphaned until the next sync
        (#1025)."""
        import decky

        from services.library import sync_orchestrator

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        commit_rows: list[list[int]] = []
        box = plugin._sync_service._box

        async def capture_commit(_rid_to_aid, chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_rows.append([r["id"] for r in chunk_rows])
            # Cancel lands the instant chunk 0's commit resolves — before the loop
            # returns to the top to emit chunk 1.
            if len(commit_rows) == 1:
                box.sync_state = SyncState.CANCELLING

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-inter-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # Chunk 1 is never emitted — the loop stopped at its top before any emit —
        # so the frontend has no orphaned chunk to churn and later fail the ack on.
        unit_events = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]
        assert len(unit_events) == 1
        assert unit_events[0]["chunk_index"] == 0
        # Chunk 0's commit persists.
        assert commit_rows == [[1, 2]]
        # Staging + chunk identity cleared so a stray late ack can't commit.
        assert box.pending_sync == {}
        assert box.pending_all_roms == {}
        assert box.unit_complete_event is None
        assert box.active_unit_id is None
        assert box.active_chunk_index is None
        assert box.abandoned_chunk is None

    @pytest.mark.asyncio
    async def test_heartbeat_timeout_on_chunk_stashes_only_that_chunk(self, plugin, fake_romm_api, monkeypatch):
        """A heartbeat timeout on chunk 1 stashes ONLY chunk 1's rows (not the whole
        unit) under chunk 1's identity, so a late ack commits just that chunk."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        box = plugin._sync_service._box

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            return None  # heartbeat timeout on chunk 1 (no cancel)

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-timeout-chunk"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        assert box.abandoned_chunk is not None
        # The dispatch identity is cleared; the chunk index lives on the stash.
        assert box.active_chunk_index is None
        assert box.abandoned_chunk.chunk_index == 1
        # Only chunk 1's rows are stashed for the late ack, not the whole unit.
        assert [r["id"] for r in box.abandoned_chunk.chunk_rows] == [3, 4]
        # The timeout requested cancel so the outer loop stops.
        assert box.sync_state == SyncState.CANCELLING


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

        async def tracked_commit(rid_to_aid, acked_roms, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_calls.append((rid_to_aid, acked_roms))
            await original_commit(
                rid_to_aid, acked_roms, platform_stamp=platform_stamp, collection_stamp=collection_stamp
            )

        plugin._sync_service._reporter.commit_unit_results = tracked_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 5001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")

        commit_mock = AsyncMock()
        plugin._sync_service._reporter.commit_unit_results = commit_mock  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"10": 5001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
    async def test_all_fetched_roms_threaded_ack_subset_binds(self, plugin, fake_romm_api):
        """Group-aware persist (ADR-0021): the WHOLE unit fetch is threaded into
        ``commit_unit_results`` (every sibling gets an identity row), while the
        frontend ack — a subset — decides which representatives bind."""
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

        async def capture_commit(rid_to_aid, unit_roms, platform_stamp=None, collection_stamp=None, fetch_id=None):
            commit_calls.append((rid_to_aid, unit_roms))

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        # Frontend ack's only 3 out of 5 ROMs.
        async def fake_wait(_u, event):
            event.set()
            return {"1": 5001, "3": 5003, "5": 5005}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        rid_to_aid, unit_roms = commit_calls[0]
        # The whole fetch is threaded (all 5 siblings), the ack binds only 3.
        assert {r["id"] for r in unit_roms} == {1, 2, 3, 4, 5}
        assert set(rid_to_aid.keys()) == {"1", "3", "5"}


class TestPlatformCompletionStamp:
    """Per-platform completion stamp written on the final platform chunk (ADR-0023 / #1025).

    The stamp lets the next sync's incremental-skip gate skip a platform that fully
    synced inside a run the user later cancelled — the run never completes, so the
    library-wide ``last_sync`` never advances, but the per-platform stamp does. It
    is written ONLY when a platform unit's LAST chunk commits, never on a
    collection unit, a cancelled unit, or a heartbeat-timed-out unit.
    """

    @pytest.mark.asyncio
    async def test_stamp_written_after_final_platform_chunk(self, plugin, fake_romm_api):
        """A platform unit that completes stamps ``platform_sync_state`` with the
        server ROM count and the clock's completion timestamp (real commit)."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"1": 5001, "2": 5002}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        with plugin._uow as uow:
            stamp = uow.platform_sync_state.get("n64")
        assert stamp is not None
        # rom_count is the unit's server count; completed_at is the injected clock.
        assert stamp.rom_count == 2
        assert stamp.completed_at == "2026-01-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_stamp_only_on_final_chunk_carries_unit_rom_count(self, plugin, fake_romm_api, monkeypatch):
        """Across a multi-chunk platform, only the FINAL chunk's commit carries the
        stamp, and it records the unit's whole ``rom_count`` (not the chunk size)."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        stamps: list[Any] = []

        async def capture_commit(_rid_to_aid, _chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            stamps.append(platform_stamp)

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # 5 singletons at chunk size 2 → 3 chunks; only the last carries the stamp.
        assert len(stamps) == 3
        assert stamps[0] is None
        assert stamps[1] is None
        assert stamps[2] is not None
        assert stamps[2].platform_slug == "n64"
        assert stamps[2].rom_count == 5

    @pytest.mark.asyncio
    async def test_no_stamp_on_user_cancel_mid_unit(self, plugin, fake_romm_api, monkeypatch):
        """A user cancel before a platform's last chunk leaves NO stamp — the platform
        is only partially applied, so the next run must re-fetch it."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        box = plugin._sync_service._box

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            box.sync_state = SyncState.CANCELLING  # user cancel during chunk 1
            return None

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        with plugin._uow as uow:
            assert uow.platform_sync_state.get("n64") is None

    @pytest.mark.asyncio
    async def test_no_stamp_on_heartbeat_timeout(self, plugin, fake_romm_api):
        """A heartbeat timeout (wait returns None while NOT cancelling) abandons the
        chunk without committing it — no stamp, even on a single-chunk platform."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def timeout_wait(_unit, _event):
            return None  # heartbeat timeout — box is NOT cancelling

        plugin._sync_service._orchestrator._wait_for_unit_complete = timeout_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        with plugin._uow as uow:
            assert uow.platform_sync_state.get("n64") is None

    @pytest.mark.asyncio
    async def test_no_stamp_for_collection_unit(self, plugin, fake_romm_api):
        """Collection units have no incremental-skip gate, so they are never stamped —
        every chunk's commit carries ``platform_stamp=None``."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )
        _seed_collection(fake_romm_api, collection_id=7, name="Favs", rom_ids=[1, 2])

        stamps: list[Any] = []

        async def capture_commit(_rid_to_aid, _chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            stamps.append(platform_stamp)

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"1": 5001, "2": 5002}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="collection", id="7", name="Favs", slug="favs", rom_count=2, collection_kind="standard")
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # The collection committed (non-vacuous) but never carried a stamp.
        assert stamps, "collection unit should have committed at least one chunk"
        assert all(s is None for s in stamps)

    @pytest.mark.asyncio
    async def test_platform_threads_one_generation_through_every_chunk(self, plugin, fake_romm_api, monkeypatch):
        """Every chunk of a platform unit carries the SAME generation, and the final
        chunk's stamp records that same value (#1504).

        A per-chunk value (e.g. a clock reading) would leave the earlier chunks'
        rows on a generation the stamp never names, so the next skip would count
        only the last chunk and wedge itself off permanently.
        """
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )

        seen: list[Any] = []

        async def capture_commit(_rid_to_aid, _chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            seen.append((fetch_id, platform_stamp))

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-abc"

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        assert len(seen) == 3  # 5 singletons at chunk size 2
        assert [fetch_id for fetch_id, _ in seen] == ["run-abc"] * 3
        # The stamp names the generation its own unit's rows all carry.
        final_stamp = seen[-1][1]
        assert final_stamp is not None
        assert final_stamp.fetch_id == "run-abc"

    @pytest.mark.asyncio
    async def test_collection_unit_writes_no_generation(self, plugin, fake_romm_api):
        """A collection spans platforms, so its commits pass no generation — marking
        a foreign platform's row would drop it from that platform's counted rows
        and suppress that platform's skip (#1504)."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )
        _seed_collection(fake_romm_api, collection_id=7, name="Favs", rom_ids=[1, 2])

        seen: list[Any] = []

        async def capture_commit(_rid_to_aid, _chunk_rows, platform_stamp=None, collection_stamp=None, fetch_id=None):
            seen.append(fetch_id)

        plugin._sync_service._reporter.commit_unit_results = capture_commit  # type: ignore[method-assign]
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"1": 5001, "2": 5002}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-abc"

        unit = WorkUnit(type="collection", id="7", name="Favs", slug="favs", rom_count=2, collection_kind="standard")
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        # Non-vacuous: the collection did commit, and no commit carried a generation.
        assert seen, "collection unit should have committed at least one chunk"
        assert all(fetch_id is None for fetch_id in seen)

    @pytest.mark.asyncio
    async def test_apply_start_clears_preexisting_stamp_on_cancel_mid_unit(self, plugin, fake_romm_api, monkeypatch):
        """A pre-existing (stale) stamp is cleared when the apply begins, so a cancel
        before the final chunk leaves NO stamp — the #1025 silent-gap regression.

        Before the apply-start clear the stale stamp survived a mid-unit cancel and
        let the next sync skip the half-applied platform, dropping every game the
        cancelled run never re-bound.
        """
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": i, "name": f"G{i}"} for i in range(1, 6)],
        )
        # A stale completion stamp left by a prior fully-synced run.
        _seed_platform_stamp(plugin, "n64", at="2020-01-01T00:00:00+00:00", rom_count=5)

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        box = plugin._sync_service._box

        async def wait(_unit, event):
            if box.active_chunk_index == 0:
                event.set()
                return {}
            box.sync_state = SyncState.CANCELLING  # user cancel during chunk 1
            return None

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait
        box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        with plugin._uow as uow:
            assert uow.platform_sync_state.get("n64") is None

    @pytest.mark.asyncio
    async def test_apply_start_clears_preexisting_stamp_on_heartbeat_timeout(self, plugin, fake_romm_api):
        """A heartbeat timeout abandons the chunk without committing it, and its late-ack
        commit (unreachable today, #1367) carries no stamp — but the apply-start clear
        already removed the pre-existing stamp, so none survives the interruption."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )
        _seed_platform_stamp(plugin, "n64", at="2020-01-01T00:00:00+00:00", rom_count=2)

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def timeout_wait(_unit, _event):
            return None  # heartbeat timeout — box is NOT cancelling

        plugin._sync_service._orchestrator._wait_for_unit_complete = timeout_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        with plugin._uow as uow:
            assert uow.platform_sync_state.get("n64") is None

    @pytest.mark.asyncio
    async def test_completing_reapply_refreshes_stale_stamp(self, plugin, fake_romm_api):
        """A platform that re-applies to completion replaces a stale stamp with a fresh
        one (current server count + the injected completion clock), not the old values."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        )
        _seed_platform_stamp(plugin, "n64", at="2020-01-01T00:00:00+00:00", rom_count=99)

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})

        async def fake_wait(_u, event):
            event.set()
            return {"1": 5001, "2": 5002}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )

        with plugin._uow as uow:
            stamp = uow.platform_sync_state.get("n64")
        assert stamp is not None
        assert stamp.completed_at == "2026-01-01T00:00:00+00:00"  # fresh clock, not the stale 2020 value
        assert stamp.rom_count == 2  # current server count, not the stale 99

    @pytest.mark.asyncio
    async def test_skipped_platform_keeps_its_stamp(self, plugin, fake_romm_api):
        """An incremental-skipped platform returns before the apply-start clear, so its
        completion stamp is preserved untouched (the skip is what the stamp exists for)."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)

        # Stamp + one matching bound row + unchanged server → the fetch incremental-skips.
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]

        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._reporter.commit_unit_results = AsyncMock()  # type: ignore[method-assign]
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=1)
        applied = await plugin._sync_service._orchestrator._sync_one_unit(
            unit,
            unit_index=0,
            total_units=1,
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
        )
        # Non-vacuous: the unit actually skipped (its ROM count flows back) rather than
        # applying — the commit was never driven.
        assert applied == 1
        plugin._sync_service._reporter.commit_unit_results.assert_not_called()

        with plugin._uow as uow:
            stamp = uow.platform_sync_state.get("n64")
        assert stamp is not None
        assert stamp.rom_count == 1
        assert stamp.completed_at == "2025-01-01T00:00:00"  # untouched

    @pytest.mark.asyncio
    async def test_fetch_failure_before_chunk_loop_keeps_stamp(self, plugin, fake_romm_api):
        """A fetch that raises before the chunk loop is not an apply start, so the old
        stamp is preserved (fetch failure ≠ apply started)."""
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform_stamp(plugin, "n64", at="2020-01-01T00:00:00+00:00", rom_count=5)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("fetch exploded")

        plugin._sync_service._orchestrator._sync_platform_unit = boom
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=5)
        with pytest.raises(RuntimeError, match="fetch exploded"):
            await plugin._sync_service._orchestrator._sync_one_unit(
                unit,
                unit_index=0,
                total_units=1,
                synced_rom_ids=set(),
                collection_memberships={},
                platform_rom_ids=set(),
            )

        with plugin._uow as uow:
            stamp = uow.platform_sync_state.get("n64")
        assert stamp is not None
        assert stamp.rom_count == 5  # untouched — the apply never started


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
        plugin._sync_service._box.sync_state = SyncState.RUNNING

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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
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
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        assert is_cancelling() is False
        plugin._sync_service._box.sync_state = SyncState.CANCELLING
        assert is_cancelling() is True

    @pytest.mark.asyncio
    async def test_forwards_unit_label_to_artwork(self, plugin):
        """The unit display name is threaded through as the cover-progress label."""
        fake_download = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._artwork = MagicMock()
        plugin._sync_service._orchestrator._artwork.download_artwork = fake_download

        await plugin._sync_service._orchestrator._download_artwork(
            [{"id": 1, "name": "A"}], progress_step=1, progress_total_steps=1, label="Game Boy Advance"
        )

        assert fake_download.call_args.kwargs["label"] == "Game Boy Advance"


class TestFetchNarrationInterplay:
    """Fetch-phase narration meets the chunk-apply phase (#1025).

    The per-unit prep (anchor + paginated fetch + cover download) narrates under
    the ``fetching`` stage; the chunk loop then hands progress to the frontend
    under ``applying``. The backend must not emit a ``fetching`` frame once a
    unit's chunks start, or the coarse label would flip back and forth.
    """

    @pytest.mark.asyncio
    async def test_unit_anchor_is_fetching_stage(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        progress_frames = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_progress"]
        # The unit's first progress frame anchors the coarse bar under FETCHING,
        # not the old APPLYING that read as a frozen "Applying shortcuts".
        first = progress_frames[0]
        assert first["stage"] == "fetching"
        assert first["message"] == "Fetching N64"
        assert first["step"] == 1
        assert first["totalSteps"] == 1

    @pytest.mark.asyncio
    async def test_no_fetching_frame_after_first_chunk_emit(self, plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "A"}, {"id": 11, "name": "B"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        first_chunk_idx = next(i for i, c in enumerate(decky.emit.call_args_list) if c[0][0] == "sync_apply_unit")
        fetching_after_chunk = [
            i
            for i, c in enumerate(decky.emit.call_args_list)
            if c[0][0] == "sync_progress" and c[0][1].get("stage") == "fetching" and i > first_chunk_idx
        ]
        assert fetching_after_chunk == []
        # And the fetch actually narrated before the chunk (guards against a
        # vacuous pass where no fetching frame was ever emitted).
        fetching_before_chunk = [
            i
            for i, c in enumerate(decky.emit.call_args_list)
            if c[0][0] == "sync_progress" and c[0][1].get("stage") == "fetching" and i < first_chunk_idx
        ]
        assert fetching_before_chunk


class TestComponentGroupKeyStamping:
    """The per-unit / preview stamp of component sibling-group keys (#1368).

    ``_stamp_component_group_keys`` delegates to the pure kernel and writes the
    result back onto the raw ROM dicts before ``build_shortcuts_data`` so the whole
    sync pipeline (shortcut build, group collapse, commit) reads the component key
    rather than a per-ROM coalesce-first key.
    """

    @pytest.mark.asyncio
    async def test_platform_unit_stamps_shared_component_key(self, plugin, fake_romm_api):
        # Two dumps of one game with UNEVEN coverage — rom 10 igdb+ss, rom 11 ss
        # only, RomM listing them as siblings. Both must persist under the SAME
        # component key (igdb), so the ss-only dump does not split into its own
        # group (the #1368 bug). A platform unit is a complete view → recompute.
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Game (USA)", "igdb_id": 1001, "ss_id": 2002, "sibling_roms": [{"id": 11}]},
                {"id": 11, "name": "Game (EU)", "ss_id": 2002, "sibling_roms": [{"id": 10}]},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow:
            assert plugin._uow.roms.get(10).sibling_group_key == "igdb:1001:1"
            assert plugin._uow.roms.get(11).sibling_group_key == "igdb:1001:1"

    def test_stamp_preserves_resident_and_keys_fresh_against_resident_summaries(self, plugin):
        # An in-unit resident (already keyed) is left untouched; a fresh member
        # edging into it adopts its canonical summary; a fresh member edging into a
        # DB-resident (fed via resident_keys) adopts THAT summary. The stamp mutates
        # the raw dicts in place.
        orch = plugin._sync_service._orchestrator
        resident = {"id": 1, "platform_id": 57, "sibling_group_key": "igdb:100:57", "sibling_roms": [{"id": 2}]}
        fresh_in_unit = {"id": 2, "platform_id": 57, "ss_id": 22, "sibling_roms": [{"id": 1}]}
        fresh_vs_db = {"id": 3, "platform_id": 57, "moby_id": 9, "sibling_roms": [{"id": 99}]}
        roms = [resident, fresh_in_unit, fresh_vs_db]

        orch._stamp_component_group_keys(roms, {99: "igdb:777:57"})

        assert resident["sibling_group_key"] == "igdb:100:57"
        assert fresh_in_unit["sibling_group_key"] == "igdb:100:57"
        assert fresh_vs_db["sibling_group_key"] == "igdb:777:57"

    def test_read_resident_group_keys_filters_null_keys(self, plugin):
        _seed_rom_row(plugin, 1, app_id=100, platform_slug="n64", sibling_group_key="igdb:5:1")
        _seed_rom_row(plugin, 2, app_id=None, platform_slug="n64", sibling_group_key=None)

        keys = plugin._sync_service._orchestrator._read_resident_group_keys()

        assert keys == {1: "igdb:5:1"}


class TestDeltaRestrictedApply:
    """Delta-restricted apply (#1383): classify in the apply path, emit only new +
    changed, skip content-unchanged shortcuts while still committing every row."""

    @staticmethod
    def _apply_setup(plugin, fake_romm_api):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-delta"

    @staticmethod
    def _apply_unit_events():
        import decky

        return [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_apply_unit"]

    @pytest.mark.asyncio
    async def test_unchanged_item_skipped_but_row_committed_and_stamped(self, plugin, fake_romm_api):
        # rom 10 is content-unchanged (identity + recorded applied "" both match the
        # uninstalled built ""); rom 11 changed its name. Only rom 11 is emitted; both
        # rows still commit and the platform is stamped.
        self._apply_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Keep", "fs_name": "keep.z64"},
                {"id": 11, "name": "New Name", "fs_name": "changed.z64"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="Old Name", fs_name="changed.z64")

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 1
        emitted_ids = [s["rom_id"] for s in events[0]["shortcuts"]]
        assert emitted_ids == [11], "only the changed item is emitted; the unchanged one is skipped"
        assert events[0]["unit_total"] == 1, "unit_total is the DELTA size, not the whole platform"

        with plugin._uow as uow:
            # Both rows committed: the changed name is persisted, the skipped row kept
            # its binding + recorded applied (never re-acked, so save() left it alone).
            assert uow.roms.get(11).name == "New Name"
            skipped = uow.roms.get(10)
            assert skipped.shortcut_app_id == 1010
            assert skipped.applied_launch_options == ""
            # The platform stamp rides the final chunk even though most items skipped.
            assert uow.platform_sync_state.get("n64") is not None

    @pytest.mark.asyncio
    async def test_empty_delta_platform_still_stamps_and_commits_all_rows(self, plugin, fake_romm_api):
        # Every item content-unchanged → an empty delta. The pipeline still emits one
        # empty chunk (unit_total 0), commits every row, and writes the platform stamp.
        self._apply_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "A", "fs_name": "a.z64"},
                {"id": 11, "name": "B", "fs_name": "b.z64"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="B", fs_name="b.z64")

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 1, "the empty-delta platform still round-trips exactly one (empty) chunk"
        assert events[0]["shortcuts"] == []
        assert events[0]["unit_total"] == 0
        with plugin._uow as uow:
            assert uow.roms.get(10).shortcut_app_id == 1010
            assert uow.roms.get(11).shortcut_app_id == 1011
            assert uow.platform_sync_state.get("n64") is not None

    @pytest.mark.asyncio
    async def test_null_recorded_applied_forces_reapply(self, plugin, fake_romm_api):
        # A bound row whose applied_launch_options is NULL (pre-migration-015 /
        # never recorded) is unknown → always re-applied, even with matching identity.
        self._apply_setup(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A", "fs_name": "a.z64"}]
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64", applied_launch_options=None
        )

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        events = self._apply_unit_events()
        assert len(events) == 1
        assert [s["rom_id"] for s in events[0]["shortcuts"]] == [10], "NULL recorded state forces a re-apply"


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
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._orchestrator._wait_for_unit_complete = _fake_wait_set_event
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
        from services.library import sync_orchestrator

        self._apply_setup(plugin, fake_romm_api)
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 2)
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

    # ── _clip_cover_refreshes (the clip primitive) ───────────────

    def test_clip_fails_open_when_rss_unavailable(self, plugin):
        orch = plugin._sync_service._orchestrator
        refreshes = [{"rom_id": 1, "app_id": 10}, {"rom_id": 2, "app_id": 20}]
        assert orch._clip_cover_refreshes(refreshes, rss_kb=None, creates=0, updates=0, limit_kb=1_000_000) == refreshes

    def test_clip_keeps_all_with_headroom(self, plugin):
        from domain.session_budget import COVER_TRANSIENT_KB, EFFECTIVE_CEILING_KB

        orch = plugin._sync_service._orchestrator
        refreshes = [{"rom_id": 1, "app_id": 10}, {"rom_id": 2, "app_id": 20}]
        rss = EFFECTIVE_CEILING_KB - 10 * COVER_TRANSIENT_KB
        assert (
            orch._clip_cover_refreshes(refreshes, rss_kb=rss, creates=0, updates=0, limit_kb=EFFECTIVE_CEILING_KB)
            == refreshes
        )

    def test_clip_accounts_for_the_chunks_own_cost(self, plugin):
        from domain.session_budget import EFFECTIVE_CEILING_KB, chunk_worst_cost_kb

        orch = plugin._sync_service._orchestrator
        refreshes = [{"rom_id": i, "app_id": i * 10} for i in range(1, 6)]
        # Headroom of exactly the chunk's own cost → zero left for refreshes.
        rss = EFFECTIVE_CEILING_KB - chunk_worst_cost_kb(3, 2)
        assert (
            orch._clip_cover_refreshes(refreshes, rss_kb=rss, creates=3, updates=2, limit_kb=EFFECTIVE_CEILING_KB) == []
        )

    def test_clip_negative_headroom_yields_empty(self, plugin):
        from domain.session_budget import EFFECTIVE_CEILING_KB

        orch = plugin._sync_service._orchestrator
        refreshes = [{"rom_id": 1, "app_id": 10}]
        assert (
            orch._clip_cover_refreshes(
                refreshes, rss_kb=EFFECTIVE_CEILING_KB + 1, creates=0, updates=0, limit_kb=EFFECTIVE_CEILING_KB
            )
            == []
        )

    def test_clip_keeps_list_order(self, plugin):
        from domain.session_budget import COVER_TRANSIENT_KB, EFFECTIVE_CEILING_KB

        orch = plugin._sync_service._orchestrator
        refreshes = [{"rom_id": i, "app_id": i * 10} for i in range(1, 6)]
        rss = EFFECTIVE_CEILING_KB - 2 * COVER_TRANSIENT_KB
        assert orch._clip_cover_refreshes(
            refreshes, rss_kb=rss, creates=0, updates=0, limit_kb=EFFECTIVE_CEILING_KB
        ) == [{"rom_id": 1, "app_id": 10}, {"rom_id": 2, "app_id": 20}]

    # ── delegation ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_refresh_delegates_to_artwork_manager(self, plugin):
        fake_refresh = AsyncMock(return_value=[{"rom_id": 1, "app_id": 10}])
        plugin._sync_service._orchestrator._artwork = MagicMock()
        plugin._sync_service._orchestrator._artwork.refresh_changed_covers = fake_refresh

        registry = {"1": {"app_id": 10, "cover_source": "/old.png?ts=1"}}
        result = await plugin._sync_service._orchestrator._refresh_changed_covers(
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


class TestSessionBudgetGate:
    """The RSS-based session-budget pause at chunk boundaries (#1383)."""

    # ── _maybe_pause_for_budget (the gate primitive) ─────────────

    @pytest.mark.asyncio
    async def test_pauses_and_marks_paused_when_over_budget(self, plugin):
        from services.library.sync_orchestrator import _SYNC_PAUSED_BUDGET

        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_100_000  # + 200*2500 = 2.6M ≥ ceiling 2.2M

        await orch._maybe_pause_for_budget(box, creates=200, updates=0)

        # 2.1M is above the GC-skip floor, so the reading is GC-settled: one GC and
        # two RSS reads (raw sample + post-GC re-read).
        assert plugin._renderer_gc.calls == 1
        assert plugin._renderer_rss.calls == 2
        # A budget stop flags run_paused (→ 'paused'), NOT run_interrupted.
        assert box.run_paused is True
        assert box.run_interrupted is False
        assert box.interrupt_reason == _SYNC_PAUSED_BUDGET
        assert box.is_cancelling() is True

    @pytest.mark.asyncio
    async def test_proceeds_with_ample_headroom(self, plugin):
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 440_000  # fresh baseline — nowhere near the cliff

        await orch._maybe_pause_for_budget(box, creates=200, updates=0)

        # 440K is below the GC-skip floor: even the worst-case chunk cost can't cross
        # the ceiling, so the raw reading is trusted and the ~5 s GC is skipped.
        assert plugin._renderer_gc.calls == 0
        assert plugin._renderer_rss.calls == 1  # a single raw sample, no re-read
        assert box.run_interrupted is False
        assert box.interrupt_reason is None
        assert box.is_cancelling() is False

    @pytest.mark.asyncio
    async def test_fail_open_when_rss_unavailable(self, plugin):
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_rss.rss_kb = None  # measurement unavailable

        await orch._maybe_pause_for_budget(box, creates=200, updates=0)

        # Fail-open: no pause, and the "RSS unavailable" note is armed once-per-run.
        assert box.run_interrupted is False
        assert box.is_cancelling() is False
        assert box.budget_measure_unavailable_logged is True

    @pytest.mark.asyncio
    async def test_cliff_limit_proceeds_just_below_the_cliff_bound(self, plugin):
        from domain.session_budget import CLIFF_KB, COVER_TRANSIENT_KB, WORST_CASE_CREATE_KB

        # The first-chunk call passes limit_kb=CLIFF. One KB below the full-chunk
        # cliff bound the gate lets the chunk through (spends into the margin, never
        # past the crash line). Each item is priced create + cover (2500).
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = CLIFF_KB - 200 * (WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB) - 1  # 1_949_999

        await orch._maybe_pause_for_budget(box, creates=200, updates=0, limit_kb=CLIFF_KB)

        assert box.run_paused is False
        assert box.is_cancelling() is False

    @pytest.mark.asyncio
    async def test_cliff_limit_pauses_when_full_chunk_would_reach_the_cliff(self, plugin):
        from domain.session_budget import CLIFF_KB, COVER_TRANSIENT_KB, WORST_CASE_CREATE_KB
        from services.library.sync_orchestrator import _SYNC_PAUSED_BUDGET

        # At the full-chunk cliff bound (each item priced create + cover = 2500) the
        # projection reaches the cliff exactly and the gate pauses (>=) — a first
        # chunk this high is stopped before the crash line even though it would clear
        # the more-permissive absolute-ceiling check the old first-chunk mode used.
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-1"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = CLIFF_KB - 200 * (WORST_CASE_CREATE_KB + COVER_TRANSIENT_KB)  # 1_950_000

        await orch._maybe_pause_for_budget(box, creates=200, updates=0, limit_kb=CLIFF_KB)

        assert box.run_paused is True
        assert box.interrupt_reason == _SYNC_PAUSED_BUDGET
        assert box.is_cancelling() is True

    @pytest.mark.asyncio
    async def test_composition_pricing_mixed_chunk_cheaper_than_all_creates(self, plugin):
        # Composition pricing (#1383): a chunk of 100 creates + 100 updates costs
        # 100*2500 + 100*1000 = 350_000 KB and proceeds at this RSS, while the SAME
        # 200-item count priced as all creates (500_000 KB) would pause — proof the
        # gate prices updates lighter than creates, not every item as a create.
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-mix"
        plugin._renderer_gc.result = True
        # 1_800_000 + 350_000 = 2_150_000 < 2_200_000 ceiling; + 500_000 = 2_300_000 ≥ it.
        plugin._renderer_rss.rss_kb = 1_800_000

        await orch._maybe_pause_for_budget(box, creates=100, updates=100)
        assert box.run_paused is False
        assert box.is_cancelling() is False

        await orch._maybe_pause_for_budget(box, creates=200, updates=0)
        assert box.run_paused is True
        assert box.is_cancelling() is True

    @pytest.mark.asyncio
    async def test_pause_log_line_states_creates_and_updates_composition(self, plugin, caplog):
        # The pause log line must name the composition so an operator can read why
        # the gate fired without recomputing the arithmetic.
        import logging

        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.sync_state = SyncState.RUNNING
        box.current_sync_id = "run-log"
        plugin._renderer_gc.result = True
        # 1_900_000 + (120*2500 + 80*1000 = 380_000) = 2_280_000 ≥ 2_200_000 → pause.
        plugin._renderer_rss.rss_kb = 1_900_000

        with caplog.at_level(logging.INFO):
            await orch._maybe_pause_for_budget(box, creates=120, updates=80)

        assert box.run_paused is True
        assert "120 creates + 80 updates" in caplog.text

    # ── _gc_then_measure_rss (GC-skip below the floor, LOW-3) ────

    @pytest.mark.asyncio
    async def test_gc_skipped_below_floor_returns_raw_reading(self, plugin):
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 1_400_000  # below the 1.5M GC-skip floor

        result = await orch._gc_then_measure_rss(box)

        assert result == 1_400_000  # the raw reading is returned as-is
        assert plugin._renderer_gc.calls == 0  # GC skipped — buys nothing this low
        assert plugin._renderer_rss.calls == 1  # a single raw read, no re-read

    @pytest.mark.asyncio
    async def test_gc_fires_and_rereads_at_or_above_floor(self, plugin):
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 1_600_000  # at/above the floor → settle first

        result = await orch._gc_then_measure_rss(box)

        assert result == 1_600_000
        assert plugin._renderer_gc.calls == 1  # GC fired to settle the reading
        assert plugin._renderer_rss.calls == 2  # raw sample + post-GC re-read

    # ── End-to-end through the chunk loop ────────────────────────

    @staticmethod
    def _arm_two_chunk_apply(plugin, fake_romm_api, monkeypatch):
        """Seed a 2-ROM platform and force one shortcut per chunk (2 chunks)."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Alpha"}, {"id": 11, "name": "Beta"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 1)

        async def fake_wait(_u, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-budget"

    @pytest.mark.asyncio
    async def test_pause_at_second_chunk_persists_paused_with_budget_reason(self, plugin, fake_romm_api, monkeypatch):
        import decky

        from services.library.sync_orchestrator import _SYNC_PAUSED_BUDGET

        self._arm_two_chunk_apply(plugin, fake_romm_api, monkeypatch)
        plugin._renderer_gc.result = True
        # Just under the ceiling: the first chunk's predictive-vs-CLIFF check
        # (2.199 GB + one item's 2500 KB well below the 2.45 GB cliff) passes, but
        # the second chunk's predictive-vs-ceiling check (+2500 ≥ 2.2 GB) crosses.
        plugin._renderer_rss.rss_kb = 2_199_000

        decky.emit.reset_mock()
        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-budget")
        assert run is not None
        # A deliberate session-budget stop is 'paused', NOT 'interrupted' (which is
        # reserved for an external death — a crash/heartbeat timeout).
        assert run.status == "paused"
        assert run.error == _SYNC_PAUSED_BUDGET
        # The first chunk's predictive-vs-cliff check passed (well below the cliff)
        # so it committed its ROM; the gate fired the GC before measuring on both
        # chunk boundaries.
        assert plugin._renderer_gc.calls >= 2
        # The distinct pause reason reaches the frontend via sync_complete so the
        # toast + QAM status read the resume-friendly guidance, not "cancelled".
        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete and complete[-1].get("interrupt_reason") == _SYNC_PAUSED_BUDGET
        assert complete[-1].get("cancelled") is True

    @staticmethod
    def _arm_single_chunk_apply(plugin, fake_romm_api, monkeypatch, *, run_id):
        """Seed a 1-ROM platform (→ one chunk = the run's first chunk)."""
        from services.library import sync_orchestrator

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "Solo"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 1)

        async def fake_wait(_u, event):
            event.set()
            return {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = run_id

    @pytest.mark.asyncio
    async def test_first_chunk_proceeds_when_projection_stays_below_cliff(self, plugin, fake_romm_api, monkeypatch):
        # One ROM → one chunk = the run's first. It is gated PREDICTIVELY against the
        # CLIFF (2.45 GB), not the ceiling: at 2.4 GB — ABOVE the 2.2 GB ceiling the
        # old absolute first-chunk check would have paused at — this light 1-item
        # chunk projects 2.4025 GB (create + cover), still below the cliff, so it
        # proceeds into the safety margin and the run completes. The gate DID run, so
        # the GC fired.
        self._arm_single_chunk_apply(plugin, fake_romm_api, monkeypatch, run_id="run-solo")
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_400_000  # above the 2.2M ceiling, below the 2.45M cliff

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-solo")
        assert run is not None
        assert run.status == "completed"
        assert plugin._renderer_gc.calls >= 1  # the predictive-vs-cliff check GCs + measures

    @pytest.mark.asyncio
    async def test_first_chunk_repauses_when_projection_reaches_cliff(self, plugin, fake_romm_api, monkeypatch):
        from services.library.sync_orchestrator import _SYNC_PAUSED_BUDGET

        # A resume whose first chunk would be PROJECTED to reach the cliff (no Steam
        # restart) must re-pause on that very first chunk rather than drive it into
        # the crash line. At 2.449 GB even the light 1-item chunk projects ≥ the
        # 2.45 GB cliff, so the run re-pauses with zero forward progress — intended.
        self._arm_single_chunk_apply(plugin, fake_romm_api, monkeypatch, run_id="run-over")
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_449_000  # +2500 (create + cover) for the one item ≥ the 2.45M cliff

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-over")
        assert run is not None
        assert run.status == "paused"
        assert run.error == _SYNC_PAUSED_BUDGET
        assert plugin._renderer_gc.calls >= 1

    @pytest.mark.asyncio
    async def test_fail_open_rss_none_completes_all_chunks(self, plugin, fake_romm_api, monkeypatch):
        self._arm_two_chunk_apply(plugin, fake_romm_api, monkeypatch)
        plugin._renderer_gc.result = False
        plugin._renderer_rss.rss_kb = None  # measurement unavailable → gate skipped

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            run = uow.sync_runs.get("run-budget")
        assert run is not None
        assert run.status == "completed"  # both chunks applied, no pause
        assert plugin._sync_service._box.budget_measure_unavailable_logged is True
        # The raw-first measure detects the unavailable reading (raw is None) BEFORE
        # any GC, and the once-per-run flag short-circuits every later measure, so a
        # run whose renderer RSS is unreadable never GCs at all.
        assert plugin._renderer_gc.calls == 0

    # ── Preview prognosis + post-run advisory ────────────────────

    @pytest.mark.asyncio
    async def test_preview_flags_pause_likely_when_run_would_cross(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 1, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._renderer_rss.rss_kb = 2_199_000  # 1 planned create (+2500) crosses 2.2M ceiling

        result = await plugin.sync_preview()

        assert result["success"] is True
        assert result["pause_likely"] is True

    @pytest.mark.asyncio
    async def test_preview_pause_likely_false_with_headroom(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 1, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._renderer_rss.rss_kb = 440_000  # fresh baseline

        result = await plugin.sync_preview()

        assert result["pause_likely"] is False

    @pytest.mark.asyncio
    async def test_preview_pause_likely_false_when_rss_unavailable(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        decky.emit.reset_mock()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 1, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._renderer_rss.rss_kb = None  # fail-open → no warning

        result = await plugin.sync_preview()

        assert result["pause_likely"] is False

    @pytest.mark.asyncio
    async def test_preview_large_unchanged_resync_does_not_warn(self, plugin, fake_romm_api):
        # MEDIUM-1: unchanged items are not priced, so a fully-unchanged re-sync
        # near the ceiling does NOT warn — even though the OLD all-touches formula
        # (which priced unchanged at the create rate) would have crossed here.
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
                {"id": 1, "name": "A", "fs_name": "a.z64"},
                {"id": 2, "name": "B", "fs_name": "b.z64"},
                {"id": 3, "name": "C", "fs_name": "c.z64"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        # Matching bound baseline rows → all three classify as unchanged.
        _seed_rom_row(plugin, 1, app_id=1001, platform_slug="n64", name="A", fs_name="a.z64")
        _seed_rom_row(plugin, 2, app_id=1002, platform_slug="n64", name="B", fs_name="b.z64")
        _seed_rom_row(plugin, 3, app_id=1003, platform_slug="n64", name="C", fs_name="c.z64")
        # rss + 3*2500 would cross (if unchanged were priced as creates); rss + 0 new
        # + 0 changed does not.
        plugin._renderer_rss.rss_kb = 2_199_000

        result = await plugin.sync_preview()

        assert result["success"] is True
        assert result["summary"]["unchanged_count"] == 3
        assert result["summary"]["new_count"] == 0
        assert result["summary"]["changed_count"] == 0
        assert result["pause_likely"] is False

    @pytest.mark.asyncio
    async def test_completed_run_recommends_restart_when_rss_high(self, plugin, fake_romm_api):
        import decky

        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "A"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._renderer_rss.rss_kb = 1_900_000  # > 1.8M post-run advisory floor

        async def fake_wait(_u, event):
            event.set()
            return {"10": 9001}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-restart"

        decky.emit.reset_mock()
        await plugin._sync_service._orchestrator._do_sync_per_unit()

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete, "sync_complete must be emitted"
        assert complete[-1].get("restart_recommended") is True

    # ── Last-run memory delta (#32) ──────────────────────────────

    @pytest.mark.asyncio
    async def test_finalize_computes_and_retains_memory_delta(self, plugin):
        # The signed growth is retained in the box for QAM remounts
        # (get_session_budget_status). _finalize_per_unit itself no longer emits
        # sync_complete (that moved to the orchestrator's post-write emit_sync_complete,
        # #39), so this test just pins the retained box value.
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.committed_app_ids = set()
        box.run_start_rss_kb = 500_000  # raw baseline captured at run start
        plugin._renderer_rss.rss_kb = 1_300_000  # post-run reading (below floor → raw)

        await orch._finalize_per_unit(
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            platform_names={},
            cancelled=False,
        )

        assert box.last_run_delta_kb == 800_000

    @pytest.mark.asyncio
    async def test_finalize_no_delta_when_run_start_unmeasured(self, plugin):
        # A run whose run-start baseline read was unavailable (None) leaves the delta
        # unmeasurable — no number retained (degrade gracefully, never a stale delta).
        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.committed_app_ids = set()
        box.run_start_rss_kb = None
        plugin._renderer_rss.rss_kb = 1_300_000

        await orch._finalize_per_unit(
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            platform_names={},
            cancelled=False,
        )

        assert box.last_run_delta_kb is None

    @pytest.mark.asyncio
    async def test_finalize_stores_delta_on_a_stopped_run(self, plugin):
        # #36: the delta is measured at EVERY terminal, not just clean completion —
        # so a paused/cancelled run overwrites a PRIOR run's delta with ITS OWN
        # consumption-so-far instead of leaving the stale number on the row.
        from services.library.sync_orchestrator import _SYNC_PAUSED_BUDGET

        orch = plugin._sync_service._orchestrator
        box = plugin._sync_service._box
        box.committed_app_ids = set()
        box.run_start_rss_kb = 500_000  # this run's raw run-start baseline
        box.last_run_delta_kb = 700_000  # a PRIOR clean run's delta, must not linger
        box.run_paused = True
        box.interrupt_reason = _SYNC_PAUSED_BUDGET
        plugin._renderer_rss.rss_kb = 2_100_000  # terminal reading — the run grew memory

        await orch._finalize_per_unit(
            synced_rom_ids=set(),
            collection_memberships={},
            platform_rom_ids=set(),
            platform_names={},
            cancelled=True,  # a stopped (paused) run
        )

        # 2_100_000 - 500_000 = 1_600_000 — this paused run's own growth, overwriting 700_000.
        assert box.last_run_delta_kb == 1_600_000

    @pytest.mark.asyncio
    async def test_baseline_captured_at_run_start_so_skip_only_run_reports_zero_delta(self, plugin, fake_romm_api):
        # LOW-1: the baseline is captured at RUN START, not at the first chunk gate,
        # so a fully-incremental-skip run (nothing applied, no gate ever fires) still
        # records a baseline and reports an honest ≈ +0.0 GB delta instead of wiping
        # last_run_delta_kb to None on every no-op re-sync.
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        # roms matches platform count + zero updates → the platform incremental-skips.
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._renderer_rss.rss_kb = 1_200_000  # below the GC-skip floor → raw both ends
        # A stale delta from a prior run — the fresh no-op run must overwrite it, not
        # leave it and not wipe it to None.
        plugin._sync_service._box.last_run_delta_kb = 500_000
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = "run-skip"

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        box = plugin._sync_service._box
        assert box.run_start_rss_kb == 1_200_000  # captured at run start despite no chunk
        assert box.last_run_delta_kb == 0  # honest zero, not None

    # ── Terminal emit ordering (#39): sync_complete AFTER the SyncRun write ──

    @staticmethod
    async def _capture_run_status_at_sync_complete(plugin, run_id: str) -> str | None:
        """Run the pipeline with a decky.emit hook that reads the run's persisted
        status the instant ``sync_complete`` is emitted; return that status."""
        import decky

        seen: dict[str, str | None] = {}

        async def _hook(event, payload=None):
            if event == "sync_complete" and "value" not in seen:
                with plugin._uow as uow:
                    run = uow.sync_runs.get(run_id)
                    seen["value"] = run.status if run is not None else None

        decky.emit.side_effect = _hook
        try:
            await plugin._sync_service._orchestrator._do_sync_per_unit()
        finally:
            decky.emit.side_effect = None
        return seen.get("value")

    @pytest.mark.asyncio
    async def test_sync_complete_emits_after_completed_syncrun_persisted(self, plugin, fake_romm_api, monkeypatch):
        # A clean run: when sync_complete fires, the SyncRun is ALREADY 'completed', so
        # a frontend stats refetch can't read the prior run's status (#39).
        self._arm_single_chunk_apply(plugin, fake_romm_api, monkeypatch, run_id="run-order-done")
        plugin._renderer_rss.rss_kb = 440_000  # low → completes

        status = await self._capture_run_status_at_sync_complete(plugin, "run-order-done")

        assert status == "completed"

    @pytest.mark.asyncio
    async def test_sync_complete_emits_after_paused_syncrun_persisted(self, plugin, fake_romm_api, monkeypatch):
        # A budget-paused run: when sync_complete fires, the SyncRun is ALREADY 'paused'
        # — the emit-last ordering that closes the emit-before-persist race (#39).
        self._arm_two_chunk_apply(plugin, fake_romm_api, monkeypatch)  # run id "run-budget"
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_199_000  # first chunk passes, second pauses

        status = await self._capture_run_status_at_sync_complete(plugin, "run-budget")

        assert status == "paused"

    # ── get_session_budget_status callable ───────────────────────

    @pytest.mark.asyncio
    async def test_session_budget_status_happy(self, plugin):
        from domain.session_budget import CLIFF_KB, EFFECTIVE_CEILING_KB, POST_RUN_ADVISORY_KB

        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = 1_100_000

        result = await plugin.get_session_budget_status()

        assert result == {
            "success": True,
            "rss_kb": 1_100_000,
            # All three colour thresholds ride the payload (single source of truth).
            "warn_kb": POST_RUN_ADVISORY_KB,
            "ceiling_kb": EFFECTIVE_CEILING_KB,
            "cliff_kb": CLIFF_KB,
            # No clean run has completed in this test, so the retained delta is None.
            "memory_delta_kb": None,
            # 1.1 + 2*0.5 = 2.1 < 2.2 ceiling → below the two-chunk headroom bar,
            # a paused run could resume now.
            "resume_ready": True,
            # No run has reached its plan in this process → the progress pair is
            # unknown, and a done count without its denominator is never surfaced.
            "run_done_items": None,
            "run_total_items": None,
        }

    @pytest.mark.asyncio
    async def test_session_budget_status_rss_none(self, plugin):
        from domain.session_budget import CLIFF_KB, EFFECTIVE_CEILING_KB, POST_RUN_ADVISORY_KB

        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = None  # measurement unavailable → fail-open

        result = await plugin.get_session_budget_status()

        assert result["success"] is True
        assert result["rss_kb"] is None
        assert result["warn_kb"] == POST_RUN_ADVISORY_KB
        assert result["ceiling_kb"] == EFFECTIVE_CEILING_KB
        assert result["cliff_kb"] == CLIFF_KB
        assert result["memory_delta_kb"] is None
        assert result["resume_ready"] is None  # RSS unreadable → undecidable

    @pytest.mark.asyncio
    async def test_session_budget_status_resume_not_ready_at_high_rss(self, plugin):
        # A still-high RSS (a paused run before a Steam restart): resume would re-pause.
        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = 2_100_000  # 2.1 + 0.5 = 2.6 ≥ 2.2 ceiling

        result = await plugin.get_session_budget_status()

        assert result["resume_ready"] is False

    @pytest.mark.asyncio
    async def test_session_budget_status_returns_retained_delta(self, plugin):
        # A prior clean run's delta is retained in the box and surfaced on a QAM
        # remount even though the live RSS read is a separate poll.
        plugin.loop = asyncio.get_event_loop()
        plugin._renderer_rss.rss_kb = 1_234_000
        plugin._sync_service._box.last_run_delta_kb = 800_000

        result = await plugin.get_session_budget_status()

        assert result["rss_kb"] == 1_234_000
        assert result["memory_delta_kb"] == 800_000


class TestRunProgressCounters:
    """The run-scoped ``X of Y games done`` counters behind the paused banner (#1383).

    They live in the backend because the plugin process survives the Steam restart the
    paused banner asks for — only the frontend reloads — and they ride the existing
    ``get_session_budget_status`` payload the QAM already polls while paused.
    """

    @staticmethod
    def _arm(plugin, fake_romm_api, *, run_id="run-progress"):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = run_id

    @staticmethod
    def _acks(plugin, *maps):
        """Drive ``_wait_for_unit_complete`` to ack *maps* in order (one per chunk)."""
        pending = list(maps)

        async def fake_wait(_unit, event):
            event.set()
            return pending.pop(0) if pending else {}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait

    @pytest.mark.asyncio
    async def test_committed_chunks_count_toward_done(self, plugin, fake_romm_api, monkeypatch):
        from services.library import sync_orchestrator

        # Two brand-new ROMs, one shortcut per chunk → two chunks, both acked and
        # committed. The plan's ROM total is the denominator.
        self._arm(plugin, fake_romm_api)
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Alpha"}, {"id": 11, "name": "Beta"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 1)
        self._acks(plugin, {"10": 1010}, {"11": 1011})

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        box = plugin._sync_service._box
        assert box.run_total_items == 2
        assert box.run_done_items == 2

    @pytest.mark.asyncio
    async def test_emitted_but_uncommitted_chunk_does_not_count(self, plugin, fake_romm_api, monkeypatch):
        from services.library import sync_orchestrator

        # The first chunk is emitted, then the user cancels mid-wait: the wait gives
        # up (None), the chunk never commits, and its item must NOT be reported done.
        self._arm(plugin, fake_romm_api, run_id="run-cancel")
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Alpha"}, {"id": 11, "name": "Beta"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 1)
        box = plugin._sync_service._box

        async def cancel_mid_wait(_unit, _event) -> dict[str, int] | None:
            # A None ack is the wait's "gave up" signal (user cancel / heartbeat timeout).
            box.request_cancel()
            return None

        plugin._sync_service._orchestrator._wait_for_unit_complete = cancel_mid_wait

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        assert box.run_total_items == 2
        assert box.run_done_items == 0, "an emitted chunk whose ack never landed is not done"

    @pytest.mark.asyncio
    async def test_paused_run_counts_only_the_committed_chunk(self, plugin, fake_romm_api, monkeypatch):
        from services.library import sync_orchestrator

        # The session-budget gate pauses at the second chunk's boundary — before its
        # emit — so exactly one chunk committed. This is the banner's own scenario.
        self._arm(plugin, fake_romm_api, run_id="run-paused")
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Alpha"}, {"id": 11, "name": "Beta"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 1)
        self._acks(plugin, {"10": 1010}, {"11": 1011})
        plugin._renderer_gc.result = True
        plugin._renderer_rss.rss_kb = 2_199_000  # first chunk passes (vs cliff), second pauses

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        with plugin._uow as uow:
            assert uow.sync_runs.get("run-paused").status == "paused"
        box = plugin._sync_service._box
        assert box.run_total_items == 2
        assert box.run_done_items == 1

    @pytest.mark.asyncio
    async def test_delta_skipped_items_count_as_done(self, plugin, fake_romm_api):
        # rom 10 is content-unchanged (skipped by the delta-restricted apply — its
        # shortcut is already correct), rom 11 changed and is applied. Both are done.
        self._arm(plugin, fake_romm_api, run_id="run-delta-progress")
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Keep", "fs_name": "keep.z64"},
                {"id": 11, "name": "New Name", "fs_name": "changed.z64"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="Old Name", fs_name="changed.z64")
        self._acks(plugin, {"11": 1011})

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        box = plugin._sync_service._box
        assert box.run_total_items == 2
        assert box.run_done_items == 2, "1 skipped-unchanged + 1 acked in the single chunk"

    @pytest.mark.asyncio
    async def test_wholesale_skipped_unit_counts_its_roms(self, plugin, fake_romm_api):
        # A platform the incremental gate skips entirely (its stamp matches the
        # server) never reaches the apply — but its games ARE done. This is what a
        # resume sees for every platform it finished before the pause, so without
        # this the resumed run's banner would under-report badly.
        self._arm(plugin, fake_romm_api, run_id="run-skip")
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        fake_romm_api.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
        plugin.settings["enabled_platforms"] = {"1": True}
        self._acks(plugin)

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        box = plugin._sync_service._box
        assert box.run_total_items == 1
        assert box.run_done_items == 1

    @pytest.mark.asyncio
    async def test_counters_reset_at_run_start(self, plugin, fake_romm_api):
        # A stale run's counters must never leak into the next run's banner.
        self._arm(plugin, fake_romm_api, run_id="run-reset")
        _seed_platform(fake_romm_api, platform_id=1, name="N64", slug="n64", roms=[{"id": 10, "name": "Alpha"}])
        plugin.settings["enabled_platforms"] = {"1": True}
        self._acks(plugin, {"10": 1010})
        box = plugin._sync_service._box
        box.run_done_items = 5000
        box.run_total_items = 9999

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        assert box.run_total_items == 1
        assert box.run_done_items == 1

    @pytest.mark.asyncio
    async def test_status_callable_surfaces_the_run_progress(self, plugin, fake_romm_api):
        # The pair rides the existing session-budget payload the QAM already polls.
        self._arm(plugin, fake_romm_api, run_id="run-status")
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[{"id": 10, "name": "Alpha"}, {"id": 11, "name": "Beta"}],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        self._acks(plugin, {"10": 1010, "11": 1011})

        await plugin._sync_service._orchestrator._do_sync_per_unit()
        result = await plugin.get_session_budget_status()

        assert result["run_done_items"] == 2
        assert result["run_total_items"] == 2

    @pytest.mark.asyncio
    async def test_status_callable_reports_unknown_before_any_run(self, plugin):
        # A fresh backend process (or a plugin reload) has no counters: the pair is
        # None, and the banner drops the sentence rather than showing zeros.
        plugin.loop = asyncio.get_event_loop()

        result = await plugin.get_session_budget_status()

        assert result["run_done_items"] is None
        assert result["run_total_items"] is None


class TestProcessedGamesNumerator:
    """``total_games`` — the terminal frame's "N of M games processed" numerator.

    It counts every PROCESSED ROM the same way ``run_done_items`` does: a
    wholesale-skipped unit's ROMs, a partial unit's delta-skipped entries, and
    the committed applies. A resumed run that is interrupted again must not
    understate the one platform it resumed into — a delta-skipped ROM is just
    as processed as a wholesale-skipped one.
    """

    @staticmethod
    def _arm(plugin, fake_romm_api, *, run_id):
        import decky

        decky.emit.reset_mock()
        plugin.loop = asyncio.get_event_loop()
        _use_fake_romm(plugin, fake_romm_api)
        plugin._sync_service._orchestrator._download_artwork = AsyncMock(return_value={})
        plugin._sync_service._box.sync_state = SyncState.RUNNING
        plugin._sync_service._box.current_sync_id = run_id

    @pytest.mark.asyncio
    async def test_clean_run_payload_counts_delta_skips(self, plugin, fake_romm_api):
        import decky

        # rom 10 is content-unchanged (delta-skipped), rom 11 changed and is
        # acked — both are processed, so the sync_complete payload reports 2.
        self._arm(plugin, fake_romm_api, run_id="run-numerator-clean")
        _seed_platform(
            fake_romm_api,
            platform_id=1,
            name="N64",
            slug="n64",
            roms=[
                {"id": 10, "name": "Keep", "fs_name": "keep.z64"},
                {"id": 11, "name": "New Name", "fs_name": "changed.z64"},
            ],
        )
        plugin.settings["enabled_platforms"] = {"1": True}
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64")
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="Old Name", fs_name="changed.z64")

        async def fake_wait(_unit, event):
            event.set()
            return {"11": 1011}

        plugin._sync_service._orchestrator._wait_for_unit_complete = fake_wait

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete[-1]["total_games"] == 2, "1 delta-skipped + 1 applied are both processed"

    @pytest.mark.asyncio
    async def test_resumed_run_interrupt_frame_counts_delta_skips(self, plugin, fake_romm_api, monkeypatch):
        """The resume-then-interrupt shape: a platform the prior run finished
        wholesale-skips, the partial platform delta-skips its already-applied
        ROM and commits one more chunk, then the heartbeat times out. The
        terminal frame's numerator counts all three kinds of processed ROM
        (and agrees with the paused banner's ``run_done_items``)."""
        import decky

        from services.library import sync_orchestrator

        self._arm(plugin, fake_romm_api, run_id="run-resume-interrupt")

        # Unit 1 (N64): wholesale-skipped — its completion stamp matches the
        # server and its one bound row reconstructs.
        _seed_platform_stamp(plugin, "n64", at="2025-01-01T00:00:00Z", rom_count=1)
        _seed_rom_row(plugin, 10, app_id=1010, platform_slug="n64", name="A", fs_name="a.z64")
        fake_romm_api.platforms.append({"id": 1, "name": "N64", "slug": "n64", "rom_count": 1})

        # Unit 2 (GBA): the partial platform — rom 20 is content-unchanged
        # (delta-skipped), roms 21/22 changed. One item per chunk: chunk 0
        # acks and commits, chunk 1's wait gives up while the box is still
        # RUNNING (heartbeat timeout) → the run ends interrupted.
        _seed_platform(
            fake_romm_api,
            platform_id=2,
            name="GBA",
            slug="gba",
            roms=[
                {"id": 20, "name": "Keep", "fs_name": "keep.gba"},
                {"id": 21, "name": "New Name", "fs_name": "changed.gba"},
                {"id": 22, "name": "Other New", "fs_name": "other.gba"},
            ],
        )
        _seed_rom_row(plugin, 20, app_id=1020, platform_slug="gba", name="Keep", fs_name="keep.gba")
        _seed_rom_row(plugin, 21, app_id=1021, platform_slug="gba", name="Old Name", fs_name="changed.gba")
        _seed_rom_row(plugin, 22, app_id=1022, platform_slug="gba", name="Old Other", fs_name="other.gba")
        plugin.settings["enabled_platforms"] = {"1": True, "2": True}
        monkeypatch.setattr(sync_orchestrator, "_APPLY_CHUNK_SIZE", 1)

        acks: list[dict[str, int] | None] = [{"21": 1021}, None]

        async def wait_ack_then_timeout(_unit, event):
            ack = acks.pop(0)
            if ack is not None:
                event.set()
                return ack
            return None  # heartbeat timeout — the box is still RUNNING

        plugin._sync_service._orchestrator._wait_for_unit_complete = wait_ack_then_timeout

        await plugin._sync_service._orchestrator._do_sync_per_unit()

        complete = [c[0][1] for c in decky.emit.call_args_list if c[0][0] == "sync_complete"]
        assert complete[-1]["total_games"] == 3, "1 wholesale-skipped + 1 delta-skipped + 1 committed"
        assert complete[-1]["interrupted"] is True
        progress = plugin._sync_service._sync_progress
        assert progress["stage"] == "cancelled"
        assert progress["message"] == "Sync interrupted: 3 of 4 games processed"
        assert progress["current"] == 3
        assert progress["total"] == 4
        # The frame's numerator and the paused banner's done counter agree.
        assert plugin._sync_service._box.run_done_items == 3
