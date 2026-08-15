"""Contract tests for adopting a ROM already on disk under a different name (#260).

Driven frontend-shaped per ``src/api/backend.ts``:
``startDownload = callable<[number, boolean, string | null, CollisionChoice | null],
BackendResult | TargetOccupiedResult | CandidatesFoundResult | RenameCollisionsResult>``
and ``adoptExistingRom = callable<[number, string | null, CollisionChoice | null], AdoptResult>``.

The real ``Plugin`` over a real filesystem is what this tier is for: the search
lists a real directory, the rename runs the real ``os.link`` / ``os.unlink``, and
the save and savestate directories come from the real RetroDECK-path and
``retroarch.cfg`` adapters — whose no-config defaults are the shape a stock
install has (savefiles content-sorted, savestates not sorted at all).

Every destructive assertion here reads the tree afterwards. A refusal that
claimed to have touched nothing is exactly the failure this feature must not
have.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path
from typing import Any

from ._seed import seed_rom

_ROM_ID = 41
_CANDIDATE = "rom-41 (U).gba"
_CANONICAL = "rom-41 (USA).gba"


def _platform_dir(harness) -> Path:
    path = Path(harness.retrodeck_paths.roms_path()) / "gba"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _saves_dir(harness) -> Path:
    # savefiles: content-sorted, so the subdirectory is the folder the ROM sits in.
    path = Path(harness.retrodeck_paths.saves_path()) / "gba"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _states_dir(harness) -> Path:
    # savestates: not sorted at all, so they sit directly under the states root.
    path = Path(harness.retrodeck_paths.states_path())
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stage(harness, *, size: int = 4, **overrides) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "id": _ROM_ID,
        "name": "rom-41",
        "platform_slug": "gba",
        "fs_name": _CANONICAL,
        "fs_size_bytes": size,
    }
    detail.update(overrides)
    harness.romm.roms[_ROM_ID] = detail
    return detail


def _place_candidate(harness, *, data: bytes = b"my own dump") -> Path:
    path = _platform_dir(harness) / _CANDIDATE
    path.write_bytes(data)
    return path


# ── the search ───────────────────────────────────────────────────────────


async def test_a_download_is_refused_with_the_file_already_on_the_device(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness, size=len(b"my own dump"))
    candidate = _place_candidate(harness)

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["success"] is False
    assert result["reason"] == "adoption_candidates"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["incoming"] == {"name": _CANONICAL, "size_bytes": len(b"my own dump")}
    assert result["truncated"] is False
    assert result["candidates"] == [
        {
            "name": _CANDIDATE,
            "path": str(candidate),
            "is_dir": False,
            "size_bytes": len(b"my own dump"),
            "modified_at": candidate.stat().st_mtime,
            "evidence": "size",
            "detail": result["candidates"][0]["detail"],
        }
    ]


async def test_a_refused_download_leaves_the_candidate_byte_identical(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)

    await harness.plugin.start_download(_ROM_ID, False)

    assert candidate.read_bytes() == b"my own dump"
    assert not (_platform_dir(harness) / _CANONICAL).exists()


async def test_an_unrelated_platform_folder_downloads_as_before(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    (_platform_dir(harness) / "Some Other Game (USA).gba").write_bytes(b"not it")

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result.get("reason") != "adoption_candidates"


# ── a namesake of the wrong shape ────────────────────────────────────────


async def _drain_download(harness) -> None:
    """Await the fire-and-forget transfer task ``start_download`` spawned, and only it.

    Gathering every pending task in the loop would also await work this test
    never started — the harness's own background jobs — and would pass whether
    or not a download was ever queued. The service hands out the task it made,
    so the test waits on exactly that one.
    """
    task = harness.plugin._download_service.task_for_rom(_ROM_ID)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def _stage_multi_file(harness) -> dict[str, Any]:
    """A ROM the server serves as a folder, extracted to ``rom-41 (USA)``."""
    return _stage(
        harness,
        fs_name_no_ext="rom-41 (USA)",
        has_multiple_files=True,
        files=[{"file_name": "disc1.bin"}, {"file_name": "disc2.bin"}],
    )


async def test_a_same_named_folder_refuses_a_single_file_download(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    folder = _platform_dir(harness) / "rom-41 (U)"
    folder.mkdir()
    (folder / "notes.txt").write_bytes(b"whatever the user keeps here")

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["success"] is False
    assert result["reason"] == "unusable_namesake"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["existing"] == [{"name": "rom-41 (U)", "path": str(folder), "kind": "dir"}]
    assert result["served_is_dir"] is False
    assert result["truncated"] is False
    assert result["incoming"] == {"name": _CANONICAL, "size_bytes": 4}
    # The point of the refusal: no transfer started behind the user's back.
    await _drain_download(harness)
    assert not (_platform_dir(harness) / _CANONICAL).exists()
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None


async def test_a_same_named_file_refuses_a_folder_download(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_multi_file(harness)
    loose = _platform_dir(harness) / "rom-41 (U).gba"
    loose.write_bytes(b"my own dump")

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["success"] is False
    assert result["reason"] == "unusable_namesake"
    assert result["existing"] == [{"name": "rom-41 (U).gba", "path": str(loose), "kind": "file"}]
    assert result["served_is_dir"] is True
    await _drain_download(harness)
    assert not (_platform_dir(harness) / "rom-41 (USA)").exists()


async def test_the_refusal_leaves_the_folder_byte_identical(harness):
    # The Cancel exit is the frontend simply not calling again, so what the
    # backend already did has to be nothing at all.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    folder = _platform_dir(harness) / "rom-41 (U)"
    folder.mkdir()
    (folder / "notes.txt").write_bytes(b"mine")

    await harness.plugin.start_download(_ROM_ID, False)
    await _drain_download(harness)

    assert sorted(path.name for path in _platform_dir(harness).iterdir()) == ["rom-41 (U)"]
    assert (folder / "notes.txt").read_bytes() == b"mine"


async def test_downloading_anyway_lands_beside_the_namesake(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"
    folder = _platform_dir(harness) / "rom-41 (U)"
    folder.mkdir()
    (folder / "notes.txt").write_bytes(b"mine")

    result = await harness.plugin.start_download(_ROM_ID, True)
    await _drain_download(harness)

    assert result["success"] is True
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"srv!"
    # A second copy, said out loud in the dialog — and the user's folder is
    # neither renamed nor removed to make room for it.
    assert (folder / "notes.txt").read_bytes() == b"mine"


# ── the game-detail read ─────────────────────────────────────────────────


async def test_the_page_reports_a_candidate_without_the_user_pressing_download(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    _place_candidate(harness)

    detail = await harness.plugin.get_cached_game_detail(_ROM_ID)

    assert detail["installed"] is False
    assert detail["adoption_candidate_present"] is True
    assert detail["target_path_occupied"] is False


async def test_an_empty_platform_folder_leaves_the_page_offering_a_download(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    _platform_dir(harness)

    detail = await harness.plugin.get_cached_game_detail(_ROM_ID)

    assert detail["adoption_candidate_present"] is False


async def test_the_page_stays_usable_when_the_roms_folder_cannot_be_read(harness, caplog):
    # A search that could not run must never make a game look uninstallable.
    #
    # The log line is the assertion that has teeth: `adoption_candidate_present`
    # is False by default, so pinning it alone would stay green with the probe
    # unwired, uncalled or deleted. The warning is emitted only by the guard
    # catching this raise, so it says the probe ran AND survived.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.plugin._rom_adoption_service._search._retrodeck_paths = _UnreadableRomsPaths()

    with caplog.at_level(logging.WARNING):
        detail = await harness.plugin.get_cached_game_detail(_ROM_ID)

    assert detail["found"] is True
    assert detail["adoption_candidate_present"] is False
    assert any("candidate probe failed" in record.message for record in caplog.records)


class _UnreadableRomsPaths:
    """A RetroDECK paths provider whose ROMs root raises, as an ejected SD card does."""

    def roms_path(self) -> str:
        raise OSError("Input/output error")


# ── adopting the candidate ───────────────────────────────────────────────


async def test_adopting_a_candidate_renames_it_and_records_the_install(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    canonical = _platform_dir(harness) / _CANONICAL

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert result["success"] is True
    assert result["file_path"] == str(canonical)
    assert canonical.read_bytes() == b"my own dump"
    assert not candidate.exists()
    installed = await harness.plugin.get_installed_rom(_ROM_ID)
    assert installed is not None
    assert installed["file_path"] == str(canonical)
    assert installed["system"] == "gba"


async def test_the_rename_carries_a_save_and_a_savestate(harness):
    # The whole reason the rename exists: an uninstall drops the ROM but never
    # the saves (ADR-0007), so a save left under the user's own name is orphaned
    # the moment the canonical name is downloaded.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    save = _saves_dir(harness) / "rom-41 (U).srm"
    save.write_bytes(b"battery")
    state = _states_dir(harness) / "rom-41 (U).state.auto"
    state.write_bytes(b"snapshot")

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert result["success"] is True
    assert (_saves_dir(harness) / "rom-41 (USA).srm").read_bytes() == b"battery"
    assert (_states_dir(harness) / "rom-41 (USA).state.auto").read_bytes() == b"snapshot"
    assert not save.exists()
    assert not state.exists()


async def test_a_pending_save_sort_migration_keeps_the_rename_where_the_sync_looks(harness):
    # End to end through the real RomInfoService: with a migration pending, the
    # sync deliberately resolves saves against the PREVIOUS layout (#238) because
    # that is where the files still are. A rename reading the live retroarch.cfg
    # — which this harness's absent config renders as content-sorted — would move
    # them into <saves>/gba/ and strand both the sync and the pending migration.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    with harness.uow_factory() as uow:
        uow.kv_config.set("save_sort_settings_previous", '{"sort_by_content": false, "sort_by_core": false}')
    unsorted_root = Path(harness.retrodeck_paths.saves_path())
    unsorted_root.mkdir(parents=True, exist_ok=True)
    save = unsorted_root / "rom-41 (U).srm"
    save.write_bytes(b"battery")

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert result["success"] is True
    assert (unsorted_root / "rom-41 (USA).srm").read_bytes() == b"battery"
    assert not save.exists()
    assert not (_saves_dir(harness) / "rom-41 (USA).srm").exists()


async def test_another_game_s_save_stays_where_it_is(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    stranger = _saves_dir(harness) / "some other game (U).srm"
    stranger.write_bytes(b"not mine")

    await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert stranger.read_bytes() == b"not mine"


async def test_an_adopted_candidate_is_launchable_like_a_downloaded_one(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert result["app_id"] == _ROM_ID  # seed_rom binds shortcut_app_id to rom_id
    assert isinstance(result["launch_options"], str)
    assert isinstance(result["prune_lease_token"], str)


async def test_a_candidate_outside_this_game_s_platform_folder_is_refused(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    elsewhere = Path(harness.retrodeck_paths.roms_path()) / "snes"
    elsewhere.mkdir(parents=True, exist_ok=True)
    intruder = elsewhere / _CANDIDATE
    intruder.write_bytes(b"different platform")

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(intruder), None)

    assert result["success"] is False
    assert isinstance(result["reason"], str)
    assert result["reason"]
    assert isinstance(result["message"], str)
    assert result["message"]
    assert "error" not in result
    assert intruder.read_bytes() == b"different platform"
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None


# ── the collision decision ───────────────────────────────────────────────


def _stage_collision(harness) -> tuple[Path, Path, Path]:
    """A user who played both versions: each left a save under its own name."""
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    mine = _saves_dir(harness) / "rom-41 (U).srm"
    mine.write_bytes(b"my progress")
    theirs = _saves_dir(harness) / "rom-41 (USA).srm"
    theirs.write_bytes(b"the other version's progress")
    return (candidate, mine, theirs)


async def test_a_taken_name_refuses_and_lists_every_collision(harness):
    candidate, _mine, theirs = _stage_collision(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert result["success"] is False
    assert result["reason"] == "rename_collisions"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["collisions"] == [{"name": theirs.name, "path": str(theirs), "kind": "save"}]


async def test_an_unanswered_collision_leaves_the_filesystem_byte_identical(harness):
    # This is the Cancel exit: the frontend simply does not call again, and what
    # the backend already did has to be nothing at all.
    candidate, mine, theirs = _stage_collision(harness)
    before = {path: path.read_bytes() for path in (candidate, mine, theirs)}

    await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), None)

    assert {path: path.read_bytes() for path in (candidate, mine, theirs)} == before
    assert not (_platform_dir(harness) / _CANONICAL).exists()
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None


async def test_overwrite_replaces_the_taken_name_and_completes_the_adoption(harness):
    candidate, mine, theirs = _stage_collision(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), "overwrite")

    assert result["success"] is True
    assert theirs.read_bytes() == b"my progress"
    assert not mine.exists()
    assert not candidate.exists()
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"my own dump"


async def test_overwrite_backs_the_replaced_save_up_rather_than_destroying_it(harness):
    # Through the real MatrixExecutor funnel: a save the user chose to lose is
    # still recoverable from .romm-backup. ADR-0028 declined to quarantine a ROM
    # because ROMs are gigabytes and re-fetchable; a save is neither, and a
    # savestate is synced nowhere at all.
    candidate, _mine, theirs = _stage_collision(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), "overwrite")

    assert result["success"] is True
    backups = sorted((_saves_dir(harness) / ".romm-backup").iterdir())
    assert [path.read_bytes() for path in backups] == [b"the other version's progress"]
    assert theirs.read_bytes() == b"my progress"


async def test_a_replaced_savestate_is_backed_up_beside_the_states_root(harness):
    # Savestates have never been through this funnel. It takes the directory it
    # is given, so the backup lands in <states>/.romm-backup/ — the same
    # discipline, one tree over.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    (_states_dir(harness) / "rom-41 (U).state").write_bytes(b"my snapshot")
    theirs = _states_dir(harness) / "rom-41 (USA).state"
    theirs.write_bytes(b"the other version's snapshot")

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), "overwrite")

    assert result["success"] is True
    backups = sorted((_states_dir(harness) / ".romm-backup").iterdir())
    assert [path.read_bytes() for path in backups] == [b"the other version's snapshot"]
    assert theirs.read_bytes() == b"my snapshot"


async def test_a_folder_at_a_save_s_name_is_refused_before_anything_moves(harness):
    # The backup funnel moves a regular file and reports False for anything else,
    # so a folder at a save's name would silently no-op the clear and then fail at
    # the link with nothing explaining why. It is refused by name, up front.
    candidate, mine, _theirs = _stage_collision(harness)
    theirs = _saves_dir(harness) / "rom-41 (USA).srm"
    theirs.unlink()
    theirs.mkdir()
    (theirs / "not a save").write_bytes(b"someone's folder")

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), "overwrite")

    assert result["success"] is False
    assert result["reason"] == "replace_failed"
    assert "rom-41 (USA).srm" in result["message"]
    assert candidate.read_bytes() == b"my own dump"
    assert mine.read_bytes() == b"my progress"
    assert (theirs / "not a save").read_bytes() == b"someone's folder"
    assert not (_saves_dir(harness) / ".romm-backup").exists()


async def test_a_dangling_symlink_at_a_save_s_name_is_refused_too(harness):
    # The other half of the widened guard. A broken link occupies the name, so the
    # collision is real, but the funnel can no more set it aside than a folder —
    # and `os.link` would then fail EEXIST with earlier pairs already backed up.
    candidate, mine, _theirs = _stage_collision(harness)
    theirs = _saves_dir(harness) / "rom-41 (USA).srm"
    theirs.unlink()
    theirs.symlink_to(_saves_dir(harness) / "gone.srm")

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), "overwrite")

    assert result["success"] is False
    assert result["reason"] == "replace_failed"
    assert "rom-41 (USA).srm" in result["message"]
    assert candidate.read_bytes() == b"my own dump"
    assert mine.read_bytes() == b"my progress"
    assert theirs.is_symlink()
    assert not (_saves_dir(harness) / ".romm-backup").exists()


async def test_keep_leaves_both_saves_and_still_adopts_the_rom(harness):
    candidate, mine, theirs = _stage_collision(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID, str(candidate), "keep")

    assert result["success"] is True
    # Nothing is lost — and the old-named save is now orphaned, which is what the
    # dialog says out loud rather than implying the move was clean.
    assert theirs.read_bytes() == b"the other version's progress"
    assert mine.read_bytes() == b"my progress"
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"my own dump"


# ── downloading over a candidate ─────────────────────────────────────────


async def test_downloading_over_a_candidate_removes_it_and_carries_its_saves(harness):
    # The dialog's second confirmation says the file is deleted, so it is — and
    # its saves go with it, or the fresh download would look for them under a
    # name nothing wrote.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    save = _saves_dir(harness) / "rom-41 (U).srm"
    save.write_bytes(b"battery")
    state = _states_dir(harness) / "rom-41 (U).state"
    state.write_bytes(b"snapshot")

    result = await harness.plugin.start_download(_ROM_ID, True, str(candidate), None)

    assert result["success"] is True
    assert not candidate.exists()
    assert (_saves_dir(harness) / "rom-41 (USA).srm").read_bytes() == b"battery"
    assert (_states_dir(harness) / "rom-41 (USA).state").read_bytes() == b"snapshot"


async def test_none_of_these_downloads_without_deleting_anything(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)

    result = await harness.plugin.start_download(_ROM_ID, True, None, None)

    assert result["success"] is True
    assert candidate.read_bytes() == b"my own dump"


async def test_a_taken_save_name_stops_the_download_before_anything_is_removed(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    mine = _saves_dir(harness) / "rom-41 (U).srm"
    mine.write_bytes(b"my progress")
    theirs = _saves_dir(harness) / "rom-41 (USA).srm"
    theirs.write_bytes(b"the other version's progress")

    result = await harness.plugin.start_download(_ROM_ID, True, str(candidate), None)

    assert result["success"] is False
    assert result["reason"] == "rename_collisions"
    assert result["collisions"] == [{"name": theirs.name, "path": str(theirs), "kind": "save"}]
    assert candidate.read_bytes() == b"my own dump"
    assert mine.read_bytes() == b"my progress"
    assert theirs.read_bytes() == b"the other version's progress"


async def test_the_collision_answer_completes_the_download(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    candidate = _place_candidate(harness)
    (_saves_dir(harness) / "rom-41 (U).srm").write_bytes(b"my progress")
    theirs = _saves_dir(harness) / "rom-41 (USA).srm"
    theirs.write_bytes(b"the other version's progress")

    result = await harness.plugin.start_download(_ROM_ID, True, str(candidate), "overwrite")

    assert result["success"] is True
    assert theirs.read_bytes() == b"my progress"
    assert not candidate.exists()


# ── verification against a candidate ─────────────────────────────────────


async def test_the_content_check_runs_against_the_candidate_not_the_empty_target(harness):
    import hashlib

    seed_rom(harness, _ROM_ID, platform_slug="gba")
    data = b"my own dump"
    candidate = _place_candidate(harness, data=data)
    _stage(
        harness,
        size=len(data),
        files=[
            {
                "file_name": _CANONICAL,
                "file_size_bytes": len(data),
                "md5_hash": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            }
        ],
    )

    result = await harness.plugin.verify_existing_content(_ROM_ID, str(candidate))

    assert result["status"] == "match"
    assert result["differences"] == []


async def test_the_content_check_reports_a_candidate_that_is_a_different_dump(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    data = b"my own dump"
    candidate = _place_candidate(harness, data=data)
    _stage(
        harness,
        size=len(data),
        files=[{"file_name": _CANONICAL, "file_size_bytes": len(data), "md5_hash": "0" * 32}],
    )

    result = await harness.plugin.verify_existing_content(_ROM_ID, str(candidate))

    assert result["status"] == "mismatch"
    assert result["differences"] == [{"name": _CANONICAL, "detail": "contents differ from the server's copy"}]


# ── a namesake that cannot become the install ────────────────────────────


def _place_link(harness, name: str = "rom-41 (U).gba", *, target: str = "real.gba") -> Path:
    """A real symlink in the platform folder, pointing at a real file."""
    real = _platform_dir(harness) / target
    real.write_bytes(b"my own dump")
    path = _platform_dir(harness) / name
    path.symlink_to(real)
    return path


async def test_a_symlink_is_never_offered_as_a_candidate(harness):
    # Adopting one writes an install row the uninstall path can never remove:
    # ``claim_source`` refuses a symlink outright.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    link = _place_link(harness)

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["success"] is False
    assert result["reason"] == "unusable_namesake"
    assert result["existing"] == [{"name": link.name, "path": str(link), "kind": "link"}]
    assert "candidates" not in result
    await _drain_download(harness)
    assert not (_platform_dir(harness) / _CANONICAL).exists()
    assert link.is_symlink()


async def test_a_link_pointing_nowhere_is_reported_the_same_way(harness):
    # The kind is the entry's own, so a dangling link needs no separate outcome —
    # and nothing offers to delete it, because nothing can prove it holds no data.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    link = _platform_dir(harness) / "rom-41 (U).gba"
    link.symlink_to(_platform_dir(harness) / "gone.gba")

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["reason"] == "unusable_namesake"
    assert result["existing"] == [{"name": link.name, "path": str(link), "kind": "link"}]
    assert link.is_symlink()


async def test_a_named_pipe_with_the_game_s_name_is_not_mentioned_at_all(harness):
    # It reported as an ordinary zero-byte file and was offered as the game.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"
    os.mkfifo(str(_platform_dir(harness) / "rom-41 (U).gba"))

    detail = await harness.plugin.get_cached_game_detail(_ROM_ID)
    result = await harness.plugin.start_download(_ROM_ID, False)
    await _drain_download(harness)

    assert detail["adoption_candidate_present"] is False
    assert result["success"] is True
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"srv!"


async def test_downloading_anyway_leaves_the_link_alone(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"
    link = _place_link(harness)

    result = await harness.plugin.start_download(_ROM_ID, True)
    await _drain_download(harness)

    assert result["success"] is True
    assert link.is_symlink()
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"srv!"


async def test_the_page_and_the_click_search_see_the_same_link(harness):
    # The page reports it — it is content the user has, and a download lands
    # beside it — and the click search answers for what it is.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    _place_link(harness)

    detail = await harness.plugin.get_cached_game_detail(_ROM_ID)
    result = await harness.plugin.start_download(_ROM_ID, False)

    assert detail["adoption_candidate_present"] is True
    assert result["reason"] == "unusable_namesake"


# ── a symlink at the ROM's own target path ───────────────────────────────


async def test_a_link_at_the_target_path_is_not_adoptable(harness):
    # Reached through the occupied-target dialog rather than the search, and the
    # same rule: an install row may not point at a link.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    real = _platform_dir(harness) / "real.gba"
    real.write_bytes(b"my own dump")
    link = _platform_dir(harness) / _CANONICAL
    link.symlink_to(real)

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["success"] is False
    assert result["reason"] == "target_occupied"
    assert result["adoptable"] is False
    assert link.is_symlink()


async def test_a_link_pointing_nowhere_at_the_target_path_is_not_silently_destroyed(harness):
    # Described as "nothing here", the finalize replace overwrote the link
    # without a word. It occupies the path, so the user is asked.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"
    link = _platform_dir(harness) / _CANONICAL
    link.symlink_to(_platform_dir(harness) / "gone.gba")

    result = await harness.plugin.start_download(_ROM_ID, False)
    await _drain_download(harness)

    assert result["success"] is False
    assert result["reason"] == "target_occupied"
    assert link.is_symlink()


async def test_a_named_pipe_at_the_target_path_is_never_offered_as_this_game(harness):
    # The search leaves one out, so the whole question reaches this door and no
    # other. Adopting one wrote a row ``claim_source`` then refused to release,
    # leaving a game that could not be uninstalled.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"
    pipe = _platform_dir(harness) / _CANONICAL
    os.mkfifo(str(pipe))

    result = await harness.plugin.start_download(_ROM_ID, False)
    adopted = await harness.plugin.adopt_existing_rom(_ROM_ID, None, None)
    await _drain_download(harness)

    assert result["success"] is False
    assert result["reason"] == "target_occupied"
    assert result["existing"]["kind"] is None
    assert result["adoptable"] is False
    assert adopted["success"] is False
    assert adopted["reason"] == "unexpected_content_kind"
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None
    # And it is still a pipe: nothing wrote over it, and nothing removed it.
    assert stat.S_ISFIFO(os.lstat(str(pipe)).st_mode)


async def test_a_symlink_at_the_target_path_cannot_be_adopted_either(harness):
    # The gate disables the button; this is the same answer from the acting
    # site, which is what a stale dialog or a repeated call reaches.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    real = _platform_dir(harness) / "real.gba"
    real.write_bytes(b"my own dump")
    link = _platform_dir(harness) / _CANONICAL
    link.symlink_to(real)

    adopted = await harness.plugin.adopt_existing_rom(_ROM_ID, None, None)

    assert adopted["success"] is False
    assert adopted["reason"] == "unexpected_content_kind"
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None
    assert link.is_symlink()
    assert real.read_bytes() == b"my own dump"


# ── the backstop ─────────────────────────────────────────────────────────


async def test_a_page_that_found_a_copy_never_ends_in_a_silent_download(harness):
    # The ordinary race: the page found the file, it was deleted before the
    # press, and nothing specific can be said about the folder any more.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"

    result = await harness.plugin.start_download(_ROM_ID, False, None, None, True)

    assert result["success"] is False
    assert result["reason"] == "candidate_vanished"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["incoming"] == {"name": _CANONICAL, "size_bytes": 4}
    await _drain_download(harness)
    assert not (_platform_dir(harness) / _CANONICAL).exists()


async def test_answering_the_backstop_downloads(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"

    result = await harness.plugin.start_download(_ROM_ID, True, None, None, True)
    await _drain_download(harness)

    assert result["success"] is True
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"srv!"


async def test_a_page_that_found_nothing_still_downloads_without_a_dialog(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage(harness)
    harness.romm.download_payloads[f"rom:{_ROM_ID}:{_CANONICAL}"] = b"srv!"

    result = await harness.plugin.start_download(_ROM_ID, False, None, None, False)
    await _drain_download(harness)

    assert result["success"] is True
    assert (_platform_dir(harness) / _CANONICAL).read_bytes() == b"srv!"
