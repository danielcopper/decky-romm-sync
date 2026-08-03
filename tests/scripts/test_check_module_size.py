"""Tests for ``scripts/check_module_size.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Each test retargets the
module's ``ROOT`` / scope / allowlist constants at a ``tmp_path`` tree, so the
real walk and the real comparison logic run against a controlled layout.

Coverage centres on the ratchet's four failure modes — an unlisted module over
the threshold, a listed module that grew, a listed module that graduated, and a
stale entry — plus the boundary conditions, where an off-by-one would silently
widen or narrow the gate, the code-line counting itself, where counting
comments would quietly turn every ceiling into a budget shared with prose, and
the walked scope, where a tree left out fails nothing at all.
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
    """Create ``relative`` under ``root`` with exactly ``lines`` lines of code.

    Every line written is code, so the file's physical length and its code-line
    count agree — which keeps the ratchet tests about the ratchet. Counting is
    pinned separately in ``TestLineCount``.
    """
    _write_raw_module(root, relative, "\n".join(f"x = {n}" for n in range(lines)) + "\n")


def _write_raw_module(root: Path, relative: str, text: str) -> None:
    """Create ``relative`` under ``root`` with verbatim ``text``."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def run_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Yield a helper that lays out modules, retargets the check, and runs it."""

    def _run(modules: dict[str, int], allowlist: dict[str, int], raw: dict[str, str] | None = None) -> int:
        (tmp_path / "py_modules" / "services").mkdir(parents=True, exist_ok=True)
        for relative, lines in modules.items():
            _write_module(tmp_path, relative, lines)
        for relative, text in (raw or {}).items():
            _write_raw_module(tmp_path, relative, text)
        monkeypatch.setattr(check, "ROOT", tmp_path)
        monkeypatch.setattr(check, "ALLOWLIST", allowlist)
        return check.main()

    return _run


class TestLineCount:
    """Lines of code — the ceiling is meaningless if counting disagrees."""

    def test_counts_code_lines(self, tmp_path: Path) -> None:
        _write_module(tmp_path, "m.py", 12)
        assert check.line_count(tmp_path / "m.py") == 12

    def test_blank_and_comment_only_lines_are_excluded(self, tmp_path: Path) -> None:
        _write_raw_module(tmp_path, "m.py", "# header\n\nx = 1\n    # indented\n   \ny = 2\n")
        assert check.line_count(tmp_path / "m.py") == 2

    def test_trailing_comment_on_a_code_line_still_counts(self, tmp_path: Path) -> None:
        _write_raw_module(tmp_path, "m.py", "x = 1  # why this is here\n")
        assert check.line_count(tmp_path / "m.py") == 1

    def test_docstring_lines_count_as_code(self, tmp_path: Path) -> None:
        """Deliberate: exempting docstrings would just move prose to dodge the counter."""
        _write_raw_module(tmp_path, "m.py", '"""Title.\n\nBody.\n"""\nx = 1\n')
        assert check.line_count(tmp_path / "m.py") == 4

    def test_hash_line_inside_a_string_counts_as_a_comment(self, tmp_path: Path) -> None:
        """Accepted inaccuracy of the textual count, pinned so it stays a known one.

        A tokenizer would call this line code. The textual form is what makes
        the ceiling reproducible by hand, and the two agree on the in/out
        verdict for every module in scope, so this stays as-is.
        """
        _write_raw_module(tmp_path, "m.py", 'DOC = """\n# not really a comment\n"""\n')
        assert check.line_count(tmp_path / "m.py") == 2

    def test_final_line_without_trailing_newline_still_counts(self, tmp_path: Path) -> None:
        _write_raw_module(tmp_path, "m.py", "a = 1\nb = 2")
        assert check.line_count(tmp_path / "m.py") == 2

    def test_empty_file_is_zero(self, tmp_path: Path) -> None:
        _write_raw_module(tmp_path, "m.py", "")
        assert check.line_count(tmp_path / "m.py") == 0


