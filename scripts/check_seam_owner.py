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

The scan is AST-shaped and covers the two ways a module takes hold of a seam:

* an **attribute** named after the seam's config attribute, read or written
  (``config.active_core``, and any other receiver — the names are distinctive,
  so this cannot be dodged by renaming the holding object), and
* an **annotation** naming the seam's Protocol type (``ActiveCoreReader``,
  ``RendererRssFn``, …) on a dataclass field, a parameter, or a return.

**A composition root may pass a seam on; it may not use one.** The façade takes
all four from ``bootstrap`` and hands each to the sub-service that owns it, so
two shapes are allowed everywhere rather than one file being exempted wholesale:
a seam attribute that *is* a call's keyword-argument value
(``ShortcutBakeInputsConfig(active_core=config.active_core)`` — handed on, not
held) and a seam annotation on a field of :data:`FACADE_CONFIG_CLASS`, the one
class ``bootstrap`` delivers into. A façade method that *reaches through* the
seam (``self._config.active_core.active_emulator_for_rom(...)``) is neither, and
is a finding — which a blanket file exemption could not tell from wiring.

It is a surface-syntax guardrail, not dataflow analysis. What it cannot see:

* a seam aliased to a differently-named attribute or local, or reached through
  ``getattr``;
* a seam passed positionally into a helper that then holds it;
* a quoted (string) annotation, which is a ``Constant`` and never a type name;
* a Protocol imported under an alias
  (``from services.protocols import ActiveCoreReader as CoreReader``), which
  leaves the annotation leaf unmatched — the aliasing this catches is of the
  *value*, not of the *type*;
* the field **name** in a foreign config, which is never inspected — so
  ``active_core: CoreReader`` combines the previous two and is invisible on both
  halves of the scan.

The last two are precision in the documented reach rather than a way through:
whatever the field is called and however its type was imported, the constructor
that unpacks it reads ``config.<seam>`` outside a keyword argument, and that
read is flagged. What actually stays on review is a seam reached without ever
naming it.

Exit 0 when every seam is held only by its owner, 1 (one line per offending
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
    "active_core": frozenset({"bake_inputs.py"}),
    "disc_resolver": frozenset({"bake_inputs.py"}),
    "renderer_rss": frozenset({"session_budget.py"}),
    "renderer_gc": frozenset({"session_budget.py"}),
}

# Protocol type name -> the seam it types, so an annotation is attributed to
# the same owner set as the attribute.
SEAM_PROTOCOLS: dict[str, str] = {
    "ActiveCoreReader": "active_core",
    "DiscResolver": "disc_resolver",
    "RendererRssFn": "renderer_rss",
    "RendererGcFn": "renderer_gc",
}

# The one config class ``bootstrap`` delivers a seam into. A seam annotation on
# one of ITS fields is the package's entry point for that seam, not a second
# holder of it.
FACADE_CONFIG_CLASS = "LibraryServiceConfig"


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


def _passed_on_nodes(tree: ast.AST) -> set[int]:
    """Node ids a composition root is allowed to name: wiring, not use.

    Two shapes, collected by node identity so nothing else inherits the
    exemption: an attribute standing as a call's keyword-argument value (the
    seam handed to a sub-service's config), and an annotation on a field of
    :data:`FACADE_CONFIG_CLASS` (the seam arriving from ``bootstrap``).
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            allowed.update(id(kw.value) for kw in node.keywords if isinstance(kw.value, ast.Attribute))
        elif isinstance(node, ast.ClassDef) and node.name == FACADE_CONFIG_CLASS:
            allowed.update(
                id(inner)
                for field in node.body
                if isinstance(field, ast.AnnAssign)
                for inner in ast.walk(field.annotation)
            )
    return allowed


def find_violations(files: list[Path] | None = None) -> list[str]:
    """Return one human-readable line per seam held outside its owner module."""
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
        passed_on = _passed_on_nodes(tree)
        for node in ast.walk(tree):
            findings.extend(_node_findings(node, module=module, rel=rel, passed_on=passed_on))
    return sorted(findings)


def _node_findings(node: ast.AST, *, module: str, rel: Path, passed_on: set[int]) -> list[str]:
    """Seam violations introduced by a single AST node."""
    findings: list[str] = []
    if isinstance(node, ast.Attribute) and node.attr in SEAM_OWNERS and id(node) not in passed_on:
        findings.extend(_report(node.attr, node, module=module, rel=rel, held_as=f"holds '....{node.attr}'"))
    for annotation in _annotation_names(node):
        for inner in ast.walk(annotation):
            if id(inner) in passed_on:
                continue
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
