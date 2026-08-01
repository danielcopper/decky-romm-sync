#!/usr/bin/env python3
"""UoW-seam nesting ban — deadlock-prevention enforcement.

A Unit of Work opens a SQLite transaction with ``BEGIN IMMEDIATE`` on its
own connection, and that write lock is **not re-entrant**. Calling a seam
that opens its OWN UoW while a UoW is already open on the same path blocks
on the nested ``BEGIN IMMEDIATE`` until ``busy_timeout`` (~5s) elapses, then
raises ``database is locked``. ``FakeUnitOfWork`` in the unit tests shares no
real connection, so the deadlock is invisible there — it only surfaces on a
real device. The hazard has bitten four sites across three issues (#1047,
#1134 twice, and the migration site fixed via #1155).

The established fix is to snapshot the rows a seam needs *inside* one short
read UoW, close it, then resolve the seam *outside* any open UoW
(``services/relaunch_options_resolver.py``, ``services/cores.py``,
``services/disc.py``, ``services/downloads.py`` are the reference shape).

This check walks ``py_modules/services/`` and fails when a known
UoW-opening seam is called lexically inside an open
``with <...>uow_factory() as uow:`` block. The two seam families and the
bare-factory open live in :data:`SEAM_METHODS` / :data:`UOW_FACTORY_SUFFIX`
at the top of the file, so registering a future seam is a one-line addition.

The matcher is lexical and conservative (a guardrail, not a prover). It only
sees calls nested inside a ``with`` block in the same function scope — a seam
reached through a helper called from inside the UoW is not caught, and a
nested ``def``/``lambda`` resets the scope (a helper *defined* inside a UoW
but *called* elsewhere is not flagged). It also matches on the surface syntax:
aliasing a seam to a local (``fn = resolver.active_core_for_rom; fn(rom_id)``)
or holding the UoW factory under an attribute whose name does not end in
``uow_factory`` slips past the name match. The escape hatch is a trailing
comment on the seam-call line:

    self._active_core.active_core_for_rom(rom_id)  # pragma: no uow-check

Exit 0 on no findings, exit 1 if any findings (one line per finding).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "py_modules" / "services"

# --- The seam list -------------------------------------------------------
# Method names whose implementation opens its OWN Unit of Work. Calling any
# of these while a UoW is already open on the same SQLite connection
# re-enters the non-reentrant ``BEGIN IMMEDIATE`` and deadlocks. Matched by
# method name regardless of the receiver attribute — the names are distinctive
# to their seam, so this can't be dodged by renaming the holding field. A new
# seam is a one-line addition here.
SEAM_METHODS: frozenset[str] = frozenset(
    {
        "active_core_for_rom",  # ActiveCoreResolver (services/active_core_resolver.py)
        "active_emulator_for_rom",  # ActiveCoreResolver (services/active_core_resolver.py)
        "installed_relaunch_items",  # RelaunchOptionsResolver (services/relaunch_options_resolver.py)
        "launch_path_for_rom",  # RelaunchOptionsResolver (services/relaunch_options_resolver.py)
        "relaunch_item_for_rom",  # RelaunchOptionsResolver (services/relaunch_options_resolver.py)
    }
)

# A Unit of Work is opened by *calling* a factory whose final name segment
# ends with this suffix — ``self._uow_factory()``, ``config.uow_factory()``,
# a bare ``uow_factory()``. Used both to recognise the enclosing ``with``
# block AND to catch a nested factory open inside one.
UOW_FACTORY_SUFFIX = "uow_factory"

ESCAPE_HATCH = "pragma: no uow-check"


def _final_name(func: ast.expr) -> str | None:
    """Return the final identifier of a call target, or None.

    ``self._uow_factory`` -> ``_uow_factory``; ``config.uow_factory`` ->
    ``uow_factory``; ``uow_factory`` -> ``uow_factory``; a subscript/other
    expression -> None.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_uow_opener(node: ast.expr) -> bool:
    """Return True when *node* is a call that opens a Unit of Work."""
    if not isinstance(node, ast.Call):
        return False
    name = _final_name(node.func)
    return name is not None and name.endswith(UOW_FACTORY_SUFFIX)


