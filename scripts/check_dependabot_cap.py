#!/usr/bin/env python3
"""Dependabot auto-merge cap gate.

Auto-merging a pip Dependabot PR is safe only while the bump stays *under the
upper version ceiling the maintainer already declared* in ``requirements-*.txt``
(the ``<X`` cap). Dependabot normally just moves the lower ``>=`` bound within
that ceiling — a routine in-range bump. When it instead RAISES the ceiling
(``<1.4`` -> ``<1.5``) it crosses a boundary the cap was deliberately drawn at
(e.g. ``pytest-asyncio`` held below ``1.4`` for #806), so that PR must get human
review rather than auto-merge.

This gate compares the base and head requirements sources and exits ``0``
("within cap", safe to auto-merge) only when every package keeps the SAME set of
upper bounds (``<`` / ``<=``) and no package is added or removed. Any upper-bound
change, or a package-set change, exits ``1`` ("needs review"). The ceiling lives
in exactly one place — the requirements source — and this gate reads it; it never
duplicates the cap into a second list (the drift the #1113/#1114/#1115-class
gates exist to prevent).
"""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def upper_bounds(req: Requirement) -> frozenset[tuple[str, str]]:
    """The ``(operator, version)`` pairs that cap a requirement from above."""
    return frozenset((spec.operator, spec.version) for spec in req.specifier if spec.operator in ("<", "<="))


def parse(text: str) -> dict[str, Requirement]:
    """Parse a requirements ``.txt`` body into ``canonical name -> Requirement``."""
    reqs: dict[str, Requirement] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # blank/comment or -r/-c include
            continue
        req = Requirement(line)
        reqs[canonicalize_name(req.name)] = req
    return reqs


def find_cap_changes(base_text: str, head_text: str) -> list[str]:
    """Return human-readable reasons the head crossed a declared ceiling (empty == within cap)."""
    base, head = parse(base_text), parse(head_text)
    issues: list[str] = []
    for name in sorted(set(base) | set(head)):
        if name not in base:
            issues.append(f"'{name}' added")
            continue
        if name not in head:
            issues.append(f"'{name}' removed")
            continue
        before, after = upper_bounds(base[name]), upper_bounds(head[name])
        if before != after:
            issues.append(f"'{name}' upper bound changed: {sorted(before) or 'none'} -> {sorted(after) or 'none'}")
    return issues


def main(argv: list[str]) -> int:
    if len(argv) < 2 or len(argv) % 2 != 0:
        print("usage: check_dependabot_cap.py BASE HEAD [BASE HEAD ...]", file=sys.stderr)
        return 2

    issues: list[str] = []
    for i in range(0, len(argv), 2):
        base_path, head_path = Path(argv[i]), Path(argv[i + 1])
        issues += find_cap_changes(base_path.read_text(), head_path.read_text())

    if issues:
        print("NEEDS REVIEW: Dependabot changed a declared version ceiling (not just an in-range bump):")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print("OK: bump stays under the declared version ceilings — safe to auto-merge.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
