#!/usr/bin/env python3
"""urlopen choke-point gate for the RomM HTTP transport.

``RommHttpAdapter`` remembers one bit of transport state — whether the RomM
server is known unreachable — and every ladder consults it: while it is set,
each retry ladder runs a single attempt with no backoff. The bit is cleared in
exactly one place, ``RommHttpAdapter._urlopen``, so that a response arriving on
ANY path clears it, the paths that deliberately skip ``with_retry`` included
(the reachability probe and the heartbeat run through ``request_once``).

A request method that reaches ``urllib.request.urlopen`` on its own therefore
never clears the bit. Nothing about that fails: the call succeeds, the tests
pass, and the plugin simply stays in its degraded single-attempt mode until some
other path happens to succeed. That is the regression this gate exists to catch.

It enforces a **structural** rule: inside ``py_modules/adapters/romm/http.py``,
a call to ``urllib.request.urlopen`` may appear only in the body of
``RommHttpAdapter._urlopen``. It is an AST check over call sites, NOT dataflow
analysis: it cannot catch an alias (``opener = urllib.request.urlopen``) or a
call reached through ``getattr``. It catches the regression that actually
happens, which is a new request method copying the ``urlopen(...)`` line from a
sibling.

Other modules are out of scope — the rule is about this adapter's own state, and
another adapter opening a URL says nothing about RomM's reachability.

Exit 0 when the choke point holds, 1 (one line per offending site) otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The module whose reachability state depends on the choke point.
TRANSPORT = REPO_ROOT / "py_modules" / "adapters" / "romm" / "http.py"

# The one method allowed to call urlopen — it clears the known-unreachable bit.
# Exemption is bound to the pair, not to the bare name: a helper called
# ``_urlopen`` nested in some other method would otherwise host its own call and
# exempt it, which is precisely the laundering this gate exists to refuse.
CHOKE_POINT_CLASS = "RommHttpAdapter"
CHOKE_POINT = "_urlopen"

# The dotted call being confined.
URLOPEN = ("urllib", "request", "urlopen")


def _dotted_name(node: ast.expr) -> tuple[str, ...]:
    """Flatten an attribute chain (``urllib.request.urlopen``) into its name parts."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _urlopen_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``urllib.request.urlopen(...)`` call site in *tree*."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _dotted_name(node.func) == URLOPEN]


def _choke_point(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The choke-point method itself — a direct method of the adapter class, or nothing."""
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != CHOKE_POINT_CLASS:
            continue
        for node in cls.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == CHOKE_POINT:
                return node
    return None


def find_violations(path: Path | None = None) -> list[str]:
    """Return one human-readable line per ``urlopen`` call outside the choke point.

    Parses the transport module, collects the call sites inside the choke-point
    method, and reports every other call site with its line number and a fix
    hint.
    """
    path = path or TRANSPORT
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    choke = _choke_point(tree)
    allowed = {id(call) for call in _urlopen_calls(choke)} if choke else set()
    rel = path.relative_to(REPO_ROOT)
    return [
        f"{rel}:{call.lineno}: calls urllib.request.urlopen outside {CHOKE_POINT}() — "
        f"a request path that opens its own connection never clears the known-unreachable "
        f"state, leaving every ladder degraded to a single attempt. Route it through "
        f"self.{CHOKE_POINT}(req, timeout=...) instead."
        for call in _urlopen_calls(tree)
        if id(call) not in allowed
    ]


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
            f"ERROR: urllib.request.urlopen must be called only from "
            f"{CHOKE_POINT_CLASS}.{CHOKE_POINT}(), the single point that clears the "
            f"known-unreachable state. See .claude/rules/romm-http.md."
        )
        return 1
    print(f"OK: urllib.request.urlopen is confined to {CHOKE_POINT_CLASS}.{CHOKE_POINT}().")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
