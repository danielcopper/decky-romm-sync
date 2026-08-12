"""Contract tests for the adopt-an-existing-ROM surface (#260).

Driven frontend-shaped per ``src/api/backend.ts``:
``startDownload = callable<[number, boolean], BackendResult | TargetOccupiedResult>``,
``adoptExistingRom = callable<[number], AdoptResult>`` and
``verifyExistingContent = callable<[number], VerifyContentResult>``.

The shape risk these pin is the refusal payload: the modal renders the whole
comparison off it, so every key it reads must cross the wire, and the verify
reply is a status union rather than a success flag because "the server has no
checksums" is neither a match nor a mismatch.
"""

from __future__ import annotations

import hashlib
import zipfile
import zlib
from pathlib import Path
from typing import Any

from ._seed import seed_group_member, seed_rom

_ROM_ID = 41
_GROUP = "igdb:900:gba"


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _place_single_file(harness, *, data: bytes = b"user's own dump") -> Path:
    """Put a file exactly where a download of ``rom-41`` would write."""
    path = Path(harness.retrodeck_paths.roms_path()) / "gba" / "rom-41"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _stage_detail(harness, **overrides) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "id": _ROM_ID,
        "name": "rom-41",
        "platform_slug": "gba",
        "fs_name": "rom-41",
        "fs_size_bytes": 4,
    }
    detail.update(overrides)
    harness.romm.roms[_ROM_ID] = detail
    return detail


# ── start_download refusal ───────────────────────────────────────────────


async def test_start_download_refuses_an_occupied_target_with_the_comparison(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)
    path = _place_single_file(harness)

    result = await harness.plugin.start_download(_ROM_ID, False)

    assert result["success"] is False
    assert result["reason"] == "target_occupied"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["existing"] == {
        "name": "rom-41",
        "path": str(path),
        "is_dir": False,
        "size_bytes": len(b"user's own dump"),
        "modified_at": path.stat().st_mtime,
    }
    assert result["incoming"] == {"name": "rom-41", "size_bytes": 4}
    assert result["sizes_match"] is False
    assert result["adoptable"] is True


async def test_a_refused_download_leaves_the_file_byte_identical(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)
    path = _place_single_file(harness)

    await harness.plugin.start_download(_ROM_ID, False)

    assert path.read_bytes() == b"user's own dump"


# ── adopt_existing_rom ───────────────────────────────────────────────────


