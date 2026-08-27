#!/usr/bin/env python3
"""Seam-owner confinement gate for the library sync package.

``py_modules/services/library/`` is a decomposition: each module owns one job,
and a job's injected seams are what identify it. When a seam leaks into a second
module the decomposition quietly reverses — the module that "does not do that
any more" grows a second reason to open the same resource, and the boundary the
split was for stops being checkable by reading one file.

Two confinements are enforced here, each recording a promise a module docstring
already makes:

* ``active_core`` / ``disc_resolver`` belong to ``bake_inputs.py``. They are the
  only reason the sync ever resolves a ROM's emulator or its disc-pinned launch
  path, and holding them in one module is what keeps the per-ROM UoW the
  ``active_core`` seam opens out of the read UoW the install-path scan holds
  (:mod:`services.library.bake_inputs`).
* ``renderer_rss`` / ``renderer_gc`` belong to ``session_budget.py``. Its
  contract is that no renderer-RSS **reading** is taken anywhere else in the
  package — every other budget site prices against a reading that module handed
  it (:mod:`services.library.session_budget`).

``service.py`` co-owns every seam: it is the façade that receives them from
``bootstrap`` and hands each to the sub-service that owns it. Passing a seam
through the composition root is not holding it.

The scan is AST-shaped and covers the two ways a module takes hold of a seam:

* an **attribute** named after the seam's config attribute, read or written
  (``config.active_core``, and any other receiver — the names are distinctive,
  so this cannot be dodged by renaming the holding object), and
* an **annotation** naming the seam's Protocol type (``ActiveCoreReader``,
  ``RendererRssFn``, …) on a dataclass field, a parameter, or a return.

It is a surface-syntax guardrail, not dataflow analysis. What it cannot see:
a seam aliased to a differently-named attribute or local, one reached through
``getattr``, one passed positionally into a helper that holds it, and a
quoted (string) annotation. Those stay on review.

Exit 0 when every seam is held only by its owners, 1 (one line per offending
site) otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = REPO_ROOT / "py_modules" / "services" / "library"

# --- The seam table ------------------------------------------------------
# Seam config-attribute name -> the modules (relative to LIBRARY_DIR) allowed
# to hold it. A new confinement is a one-line addition here plus its Protocol
# type in SEAM_PROTOCOLS below.
SEAM_OWNERS: dict[str, frozenset[str]] = {
    "active_core": frozenset({"service.py", "bake_inputs.py"}),
    "disc_resolver": frozenset({"service.py", "bake_inputs.py"}),
    "renderer_rss": frozenset({"service.py", "session_budget.py"}),
    "renderer_gc": frozenset({"service.py", "session_budget.py"}),
}

# Protocol type name -> the seam it types, so an annotation is attributed to
# the same owner set as the attribute.
SEAM_PROTOCOLS: dict[str, str] = {
    "ActiveCoreReader": "active_core",
    "DiscResolver": "disc_resolver",
    "RendererRssFn": "renderer_rss",
    "RendererGcFn": "renderer_gc",
}


def _iter_scanned_files(library_dir: Path) -> list[Path]:
    """Every ``.py`` under the library package, owners included.

    Ownership is decided per seam, not per file, so no file is skipped up
    front — ``session_budget.py`` owning the renderer seams says nothing about
    its right to hold ``disc_resolver``.
    """
    return sorted(library_dir.rglob("*.py"))


def _annotation_names(node: ast.AST) -> list[ast.expr]:
    """The annotation expressions a node introduces (empty for everything else)."""
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    if isinstance(node, ast.arg):
        return [node.annotation] if node.annotation is not None else []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return [node.returns] if node.returns is not None else []
    return []


def _annotation_leaf(annotation: ast.expr) -> str | None:
    """The bare type name an annotation ends in (``x.Y`` -> ``Y``), or ``None``.

    Subscripted annotations (``X | None`` is a ``BinOp``, ``list[X]`` a
    ``Subscript``) are reached by the caller's ``ast.walk``, which visits the
    inner ``Name`` / ``Attribute`` nodes in their own right.
    """
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


def find_violations(files: list[Path] | None = None) -> list[str]:
    """Return one human-readable line per seam held outside its owner modules."""
    if files is None:
        files = _iter_scanned_files(LIBRARY_DIR)
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
        module = path.relative_to(LIBRARY_DIR).as_posix()
        rel = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            findings.extend(_node_findings(node, module=module, rel=rel))
    return sorted(findings)


def _node_findings(node: ast.AST, *, module: str, rel: Path) -> list[str]:
    """Seam violations introduced by a single AST node."""
    findings: list[str] = []
    if isinstance(node, ast.Attribute) and node.attr in SEAM_OWNERS:
        findings.extend(_report(node.attr, node, module=module, rel=rel, held_as=f"holds '....{node.attr}'"))
    for annotation in _annotation_names(node):
        for inner in ast.walk(annotation):
            leaf = _annotation_leaf(inner) if isinstance(inner, ast.Name | ast.Attribute) else None
            seam = SEAM_PROTOCOLS.get(leaf) if leaf is not None else None
            if seam is not None:
                findings.extend(_report(seam, inner, module=module, rel=rel, held_as=f"annotates '{leaf}'"))
    return findings


def _report(seam: str, node: ast.expr, *, module: str, rel: Path, held_as: str) -> list[str]:
    """One finding line, or none when *module* is allowed to hold *seam*."""
    owners = SEAM_OWNERS[seam]
    if module in owners:
        return []
    owner_list = " / ".join(sorted(owners))
    return [f"{rel}:{node.lineno}:{node.col_offset} {held_as} — the '{seam}' seam is held only by {owner_list}."]


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
            "ERROR: a library-sync seam is held outside its owner module — reach it through the "
            "sub-service that owns it instead of injecting it a second time "
            "(CLAUDE.md → Invariant register, #1777)."
        )
        return 1
    print(f"OK: all {len(SEAM_OWNERS)} confined seams are held only by their owners (services/library/).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
