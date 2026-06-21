"""Contract tests for the disc-picker callables over the real Plugin/bootstrap.

Driven frontend-shaped per ``src/api/backend.ts``:
``getDiscSelection = callable<[number], DiscSelection>`` and
``selectDisc = callable<[number, string | null], SelectDiscResult>`` — the
``null`` argument is passed as literal Python ``None``.

The harness runs the REAL ``DiscLaunchResolver`` over the REAL download
file-store, so a folder-backed install with real ``.cue`` files on disk under
``tmp_path`` enumerates exactly as production would. es_systems.xml does not
exist under the fake plugin dir, so ``get_supported_extensions`` returns empty
and the resolver falls back to the full disc set — deterministic.
"""

from __future__ import annotations

from pathlib import Path

from domain.rom import Rom
from domain.rom_install import RomInstall

_SYSTEM = "psx"
_DISC1 = "Game (Disc 1).cue"
_DISC2 = "Game (Disc 2).cue"


def _seed_multi_disc_install(harness, rom_id: int, *, disc_names: list[str], selected_disc: str | None = None) -> str:
    """Write real disc files on disk + seed a folder-backed RomInstall.

    Returns the ``file_path`` (the first disc). The Rom row carries
    *selected_disc* so the picker's ``selected`` field reflects a persisted pin.
    """
    rom_dir = Path(harness.tmp_path) / "retrodeck" / "roms" / _SYSTEM / f"game-{rom_id}"
    rom_dir.mkdir(parents=True, exist_ok=True)
    for name in disc_names:
        (rom_dir / name).write_text("FILE ... BINARY 2352\n", encoding="utf-8")
    file_path = str(rom_dir / disc_names[0])
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug=_SYSTEM,
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=rom_id,
                last_synced_at="2026-01-01T00:00:00",
            )
        )
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=str(rom_dir),
                platform_slug=_SYSTEM,
                system=_SYSTEM,
                installed_at="2026-01-01T00:00:00",
            )
        )
        # selected_disc is EXCLUDED from the sync UPSERT (Rom.save), so a pin must
        # go through the dedicated pin-only write path — same as production.
        if selected_disc is not None:
            uow.roms.set_selected_disc(rom_id, selected_disc)
    return file_path


def _seed_single_file_install(harness, rom_id: int) -> None:
    file_path = str(Path(harness.tmp_path) / "retrodeck" / "roms" / "snes" / f"game-{rom_id}.sfc")
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug="snes",
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=rom_id,
                last_synced_at="2026-01-01T00:00:00",
            )
        )
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=None,
                platform_slug="snes",
                system="snes",
                installed_at="2026-01-01T00:00:00",
            )
        )


# ── get_disc_selection ───────────────────────────────────────────────────


async def test_get_disc_selection_multi_disc_shape(harness):
    """A folder-backed multi-disc ROM returns the full picker descriptor."""
    _seed_multi_disc_install(harness, 1, disc_names=[_DISC1, _DISC2], selected_disc=_DISC2)
    result = await harness.plugin.get_disc_selection(1)
    assert result["multi_disc"] is True
    assert [d["filename"] for d in result["discs"]] == [_DISC1, _DISC2]
    assert result["discs"][0] == {"filename": _DISC1, "label": "Disc 1", "index": 1}
    assert result["selected"] == _DISC2
    assert result["default"] == {"kind": "disc", "label": "Disc 1", "filename": _DISC1}


async def test_get_disc_selection_unpinned_selected_none(harness):
    """An unpinned multi-disc ROM reports ``selected: None``."""
    _seed_multi_disc_install(harness, 2, disc_names=[_DISC1, _DISC2])
    result = await harness.plugin.get_disc_selection(2)
    assert result["multi_disc"] is True
    assert result["selected"] is None


async def test_get_disc_selection_single_file_not_multi(harness):
    """A single-file install is not multi-disc → ``{"multi_disc": False}`` only."""
    _seed_single_file_install(harness, 3)
    result = await harness.plugin.get_disc_selection(3)
    assert result == {"multi_disc": False}


async def test_get_disc_selection_unknown_rom_not_multi(harness):
    """An unknown rom_id is not multi-disc → ``{"multi_disc": False}``."""
    result = await harness.plugin.get_disc_selection(999)
    assert result == {"multi_disc": False}


# ── select_disc ──────────────────────────────────────────────────────────


async def test_select_disc_pin_happy_path(harness):
    """Pinning a valid disc succeeds, persists, and bakes its path."""
    _seed_multi_disc_install(harness, 10, disc_names=[_DISC1, _DISC2])
    result = await harness.plugin.select_disc(10, _DISC2)
    assert result["success"] is True
    assert result["selected"] == _DISC2
    assert _DISC2 in result["launch_options"]
    # Pin persisted — a follow-up read reflects it.
    follow_up = await harness.plugin.get_disc_selection(10)
    assert follow_up["selected"] == _DISC2


async def test_select_disc_clear_to_default(harness):
    """Clearing with literal None resets to the default and persists NULL."""
    _seed_multi_disc_install(harness, 11, disc_names=[_DISC1, _DISC2], selected_disc=_DISC2)
    result = await harness.plugin.select_disc(11, None)
    assert result["success"] is True
    assert result["selected"] is None
    assert _DISC1 in result["launch_options"]  # default → disc 1
    follow_up = await harness.plugin.get_disc_selection(11)
    assert follow_up["selected"] is None


async def test_select_disc_invalid_filename_failure_shape(harness):
    """An unknown disc filename → canonical ``{success, reason, message}``."""
    _seed_multi_disc_install(harness, 12, disc_names=[_DISC1, _DISC2])
    result = await harness.plugin.select_disc(12, "Game (Disc 9).cue")
    assert result["success"] is False
    assert result["reason"] == "not_found"
    assert isinstance(result["message"], str)
    assert "error" not in result
    assert "error_code" not in result


async def test_select_disc_not_installed_failure_shape(harness):
    """An uninstalled / unknown ROM → canonical failure shape, nothing written."""
    result = await harness.plugin.select_disc(999, _DISC1)
    assert result["success"] is False
    assert result["reason"] == "not_installed"
    assert isinstance(result["message"], str)
    assert "error" not in result


async def test_select_disc_single_file_failure_shape(harness):
    """A single-file install cannot pin a disc → canonical failure shape."""
    _seed_single_file_install(harness, 13)
    result = await harness.plugin.select_disc(13, _DISC1)
    assert result["success"] is False
    assert result["reason"] == "not_installed"
