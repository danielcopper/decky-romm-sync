#!/usr/bin/env python3
"""Aggregate field-assignment ban — Cosmic Python enforcement.

Aggregate roots in ``py_modules/domain/`` decorated with ``@cosmic_aggregate``
expose mutation only through verb-named methods on the root. External code
must NOT do ``aggregate.field = value`` — that bypasses the aggregate's
invariants. This check scans ``py_modules/services/`` for such patterns and
fails CI if any are found.

Heuristic (conservative by design):

  1. Parse every file under ``py_modules/domain/``; collect the class names
     decorated with ``@cosmic_aggregate`` (or attribute paths ending in
     ``cosmic_aggregate``). Build a lowercase-name set.
  2. Parse every file under ``py_modules/services/``; flag every ``Assign``
     node whose target is an ``Attribute`` on a plain ``Name`` whose name
     (case-insensitively) contains one of the aggregate names as a substring.
     Skip ``self.x = ...`` and ``some[key].x = ...`` — those are method-body
     internals and subscript-receiver patterns, not aggregate-field
     assignments.

The heuristic can produce both false positives (variable named ``rom``
holding something else) and false negatives (assignment via complex
expressions like ``service.get_rom().field = ...``). It is a guardrail,
not a prover. The escape hatch is a trailing comment on the same line:

    rom.cover_path = path  # pragma: no aggregate-check

Aggregates that don't carry the decorator (e.g. old JSON-era containers
like ``SaveSyncState``) are NOT flagged — old code keeps working until
the SQLite cutover wave replaces them.

Exit 0 on no findings, exit 1 if any findings (one line per finding).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOMAIN_DIR = REPO_ROOT / "py_modules" / "domain"
SERVICES_DIR = REPO_ROOT / "py_modules" / "services"
DECORATOR_NAME = "cosmic_aggregate"
ESCAPE_HATCH = "pragma: no aggregate-check"


def _decorator_matches(node: ast.expr) -> bool:
    """Return True when ``node`` is ``cosmic_aggregate`` or ``...cosmic_aggregate``."""
    if isinstance(node, ast.Name):
        return node.id == DECORATOR_NAME
    if isinstance(node, ast.Attribute):
        return node.attr == DECORATOR_NAME
    if isinstance(node, ast.Call):
        return _decorator_matches(node.func)
    return False


def collect_aggregate_class_names(domain_dir: Path) -> set[str]:
    """Walk ``domain_dir`` and return all class names decorated with ``@cosmic_aggregate``."""
    names: set[str] = set()
    if not domain_dir.is_dir():
        return names
    for path in sorted(domain_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(_decorator_matches(d) for d in node.decorator_list):
                names.add(node.name)
    return names


def _receiver_name(target: ast.expr) -> str | None:
    """Return the variable name of an Attribute target's receiver, or None.

    ``rom.cover_path``        -> "rom"
    ``self.x``                -> None  (skipped — method-body internal)
    ``d["k"].field``          -> None  (Subscript receiver — not flagged)
    ``a.b.c``                 -> None  (nested attribute, not a plain Name)
    """
    if not isinstance(target, ast.Attribute):
        return None
    receiver = target.value
    if not isinstance(receiver, ast.Name):
        return None
    if receiver.id == "self":
        return None
    return receiver.id


def _name_matches_any_aggregate(name: str, aggregate_names: set[str]) -> str | None:
    """Return the matching aggregate class name if ``name`` (case-insensitive substring) hits one."""
    lowered = name.lower()
    for agg in aggregate_names:
        if agg.lower() in lowered:
            return agg
    return None


def find_violations(services_dir: Path, aggregate_names: set[str]) -> list[str]:
    """Scan ``services_dir`` for aggregate.field = value assignments. Return ``file:line:col message`` lines."""
    findings: list[str] = []
    if not aggregate_names or not services_dir.is_dir():
        return findings

    for path in sorted(services_dir.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        source_lines = source.splitlines()

        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign) or (isinstance(node, ast.AnnAssign) and node.value is not None):
                targets = [node.target]
            else:
                continue
            for target in targets:
                receiver = _receiver_name(target)
                if receiver is None:
                    continue
                matched = _name_matches_any_aggregate(receiver, aggregate_names)
                if matched is None:
                    continue
                line_idx = node.lineno - 1
                if 0 <= line_idx < len(source_lines) and ESCAPE_HATCH in source_lines[line_idx]:
                    continue
                rel = path.relative_to(REPO_ROOT)
                if not isinstance(target, ast.Attribute):
                    continue  # unreachable: _receiver_name only succeeds on Attribute targets
                findings.append(
                    f"{rel}:{node.lineno}:{node.col_offset} "
                    f"aggregate field-assignment '{receiver}.{target.attr} = ...' "
                    f"forbidden ({matched} is @cosmic_aggregate; call a verb-named method instead)"
                )
    return findings


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    aggregate_names = collect_aggregate_class_names(DOMAIN_DIR)
    findings = find_violations(SERVICES_DIR, aggregate_names)
    if findings:
        for line in findings:
            print(line)
        print()
        print(
            "ERROR: aggregate roots must mutate state only through verb-named methods "
            "on the aggregate (CLAUDE.md → Aggregates)."
        )
        return 1
    print(f"OK: no aggregate field-assignment violations in {SERVICES_DIR.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
