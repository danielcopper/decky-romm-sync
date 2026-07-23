#!/usr/bin/env python3
"""404-vs-unreachable gate — exception-type-blind classification enforcement.

RomM answers a request for an entity it no longer has with HTTP 404. That is
the server *answering*, not the server being unreachable, so it must reach the
frontend as ``not_found`` — never as ``server_unreachable``. The plugin already
owns the funnel that decides this (:func:`lib.errors.classify_error`, which maps
``RommNotFoundError`` to :data:`ErrorCode.NOT_FOUND`); the recurring defect is a
catch-all ``except Exception`` that ignores the funnel and hardcodes the
unreachable verdict for every exception it sees. The whole UI then claims "RomM
offline" while the server is plainly answering.

This class of bug was swept once before (#971) and came back, because
``check_failure_shape.py`` inspects key *presence* only — a hardcoded
``server_unreachable`` is a perfectly canonical failure shape, so every one of
these sites is green there. This check inspects the slug *choice* instead.

The rule
========

Inside ``py_modules/services/``, a **catch-all** exception handler (``except
Exception``, ``except BaseException``, or a bare ``except:``) may not *return* a
hardcoded ``server_unreachable`` verdict. A handler returns one when a dict
literal in its body binds a verdict key — ``reason``, ``status``, or
``recommended_action`` — to ``ErrorCode.SERVER_UNREACHABLE`` or to the bare
``"server_unreachable"`` string. Both spellings count: the canonical failure
shape uses the enum, while the discriminated-status and ``recommended_action``
carve-outs spell the slug out as a literal.

Binding the key to a *name* (``{"reason": reason}`` after ``reason, message =
classify_error(e)``) is the correct shape and is what this check is steering
towards, so it is never flagged. Keying on the verdict's position — rather than
on the mere mention of the slug anywhere in the handler — is deliberate: it
catches the sharpest form of this bug, a handler that calls ``classify_error``
and then **discards** its verdict in favour of the hardcoded one, which a
"does the handler call the funnel?" test would wave through.

One escape clears an otherwise-flagged handler:

* **A sibling handler peels the 404 off first** — the same ``try`` carries an
  ``except RommNotFoundError`` clause, so the definitive-404 case never reaches
  the catch-all. This is the shape the partial-success carve-outs use, where the
  verdict is a flag rather than a ``reason`` slug and there is no funnel to call.

``EXEMPT`` holds service modules deliberately kept out of the scan — a module
that does not talk to RomM at all, so ``classify_error`` (a RomM-shaped funnel)
has nothing to say about its failures. Adding to it is a deliberate act that
shows up in review.

Two modes:

  * report (default) — print every catch-all handler carrying an unreachable
    verdict, grouped by verdict, then exit 0. Report mode never fails; it is the
    inventory.
  * ``--check`` — enforce mode. Exit 1 on any violation.

A **typed** handler is never flagged. ``except RommConnectionError`` or
``except SaveSyncTimeoutError`` returning ``server_unreachable`` is a deliberate
statement about a known exception type, which is exactly what this check wants
more of.

The AST heuristic is intentionally conservative, and a guardrail rather than a
prover: it reads one ``try`` statement at a time and does not follow a verdict
returned by a helper the handler calls, nor one assembled across statements
(``resp = {...}; resp["reason"] = ...``). A ``try`` nested *inside* a handler is
scanned as its own statement, with its own sibling-peel check, and its dict
literals are NOT attributed to the enclosing handler — otherwise a nested peel
would read as an unpeeled verdict on the outer one and false-positive.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "py_modules" / "services"

# The canonical slug, in both spellings a service can write it.
UNREACHABLE_ENUM_MEMBER = "SERVER_UNREACHABLE"
UNREACHABLE_SLUG = "server_unreachable"

# Dict keys whose value is the verdict a consumer routes on: the canonical
# failure shape's ``reason``, plus the two documented carve-out discriminants.
VERDICT_KEYS = frozenset({"reason", "status", "recommended_action"})

# The exception whose presence as a sibling clause proves the 404 was peeled off.
NOT_FOUND_EXCEPTION = "RommNotFoundError"

# Catch-all handler types (``except:`` with no type is handled separately).
CATCH_ALL_NAMES = frozenset({"Exception", "BaseException"})

# Service modules deliberately out of scope, as ``<path>: <why>``. Only for
# modules that never talk to RomM — classify_error cannot classify their errors.
EXEMPT: dict[str, str] = {
    "steamgrid.py": (
        "talks to SteamGridDB, not RomM — SGDB failures arrive as SgdbApiError "
        "(carrying its own status_code) and classify_error has no SGDB branch"
    ),
}


@dataclass(frozen=True)
class Finding:
    """One catch-all handler that hardcodes the unreachable verdict."""

    path: Path
    lineno: int
    spelling: str
    function: str

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))

    def render(self) -> str:
        return f"{self.rel}:{self.lineno}  in {self.function}()  [{self.spelling}]"


def _handler_is_catch_all(handler: ast.ExceptHandler) -> bool:
    """True when *handler* catches everything (bare, Exception, BaseException).

    A tuple clause counts when any member is a catch-all name: ``except
    (Exception, X)`` still swallows every exception.
    """
    if handler.type is None:  # bare ``except:``
        return True
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(node, ast.Name) and node.id in CATCH_ALL_NAMES for node in candidates)


def _handler_catches_not_found(handler: ast.ExceptHandler) -> bool:
    """True when *handler* explicitly catches ``RommNotFoundError``."""
    if handler.type is None:
        return False
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(node, ast.Name) and node.id == NOT_FOUND_EXCEPTION for node in candidates)


def _hardcoded_verdict_spelling(value: ast.expr) -> str | None:
    """Return how *value* spells a hardcoded unreachable verdict, else None.

    ``ErrorCode.SERVER_UNREACHABLE`` and ``ErrorCode.SERVER_UNREACHABLE.value``
    both surface as an ``Attribute`` named ``SERVER_UNREACHABLE``; the carve-out
    shapes write the bare ``"server_unreachable"`` string instead. A ``Name``
    (the classified slug held in a variable) is neither, and returns None.
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Attribute) and node.attr == UNREACHABLE_ENUM_MEMBER:
            return f"ErrorCode.{UNREACHABLE_ENUM_MEMBER}"
        if isinstance(node, ast.Constant) and node.value == UNREACHABLE_SLUG:
            return f'"{UNREACHABLE_SLUG}"'
    return None


