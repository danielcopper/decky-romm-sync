"""Unit tests for ``RendererRssAdapter`` — the ``/proc`` VmRSS reader.

Drives a fake ``/proc`` tree under ``tmp_path`` by pointing the adapter's
``_PROC`` module constant at it, so the max-``steamwebhelper`` heuristic and every
fail-open path are exercised without a live process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from adapters import renderer_rss
from adapters.renderer_rss import RendererRssAdapter

if TYPE_CHECKING:
    from pathlib import Path


def _make_proc_entry(proc_root: Path, pid: int, comm: str, vmrss_kb: int | None) -> None:
    """Write a fake ``/proc/<pid>/comm`` + ``status`` pair under *proc_root*."""
    entry = proc_root / str(pid)
    entry.mkdir()
    (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
    status = "Name:\t" + comm + "\nVmPeak:\t  1 kB\n"
    if vmrss_kb is not None:
        status += f"VmRSS:\t{vmrss_kb:>10} kB\n"
    (entry / "status").write_text(status, encoding="utf-8")


@pytest.fixture
def proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(renderer_rss, "_PROC", str(root))
    return root


def test_returns_max_steamwebhelper_rss(proc_root: Path) -> None:
    _make_proc_entry(proc_root, 100, "steamwebhelper", 438_000)
    _make_proc_entry(proc_root, 200, "steamwebhelper", 2_528_000)  # the renderer
    _make_proc_entry(proc_root, 300, "steamwebhelper", 512_000)
    assert RendererRssAdapter()() == 2_528_000


def test_ignores_non_steamwebhelper_processes(proc_root: Path) -> None:
    _make_proc_entry(proc_root, 100, "steamwebhelper", 900_000)
    _make_proc_entry(proc_root, 200, "chrome", 4_000_000)  # larger, but wrong comm
    _make_proc_entry(proc_root, 300, "gamescope", 3_000_000)
    assert RendererRssAdapter()() == 900_000


def test_none_when_no_steamwebhelper_present(proc_root: Path) -> None:
    _make_proc_entry(proc_root, 100, "chrome", 4_000_000)
    _make_proc_entry(proc_root, 200, "systemd", 12_000)
    assert RendererRssAdapter()() is None


def test_none_when_proc_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_rss, "_PROC", str(tmp_path / "does-not-exist"))
    assert RendererRssAdapter()() is None


def test_skips_non_numeric_and_vanished_entries(proc_root: Path) -> None:
    # A non-numeric dir (e.g. /proc/self) is skipped; a numeric entry missing its
    # files (a process that exited mid-scan) is skipped, not fatal.
    (proc_root / "self").mkdir()
    (proc_root / "acpi").mkdir()
    (proc_root / "404").mkdir()  # numeric but no comm/status — the vanished race
    _make_proc_entry(proc_root, 100, "steamwebhelper", 1_234_000)
    assert RendererRssAdapter()() == 1_234_000


def test_steamwebhelper_without_vmrss_line_contributes_nothing(proc_root: Path) -> None:
    _make_proc_entry(proc_root, 100, "steamwebhelper", None)  # no VmRSS line
    _make_proc_entry(proc_root, 200, "steamwebhelper", 777_000)
    assert RendererRssAdapter()() == 777_000


def test_all_steamwebhelper_without_vmrss_returns_none(proc_root: Path) -> None:
    _make_proc_entry(proc_root, 100, "steamwebhelper", None)
    assert RendererRssAdapter()() is None
