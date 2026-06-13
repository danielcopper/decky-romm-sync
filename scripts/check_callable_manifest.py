#!/usr/bin/env python3
"""Frontend↔backend callable-manifest parity gate.

The Decky callable surface is declared twice: once on the frontend as
``callable<[Args], Return>("wire_name")`` (TypeScript) and once on the backend
as a public ``async def`` method on the ``Plugin`` class in ``main.py``. Nothing
ties the two together at build time — a renamed/added/removed callable on either
side, or an arg-count change that the other side doesn't follow, only surfaces as
a runtime "method not found" / wrong-arity failure once the plugin is loaded.

This check derives both surfaces from source and fails when they diverge, so the
wire stays one source of truth. It is the static sibling of the
``tests/contract/`` tier: the contract tests drive the *real* callables
frontend-shaped; this gate pins that the two *declarations* agree before any
callable is driven.

What it guarantees (and what it deliberately does not):

  * Every frontend ``callable("name")`` has a matching backend ``async def name``
    and vice versa — no orphan on either side.
  * Where a name exists on both sides, the **arity** (positional parameter count,
    ``self`` dropped on the backend) matches. Python method signatures carry no
    type hints, so arity is the only mechanically checkable shape — arg TYPES are
    out of scope (the contract tier exercises those by driving real values).
  * A backend method that takes ``*args`` has a variable arity; its name is still
    checked, but the arity comparison is skipped for it.

``EXEMPT`` holds wire names deliberately kept out of the parity check. It is
empty today; an entry here is a conscious "this name is intentionally declared on
only one side" decision — never a lever to silence a real drift. A genuine drift
is a finding to triage, not to exempt.

Exit 0 when the two surfaces match, 1 (one line per discrepancy) otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
MAIN_PY = REPO_ROOT / "main.py"

# Wire names deliberately excluded from the parity check. Empty today; add an
# entry only as a conscious decision (see the module docstring). Do NOT add a
# name here to silence a real drift — a drift is a finding to triage.
EXEMPT: frozenset[str] = frozenset()

# Brackets that open/close a balance-tracked region inside a ``callable<...>``.
_OPENERS = {"<": ">", "[": "]", "{": "}", "(": ")"}
_CLOSERS = frozenset(_OPENERS.values())
_QUOTES = frozenset({'"', "'", "`"})


def _skip_string(text: str, start: int) -> int:
    """Return the index just past the string/template literal opening at *start*.

    *start* must index a quote character. Handles backslash escapes; a template
    literal's ``${...}`` interpolation is treated as opaque body text (its inner
    brackets never reach the depth tracker because the whole literal is skipped).
    """
    quote = text[start]
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def _strip_comments(text: str) -> str:
    """Return *text* with TS ``//`` and ``/* */`` comments blanked out.

    Comment spans are replaced with a single space (preserving token separation
    and source length is unnecessary — the parser is whitespace-tolerant), so a
    comment containing an apostrophe, a ``<``/``>``, an unbalanced bracket, or a
    whole commented-out ``callable<...>("dead")`` declaration cannot corrupt the
    downstream bracket-depth tracking or be mistaken for a live callable.

    String and template literals are preserved verbatim — a ``//`` or ``/*``
    inside ``"http://x"`` or `` `a/*b` `` is part of the string, not a comment.
    Scans char by char so a comment-opener inside a literal is never honoured.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTES:
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                end = text.find("\n", i + 2)
                # Keep the newline so line structure (and any line comment a
                # later scan relies on) survives; blank only the comment body.
                if end == -1:
                    out.append(" ")
                    return "".join(out)
                out.append(" \n")
                i = end + 1
                continue
            if nxt == "*":
                end = text.find("*/", i + 2)
                out.append(" ")
                i = len(text) if end == -1 else end + 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _is_arrow(text: str, i: int) -> bool:
    """Return True when *i* indexes the ``=`` of a ``=>`` arrow token.

    The ``>`` in ``=>`` (arrow-function return type, e.g. ``(a: number) => void``)
    is not a closing angle bracket and must not decrement bracket depth. The
    scanners treat ``=>`` as an opaque two-char token so the ``>`` never reaches
    the depth/closer logic.
    """
    return text[i] == "=" and i + 1 < len(text) and text[i + 1] == ">"


