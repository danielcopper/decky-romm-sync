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
from pathlib import Path
from typing import Any

from ._seed import seed_rom

_ROM_ID = 41


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
    assert isinstance(result["message"], str) and result["message"]
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


async def test_adopt_refuses_when_nothing_is_there(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _stage_detail(harness)

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is False
    assert result["reason"] == "nothing_to_adopt"
    assert isinstance(result["message"], str) and result["message"]
    assert await harness.plugin.get_installed_rom(_ROM_ID) is None


async def test_adopt_surfaces_a_server_failure_in_the_canonical_shape(harness):
    seed_rom(harness, _ROM_ID, platform_slug="gba")
    _place_single_file(harness)
    harness.romm.fail_on_next(OSError("no route to host"))

    result = await harness.plugin.adopt_existing_rom(_ROM_ID)

    assert result["success"] is False
    assert isinstance(result["reason"], str) and result["reason"]
    assert isinstance(result["message"], str) and result["message"]
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
    assert result["differences"] == [{"name": "rom-41", "expected": f"md5 {'0' * 32}", "actual": f"md5 {_md5(data)}"}]


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
    assert isinstance(result["message"], str) and result["message"]
    assert result["differences"] == []


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
    assert result["differences"] == [{"name": "inner/data.bin", "expected": "present", "actual": "missing"}]
