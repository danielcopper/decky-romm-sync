"""Tests for ``bin/romm-launcher`` — the out-of-process ROM-path resolver.

The launcher is the one reader of install state that lives outside the plugin
process. Per ADR-0005 it resolves the launch path dynamically at launch time
from the SQLite ``rom_installs`` table (opened read-only), then execs RetroDECK.
These tests run the real bash script against a real database built with the v1
schema, stubbing ``flatpak`` on PATH so the resolved argument is observable.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from adapters.repositories.unit_of_work import SqliteUnitOfWork
from adapters.sqlite_migrations import MIGRATIONS_DIR, apply_migrations
from domain.rom import Rom
from domain.rom_install import RomInstall

_LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "romm-launcher"


def _seed_install(db_path: str, *, rom_id: int, file_path: str) -> None:
    """Insert a ``Rom`` + ``RomInstall`` row (the FK requires the Rom first)."""
    with SqliteUnitOfWork(db_path) as uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug="n64",
                name=f"Game {rom_id}",
                fs_name=f"game_{rom_id}.z64",
                shortcut_app_id=1000 + rom_id,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=None,
                platform_slug="n64",
                system="n64",
                installed_at="2026-02-02T00:00:00+00:00",
            )
        )


def _flatpak_stub(bin_dir: Path) -> Path:
    """Write a ``flatpak`` stub that records its args to ``captured-args``."""
    capture = bin_dir / "captured-args"
    stub = bin_dir / "flatpak"
    stub.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{capture}"\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return capture


def _run_launcher(runtime_dir: Path, bin_dir: Path, home: Path, arg: str) -> subprocess.CompletedProcess[str]:
    """Run the launcher with a stubbed flatpak on PATH and an isolated HOME."""
    env = {
        **os.environ,
        "DECKY_PLUGIN_RUNTIME_DIR": str(runtime_dir),
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(_LAUNCHER), arg],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolves_path_from_db_and_execs_flatpak(tmp_path: Path) -> None:
    """Happy path: launcher reads file_path from rom_installs and hands it to flatpak."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    # The launcher checks the resolved path exists on disk before exec.
    rom_file = runtime_dir / "zelda.z64"
    rom_file.write_bytes(b"\x00" * 16)

    db_path = str(runtime_dir / "romm_sync.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    _seed_install(db_path, rom_id=42, file_path=str(rom_file))

    capture = _flatpak_stub(bin_dir)

    result = _run_launcher(runtime_dir, bin_dir, home, "romm:42")

    assert result.returncode == 0, result.stderr
    captured = capture.read_text().splitlines()
    # flatpak run net.retrodeck.retrodeck <resolved path>
    assert captured == ["run", "net.retrodeck.retrodeck", str(rom_file)]


def test_missing_row_exits_nonzero(tmp_path: Path) -> None:
    """A rom_id with no rom_installs row fails with a non-zero exit."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    db_path = str(runtime_dir / "romm_sync.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    _seed_install(db_path, rom_id=42, file_path=str(runtime_dir / "zelda.z64"))
    _flatpak_stub(bin_dir)

    result = _run_launcher(runtime_dir, bin_dir, home, "romm:999")

    assert result.returncode != 0
    assert "not found in database" in result.stderr


def test_empty_path_exits_nonzero(tmp_path: Path) -> None:
    """A row whose file_path is empty fails with a non-zero exit."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    db_path = str(runtime_dir / "romm_sync.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    # Insert a Rom and an install row with an empty file_path directly — the
    # aggregate factory has no empty-path invariant, so this is reachable state.
    _seed_install(db_path, rom_id=7, file_path="")
    _flatpak_stub(bin_dir)

    result = _run_launcher(runtime_dir, bin_dir, home, "romm:7")

    assert result.returncode != 0
    assert "not found in database" in result.stderr


def test_missing_db_exits_nonzero(tmp_path: Path) -> None:
    """No romm_sync.db in the runtime dir fails with a clear message."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _flatpak_stub(bin_dir)

    result = _run_launcher(runtime_dir, bin_dir, home, "romm:42")

    assert result.returncode != 0
    assert "Database not found" in result.stderr


def test_invalid_argument_exits_nonzero(tmp_path: Path) -> None:
    """A non-``romm:<id>`` argument is rejected before any DB access."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    _flatpak_stub(bin_dir)

    result = _run_launcher(runtime_dir, bin_dir, home, "garbage")

    assert result.returncode != 0
    assert "Invalid argument" in result.stderr