async def test_adopt_records_an_install_the_read_surface_reports(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)
    path = _place_single_file(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is True
    assert result["file_path"] == str(path)
    assert result["rom_dir"] is None
    installed = await harness.plugin.get_installed_rom(_ROM_ID)
    assert installed is not None
    assert installed["file_path"] == str(path)
    assert installed["system"] == "gba"


async def test_adopt_leaves_the_bytes_untouched(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)
    path = _place_single_file(harness)

    await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert path.read_bytes() == b"user's own dump"


async def test_a_bound_adopt_carries_a_prune_lease_for_the_frontend_s_steam_write(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)
    _place_single_file(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["app_id"] == _ROM_ID  # seed_rom binds shortcut_app_id to rom_id
    assert isinstance(result["prune_lease_token"], str)
    assert result["prune_lease_token"]


async def test_an_unbound_adopt_is_issued_no_prune_lease(harness):
    # The lease covers the frontend's write of the launch command onto the
    # shortcut. An unbound ROM has no shortcut, so the frontend has nothing to
    # do and nothing to release — a token here would be held for its full TTL,
    # blocking prune. Acquisition is guarded, exactly as the download-complete
    # emit guards it.
    seed_rom(harness, _ROM_ID, platform_slug="gba", shortcut_app_id=None)
    _stage_detail(harness)
    _place_single_file(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is True
    assert result["app_id"] is None
    assert "prune_lease_token" not in result


async def test_adopting_supersedes_the_group_s_other_installed_version(harness):
    # #1298 through the real removal service: one installed version per shortcut
    # binding, whichever route produced it. The sibling's files are content the
    # plugin downloaded and can fetch again — a different class from the file the
    # dialog protects, at a different path (ADR-0028).
    seed_group_member(harness, _ROM_ID, group_key=_GROUP, shortcut_app_id=700)
    sibling_path = seed_group_member(
        harness, 42, group_key=_GROUP, shortcut_app_id=None, installed=True, file_name="sibling.gba"
    )
    assert sibling_path is not None
    Path(sibling_path).parent.mkdir(parents=True, exist_ok=True)
    Path(sibling_path).write_bytes(b"the downloaded version")
    _stage_detail(harness)
    _place_single_file(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is True
    assert await harness.plugin.get_installed_rom(_ROM_ID) is not None
    assert await harness.plugin.get_installed_rom(42) is None
    assert not Path(sibling_path).exists()


async def test_adopting_leaves_a_sibling_bound_to_a_different_shortcut_alone(harness):
    # ADR-0021 §5: a grandfathered duplicate has its own Steam entry and is never
    # superseded. The selection rule lives behind the seam — this pins that
    # adoption inherits it rather than reimplementing it.
    seed_group_member(harness, _ROM_ID, group_key=_GROUP, shortcut_app_id=700)
    sibling_path = seed_group_member(
        harness, 42, group_key=_GROUP, shortcut_app_id=800, installed=True, file_name="sibling.gba"
    )
    assert sibling_path is not None
    Path(sibling_path).parent.mkdir(parents=True, exist_ok=True)
    Path(sibling_path).write_bytes(b"its own shortcut")
    _stage_detail(harness)
    _place_single_file(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is True
    assert await harness.plugin.get_installed_rom(42) is not None
    assert Path(sibling_path).read_bytes() == b"its own shortcut"


async def test_adopt_refuses_when_nothing_is_there(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is False
    assert result["reason"] == "nothing_to_adopt"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None


async def test_adopt_surfaces_a_server_failure_in_the_canonical_shape(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _place_single_file(harness)
    harness.romm.fail_on_next(OSError("no route to host"))

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is False
    assert isinstance(result["reason"], str)
    assert result["reason"]
    assert isinstance(result["message"], str)
    assert result["message"]
    assert "error" not in result
    assert "error_code" not in result


# ── verify_existing_content ──────────────────────────────────────────────


async def test_verify_reports_a_match_against_the_server_digest(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    data = b"user's own dump"
    _place_single_file(harness, data=data)
    _stage_detail(
        harness,
        fs_size_bytes=len(data),
        files=[{"file_name": "rom-41", "file_size_bytes": len(data), "md5_hash": _md5(data)}],
    )

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result == {"status": "match", "message": result["message"], "differences": []}
    assert result["message"]


async def test_verify_names_what_differed(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    data = b"user's own dump"
    _place_single_file(harness, data=data)
    _stage_detail(
        harness,
        fs_size_bytes=len(data),
        files=[{"file_name": "rom-41", "file_size_bytes": len(data), "md5_hash": "0" * 32}],
    )

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "mismatch"
    # One line per difference, and no digests in it: two 32-character hex
    # strings said no more than "these differ" and wrapped into a block.
    assert result["differences"] == [{"name": "rom-41", "detail": "contents differ from the server's copy"}]
    assert _md5(data) not in result["differences"][0]["detail"]


async def test_verify_reports_a_checksumless_server_as_its_own_outcome(harness):
    # ``filesystem.skip_hash_calculation``: neither a match nor a mismatch.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    data = b"user's own dump"
    _place_single_file(harness, data=data)
    _stage_detail(harness, fs_size_bytes=len(data), files=[{"file_name": "rom-41", "file_size_bytes": len(data)}])

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "unverifiable"
    assert result["differences"] == []


async def test_verify_reports_a_server_failure_as_error(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _place_single_file(harness)
    harness.romm.fail_on_next(OSError("no route to host"))

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "error"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["differences"] == []


def _place_archive(harness, members: dict[str, bytes], *, name: str = "rom-41.zip") -> Path:
    """Write a real ZIP exactly where a download of ``rom-41`` would."""
    path = Path(harness.retrodeck_paths.roms_path()) / "gba" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return path


def _stage_archived_detail(harness, archive: Path, members: dict[str, bytes], *, state_members: bool = False) -> None:
    """The payload RomM sends for a ROM it stores as an archive.

    ``file_size_bytes`` is the container's size on disk while the digests beside
    it describe the **content**: the scanner accumulates over every member's
    decompressed bytes in ASCII name order. Holding the archive's own bytes to
    that digest is what reported a mismatch on a byte-perfect copy of what the
    server sent.

    ``archive_members`` is off by default because a live RomM 5.1.0 instance
    sends none — the column arrived in 4.9.0 and stays null until the library is
    rescanned — so the file-level digest is the carrier and the extra shape is
    opt-in.
    """
    composite = hashlib.md5(usedforsecurity=False)
    composite_crc = 0
    for member in sorted(members):
        composite.update(members[member])
        composite_crc = zlib.crc32(members[member], composite_crc)
    entry: dict[str, Any] = {
        "file_name": archive.name,
        "file_size_bytes": archive.stat().st_size,
        "md5_hash": composite.hexdigest(),
        "crc_hash": f"{composite_crc & 0xFFFFFFFF:08x}",
    }
    if state_members:
        entry["archive_members"] = [
            {
                "name": member,
                "size": len(data),
                "crc_hash": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
                "md5_hash": _md5(data),
                "sha1_hash": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
            }
            for member, data in sorted(members.items())
        ]
    _stage_detail(harness, fs_name=archive.name, fs_size_bytes=archive.stat().st_size, files=[entry])


async def test_verify_matches_a_zipped_rom_the_plugin_itself_downloaded(harness):
    # Measured on device: the same bytes RomM served, re-offered to the gate,
    # reported a mismatch because the digest describes the ROM inside the zip
    # and the check hashed the zip.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    members = {"rom-41.gba": b"cartridge bytes" * 64}
    archive = _place_archive(harness, members)
    _stage_archived_detail(harness, archive, members)

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result == {"status": "match", "message": result["message"], "differences": []}


async def test_verify_reports_a_changed_byte_inside_an_archive(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    # Same length, different bytes: the digest is what has to catch this.
    archive = _place_archive(harness, {"rom-41.gba": b"cartridge bytez" * 64})
    _stage_archived_detail(harness, archive, {"rom-41.gba": b"cartridge bytes" * 64})

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "mismatch"
    assert result["differences"] == [{"name": "rom-41.zip", "detail": "contents differ from the server's copy"}]


async def test_verify_cannot_confirm_an_archive_described_only_as_a_whole(harness):
    # An arcade set: many members, one server digest, and nothing saying whether
    # it covers all of them or just the largest.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    members = {"disc1.bin": b"one" * 32, "disc2.bin": b"two" * 32}
    archive = _place_archive(harness, members)
    _stage_archived_detail(harness, archive, members)

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "unverifiable"
    assert result["differences"] == []


async def test_verify_names_the_member_that_differs_when_the_server_lists_them(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    # Same length, different bytes: the digest is what has to catch this.
    archive = _place_archive(harness, {"rom-41.gba": b"cartridge bytez" * 64})
    _stage_archived_detail(harness, archive, {"rom-41.gba": b"cartridge bytes" * 64}, state_members=True)

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "mismatch"
    assert result["differences"] == [
        {"name": "rom-41.zip/rom-41.gba", "detail": "contents differ from the server's copy"}
    ]


async def test_verify_reports_a_multi_member_archive_that_lost_a_member(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    archive = _place_archive(harness, {"disc1.bin": b"one" * 32})
    _stage_archived_detail(harness, archive, {"disc1.bin": b"one" * 32, "disc2.bin": b"two" * 32}, state_members=True)

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "mismatch"
    assert result["differences"] == [{"name": "rom-41.zip/disc2.bin", "detail": "missing from the archive"}]


def _stage_directory_rom(harness, *, data: bytes, on_disk_subdir: str) -> None:
    """A directory ROM whose one manifest file RomM locates under ``inner/``.

    *on_disk_subdir* is where the bytes actually sit, so a caller can put them in
    the right place or the wrong one.
    """
    _stage_detail(
        harness,
        fs_name="rom-41.zip",
        fs_name_no_ext="rom-41",
        fs_size_bytes=len(data),
        has_multiple_files=True,
        full_path="roms/gba/rom-41",
        files=[
            {
                "file_name": "data.bin",
                "file_path": "roms/gba/rom-41/inner",
                "file_size_bytes": len(data),
                "md5_hash": _md5(data),
            }
        ],
    )
    placed = Path(harness.retrodeck_paths.roms_path()) / "gba" / "rom-41" / on_disk_subdir / "data.bin"
    placed.parent.mkdir(parents=True, exist_ok=True)
    placed.write_bytes(data)


async def test_verify_holds_a_directory_file_to_the_place_the_server_named(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_directory_rom(harness, data=b"nested payload", on_disk_subdir="inner")

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "match"


async def test_verify_reports_a_file_in_the_wrong_subdirectory_as_missing(harness):
    # The bytes are present and correct, just not where this game's file belongs.
    # An adopted row carries deletion authority, so "somewhere in the tree" is
    # not evidence enough to bless it.
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_directory_rom(harness, data=b"nested payload", on_disk_subdir="elsewhere")

    result = await harness.plugin.verify_existing_content(_ROM_ID)

    assert result["status"] == "mismatch"
    assert result["differences"] == [{"name": "inner/data.bin", "detail": "missing"}]
