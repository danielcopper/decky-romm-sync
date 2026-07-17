"""Contract tests for cover-cache invalidation across sync runs (#1386).

Drives two real per-unit syncs over the real wired plugin: run one downloads a
new ROM's cover and records the ``cover_source`` fingerprint; the fake RomM then
changes the cover's ``?ts=`` cache-buster, and run two must re-download the
cache file and carry the ``{rom_id, app_id}`` refresh entry on the emitted
``sync_apply_unit`` payload — while the item itself stays delta-skipped (a
cover-only change never re-applies the shortcut). The NULL-adopt upgrade path
(a pre-#1386 row with an existing cache file) is exercised the same way and
must persist the fingerprint without any download.

The preview→apply flow gap is exercised through the callables the QAM actually
drives: ``sync_preview`` must count the cover-only work (``cover_refresh_count``)
without side effects, and the subsequent ``sync_apply_delta`` run must advance
the fingerprint and carry the refresh entry across the wire — while the pure
no-changes preview keeps its zero-everything shape.
"""

from __future__ import annotations

import asyncio

from domain.rom import Rom
from domain.sync_state import SyncState
from lib.errors import RommNotFoundError

_COVER_OLD = "/assets/romm/resources/roms/10/cover/big.png?ts=2026-01-01 00:00:00"
_COVER_NEW = "/assets/romm/resources/roms/10/cover/big.png?ts=2026-07-11 12:00:00"
# The ts-stripped asset path — the ETag is opaque to ``?ts=`` (the live-probe fact),
# so the fake keys its validators here (#1454).
_COVER_ASSET = "/assets/romm/resources/roms/10/cover/big.png"
_URL_COVER = "https://cdn2.steamgriddb.com/grid/abc123.png"


def _orchestrator(harness):
    return harness.plugin._sync_service._orchestrator


def _box(harness):
    return harness.plugin._sync_service._box


def _ack_with(bindings):
    """A ``_wait_for_unit_complete`` stand-in acking with *bindings*.

    The frontend's ``report_unit_results`` never runs in the contract tier;
    returning a real rom_id→app_id map lets the reporter's commit bind the
    shortcut exactly as a frontend ack would.
    """

    async def _wait(_unit, event):
        event.set()
        return dict(bindings)

    return _wait


def _seed_library(harness, *, cover: str, updated_at: str | None = None) -> None:
    """Seed one platform with one ROM carrying *cover* as its cover source."""
    harness.romm.platforms = [{"id": 1, "name": "N64", "slug": "n64", "rom_count": 1}]
    rom = {
        "id": 10,
        "name": "Game",
        "fs_name": "game.z64",
        "platform_id": 1,
        "platform_name": "N64",
        "platform_slug": "n64",
        "path_cover_large": cover,
    }
    if updated_at is not None:
        rom["updated_at"] = updated_at
    harness.romm.roms[10] = rom
    harness.plugin.settings["enabled_platforms"] = {"1": True}


def _make_grid_resolvable(harness) -> None:
    """Materialise a Steam userdata dir under the harness home so grid_dir() resolves."""
    (harness.tmp_path / "home" / ".steam" / "steam" / "userdata" / "12345").mkdir(parents=True)


def _cache_file(harness):
    return harness.tmp_path / "runtime" / "covers" / "10.png"


def _apply_unit_events(harness):
    return [c.args[1] for c in harness.emit.call_args_list if c.args and c.args[0] == "sync_apply_unit"]


def _download_cover_urls(harness):
    return [args[0] for name, args, _kwargs in harness.romm.call_log if name == "download_cover"]


def _download_cover_calls(harness):
    """Every ``download_cover`` call as ``(url, kwargs)`` — kwargs carry the #1454 validators."""
    return [(args[0], kwargs) for name, args, kwargs in harness.romm.call_log if name == "download_cover"]


def _download_cover_from_url_urls(harness):
    return [args[0] for name, args, _kwargs in harness.romm.call_log if name == "download_cover_from_url"]