def _capture_generic(text: str, lt_index: int) -> tuple[str, int] | None:
    """Capture the balanced ``<...>`` body starting at the ``<`` at *lt_index*.

    Returns ``(inner, end)`` where *inner* is the text between the outer angle
    brackets and *end* indexes just past the closing ``>``. String/template
    literals are skipped so brackets/commas inside them never affect the depth
    count, and a ``=>`` arrow's ``>`` does not close a generic. (Comments are
    already removed upstream by :func:`_strip_comments`.) Returns None if the
    angle brackets never balance (truncated source).
    """
    depth = 0
    i = lt_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTES:
            i = _skip_string(text, i)
            continue
        if _is_arrow(text, i):
            i += 2
            continue
        if ch in _OPENERS:
            depth += 1
            i += 1
            continue
        if ch in _CLOSERS:
            depth -= 1
            if depth == 0 and ch == ">":
                return text[lt_index + 1 : i], i + 1
            i += 1
            continue
        i += 1
    return None


def _split_top_level(text: str) -> list[str]:
    """Split *text* on TOP-LEVEL commas, respecting nesting and string literals.

    Nested generics (``Record<string, number>``), tuples, objects, string
    literals and ``=>`` arrows do not split — only commas at bracket depth 0 do.
    (Comments are already removed upstream by :func:`_strip_comments`.) A wholly
    empty *text* yields ``[]``; a single trailing comma (``number, string,``)
    drops the empty final fragment so it does not inflate the element count.
    """
    if not text.strip():
        return []
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTES:
            i = _skip_string(text, i)
            continue
        if _is_arrow(text, i):
            i += 2
            continue
        if ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    # Drop a single trailing empty fragment from a trailing comma (``a, b,``);
    # an all-empty *text* already returned [] above, so this only trims the
    # spurious final element, never a legitimate one.
    if len(parts) > 1 and not parts[-1].strip():
        parts.pop()
    return parts


def _args_arity(args_fragment: str) -> int:
    """Return the arity of the leading ``[...]`` args tuple of a callable generic.

    *args_fragment* is the first top-level element of the ``callable<...>``
    generic — a tuple type like ``[number, string]``. Arity is the count of
    top-level comma-separated elements inside the brackets (``[]`` -> 0). Nested
    generics, unions and object literals inside an element do not inflate it.
    """
    inner = args_fragment.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return len(_split_top_level(inner))


def _trailing_call_name(text: str, after: int) -> str | None:
    """Return the string literal in the ``("name")`` immediately following *after*.

    *after* indexes just past the closing ``>`` of the ``callable<...>`` generic.
    Skips whitespace, expects ``(`` then a quoted string literal.
    """
    i = after
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i >= n or text[i] != "(":
        return None
    i += 1
    while i < n and text[i].isspace():
        i += 1
    if i >= n or text[i] not in _QUOTES:
        return None
    quote = text[i]
    end = _skip_string(text, i)
    # end indexes just past the closing quote; the literal body excludes quotes.
    body = text[i + 1 : end - 1]
    return body if quote != "`" or "${" not in body else None


def parse_frontend_callables(src_dir: Path) -> dict[str, int]:
    """Parse every ``callable<[Args], Return>("name")`` under *src_dir*.

    Scans all ``.ts``/``.tsx`` files as text (declarations may span multiple
    lines). Comments are stripped first (:func:`_strip_comments`) so a comment
    inside a ``callable<...>`` block can't corrupt the bracket tracker and a
    commented-out declaration isn't mistaken for a live one. Returns
    ``{wire_name: arity}`` where arity is the number of top-level elements in the
    ``[Args]`` tuple. A wire name declared more than once maps to a sentinel
    arity of ``-1`` so the diff surfaces the duplicate.
    """
    result: dict[str, int] = {}
    if not src_dir.is_dir():
        return result
    files = sorted(p for p in src_dir.rglob("*") if p.suffix in (".ts", ".tsx") and p.is_file())
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _parse_text_into(_strip_comments(text), result)
    return result


def _parse_text_into(text: str, result: dict[str, int]) -> None:
    """Find every ``callable<...>("name")`` in *text* and record name -> arity."""
    needle = "callable"
    i = 0
    n = len(text)
    while True:
        idx = text.find(needle, i)
        if idx == -1:
            return
        i = idx + len(needle)
        # The ``<`` of the generic must follow ``callable`` (allowing whitespace).
        j = i
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] != "<":
            continue
        captured = _capture_generic(text, j)
        if captured is None:
            continue
        inner, end = captured
        elements = _split_top_level(inner)
        if not elements:
            continue
        name = _trailing_call_name(text, end)
        if name is None:
            continue
        arity = _args_arity(elements[0])
        # A second declaration of the same name is itself a finding.
        result[name] = -1 if name in result else arity
        i = end