def _seam_method(node: ast.Call) -> str | None:
    """Return the seam method name a call invokes, or None."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in SEAM_METHODS:
        return func.attr
    return None


def _finding_for_call(node: ast.Call, source_lines: list[str], rel: str) -> str | None:
    """Classify one call reached while a UoW is open. None = not a hazard."""
    seam = _seam_method(node)
    if seam is not None:
        detail = f"UoW-opening seam '.{seam}(...)'"
    elif _is_uow_opener(node):
        detail = f"nested UoW open '{_final_name(node.func)}(...)'"
    else:
        return None

    line_idx = node.lineno - 1
    if 0 <= line_idx < len(source_lines) and ESCAPE_HATCH in source_lines[line_idx]:
        return None

    return (
        f"{rel}:{node.lineno}:{node.col_offset} "
        f"{detail} called while a UoW is open — snapshot inside the UoW, close it, "
        f"then resolve outside (the nested BEGIN IMMEDIATE deadlocks → 'database is locked')"
    )


def _scan(node: ast.AST, in_uow: bool, source_lines: list[str], rel: str, findings: list[str]) -> None:
    """Walk *node*, flagging seam calls reached while *in_uow* is True.

    ``in_uow`` tracks whether the current position is lexically inside an open
    ``with <...>uow_factory()`` block within the same function scope. A nested
    ``def``/``lambda`` starts a fresh scope (``in_uow`` resets to False).
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        # A nested def/lambda is a new scope: its body does not execute inside
        # the enclosing UoW even when lexically nested. Reset so a helper
        # *defined* inside a UoW is not mistaken for a call inside it.
        for child in ast.iter_child_nodes(node):
            _scan(child, False, source_lines, rel, findings)
        return

    if isinstance(node, (ast.With, ast.AsyncWith)):
        opens_uow = False
        for item in node.items:
            # The context expression is evaluated in the enclosing scope, so a
            # factory open here that is itself nested inside another UoW counts.
            _scan(item.context_expr, in_uow, source_lines, rel, findings)
            if _is_uow_opener(item.context_expr):
                opens_uow = True
        body_in_uow = in_uow or opens_uow
        for stmt in node.body:
            _scan(stmt, body_in_uow, source_lines, rel, findings)
        return

    if isinstance(node, ast.Call):
        if in_uow:
            finding = _finding_for_call(node, source_lines, rel)
            if finding is not None:
                findings.append(finding)
        for child in ast.iter_child_nodes(node):
            _scan(child, in_uow, source_lines, rel, findings)
        return

    for child in ast.iter_child_nodes(node):
        _scan(child, in_uow, source_lines, rel, findings)


def scan_source(source: str, filename: str = "<source>") -> list[str]:
    """Return every nested-UoW-seam finding in one module's *source* text."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    source_lines = source.splitlines()
    findings: list[str] = []
    for node in tree.body:
        _scan(node, False, source_lines, filename, findings)
    return findings


def find_violations(services_dir: Path = SERVICES_DIR) -> list[str]:
    """Walk *services_dir* and return every nested-UoW-seam finding."""
    findings: list[str] = []
    if not services_dir.is_dir():
        return findings
    for path in sorted(services_dir.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        findings.extend(scan_source(source, rel))
    return findings


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    findings = find_violations(SERVICES_DIR)
    if findings:
        for line in findings:
            print(line)
        print()
        print(
            "ERROR: a UoW-opening seam (ActiveCoreResolver / RelaunchOptionsResolver / "
            "uow_factory) must not be called while a UoW is open on the same path "
            "(CLAUDE.md → Invariant register). Snapshot inside the UoW, close it, then "
            "resolve outside."
        )
        return 1
    print(f"OK: no nested UoW-seam calls in {SERVICES_DIR.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