async def _run_sync(harness, run_id: str) -> None:
    box = _box(harness)
    assert box.try_begin_run(run_id) is True
    await _orchestrator(harness)._do_sync_per_unit()


async def _drain_apply(harness, tries: int = 5000) -> None:
    """Wait for the background apply task ``sync_apply_delta`` scheduled.

    The callable claims the run slot before returning and the per-unit task
    resets the box to IDLE on finish, so polling the state drains the task on
    the harness's own loop (everything behind it is fake/fast).
    """
    for _ in range(tries):
        if harness.plugin._sync_service._sync_state is SyncState.IDLE:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("sync_apply_delta's background apply task never finished")


async def test_changed_cover_ts_between_runs_re_downloads_and_emits_refresh_entry(harness):
    _make_grid_resolvable(harness)
    _seed_library(harness, cover=_COVER_OLD)
    harness.romm.download_payloads[f"cover:{_COVER_OLD}"] = b"old cover bytes"
    harness.romm.download_payloads[f"cover:{_COVER_NEW}"] = b"new cover bytes"
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({"10": 7777})

    # Run 1: the new ROM's cover downloads into the cache and the fingerprint
    # is recorded at commit.
    await _run_sync(harness, "run-cover-1")
    assert _cache_file(harness).read_bytes() == b"old cover bytes"
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.shortcut_app_id == 7777
    assert rom.cover_source == _COVER_OLD

    # The server changes the cover: fresh ?ts= cache-buster + a bumped
    # updated_at (RomM stamps the ROM row on a cover change, which is what
    # invalidates the platform's incremental skip).
    _seed_library(harness, cover=_COVER_NEW, updated_at="2027-01-01T00:00:00")
    harness.emit.reset_mock()
    downloads_before_run_2 = len(_download_cover_urls(harness))

    await _run_sync(harness, "run-cover-2")

    # The cache was re-downloaded from the NEW source and the fingerprint advanced.
    assert _download_cover_urls(harness)[downloads_before_run_2:] == [_COVER_NEW]
    assert _cache_file(harness).read_bytes() == b"new cover bytes"
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.cover_source == _COVER_NEW

    # The emitted payload: the item stays delta-skipped (no shortcut re-apply for
    # a cover-only change) while the refresh entry rides the first chunk.
    events = _apply_unit_events(harness)
    assert len(events) == 1
    assert events[0]["shortcuts"] == []
    assert events[0]["cover_refreshes"] == [{"rom_id": 10, "app_id": 7777}]


async def test_null_fingerprint_with_cache_adopts_without_download(harness):
    _make_grid_resolvable(harness)
    _seed_library(harness, cover=_COVER_NEW)
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({})

    # A pre-#1386 state: a bound row with NO fingerprint whose cache file exists.
    # Identity matches the fetch and the recorded applied "" matches the
    # uninstalled build, so the item delta-skips.
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom(
                rom_id=10,
                platform_slug="n64",
                name="Game",
                fs_name="game.z64",
                shortcut_app_id=7777,
                last_synced_at="2026-01-01T00:00:00",
                sibling_group_key="romm:10:1",
            )
        )
        uow.roms.set_applied_launch_options(10, "")
    cache = _cache_file(harness)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(b"pre-existing cache bytes")

    await _run_sync(harness, "run-cover-adopt")

    # Adopted: fingerprint persisted, cache untouched, and the fake saw NO
    # cover download at all — no thundering herd on upgrade.
    assert _download_cover_urls(harness) == []
    assert cache.read_bytes() == b"pre-existing cache bytes"
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.cover_source == _COVER_NEW

    events = _apply_unit_events(harness)
    assert len(events) == 1
    assert events[0]["cover_refreshes"] == []