def _walk_outside_nested_try(node: ast.AST) -> Iterator[ast.AST]:
    """Yield *node*'s descendants, never descending into a nested ``ast.Try``.

    The caller's top-level walk visits every ``ast.Try`` in the module, so a
    nested one is scanned on its own terms — with its own sibling-peel check.
    Attributing its dict literals to the enclosing handler as well would make a
    nested ``except RommNotFoundError`` peel look like an unpeeled verdict on
    the outer handler, which is a false positive.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Try):
            continue
        yield child
        yield from _walk_outside_nested_try(child)


def _unreachable_spelling(handler: ast.ExceptHandler) -> str | None:
    """Return how *handler* hardcodes the unreachable verdict, or None.

    Only a verdict *key* counts (see :data:`VERDICT_KEYS`) — mentioning the slug
    anywhere else in the handler (a log line, a comparison against a classified
    reason) is not a hardcoded verdict.
    """
    for node in _walk_outside_nested_try(handler):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value in VERDICT_KEYS):
                continue
            spelling = _hardcoded_verdict_spelling(value)
            if spelling is not None:
                return spelling
    return None


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map every node's line to the name of the function that contains it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            lineno = getattr(child, "lineno", None)
            if lineno is not None:
                # Inner functions win: they are visited after the outer one only
                # when nested, so record the most specific (shortest) span.
                owner[lineno] = node.name
    return owner


def scan_source(path: Path, source: str) -> list[Finding]:
    """Return every catch-all handler in *source* that hardcodes the verdict."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    function_of = _enclosing_functions(tree)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        peels_not_found = any(_handler_catches_not_found(h) for h in node.handlers)
        if peels_not_found:
            continue
        for handler in node.handlers:
            if not _handler_is_catch_all(handler):
                continue
            spelling = _unreachable_spelling(handler)
            if spelling is None:
                continue
            findings.append(
                Finding(
                    path=path,
                    lineno=handler.lineno,
                    spelling=spelling,
                    function=function_of.get(handler.lineno, "<module>"),
                )
            )
    return findings


def scan_file(path: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return scan_source(path, source)


def collect_findings(services_dir: Path = SERVICES_DIR) -> list[Finding]:
    """Walk *services_dir* and return every hardcoded-verdict finding."""
    findings: list[Finding] = []
    if not services_dir.is_dir():
        return findings
    for path in sorted(services_dir.rglob("*.py")):
        if str(path.relative_to(services_dir)) in EXEMPT:
            continue
        findings.extend(scan_file(path))
    return findings


def _print_report(findings: list[Finding]) -> None:
    print(f"=== CATCH-ALL HANDLERS HARDCODING server_unreachable ({len(findings)}) ===")
    for finding in findings:
        print(f"  {finding.render()}")
    print()
    if EXEMPT:
        print("=== EXEMPT MODULES ===")
        for name, why in sorted(EXEMPT.items()):
            print(f"  {name} — {why}")
        print()
    print("=== SUMMARY ===")
    print(f"  TOTAL: {len(findings)}")


def _print_violations(findings: list[Finding]) -> None:
    print(f"=== CATCH-ALL HANDLERS HARDCODING server_unreachable ({len(findings)}) ===")
    for finding in findings:
        print(f"  {finding.render()}")
    print()
    print(
        "ERROR: a catch-all 'except Exception' in py_modules/services/ must not hardcode "
        "ErrorCode.SERVER_UNREACHABLE — a definitive 404 is the server ANSWERING, and must "
        "reach the frontend as 'not_found'. Route the exception through classify_error() "
        "(lib/errors.py), or peel the 404 off with a sibling 'except RommNotFoundError' "
        "clause when the verdict is a partial-success flag rather than a reason slug "
        "(CLAUDE.md → invariant register)."
    )


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0

    enforce = "--check" in argv
    findings = collect_findings(SERVICES_DIR)

    if enforce:
        if findings:
            _print_violations(findings)
            return 1
        print(f"OK: no catch-all handler hardcodes server_unreachable in {SERVICES_DIR.relative_to(REPO_ROOT)}.")
        return 0

    _print_report(findings)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
