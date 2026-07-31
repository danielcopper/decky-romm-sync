"""Tests for ``scripts/check_module_size.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Each test retargets the
module's ``ROOT`` / scope / allowlist constants at a ``tmp_path`` tree, so the
real walk and the real comparison logic run against a controlled layout.

Coverage centres on the ratchet's four failure modes — an unlisted module over
the threshold, a listed module that grew, a listed module that graduated, and a
stale entry — plus the boundary conditions, where an off-by-one would silently
widen or narrow the gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_module_size.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_module_size", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _write_module(root: Path, relative: str, lines: int) -> None:
    """Create ``relative`` under ``root`` with exactly ``lines`` physical lines."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"x = {n}" for n in range(lines)) + "\n", encoding="utf-8")


@pytest.fixture
def run_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Yield a helper that lays out modules, retargets the check, and runs it."""

    def _run(modules: dict[str, int], allowlist: dict[str, int]) -> int:
        (tmp_path / "py_modules" / "services").mkdir(parents=True, exist_ok=True)
        for relative, lines in modules.items():
            _write_module(tmp_path, relative, lines)
        monkeypatch.setattr(check, "ROOT", tmp_path)
        monkeypatch.setattr(check, "ALLOWLIST", allowlist)
        return check.main()

    return _run


class TestLineCount:
    """``wc -l`` parity — the ceiling is meaningless if counting disagrees."""

    def test_counts_physical_lines(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "m.py", 12)
        assert check.line_count(tmp_path / "m.py") == 12

    def test_final_line_without_trailing_newline_still_counts(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("a = 1\nb = 2", encoding="utf-8")
        assert check.line_count(tmp_path / "m.py") == 2

    def test_empty_file_is_zero(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("", encoding="utf-8")
        assert check.line_count(tmp_path / "m.py") == 0


class TestHappyPath:
    def test_passes_when_everything_is_small(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({"py_modules/services/small.py": 120}, {}) == 0
        assert "OK: no module over 700 lines" in capsys.readouterr().out

    def test_listed_module_at_its_ceiling_passes(self, run_check) -> None:
        modules = {"py_modules/services/big.py": 900}
        assert run_check(modules, {"py_modules/services/big.py": 900}) == 0

    def test_bootstrap_is_in_scope_via_scope_files(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({"py_modules/bootstrap.py": 900}, {}) == 1
        assert "py_modules/bootstrap.py: 900 lines exceeds" in capsys.readouterr().err

    def test_main_py_is_out_of_scope(self, run_check) -> None:
        """``main.py`` grows with the callable surface by design — never flagged."""
        assert run_check({"main.py": 5000}, {}) == 0


class TestFailureModes:
    def test_unlisted_module_over_threshold_fails(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({"py_modules/services/new.py": 701}, {}) == 1
        err = capsys.readouterr().err
        assert "py_modules/services/new.py: 701 lines exceeds the 700-line threshold" in err
        assert "Adding it to ALLOWLIST is not the fix" in err

    def test_listed_module_that_grew_fails_with_the_delta(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        modules = {"py_modules/services/big.py": 910}
        assert run_check(modules, {"py_modules/services/big.py": 900}) == 1
        err = capsys.readouterr().err
        assert "910 lines, up from its 900-line ceiling" in err
        assert "Move the 10 added line(s)" in err

    def test_graduated_module_must_leave_the_allowlist(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        modules = {"py_modules/services/shrunk.py": 650}
        assert run_check(modules, {"py_modules/services/shrunk.py": 900}) == 1
        assert "back under the 700-line threshold" in capsys.readouterr().err

    def test_stale_allowlist_entry_fails(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({}, {"py_modules/services/deleted.py": 900}) == 1
        assert "listed in ALLOWLIST but not found" in capsys.readouterr().err

    def test_every_failing_module_is_reported_not_just_the_first(
        self, run_check, capsys: pytest.CaptureFixture[str]
    ) -> None:
        modules = {"py_modules/services/a.py": 800, "py_modules/services/b.py": 900}
        assert run_check(modules, {}) == 1
        err = capsys.readouterr().err
        assert "2 module(s)" in err
        assert "services/a.py" in err
        assert "services/b.py" in err


class TestBoundaries:
    """An off-by-one here silently widens or narrows the gate."""

    def test_exactly_at_threshold_is_allowed(self, run_check) -> None:
        assert run_check({"py_modules/services/edge.py": 700}, {}) == 0

    def test_one_over_threshold_is_not(self, run_check) -> None:
        assert run_check({"py_modules/services/edge.py": 701}, {}) == 1

    def test_one_over_ceiling_is_not(self, run_check) -> None:
        modules = {"py_modules/services/big.py": 901}
        assert run_check(modules, {"py_modules/services/big.py": 900}) == 1

    def test_threshold_plus_one_stays_listed_rather_than_graduating(self, run_check) -> None:
        """701 is still over the threshold, so the entry is still required."""
        modules = {"py_modules/services/big.py": 701}
        assert run_check(modules, {"py_modules/services/big.py": 900}) == 0


class TestSlackAdvisory:
    """The advisory nudges the ratchet down without failing an honest refactor."""

    def test_banked_slack_prints_a_note_and_still_passes(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        modules = {"py_modules/services/big.py": 850}
        assert run_check(modules, {"py_modules/services/big.py": 900}) == 0
        out = capsys.readouterr().out
        assert "note: py_modules/services/big.py: 850 lines vs. a 900-line ceiling — lower it to 850." in out

    def test_slack_below_the_advisory_threshold_stays_quiet(
        self, run_check, capsys: pytest.CaptureFixture[str]
    ) -> None:
        modules = {"py_modules/services/big.py": 899}
        assert run_check(modules, {"py_modules/services/big.py": 900}) == 0
        assert "note:" not in capsys.readouterr().out


class TestRealRepository:
    """The shipped allowlist must describe the repository as it actually is."""

    def test_gate_passes_on_the_real_tree(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert check.main() == 0
        capsys.readouterr()

    def test_allowlist_has_no_entry_at_or_below_the_threshold(self) -> None:
        too_small = {name: n for name, n in check.ALLOWLIST.items() if n <= check.THRESHOLD}
        assert not too_small, f"these entries belong in no allowlist: {too_small}"
