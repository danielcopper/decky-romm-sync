#!/usr/bin/env python3
"""Settings-owner confinement gate.

``settings.json`` is written crash-safe by a single owner — the persistence
adapter (``adapters/persistence.py``: write-tmp → fsync → ``os.replace`` →
fsync-dir, plus corrupt-file quarantine). If any other module names the file by
its literal filename, that module could read or write it directly, bypassing the
crash-safe path and the version-stamping/quarantine invariants the owner holds.

This check enforces a **proxy** for "all writes go through the owner": the
string literal ``"settings.json"`` may appear ONLY in the owner module.
Confining the filename literal to the owner means no other module can name —
hence open or write — the file. This is name-confinement, NOT taint/dataflow
analysis: it cannot catch a module that receives an already-constructed path
from the owner and writes to it. It catches the common regression, which is a
second module hardcoding the filename and writing its own copy.

It scans every backend ``.py`` (all of ``py_modules/`` except ``_vendor/`` and
except the owner, plus ``main.py``) via ``ast`` and flags any ``ast.Constant``
whose value is exactly the str ``"settings.json"``. A docstring or comment that
merely *mentions* settings.json in prose does not match — only an exact
string-constant ``"settings.json"`` does (a docstring's constant value is the
whole docstring text, not the bare filename). ``tests/`` is not scanned: tests
legitimately reference the filename.

Exit 0 when the literal is confined to the owner, 1 (one line per offending
site) otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_MODULES_DIR = REPO_ROOT / "py_modules"
VENDOR_DIR = PY_MODULES_DIR / "_vendor"
MAIN_PY = REPO_ROOT / "main.py"

# The single module allowed to name settings.json — its crash-safe owner.
OWNER = REPO_ROOT / "py_modules" / "adapters" / "persistence.py"

# The confined literal.
SETTINGS_FILENAME = "settings.json"


def _iter_scanned_files() -> list[Path]:
    """Every backend ``.py`` to scan: py_modules/ minus _vendor/ and the owner, plus main.py."""
    owner = OWNER.resolve()
    files = sorted(
        p
        for p in PY_MODULES_DIR.rglob("*.py")
        if VENDOR_DIR not in p.parents and p != VENDOR_DIR and p.resolve() != owner
    )
    if MAIN_PY.is_file():
        files.append(MAIN_PY)
    return files


def find_violations(files: list[Path] | None = None) -> list[str]:
    """Return one human-readable line per ``"settings.json"`` literal outside the owner.

    Walks each file's AST for any ``ast.Constant`` whose value is exactly the str
    ``"settings.json"``. Each occurrence outside the owner is a violation; the
    line reports the repo-relative path, line number, and a fix hint.
    """
    if files is None:
        files = _iter_scanned_files()
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
            f'{rel}:{node.lineno}: names the literal "settings.json" — settings.json '
            f"is written crash-safe only by its owner (adapters/persistence.py). Route "
            f"reads/writes through the owner instead of hardcoding the filename here."
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == SETTINGS_FILENAME
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
            'ERROR: the literal "settings.json" must appear only in its owning adapter '
            "(adapters/persistence.py), which writes the file crash-safe. Any other module "
            "naming the file can bypass the crash-safe write path and version/quarantine "
            "invariants — route through the owner instead."
        )
        return 1
    print("OK: settings.json literal is confined to its owner (adapters/persistence.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