class TestHappyPath:
    def test_passes_when_everything_is_small(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({"py_modules/services/small.py": 120}, {}) == 0
        assert "OK: no module over 1000 lines" in capsys.readouterr().out

    def test_listed_module_at_its_ceiling_passes(self, run_check) -> None:
        modules = {"py_modules/services/big.py": 1200}
        assert run_check(modules, {"py_modules/services/big.py": 1200}) == 0


class TestScope:
    """Which trees the walk reaches. A tree left out of ``SCOPE_DIRS`` fails nothing, silently."""

    @pytest.mark.parametrize(
        "tree",
        [
            "py_modules/adapters",
            "py_modules/bootstrap",
            "py_modules/domain",
            "py_modules/lib",
            "py_modules/models",
            "py_modules/services",
        ],
    )
    def test_governed_tree_is_walked(self, tree: str, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({f"{tree}/big.py": 1200}, {}) == 1
        assert f"{tree}/big.py: 1200 lines exceeds" in capsys.readouterr().err

    def test_subpackages_are_walked(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        """The walk recurses — the largest modules in scope live in subpackages."""
        assert run_check({"py_modules/adapters/romm/big.py": 1200}, {}) == 1
        assert "py_modules/adapters/romm/big.py: 1200 lines exceeds" in capsys.readouterr().err

    def test_main_py_is_out_of_scope(self, run_check) -> None:
        """``main.py`` grows with the callable surface by design — never flagged."""
        assert run_check({"main.py": 5000}, {}) == 0

    def test_vendored_code_is_out_of_scope(self, run_check) -> None:
        """``_vendor/`` sits beside the governed trees but its size is upstream's decision."""
        assert run_check({"py_modules/_vendor/big.py": 5000}, {}) == 0


class TestCodeLinesNotPhysicalLines:
    """Comments must not compete with code for the ceiling."""

    def test_comments_and_blanks_do_not_count_toward_the_threshold(self, run_check) -> None:
        """Physically 1190 lines, 990 of code — a heavily documented module passes."""
        body = "\n".join(f"x = {n}" for n in range(990))
        padding = "\n".join("# explanation" if n % 2 else "" for n in range(200))
        raw = {"py_modules/services/documented.py": f"{body}\n{padding}\n"}
        assert run_check({}, {}, raw) == 0

    def test_comments_do_not_buy_room_above_a_ceiling(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        """Being mostly prose does not excuse a listed module that grew in code."""
        body = "\n".join(f"x = {n}" for n in range(1210))
        padding = "\n".join("# explanation" for _ in range(300))
        raw = {"py_modules/services/big.py": f"{body}\n{padding}\n"}
        assert run_check({}, {"py_modules/services/big.py": 1200}, raw) == 1
        assert "1210 lines, up from its 1200-line ceiling" in capsys.readouterr().err


class TestFailureModes:
    def test_unlisted_module_over_threshold_fails(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({"py_modules/services/new.py": 1001}, {}) == 1
        err = capsys.readouterr().err
        assert "py_modules/services/new.py: 1001 lines exceeds the 1000-line threshold" in err
        assert "Adding it to ALLOWLIST is not the fix" in err

    def test_listed_module_that_grew_fails_with_the_delta(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        modules = {"py_modules/services/big.py": 1210}
        assert run_check(modules, {"py_modules/services/big.py": 1200}) == 1
        err = capsys.readouterr().err
        assert "1210 lines, up from its 1200-line ceiling" in err
        assert "Move the 10 added line(s)" in err

    def test_graduated_module_must_leave_the_allowlist(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        modules = {"py_modules/services/shrunk.py": 950}
        assert run_check(modules, {"py_modules/services/shrunk.py": 1200}) == 1
        assert "back under the 1000-line threshold" in capsys.readouterr().err

    def test_stale_allowlist_entry_fails(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_check({}, {"py_modules/services/deleted.py": 1200}) == 1
        assert "listed in ALLOWLIST but not found" in capsys.readouterr().err

    def test_every_failing_module_is_reported_not_just_the_first(
        self, run_check, capsys: pytest.CaptureFixture[str]
    ) -> None:
        modules = {"py_modules/services/a.py": 1100, "py_modules/services/b.py": 1200}
        assert run_check(modules, {}) == 1
        err = capsys.readouterr().err
        assert "2 module(s)" in err
        assert "services/a.py" in err
        assert "services/b.py" in err


class TestBoundaries:
    """An off-by-one here silently widens or narrows the gate."""

    def test_exactly_at_threshold_is_allowed(self, run_check) -> None:
        assert run_check({"py_modules/services/edge.py": 1000}, {}) == 0

    def test_one_over_threshold_is_not(self, run_check) -> None:
        assert run_check({"py_modules/services/edge.py": 1001}, {}) == 1

    def test_one_over_ceiling_is_not(self, run_check) -> None:
        modules = {"py_modules/services/big.py": 1201}
        assert run_check(modules, {"py_modules/services/big.py": 1200}) == 1

    def test_threshold_plus_one_stays_listed_rather_than_graduating(self, run_check) -> None:
        """1001 is still over the threshold, so the entry is still required."""
        modules = {"py_modules/services/big.py": 1001}
        assert run_check(modules, {"py_modules/services/big.py": 1200}) == 0


class TestSlackAdvisory:
    """The advisory nudges the ratchet down without failing an honest refactor."""

    def test_banked_slack_prints_a_note_and_still_passes(self, run_check, capsys: pytest.CaptureFixture[str]) -> None:
        modules = {"py_modules/services/big.py": 1150}
        assert run_check(modules, {"py_modules/services/big.py": 1200}) == 0
        out = capsys.readouterr().out
        assert "note: py_modules/services/big.py: 1150 lines vs. a 1200-line ceiling — lower it to 1150." in out

    def test_slack_below_the_advisory_threshold_stays_quiet(
        self, run_check, capsys: pytest.CaptureFixture[str]
    ) -> None:
        modules = {"py_modules/services/big.py": 1199}
        assert run_check(modules, {"py_modules/services/big.py": 1200}) == 0
        assert "note:" not in capsys.readouterr().out


class TestRealRepository:
    """The shipped allowlist must describe the repository as it actually is."""

    def test_gate_passes_on_the_real_tree(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert check.main() == 0
        capsys.readouterr()

    def test_every_scope_dir_exists(self) -> None:
        """A misspelled scope entry governs nothing: ``rglob`` on a missing directory just yields nothing."""
        missing = [d for d in check.SCOPE_DIRS if not (check.ROOT / d).is_dir()]
        assert not missing, f"SCOPE_DIRS entries that do not exist: {missing}"

    def test_every_backend_package_with_python_is_governed(self) -> None:
        """The mirror of the entry-exists check: a new package under ``py_modules/`` must not escape the gate.

        ``_vendor/`` is the one deliberate exemption — a checksum-pinned verbatim copy whose size is upstream's
        decision, not ours (the reasoning is recorded beside ``SCOPE_DIRS``). Every other package holding Python
        belongs in scope, so do not widen this exemption to silence a failure.
        """
        governed = {d.split("/", 1)[1] for d in check.SCOPE_DIRS if d.startswith("py_modules/")}
        ungoverned = sorted(
            d.name
            for d in (check.ROOT / "py_modules").iterdir()
            if d.is_dir() and d.name not in governed and d.name != "_vendor" and any(d.rglob("*.py"))
        )
        assert not ungoverned, f"py_modules packages holding Python but outside SCOPE_DIRS: {ungoverned}"

    def test_allowlist_has_no_entry_at_or_below_the_threshold(self) -> None:
        too_small = {name: n for name, n in check.ALLOWLIST.items() if n <= check.THRESHOLD}
        assert not too_small, f"these entries belong in no allowlist: {too_small}"
