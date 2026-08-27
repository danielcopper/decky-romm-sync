#!/usr/bin/env python3
"""Read-only module gate: a module whose name promises reads may not write.

A module named for reading is a promise about *when* it may be called. The
promise is worth something only while it holds: the sync's registry reads are
offloaded to an executor and opened as their own short Units of Work, chosen for
where they are cheap rather than for where a write would be safe. Drop a write
in among them and it commits at a moment nobody picked.

Each module listed in :data:`READ_ONLY_MODULES` is walked for repository calls —
``<anything>.<repo>.<method>(...)`` where ``<repo>`` is one of the eleven
repositories :class:`services.protocols.uow.UnitOfWork` exposes — and every
method that is not a read is a finding. Read or write is decided **by the name's
shape**: ``get`` / ``get_*`` / ``iter_*`` / ``count`` plus the explicit names in
:data:`READ_METHODS`. That partition is exact over
``services/protocols/repositories.py`` as it stands today (no write there matches
a read shape: ``save``, ``delete``, ``clear``, ``clear_*``, ``replace_all``,
``set``, ``set_*``), and `test_check_read_only_module.py` re-derives every method
name from that file so a new one has to be classified here rather than drift past.

**The two directions are not symmetric, and the asymmetry runs the wrong way.**
A read named outside the shapes is called a write: it fails loud until a line is
added below, which is safe. A **write named like a read is called a read and
passes in silence** — ``uow.roms.get_or_create(1)`` and
``uow.roms.iter_and_purge()`` are both green today. Nothing in the scan looks at
what a method does, so a repository that grows a read-shaped mutator reopens
exactly the accident this gate is otherwise for. That is a reason to keep naming
repository writes as writes, not a hole the gate can close.

It is a surface-syntax guardrail over one file each, not dataflow analysis. What
it cannot see:

* a write reached through a **helper** — the listed module calling a function
  elsewhere that writes; only calls written in the module itself are inspected;
* a write **passed as a bound method rather than called** —
  ``loop.run_in_executor(None, uow.roms.save, rom)`` is an ``ast.Attribute``, not
  an ``ast.Call``, so the scan never sees it. This one is pointed rather than
  theoretical: every method in the declared module is *invoked* through that
  exact idiom, so it is the shape a reader of this area reaches for;
* a **read-shaped write name**, per the asymmetry above;
* an **aliased repository handle** (``repo = uow.roms`` then ``repo.save(...)``),
  which flattens the two-attribute shape the scan matches on;
* ``getattr(uow, "roms").save(...)`` or any other dynamically-named access;
* a write through a repository the UoW does not expose under one of the eleven
  names in :data:`REPOSITORY_ATTRS`, including a future twelfth not added here.

What the gate does catch is the plain ``uow.roms.save(...)`` added to a read
module because it was the closest place with a Unit of Work already open. The
list above is the honest boundary of that: most of it is what a deliberate
evasion would look like, but the bound-method reference and the read-shaped name
are accidents too, and they pass.

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
    "py_modules/services/library/local_library_reader.py": (
        "it reads this device's own record of the library — the Rom rows, the completion stamps, "
        "the last finished run — and nothing else; a write belongs with the pipeline that performs it"
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
# read is one of these, and — as that file stands today — no write is. The test
# re-derives every method name from it, so a new one has to be classified.
READ_PREFIXES: tuple[str, ...] = ("get_", "iter_")
# Reads whose names carry neither prefix. Exact names, not prefixes, so a
# hypothetical ``count_and_prune`` would not inherit the exemption.
READ_METHODS: frozenset[str] = frozenset({"get", "count", "rom_ids_with_pending_device"})


def _is_read(method: str) -> bool:
    """True when *method* is one of the repository reads.

    ``get`` is exact and ``get_*`` is a prefix; ``iter_`` has only prefixed
    forms, there being no bare ``iter``.
    """
    return method in READ_METHODS or method.startswith(READ_PREFIXES)


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
        except SyntaxError as exc:
            # Not "nothing to scan": a declared module the parser cannot read is
            # a module nobody is checking. Failing here costs a CI run on code
            # that would not import anyway.
            findings.append(f"{module}:{exc.lineno or 0}: declared read-only but does not parse — {exc.msg}.")
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
            "ERROR: a module declared read-only does not hold to it — put the write with the "
            "pipeline that performs it (or fix the declared path), or drop the module's read-only "
            "declaration (CLAUDE.md → Invariant register, #1777)."
        )
        return 1
    print(f"OK: all {len(READ_ONLY_MODULES)} read-only module(s) call repository reads only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