def parse_backend_callables(main_py: Path) -> dict[str, int]:
    """Parse the public ``async def`` methods of the ``Plugin`` class in *main_py*.

    Uses ``ast`` (never imports ``main.py`` — it needs a decky runtime). Returns
    ``{method_name: arity}`` for every ``AsyncFunctionDef`` on ``Plugin`` whose
    name does not start with ``_`` (those are internal lifecycle, not callables).
    Arity counts positional parameters only — ``posonlyargs`` + ``args`` minus
    ``self``; a method with ``*args`` has variable arity, recorded as ``None``
    (name still checked). Keyword-only args (``*, c``) and ``**kwargs`` are NOT
    part of positional arity: the frontend's positional ``[Args]`` tuple can't
    fill them, so they're excluded by design (a non-occurring corner on this
    callable surface).
    """
    result: dict[str, int | None] = {}
    source = main_py.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(main_py))
    plugin = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Plugin"),
        None,
    )
    if plugin is None:
        return {}
    for node in plugin.body:
        if not isinstance(node, ast.AsyncFunctionDef) or node.name.startswith("_"):
            continue
        args = node.args
        if args.vararg is not None:
            result[node.name] = None
            continue
        # posonly + regular positional params, minus ``self``. Defaults still
        # occupy a positional slot the frontend fills (e.g. ``null``).
        result[node.name] = len(args.posonlyargs) + len(args.args) - 1
    return result


def find_discrepancies(
    frontend: dict[str, int],
    backend: dict[str, int | None],
    exempt: frozenset[str],
) -> list[str]:
    """Diff the two callable surfaces. One human-readable line per discrepancy.

    Reports: (a) a name on the frontend but not the backend, (b) a name on the
    backend but not the frontend, (c) a name on both whose arity differs, and
    (d) a frontend name declared twice (arity sentinel ``-1``). ``exempt`` names
    are skipped for the name-presence checks (a)/(b).
    """
    findings: list[str] = [
        f"{name}: declared more than once on the frontend "
        f"(src/**/*.ts callable<...>) — remove the duplicate declaration."
        for name in sorted(frontend)
        if frontend[name] == -1
    ]

    frontend_names = set(frontend) - exempt
    backend_names = set(backend) - exempt

    findings.extend(
        f'{name}: frontend declares callable("{name}") but main.py has no public '
        f"async def {name} on Plugin — add the backend method, fix a rename, or "
        f"add it to EXEMPT in this script if it is intentionally frontend-only."
        for name in sorted(frontend_names - backend_names)
    )
    findings.extend(
        f"{name}: main.py exposes public async def {name} on Plugin but no frontend "
        f'callable("{name}") declares it — add the frontend declaration, fix a '
        f"rename, or add it to EXEMPT in this script if it is intentionally backend-only."
        for name in sorted(backend_names - frontend_names)
    )

    for name in sorted(frontend_names & backend_names):
        fe_arity = frontend[name]
        be_arity = backend[name]
        if fe_arity == -1 or be_arity is None:
            # Duplicate frontend name already flagged above; ``*args`` backend
            # method has variable arity — name checked, arity skipped.
            continue
        if fe_arity != be_arity:
            findings.append(
                f"{name}: arity mismatch — frontend declares {fe_arity} arg(s) "
                f"(callable<[...]>) but main.py async def {name} takes {be_arity} "
                f"(self dropped). Align the argument count on one side."
            )

    return sorted(findings)


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    frontend = parse_frontend_callables(SRC_DIR)
    backend = parse_backend_callables(MAIN_PY)
    findings = find_discrepancies(frontend, backend, EXEMPT)
    if findings:
        for line in findings:
            print(line)
        print()
        print(
            "ERROR: the frontend (src/**/*.ts callable declarations) and backend "
            "(Plugin async methods in main.py) callable surfaces have drifted. Every "
            "callable must be declared on both sides with matching arity (or be "
            "explicitly EXEMPT) so the frontend↔backend wire stays one source of truth."
        )
        return 1
    matched = len(set(frontend) & set(backend))
    print(f"OK: frontend↔backend callable manifest matches ({matched} callables).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
