#!/usr/bin/env python3
"""Hold the line on module size: no new god classes, no growth in the old ones.

CLAUDE.md sets a ~700-LOC decomposition threshold for ``services/`` and an
explicit split trigger for ``bootstrap.py``. Both lived in prose, and prose
drifted: ``sync_orchestrator.py`` was 901 lines when #999 wrote it down and is
past 2000 today. Nothing failed in between, because nothing was watching.

This gate watches. Every in-scope module above the threshold carries a ceiling
in ``ALLOWLIST`` — the size it had when it was recorded. That buys the property
the prose rule never had:

* a **new** module cannot cross the threshold at all, because it is not listed;
* a **listed** module cannot grow past its recorded ceiling;
* a module that shrinks back under the threshold must leave the list, so the
  list only ever gets shorter.

Shrinking below the ceiling without reaching the threshold passes. Demanding an
exact match would fail CI on any refactor that nets a single line; the gate
prints an advisory instead, once the banked slack is worth writing down.

Ceilings are physical line counts, so ``wc -l`` reproduces them exactly. There
is deliberately no ``--update`` flag: re-baselining is the one edit that has to
be a conscious, reviewable diff — never a command run to get back to green.

Usage:
    python scripts/check_module_size.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
THRESHOLD = 700
SLACK_ADVISORY = 50

# Trees and files the threshold governs. ``main.py`` is deliberately absent: it
# owns the Decky lifecycle plus one ``async def`` per callable, so it grows with
# the callable surface by design (CLAUDE.md, "Process boundaries").
SCOPE_DIRS = ("py_modules/services",)
SCOPE_FILES = ("py_modules/bootstrap.py",)

# Modules that were already over the threshold when this gate landed, each
# pinned at the size it had that day. Entries come out when the module drops
# back under the threshold; numbers go down when a refactor banks real slack.
# A number is never raised — that is the whole point of the gate.
ALLOWLIST = {
    "py_modules/bootstrap.py": 844,
    "py_modules/services/artwork.py": 1063,
    "py_modules/services/connection.py": 844,
    "py_modules/services/downloads.py": 1501,
    "py_modules/services/firmware.py": 879,
    "py_modules/services/library/fetcher.py": 1406,
    "py_modules/services/library/reporter.py": 920,
    "py_modules/services/library/sync_orchestrator.py": 2019,
    "py_modules/services/migration.py": 1093,
    "py_modules/services/playtime.py": 878,
    "py_modules/services/saves/sync_engine/engine.py": 1170,
    "py_modules/services/saves/sync_engine/matrix.py": 1063,
    "py_modules/services/version_switch.py": 917,
}


def in_scope() -> list[pathlib.Path]:
    """Every Python module the threshold applies to, in stable order."""
    paths: set[pathlib.Path] = set()
    for directory in SCOPE_DIRS:
        paths.update((ROOT / directory).rglob("*.py"))
    for filename in SCOPE_FILES:
        path = ROOT / filename
        if path.is_file():
            paths.add(path)
    return sorted(paths)


def line_count(path: pathlib.Path) -> int:
    """Physical lines, matching ``wc -l``."""
    return len(path.read_text(encoding="utf-8").splitlines())


def main() -> int:
    sizes = {p.relative_to(ROOT).as_posix(): line_count(p) for p in in_scope()}

    failures: list[str] = []
    advisories: list[str] = []

    for name, size in sorted(sizes.items()):
        ceiling = ALLOWLIST.get(name)
        if ceiling is None:
            if size > THRESHOLD:
                failures.append(
                    f"{name}: {size} lines exceeds the {THRESHOLD}-line threshold.\n"
                    f"      Decompose it into sub-services (services/saves/ is the reference).\n"
                    f"      Adding it to ALLOWLIST is not the fix — that list is for modules\n"
                    f"      that predate this gate."
                )
            continue
        if size > ceiling:
            failures.append(
                f"{name}: {size} lines, up from its {ceiling}-line ceiling.\n"
                f"      This module is already over the threshold; it may not grow.\n"
                f"      Move the {size - ceiling} added line(s) into a new module."
            )
        elif size <= THRESHOLD:
            failures.append(
                f"{name}: {size} lines is back under the {THRESHOLD}-line threshold.\n"
                f"      Drop its ALLOWLIST entry — the list only ever gets shorter."
            )
        elif ceiling - size >= SLACK_ADVISORY:
            advisories.append(f"{name}: {size} lines vs. a {ceiling}-line ceiling — lower it to {size}.")

    failures.extend(
        f"{name}: listed in ALLOWLIST but not found. Drop the stale entry."
        for name in sorted(set(ALLOWLIST) - set(sizes))
    )

    for line in advisories:
        print(f"note: {line}")

    if failures:
        print(
            f"ERROR: module-size gate failed ({len(failures)} module(s)) — see scripts/check_module_size.py:",
            file=sys.stderr,
        )
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    over = len(ALLOWLIST)
    print(f"OK: no module over {THRESHOLD} lines outside the allowlist ({over} grandfathered, none grew)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