async def test_cover_only_change_flows_from_preview_to_apply_via_callables(harness):
    """The QAM flow for a cover-only change (the #1386 flow gap).

    ``sync_preview`` must count the pending cover refresh in its summary — the
    signal the frontend uses to offer Apply instead of short-circuiting on
    "no changes" — while staying side-effect-free (no download, fingerprint
    untouched). The ``sync_apply_delta`` run it gates must then re-download the
    cover, advance the fingerprint in SQLite, and carry the refresh entry on an
    empty-``shortcuts`` first chunk.
    """
    _make_grid_resolvable(harness)
    _seed_library(harness, cover=_COVER_OLD)
    harness.romm.download_payloads[f"cover:{_COVER_OLD}"] = b"old cover bytes"
    harness.romm.download_payloads[f"cover:{_COVER_NEW}"] = b"new cover bytes"
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({"10": 7777})

    # Seeding run: bind the shortcut and record the OLD fingerprint.
    await _run_sync(harness, "run-seed")
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.cover_source == _COVER_OLD

    # The server changes ONLY the cover (fresh ?ts= + the updated_at bump RomM
    # stamps on a cover change, which invalidates the platform's wholesale skip).
    _seed_library(harness, cover=_COVER_NEW, updated_at="2027-01-01T00:00:00")
    harness.emit.reset_mock()
    downloads_before = len(_download_cover_urls(harness))

    preview = await harness.plugin.sync_preview()
    assert preview["success"] is True
    summary = preview["summary"]
    assert summary["cover_refresh_count"] == 1
    assert summary["new_count"] == 0
    assert summary["changed_count"] == 0
    assert summary["remove_count"] == 0
    # The seeding run stamped the platform, so a cover-only change reports no
    # re-stamp — Apply is offered on the cover work alone (#1416 regression pin).
    assert summary["restamp_platform_count"] == 0
    # The preview stayed read-only: no cover download, fingerprint unchanged.
    assert _download_cover_urls(harness)[downloads_before:] == []
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.cover_source == _COVER_OLD

    apply_result = await harness.plugin.sync_apply_delta(preview["preview_id"])
    assert apply_result == {"success": True, "message": "Applying changes"}
    await _drain_apply(harness)

    # The apply run refreshed the cache, advanced the fingerprint, and pushed
    # the refresh entry across the wire on the empty-delta chunk.
    assert _download_cover_urls(harness)[downloads_before:] == [_COVER_NEW]
    assert _cache_file(harness).read_bytes() == b"new cover bytes"
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.cover_source == _COVER_NEW
    events = _apply_unit_events(harness)
    assert len(events) == 1
    assert events[0]["shortcuts"] == []
    assert events[0]["cover_refreshes"] == [{"rom_id": 10, "app_id": 7777}]


async def test_cover_asset_404_falls_back_to_url_cover_and_records_it(harness):
    """A 404 on the RomM cover asset retries against the ROM's ``url_cover`` and
    persists the url_cover as the fingerprint across the real reporter commit —
    the source actually applied, so a later fixed asset is still detected (#1450).
    """
    _make_grid_resolvable(harness)
    _seed_library(harness, cover=_COVER_OLD)
    harness.romm.roms[10]["url_cover"] = _URL_COVER
    # The RomM-local cover asset 404s; the external url_cover serves real bytes.
    harness.romm.download_cover_side_effect = RommNotFoundError("HTTP 404: Not Found")
    harness.romm.download_payloads[f"cover_url:{_URL_COVER}"] = b"cdn cover bytes"
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({"10": 7777})

    await _run_sync(harness, "run-fallback-1")

    # The cache holds the external bytes, fetched via the bearer-free fallback path.
    assert _cache_file(harness).read_bytes() == b"cdn cover bytes"
    assert _download_cover_from_url_urls(harness) == [_URL_COVER]
    with harness.uow_factory() as uow:
        rom = uow.roms.get(10)
    assert rom is not None
    assert rom.shortcut_app_id == 7777
    # The recorded fingerprint is the applied url_cover — NOT the 404'd RomM path.
    assert rom.cover_source == _URL_COVER


