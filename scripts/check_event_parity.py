#!/usr/bin/env python3
"""Backend↔frontend emit-event parity gate.

The realtime event channel is declared twice: the backend emits a named event
via ``self._emit("name", payload)`` (Python) and the frontend subscribes to it
via ``addEventListener<[Payload]>("name", handler)`` (TypeScript). Nothing ties
the two together at build time — a renamed/added/removed event on either side
only surfaces at runtime as an event that fires into the void (emit with no
listener) or a listener that never wakes (listener with no emitter).

This check derives both surfaces from source and fails when they diverge, so the
event channel stays one source of truth. It is the static sibling of
``scripts/check_callable_manifest.py``: the callable gate pins the request/reply
surface, this gate pins the fire-and-forget event surface.

What it guarantees (and what it deliberately does not):

  * Every backend ``emit("name", ...)`` has a matching frontend
    ``addEventListener("name", ...)`` and vice versa — no orphan on either side.
  * Only **literal** event names are checked. A dynamic/variable event name
    (``self._emit(some_var, ...)`` or ``addEventListener(name, ...)``) can't be
    matched statically and is skipped — same limitation the callable gate has
    with literal wire names.
  * Only the bare ``@decky/api`` ``addEventListener(...)`` counts on the
    frontend. ``globalThis.addEventListener`` / ``el.addEventListener`` are DOM
    ``CustomEvent`` subscriptions, not backend emits, and are excluded.

``EXEMPT`` holds event names deliberately kept out of the parity check. It is
empty today; an entry here is a conscious "this event is intentionally declared
on only one side" decision — never a lever to silence a real drift. A genuine
drift is a finding to triage, not to exempt.

Exit 0 when the two surfaces match, 1 (one line per discrepancy) otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_MODULES_DIR = REPO_ROOT / "py_modules"
VENDOR_DIR = PY_MODULES_DIR / "_vendor"
SRC_DIR = REPO_ROOT / "src"
MAIN_PY = REPO_ROOT / "main.py"

# Event names deliberately excluded from the parity check. Empty today; add an
# entry only as a conscious decision (see the module docstring). Do NOT add a
# name here to silence a real drift — a drift is a finding to triage.
EXEMPT: frozenset[str] = frozenset()

# Attribute names that name an emit call (``self.emit`` / ``self._emit``).
_EMIT_ATTRS = frozenset({"emit", "_emit"})

# Mirrors the TS-source helpers in check_callable_manifest.py (kept local:
# scripts/ is not importable from the importlib-loaded contract tests).
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
    whole commented-out ``addEventListener("dead", ...)`` call cannot corrupt the
    downstream scanning or be mistaken for a live listener.

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


def _skip_generic(text: str, lt_index: int) -> int | None:
    """Return the index just past the balanced ``<...>`` opening at *lt_index*.

    *lt_index* must index a ``<``. Depth-counts ``<``/``>`` so a nested generic
    (``addEventListener<[{ a: number }]>``) balances correctly; string literals
    inside are skipped so brackets/quotes within them never affect the count.
    Returns None if the angle brackets never balance (truncated source).
    """
    depth = 0
    i = lt_index
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _QUOTES:
            i = _skip_string(text, i)
            continue
        if ch == "<":
            depth += 1
            i += 1
            continue
        if ch == ">":
            depth -= 1
            i += 1
            if depth == 0:
                return i
            continue
        i += 1
    return None


def _is_word_char(ch: str) -> bool:
    """Return True for a JS identifier char ([A-Za-z0-9_$])."""
    return ch.isalnum() or ch in {"_", "$"}


def _listener_name(text: str, after: int) -> str | None:
    """Return the event name of the ``addEventListener(...)`` call after *after*.

    *after* indexes just past the ``addEventListener`` needle. Skips whitespace;
    if a ``<`` follows, skips the balanced generic; then expects ``(``, optional
    whitespace, and a quoted string literal — that literal is the event name. A
    template literal with a ``${...}`` interpolation is not a static name and is
    skipped (returns None).
    """
    i = after
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i < n and text[i] == "<":
        skipped = _skip_generic(text, i)
        if skipped is None:
            return None
        i = skipped
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


def parse_frontend_listeners(src_dir: Path) -> set[str]:
    """Parse every bare ``addEventListener("name", ...)`` under *src_dir*.

    Scans all ``.ts``/``.tsx`` files as text (calls may span multiple lines),
    EXCLUDING test files (``*.test.*``), ``src/test-utils/`` and
    ``src/test-setup.ts``. Comments are stripped first so a commented-out
    listener isn't mistaken for a live one. Only the bare ``addEventListener``
    needle counts: the char immediately before it must be neither ``.`` (rejects
    ``globalThis.addEventListener`` / ``el.addEventListener`` — DOM CustomEvents,
    not backend emits) nor an identifier char. Returns the set of literal event
    names.
    """
    result: set[str] = set()
    if not src_dir.is_dir():
        return result
    test_utils = src_dir / "test-utils"
    test_setup = src_dir / "test-setup.ts"
    files = sorted(p for p in src_dir.rglob("*") if p.suffix in (".ts", ".tsx") and p.is_file())
    for path in files:
        if ".test." in path.name or test_utils in path.parents or path == test_setup:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _parse_text_into(_strip_comments(text), result)
    return result


def _parse_text_into(text: str, result: set[str]) -> None:
    """Find every bare ``addEventListener("name", ...)`` in *text* and record it."""
    needle = "addEventListener"
    i = 0
    while True:
        idx = text.find(needle, i)
        if idx == -1:
            return
        i = idx + len(needle)
        # The char immediately before the needle must be neither ``.`` nor an
        # identifier char — that rejects ``globalThis.addEventListener`` and any
        # ``fooAddEventListener``-style member, keeping only the bare import.
        if idx > 0:
            prev = text[idx - 1]
            if prev == "." or _is_word_char(prev):
                continue
        name = _listener_name(text, i)
        if name is not None:
            result.add(name)


def _iter_backend_files() -> list[Path]:
    """Every backend ``.py`` to scan: all of py_modules/ minus _vendor/, plus main.py."""
    files = sorted(p for p in PY_MODULES_DIR.rglob("*.py") if VENDOR_DIR not in p.parents and p != VENDOR_DIR)
    if MAIN_PY.is_file():
        files.append(MAIN_PY)
    return files


def parse_backend_emits(files: list[Path] | None = None) -> set[str]:
    """Parse every literal ``self.emit("name", ...)`` / ``self._emit("name", ...)``.

    Uses ``ast`` (emits span multiple lines, so a regex would be brittle). Walks
    every ``ast.Call`` whose ``func`` is an ``ast.Attribute`` with ``.attr`` in
    ``{"emit", "_emit"}`` and a first positional arg that is a string constant —
    that constant is the event name. Dynamic/variable event names can't be
    matched statically and are skipped. Returns the set of literal event names.
    """
    if files is None:
        files = _iter_backend_files()
    result: set[str] = set()
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in _EMIT_ATTRS):
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                result.add(first.value)
    return result


def find_discrepancies(
    backend: set[str],
    frontend: set[str],
    exempt: frozenset[str],
) -> list[str]:
    """Diff the two event surfaces. One human-readable line per discrepancy.

    Reports: (a) an event emitted by the backend with no frontend listener, and
    (b) a frontend listener for an event no backend emits. ``exempt`` names are
    skipped in both directions.
    """
    backend_names = backend - exempt
    frontend_names = frontend - exempt

    findings: list[str] = [
        f'{name}: backend emits "{name}" but no frontend addEventListener("{name}", ...) '
        f"subscribes to it — add the frontend listener, fix a rename, or add it to EXEMPT "
        f"in this script if the event is intentionally backend-only."
        for name in sorted(backend_names - frontend_names)
    ]
    findings.extend(
        f'{name}: frontend addEventListener("{name}", ...) subscribes but no backend '
        f'emit("{name}", ...) fires it — add the backend emit, fix a rename, or add it to '
        f"EXEMPT in this script if the listener is intentionally frontend-only."
        for name in sorted(frontend_names - backend_names)
    )
    return sorted(findings)


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    backend = parse_backend_emits()
    frontend = parse_frontend_listeners(SRC_DIR)
    findings = find_discrepancies(backend, frontend, EXEMPT)
    if findings:
        for line in findings:
            print(line)
        print()
        print(
            "ERROR: the backend (py_modules emit calls) and frontend (src/**/*.ts "
            "addEventListener calls) event surfaces have drifted. Every emitted event "
            "must have a frontend listener and vice versa (or be explicitly EXEMPT) so "
            "the event channel stays one source of truth."
        )
        return 1
    matched = len(backend & frontend)
    print(f"OK: backend↔frontend event parity matches ({matched} events).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
