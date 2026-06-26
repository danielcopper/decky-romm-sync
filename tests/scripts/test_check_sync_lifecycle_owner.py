"""Tests for ``scripts/check_sync_lifecycle_owner.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures lay out a small
``py_modules/services/library/`` tree under ``tmp_path`` so the check walks it
with its real ``_iter_scanned_files`` owner-exclusion logic.

Coverage centres on the confinement rule (``sync_state`` / ``current_sync_id``
attribute assignment is allowed only in the owner ``_state.py``) and the
distinction between an assignment store and a read/annotation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_sync_lifecycle_owner.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_sync_lifecycle_owner", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _make_library_tree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """Build a fake ``py_modules/services/library/`` tree: ``name -> source``."""
    library_dir = tmp_path / "py_modules" / "services" / "library"
    library_dir.mkdir(parents=True)
    for name, source in files.items():
        (library_dir / name).write_text(source, encoding="utf-8")
    return library_dir, library_dir / "_state.py"


@pytest.fixture
def patched_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Yield a helper that retargets the check at a tmp-path library tree + runs it."""

    def _run(files: dict[str, str]) -> list[str]:
        library_dir, owner = _make_library_tree(tmp_path, files)
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "LIBRARY_DIR", library_dir)
        monkeypatch.setattr(check, "OWNER", owner)
        files_scanned = check._iter_scanned_files(library_dir, owner)
        return check.find_violations(files_scanned)

    return _run


class TestFindViolations:
    def test_flags_box_sync_state_assignment(self, patched_check):
        findings = patched_check(
            {
                "_state.py": "class Box:\n    sync_state = 0\n",
                "sync_orchestrator.py": "def go(box):\n    box.sync_state = 1\n",
            }
        )
        assert len(findings) == 1
        assert "sync_orchestrator.py" in findings[0]
        assert "sync_state" in findings[0]

    def test_flags_current_sync_id_assignment(self, patched_check):
        findings = patched_check(
            {
                "_state.py": "x = 1\n",
                "reporter.py": "def go(box):\n    box.current_sync_id = None\n",
            }
        )
        assert len(findings) == 1
        assert "reporter.py" in findings[0]
        assert "current_sync_id" in findings[0]

    def test_flags_nested_receiver_assignment(self, patched_check):
        # The receiver shape doesn't matter — any ``.sync_state = ...`` store is caught.
        findings = patched_check(
            {
                "_state.py": "x = 1\n",
                "reporter.py": "class R:\n    def go(self):\n        self._box.sync_state = 2\n",
            }
        )
        assert len(findings) == 1
        assert "sync_state" in findings[0]

    def test_owner_assignment_not_flagged(self, patched_check):
        findings = patched_check(
            {
                "_state.py": "def go(self):\n    self.sync_state = 1\n    self.current_sync_id = 'r'\n",
            }
        )
        assert findings == []

    def test_reads_not_flagged(self, patched_check):
        findings = patched_check(
            {
                "_state.py": "x = 1\n",
                "service.py": "def go(box):\n    return box.sync_state == box.current_sync_id\n",
            }
        )
        assert findings == []

    def test_bare_annotation_not_flagged(self, patched_check):
        # ``sync_state: SyncState`` (no value) is a dataclass field annotation,
        # not a mutation.
        findings = patched_check(
            {
                "_state.py": "x = 1\n",
                "config.py": "class C:\n    sync_state: int\n",
            }
        )
        assert findings == []

    def test_aug_assign_flagged(self, patched_check):
        findings = patched_check(
            {
                "_state.py": "x = 1\n",
                "orch.py": "def go(box):\n    box.current_sync_id += 1\n",
            }
        )
        assert len(findings) == 1
        assert "current_sync_id" in findings[0]

    def test_unrelated_attribute_not_flagged(self, patched_check):
        findings = patched_check(
            {
                "_state.py": "x = 1\n",
                "orch.py": "def go(box):\n    box.sync_state_box = 1\n    box.last_heartbeat = 2\n",
            }
        )
        assert findings == []


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys):
        rc = check.main(["--help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Sync-lifecycle owner confinement gate" in captured.out

    def test_real_repo_run_is_clean(self, capsys):
        # After the #1202 change, the real library package assigns the lifecycle
        # pair only in _state.py — the check must exit 0.
        rc = check.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "OK:" in captured.out

    def test_main_reports_and_returns_one_on_planted_violation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ):
        library_dir, owner = _make_library_tree(
            tmp_path,
            {
                "_state.py": "x = 1\n",
                "sync_orchestrator.py": "def go(box):\n    box.sync_state = 1\n",
            },
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "LIBRARY_DIR", library_dir)
        monkeypatch.setattr(check, "OWNER", owner)

        rc = check.main([])
        assert rc == 1
        captured = capsys.readouterr()
        assert "sync_state" in captured.out
        assert "ERROR:" in captured.out
