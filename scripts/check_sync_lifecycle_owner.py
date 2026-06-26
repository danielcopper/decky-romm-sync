#!/usr/bin/env python3
"""Sync-lifecycle owner confinement gate.

The library sync run-lifecycle pair — ``sync_state`` and ``current_sync_id``
on :class:`LibrarySyncStateBox` — is mutated **only** through the box's own
verb methods (``try_begin_run`` / ``request_cancel`` / ``finish_run``). Confining
those two writes to the box keeps run admission, cancellation, and termination
a single compare-and-swap on the one event loop, so a rapid Sync/Cancel can't
leave a half-reset run id (#1202).

This check enforces that confinement: a raw attribute assignment whose target
attribute is exactly ``sync_state`` or ``current_sync_id`` may appear ONLY in
the owner module (``py_modules/services/library/_state.py``). Any other module
under ``py_modules/services/library/`` that assigns one of those two fields —
``box.sync_state = ...``, ``self._box.current_sync_id = ...``, etc. — bypasses
the verb-method API and is flagged.

It is an AST attribute-store check, NOT taint/dataflow analysis: it matches the
target attribute name on ``Assign`` / ``AugAssign`` / ``AnnAssign`` (with a
value) nodes. Reads (``ast.Load``) never match. A receiver of any shape is
caught (a plain ``Name`` like ``box`` or a nested ``Attribute`` like
``self._sync_state``), so the common regression — a sub-service flipping the
state field directly — cannot slip through.

Exit 0 when the two fields are written only by the owner, 1 (one line per
offending site) otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = REPO_ROOT / "py_modules" / "services" / "library"

# The single module allowed to assign the run-lifecycle fields — the box itself.
OWNER = LIBRARY_DIR / "_state.py"

# The confined attribute names.
LIFECYCLE_FIELDS = frozenset({"sync_state", "current_sync_id"})


def _iter_scanned_files(library_dir: Path, owner: Path) -> list[Path]:
    """Every ``.py`` under the library package except the owner module."""
    owner_resolved = owner.resolve()
    return sorted(p for p in library_dir.rglob("*.py") if p.resolve() != owner_resolved)


def _assignment_targets(node: ast.AST) -> list[ast.expr]:
    """Return the target expressions of an assignment node, or ``[]``.

    ``AnnAssign`` without a value is a bare annotation (no mutation) and yields
    no targets.
    """
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AugAssign):
        return [node.target]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target]
    return []


def find_violations(files: list[Path] | None = None) -> list[str]:
    """Return one human-readable line per lifecycle-field assignment outside the owner.

    Walks each file's AST for any assignment whose target is an ``Attribute``
    whose ``.attr`` is exactly ``sync_state`` or ``current_sync_id``.
    """
    if files is None:
        files = _iter_scanned_files(LIBRARY_DIR, OWNER)
    findings: list[str] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT)
        findings.extend(
            f"{rel}:{target.lineno}:{target.col_offset} "
            f"lifecycle field-assignment '....{target.attr} = ...' forbidden — "
            f"sync_state / current_sync_id are written only by LibrarySyncStateBox's "
            f"verb methods (try_begin_run / request_cancel / finish_run) in _state.py."
            for node in ast.walk(tree)
            for target in _assignment_targets(node)
            if isinstance(target, ast.Attribute) and target.attr in LIFECYCLE_FIELDS
        )
    return sorted(findings)


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    findings = find_violations()
    if findings:
        for line in findings:
            print(line)
        print()
        print(
            "ERROR: the run-lifecycle pair (sync_state / current_sync_id) must be mutated only "
            "through LibrarySyncStateBox's verb methods (try_begin_run / request_cancel / "
            "finish_run) in services/library/_state.py — route writes through a verb method "
            "instead of assigning the field directly (CLAUDE.md → Invariant register, #1202)."
        )
        return 1
    print("OK: sync_state / current_sync_id are written only by their owner (services/library/_state.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
