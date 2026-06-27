"""Tests for ``scripts/check_dependabot_cap.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). The gate decides whether a
Dependabot pip bump stayed *under the declared ``<X`` ceiling* (safe to
auto-merge) or *raised* it (needs human review), by comparing the upper bounds in
the base vs head requirements sources.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_dependabot_cap.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_dependabot_cap", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


# --- find_cap_changes: the decision kernel ---------------------------------


def test_in_range_lower_bound_bump_is_within_cap() -> None:
    """The routine case: lower bound moves, the `<` cap is untouched."""
    base = "ruff>=0.15.18,<0.16\n"
    head = "ruff>=0.15.20,<0.16\n"
    assert check.find_cap_changes(base, head) == []


def test_raising_the_ceiling_needs_review() -> None:
    """The #806 case: `<1.4` -> `<1.5` crosses a deliberately-drawn boundary."""
    base = "pytest-asyncio>=1.3.0,<1.4\n"
    head = "pytest-asyncio>=1.4.0,<1.5\n"
    issues = check.find_cap_changes(base, head)
    assert len(issues) == 1
    assert "pytest-asyncio" in issues[0]
    assert "upper bound changed" in issues[0]


def test_comments_and_whitespace_are_ignored() -> None:
    base = "hypothesis>=6.155.6,<7.0  # property tests (#1028)\n"
    head = "hypothesis>=6.155.7,<7.0  # property tests (#1028)\n"
    assert check.find_cap_changes(base, head) == []


def test_added_dependency_needs_review() -> None:
    base = "ruff>=0.15.18,<0.16\n"
    head = "ruff>=0.15.18,<0.16\nnewdep>=1.0,<2.0\n"
    issues = check.find_cap_changes(base, head)
    assert issues == ["'newdep' added"]


def test_removed_dependency_needs_review() -> None:
    base = "ruff>=0.15.18,<0.16\nold>=1.0,<2.0\n"
    head = "ruff>=0.15.18,<0.16\n"
    issues = check.find_cap_changes(base, head)
    assert issues == ["'old' removed"]


def test_no_upper_bound_either_side_is_within_cap() -> None:
    """An uncapped requirement has no ceiling to cross."""
    base = "somelib>=1.0\n"
    head = "somelib>=1.5\n"
    assert check.find_cap_changes(base, head) == []


def test_name_canonicalization_matches_across_styles() -> None:
    """`Import-Linter` and `import_linter` are the same package."""
    base = "Import-Linter>=2.11,<3.0\n"
    head = "import_linter>=2.12,<3.0\n"
    assert check.find_cap_changes(base, head) == []


# --- main: CLI exit codes ---------------------------------------------------


def _write_pair(tmp_path: Path, base: str, head: str) -> tuple[str, str]:
    base_path = tmp_path / "base.txt"
    head_path = tmp_path / "head.txt"
    base_path.write_text(base)
    head_path.write_text(head)
    return str(base_path), str(head_path)


def test_main_within_cap_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, head = _write_pair(tmp_path, "ruff>=0.15.18,<0.16\n", "ruff>=0.15.20,<0.16\n")
    assert check.main([base, head]) == 0
    assert "safe to auto-merge" in capsys.readouterr().out


def test_main_cap_raise_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, head = _write_pair(tmp_path, "pytest-asyncio>=1.3.0,<1.4\n", "pytest-asyncio>=1.3.0,<1.5\n")
    assert check.main([base, head]) == 1
    assert "NEEDS REVIEW" in capsys.readouterr().out


def test_main_multiple_pairs_any_change_fails(tmp_path: Path) -> None:
    """dev pair is clean, docs pair raises a cap -> overall needs review."""
    dev_base = tmp_path / "dev_base.txt"
    dev_head = tmp_path / "dev_head.txt"
    docs_base = tmp_path / "docs_base.txt"
    docs_head = tmp_path / "docs_head.txt"
    dev_base.write_text("ruff>=0.15.18,<0.16\n")
    dev_head.write_text("ruff>=0.15.20,<0.16\n")
    docs_base.write_text("mkdocs>=1.6,<2.0\n")
    docs_head.write_text("mkdocs>=2.0,<3.0\n")
    assert check.main([str(dev_base), str(dev_head), str(docs_base), str(docs_head)]) == 1


def test_main_rejects_odd_argument_count() -> None:
    assert check.main(["only-one-path.txt"]) == 2