async def test_pure_no_changes_preview_keeps_zero_cover_count(harness):
    """A settled library previews all-zero INCLUDING ``cover_refresh_count`` —
    the frontend's "Everything is up to date." short-circuit stays intact."""
    _make_grid_resolvable(harness)
    _seed_library(harness, cover=_COVER_OLD)
    harness.romm.download_payloads[f"cover:{_COVER_OLD}"] = b"old cover bytes"
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({"10": 7777})
    await _run_sync(harness, "run-seed-settled")
    harness.emit.reset_mock()

    preview = await harness.plugin.sync_preview()

    assert preview["success"] is True
    summary = preview["summary"]
    assert summary["new_count"] == 0
    assert summary["changed_count"] == 0
    assert summary["remove_count"] == 0
    assert summary["cover_refresh_count"] == 0
    # Fully stamped + unchanged → no re-stamp owed either: the "Everything is up
    # to date." short-circuit stays intact (#1416).
    assert summary["restamp_platform_count"] == 0


async def test_ts_only_rescan_revalidates_304_then_second_sync_does_zero_cover_work(harness):
    """The #1454 win, end to end over the real wiring.

    A rescan re-stamps every cover's ``?ts=`` without touching the files, so the
    fingerprint changes; run two REVALIDATES with a conditional request (the file
    server's ETag is stable across ts), draws a 304, keeps the cached bytes, and
    adopts the fresh fingerprint. A third sync over the unchanged state then does
    ZERO cover work — the fingerprint is clean.
    """
    _make_grid_resolvable(harness)
    _seed_library(harness, cover=_COVER_OLD)
    harness.romm.download_payloads[f"cover:{_COVER_OLD}"] = b"the cover bytes"
    # The validator is stable across ?ts= values (the live-probe fact).
    harness.romm.cover_etags[_COVER_ASSET] = '"etag-v1"'
    _orchestrator(harness)._wait_for_unit_complete = _ack_with({"10": 7777})

    # Run 1: fresh download seeds the cache, the cover_source, AND the validator sidecar.
    await _run_sync(harness, "run-reval-1")
    assert _cache_file(harness).read_bytes() == b"the cover bytes"
    with harness.uow_factory() as uow:
        assert uow.roms.get(10).cover_source == _COVER_OLD

    # A rescan re-stamps the ts + bumps updated_at, but the file (and its ETag) is unchanged.
    _seed_library(harness, cover=_COVER_NEW, updated_at="2027-01-01T00:00:00")
    harness.romm.cover_etags[_COVER_ASSET] = '"etag-v1"'
    harness.emit.reset_mock()
    calls_before_run_2 = len(_download_cover_calls(harness))

    await _run_sync(harness, "run-reval-2")

    # Run 2 REVALIDATED: exactly one conditional GET carrying the stored validator,
    # a 304 → the cache bytes were kept (never re-downloaded) and the fingerprint adopted.
    run_2_calls = _download_cover_calls(harness)[calls_before_run_2:]
    assert len(run_2_calls) == 1
    assert run_2_calls[0][1] == {"etag": '"etag-v1"', "last_modified": None}
    assert _cache_file(harness).read_bytes() == b"the cover bytes"  # untouched by the 304
    with harness.uow_factory() as uow:
        assert uow.roms.get(10).cover_source == _COVER_NEW
    # A 304 needs no in-session tile re-apply — the emitted chunk carries no cover refresh.
    events = _apply_unit_events(harness)
    assert len(events) == 1
    assert events[0]["cover_refreshes"] == []

    # Run 3 (the whole point): unchanged server state → ZERO cover work.
    harness.emit.reset_mock()
    calls_before_run_3 = len(_download_cover_calls(harness))
    await _run_sync(harness, "run-reval-3")
    assert _download_cover_calls(harness)[calls_before_run_3:] == []
