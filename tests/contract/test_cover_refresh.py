"""Contract tests for cover-cache invalidation across sync runs (#1386).

Drives two real per-unit syncs over the real wired plugin: run one downloads a
new ROM's cover and records the ``cover_source`` fingerprint; the fake RomM then
changes the cover's ``?ts=`` cache-buster, and run two must re-download the
cache file and carry the ``{rom_id, app_id}`` refresh entry on the emitted
``sync_apply_unit`` payload — while the item itself stays delta-skipped (a
cover-only change never re-applies the shortcut). The NULL-adopt upgrade path
(a pre-#1386 row with an existing cache file) is exercised the same way and
must persist the fingerprint without any download.
"""

from __future__ import annotations

from domain.rom import Rom

_COVER_OLD = "/assets/romm/resources/roms/10/cover/big.png?ts=2026-01-01 00:00:00"
_COVER_NEW = "/assets/romm/resources/roms/10/cover/big.png?ts=2026-07-11 12:00:00"


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


async def _run_sync(harness, run_id: str) -> None:
    box = _box(harness)
    assert box.try_begin_run(run_id) is True
    await _orchestrator(harness)._do_sync_per_unit()


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
