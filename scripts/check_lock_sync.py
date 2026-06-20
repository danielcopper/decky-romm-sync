#!/usr/bin/env python3
"""Lockfile constraint gate.

``requirements-*.lock`` are compiled from ``requirements-*.txt`` via
``uv pip compile`` (``mise run lock-update``). CI and ``mise run setup`` install
from the LOCK, so a bump to a ``.txt`` constraint that does not regenerate the
lock is INERT (the old pin keeps getting installed) and can leave the lock
VIOLATING its source constraint, silently and green — the failure mode behind
#1113 / #1114 / #1115.

This gate asserts that every DIRECT dependency pinned in the lock SATISFIES the
version constraint declared in its ``.txt`` source (and that every source dep is
present in the lock). It deliberately does NOT recompile-and-diff against the
live package index: a fresh ``uv pip compile`` resolves to the newest versions
available *at compile time*, so an unrelated transitive upstream release (or a
different uv index-cache state) would make recompile-and-diff flap red with no
source change. Checking *satisfaction* — not "is it the newest" — targets
exactly the harmful drift and stays deterministic and offline.

Out of scope (not constrained by the source, so not checked): transitive pins
and lock-only orphans.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PAIRS = [
    ("requirements-dev.txt", "requirements-dev.lock"),
    ("requirements-docs.txt", "requirements-docs.lock"),
]

# A pinned line in a uv-compiled lock: `name==version` at column 0 (the `# via`
# provenance lines are indented, the header lines start with `#`).
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(.+?)\s*$")


def parse_sources(path: Path) -> list[Requirement]:
    """Parse a requirements ``.txt`` into its declared direct requirements."""
    reqs: list[Requirement] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # blank/comment or -r/-c include
            continue
        reqs.append(Requirement(line))
    return reqs


def parse_lock_pins(path: Path) -> dict[str, str]:
    """Map canonical package name -> pinned version from a compiled lock."""
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        m = _PIN_RE.match(raw)
        if m:
            pins[canonicalize_name(m.group(1))] = m.group(2)
    return pins


def main() -> int:
    errors: list[str] = []
    for src_name, lock_name in PAIRS:
        src, lock = Path(src_name), Path(lock_name)
        if not src.is_file():
            errors.append(f"missing source {src_name}")
            continue
        if not lock.is_file():
            errors.append(f"missing lock {lock_name}")
            continue

        pins = parse_lock_pins(lock)
        for req in parse_sources(src):
            pinned = pins.get(canonicalize_name(req.name))
            if pinned is None:
                errors.append(f"{src_name}: '{req.name}' is declared but not pinned in {lock_name}")
            elif not req.specifier.contains(pinned, prereleases=True):
                errors.append(
                    f"{lock_name}: '{req.name}' is pinned at {pinned}, "
                    f"which violates the '{req}' constraint in {src_name}"
                )

    if errors:
        print("ERROR: dependency lock(s) out of sync with their sources — run 'mise run lock-update' and commit:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: every locked pin satisfies its source constraint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
