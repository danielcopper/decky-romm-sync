#!/usr/bin/env python3
"""Keep every stated RomM minimum version equal to the one the plugin enforces.

``Plugin._MIN_REQUIRED_VERSION`` in ``main.py`` is the single source of truth:
it is the floor ``test_connection()`` rejects servers against, so the plugin is
inert below it. Several places restate that number for humans — a badge, the
requirements list, the trap note in CLAUDE.md — and a restated number drifts.

The constant is read with ``ast`` rather than by importing ``main``, which would
need Decky's runtime present.

Frozen history is deliberately out of scope: ADRs record the floor as it stood
when the decision was taken and must not be rewritten.

Usage:
    python scripts/check_romm_min_version.py            # report drift, exit 1
    python scripts/check_romm_min_version.py --fix      # rewrite the claims
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "main.py"
CONSTANT = "_MIN_REQUIRED_VERSION"

# Each claim site: file, a regex with the version as group 1, and a label.
# The regexes are deliberately narrow — a loose one would rewrite unrelated
# version numbers.
CLAIMS = [
    (
        "README.md",
        re.compile(r"(?<=badge/RomM-%E2%89%A5%20)(\d+\.\d+\.\d+)(?=-)"),
        "readme badge",
    ),
    (
        "README.md",
        re.compile(r"(?<=\*\*version )(\d+\.\d+\.\d+)(?= or newer\*\*)"),
        "readme requirements",
    ),
    (
        "CLAUDE.md",
        re.compile(r"(?<=Requires RomM >= )(\d+\.\d+\.\d+)"),
        "claude.md trap note",
    ),
]


def enforced_version() -> str:
    """The floor as ``main.py`` states it, e.g. ``4.9.0``."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if CONSTANT not in names:
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, tuple) or not all(isinstance(p, int) for p in value):
            raise SystemExit(f"{CONSTANT} is not a tuple of ints: {value!r}")
        return ".".join(str(p) for p in value)
    raise SystemExit(f"{CONSTANT} not found in {SOURCE.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="rewrite the claims instead of reporting")
    args = parser.parse_args()

    expected = enforced_version()
    drift: list[str] = []
    fixed: list[str] = []

    for filename, pattern, label in CLAIMS:
        path = ROOT / filename
        text = path.read_text(encoding="utf-8")
        found = pattern.findall(text)
        if not found:
            drift.append(f"{filename}: no RomM version found for the {label}")
            continue
        wrong = [v for v in found if v != expected]
        if not wrong:
            continue
        if args.fix:
            path.write_text(pattern.sub(expected, text), encoding="utf-8")
            fixed.append(f"{filename}: {label} {', '.join(wrong)} -> {expected}")
        else:
            drift.append(f"{filename}: {label} says {', '.join(wrong)}, {SOURCE.name} enforces {expected}")

    for line in fixed:
        print(f"fixed {line}")
    if drift:
        print(
            f"ERROR: the stated RomM minimum has drifted from {SOURCE.name}'s {CONSTANT} ({expected}):",
            file=sys.stderr,
        )
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nRun: python scripts/check_romm_min_version.py --fix",
            file=sys.stderr,
        )
        return 1
    if not fixed:
        print(f"OK: every stated RomM minimum matches {CONSTANT} ({expected})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
