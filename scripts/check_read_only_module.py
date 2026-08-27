#!/usr/bin/env python3
"""Read-only module gate: a module whose name promises reads may not write.

A module named for reading is a promise about *when* it may be called. The
promise is worth something only while it holds: the sync's registry reads are
offloaded to an executor and opened as their own short Units of Work, chosen for
where they are cheap rather than for where a write would be safe. Drop a write
in among them and it commits at a moment nobody picked — which is exactly the
distinction that kept the platform stamp's DELETE in the orchestrator, where its
position in the apply's recovery protocol is the whole point (ADR-0023 / #1025).

Each module listed in :data:`READ_ONLY_MODULES` is walked for repository calls —
``<anything>.<repo>.<method>(...)`` where ``<repo>`` is one of the eleven
repositories :class:`services.protocols.uow.UnitOfWork` exposes — and every
method not in :data:`READ_METHODS` is a finding. The read set is derived from
``services/protocols/repositories.py``: every read there is ``get`` / ``get_*``
/ ``iter_*`` / ``count`` or is named explicitly below, and no write matches
those shapes (``save``, ``delete``, ``clear``, ``clear_*``, ``replace_all``,
``set``, ``set_*``). A repository that grows a read outside those shapes needs a
line here — until then the gate calls it a write, which fails loud rather than
silently widening.

It is a surface-syntax guardrail over one file each, not dataflow analysis. What
it cannot see:

* a write reached through a **helper** — the listed module calling a function
  elsewhere that writes; only calls written in the module itself are inspected;
* an **aliased repository handle** (``repo = uow.roms`` then ``repo.save(...)``),
  which flattens the two-attribute shape the scan matches on;
* ``getattr(uow, "roms").save(...)`` or any other dynamically-named access;
* a write through a repository the UoW does not expose under one of the eleven
  names in :data:`REPOSITORY_ATTRS`, including a future twelfth not added here.

None of those is a way to keep a module honest-looking while writing — they are
what a deliberate evasion would have to look like. What the gate does catch is
the accident: the plain ``uow.roms.save(...)`` added to a read module because it
was the closest place with a Unit of Work already open.

Exit 0 when every listed module only reads, 1 (one line per offending call)
otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- The declaration table ----------------------------------------------
# Module path (relative to the repo root) -> why it is read-only, quoted in the
# finding so the message carries the reason rather than only the rule.
READ_ONLY_MODULES: dict[str, str] = {
    "py_modules/services/library/registry_queries.py": (
        "it reads the registry and the completion stamps into projections the sync decides against; "
        "a write belongs where its position in the run is chosen"
    ),
}

# The repositories a Unit of Work exposes (services/protocols/uow.py). A call is
# only inspected when its receiver attribute is one of these, which is what
# makes ``uow.roms.save`` distinguishable from any other two-deep call.
REPOSITORY_ATTRS: frozenset[str] = frozenset(
    {
        "roms",
        "rom_installs",
        "rom_metadata",
        "playtime",
        "rom_save_sync_states",
        "bios_files",
        "firmware_cache",
        "sync_runs",
        "platform_sync_state",
        "collection_sync_state",
        "kv_config",
    }
)

# Read-method shapes, derived from services/protocols/repositories.py: every
# read is one of these, and no write is.
READ_PREFIXES: tuple[str, ...] = ("get", "iter_")
# Reads whose names carry neither prefix. ``count`` is exact (not a prefix) so a
# hypothetical ``count_and_prune`` would not inherit the exemption.
READ_METHODS: frozenset[str] = frozenset({"count", "rom_ids_with_pending_device"})


def _is_read(method: str) -> bool:
    """True when *method* is one of the repository reads.

    ``get`` matches both the bare name and every ``get_*``; ``iter_`` only the
    prefixed forms, there being no bare ``iter``.
    """
    if method in READ_METHODS:
        return True
    return method == "get" or method.startswith(("get_", "iter_"))


def _repository_call(node: ast.Call) -> tuple[str, str] | None:
    """``(repository, method)`` for a ``<...>.<repo>.<method>(...)`` call, else ``None``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    receiver = func.value
    if not isinstance(receiver, ast.Attribute) or receiver.attr not in REPOSITORY_ATTRS:
        return None
    return receiver.attr, func.attr


def find_violations(modules: dict[str, str] | None = None, *, root: Path | None = None) -> list[str]:
    """Return one human-readable line per repository write inside a read-only module."""
    if modules is None:
        modules = READ_ONLY_MODULES
    repo_root = root if root is not None else REPO_ROOT
    findings: list[str] = []
    for module, reason in sorted(modules.items()):
        path = repo_root / module
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            findings.append(f"{module}: declared read-only but could not be read — remove the entry or fix the path.")
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call = _repository_call(node)
            if call is None:
                continue
            repository, method = call
            if _is_read(method):
                continue
            findings.append(
                f"{module}:{node.lineno}:{node.col_offset} writes via '{repository}.{method}(...)' — "
                f"the module is declared read-only because {reason}."
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
            "ERROR: a module declared read-only calls a repository write — put the write where the "
            "moment it happens is chosen, or drop the module's read-only declaration "
            "(CLAUDE.md → Invariant register, #1777)."
        )
        return 1
    print(f"OK: all {len(READ_ONLY_MODULES)} read-only module(s) call repository reads only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
