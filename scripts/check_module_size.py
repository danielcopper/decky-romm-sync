#!/usr/bin/env python3
"""Hold the line on module size: no new god classes, no growth in the old ones.

CLAUDE.md sets a decomposition threshold for every first-party backend tree —
``services/``, ``bootstrap/``, ``adapters/``, ``domain/``, ``lib/`` and
``models/``. It lived in prose, and prose drifted: ``sync_orchestrator.py`` was
901 lines when #999 wrote it down and kept growing. Nothing failed in between,
because nothing was watching.

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

Ceilings are lines of code: blank and comment-only lines do not count, so
``grep -cvE '^[[:space:]]*(#|$)' <file>`` reproduces them exactly. Comments must
not compete with code for the ceiling — counting them makes deleting an
explanation the cheapest way to afford a line of logic, and that trade is worst
in exactly the largest modules. Docstrings do count as code, deliberately:
exempting them would just move the prose one quote-mark over to dodge the
counter.

There is deliberately no ``--update`` flag: re-baselining is the one edit that
has to be a conscious, reviewable diff — never a command run to get back to
green.

Usage:
    python scripts/check_module_size.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
THRESHOLD = 1000
SLACK_ADVISORY = 50

# Trees the threshold governs — every first-party backend tree. What is absent
# is absent on purpose, not by oversight:
#   ``main.py`` owns the Decky lifecycle plus one ``async def`` per callable,
#     so it grows with the callable surface by design (CLAUDE.md, "Process
#     boundaries").
#   ``py_modules/_vendor`` is a checksum-pinned verbatim copy; its size is
#     upstream's decision (.claude/rules/vendored-assets.md).
#   ``tests/`` is one file per source module by rule
#     (.claude/rules/testing-backend.md), so a large test file is that rule
#     working — gating it here would put two of our own rules in conflict.
#   ``scripts/`` is developer tooling that never ships.
#   ``src/`` has the same god-class problem and no enforcement, but needs a
#     per-scope glob first: ``in_scope`` hardcodes ``*.py``.
SCOPE_DIRS = (
    "py_modules/adapters",
    "py_modules/bootstrap",
    "py_modules/domain",
    "py_modules/lib",
    "py_modules/models",
    "py_modules/services",
)

# Modules that were already over the threshold when this gate landed, each
# pinned at the size it had that day. Entries come out when the module drops
# back under the threshold; numbers go down when a refactor banks real slack.
# A number is never raised — that is the whole point of the gate.
ALLOWLIST = {
    "py_modules/services/downloads.py": 1119,
    "py_modules/services/library/fetcher.py": 1150,
    "py_modules/services/library/sync_orchestrator.py": 1480,
}


def in_scope() -> list[pathlib.Path]:
    """Every Python module the threshold applies to, in stable order."""
    paths: set[pathlib.Path] = set()
    for directory in SCOPE_DIRS:
        paths.update((ROOT / directory).rglob("*.py"))
    return sorted(paths)


def line_count(path: pathlib.Path) -> int:
    """Lines of code — blank and comment-only lines excluded.

    Deliberately textual rather than ``tokenize``-based: the two agree on the
    in/out verdict for every module in scope, and this form keeps the number
    reproducible by hand with ``grep -cvE '^[[:space:]]*(#|$)'``. The known cost
    is a ``#``-prefixed line inside a triple-quoted string, which counts as a
    comment; accepted.
    """
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    )


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
