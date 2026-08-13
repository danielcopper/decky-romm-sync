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
