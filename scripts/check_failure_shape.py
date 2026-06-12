#!/usr/bin/env python3
"""Failure-shape dialect gate — canonical-response enforcement.

Decky callables that return a plain ``dict`` and can fail use the canonical
failure shape ``{"success": False, "reason": ErrorCode | str, "message": str}``
(plus per-callable payload extras). The convention — documented in
``py_modules/lib/list_result.py`` and ``CLAUDE.md`` → "Callable response shapes"
— forbids a second ``error`` field and the legacy ``error_code`` key.

This check walks ``py_modules/services/`` and classifies every failure-shaped
``return`` against a **required-key rule**: a failure shape must carry both
``reason`` and ``message`` and must NOT carry ``error`` or ``error_code``.

Classification of each failure-shaped return (a ``return`` of a dict literal
with a falsy ``success`` entry):

  * CANONICAL — has ``reason`` and ``message``, no ``error`` / ``error_code``.
    (Extra/additive payload keys are allowed.)
  * ERROR_CODE_DIALECT — carries the forbidden ``error_code`` key.
  * ERROR_KEY_DIALECT — carries the forbidden ``error`` key.
  * AD_HOC — ``success: False`` but missing ``reason`` and/or ``message``
    (an extra-keys-only or message-only stray with no routing slug).
  * CARVE_OUT_CANDIDATE — a documented carve-out shape, pattern-exempt and
    never flagged: a discriminated-status union (``status`` discriminant, no
    ``success``) or a partial-success payload (a full data payload alongside
    an additive failure flag from :data:`PARTIAL_SUCCESS_FLAGS`).

Two modes:

  * report (default) — print every site grouped by classification with a count
    summary, then exit 0. Report mode never fails; it is the inventory.
  * ``--check`` — enforce mode. Exit 1 on any ERROR_CODE_DIALECT,
    ERROR_KEY_DIALECT, or AD_HOC finding (the three collapsed dialects).
    CANONICAL and the pattern-exempt CARVE_OUT_CANDIDATE sites pass.

The dict-literal heuristic is intentionally conservative: it only inspects
``return {...}`` literals. A failure dict built across several statements
(``resp = {...}; resp["x"] = y; return resp``) or returned from a helper is not
caught — a guardrail, not a prover. A ``**spread`` or computed key hides the
full key set; such returns are flagged for manual review (AD_HOC) unless they
look like a carve-out, so the heuristic never silently passes an unknown shape.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "py_modules" / "services"

REASON_KEY = "reason"
MESSAGE_KEY = "message"
SUCCESS_KEY = "success"
STATUS_KEY = "status"
ERROR_CODE_KEY = "error_code"
ERROR_KEY = "error"

# Classification labels (also the report group order).
CANONICAL = "CANONICAL"
ERROR_CODE_DIALECT = "ERROR_CODE_DIALECT"
ERROR_KEY_DIALECT = "ERROR_KEY_DIALECT"
CARVE_OUT_CANDIDATE = "CARVE_OUT_CANDIDATE"
AD_HOC = "AD_HOC"

REPORT_ORDER = (
    ERROR_CODE_DIALECT,
    ERROR_KEY_DIALECT,
    AD_HOC,
    CARVE_OUT_CANDIDATE,
    CANONICAL,
)

# Findings in these classifications fail enforce mode (``--check``).
VIOLATION_CLASSES = frozenset({ERROR_CODE_DIALECT, ERROR_KEY_DIALECT, AD_HOC})

# Additive failure-flag keys that mark a partial-success payload (carve-out 2).
# A return carrying one of these alongside a full payload keeps the flag.
PARTIAL_SUCCESS_FLAGS = frozenset({"server_query_failed", "recommended_action"})


@dataclass(frozen=True)
class Finding:
    """One failure-shaped return site and its classification."""

    path: Path
    lineno: int
    classification: str
    keys: tuple[str, ...]
    detail: str

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))

    def render(self) -> str:
        key_set = "{" + ", ".join(self.keys) + "}" if self.keys else "{}"
        suffix = f" — {self.detail}" if self.detail else ""
        return f"{self.rel}:{self.lineno}  {key_set}{suffix}"


def _is_falsy_success(value: ast.expr) -> bool:
    """Return True when *value* is a literal falsy ``success`` value (False/0/None)."""
    if isinstance(value, ast.Constant):
        return value.value in (False, 0, None)
    return False


def _literal_keys(node: ast.Dict) -> list[str] | None:
    """Return the string keys of a dict literal, or None if any key is non-constant.

    A ``**spread`` entry (key is None) or a computed key means the literal's full
    key set is unknown at parse time — return None so the caller treats it as
    unclassifiable-by-keys rather than guessing.
    """
    keys: list[str] = []
    for key in node.keys:
        if key is None:  # ``**other`` spread
            return None
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        keys.append(key.value)
    return keys


def _classify_keys(keys: list[str]) -> str:
    """Classify a failure-shaped dict literal by its key set (required-key rule).

    Precondition: the caller has already confirmed this dict is failure-shaped
    (a falsy ``success`` entry, a ``status`` discriminant, or a partial-success
    flag). Order matters: carve-outs are recognised first so they never fall
    through to the dialect/ad-hoc checks.
    """
    key_set = set(keys)

    # Carve-out 1: discriminated-status union (status discriminant, no success).
    if STATUS_KEY in key_set and SUCCESS_KEY not in key_set:
        return CARVE_OUT_CANDIDATE
    # Carve-out 2: partial-success payload (additive failure flag).
    if key_set & PARTIAL_SUCCESS_FLAGS:
        return CARVE_OUT_CANDIDATE

    # Forbidden keys — the two legacy dialects.
    if ERROR_CODE_KEY in key_set:
        return ERROR_CODE_DIALECT
    if ERROR_KEY in key_set:
        return ERROR_KEY_DIALECT

    # Required-key rule: a canonical failure carries both reason and message.
    if REASON_KEY in key_set and MESSAGE_KEY in key_set:
        return CANONICAL

    # success: False but missing reason and/or message — an ad-hoc stray.
    return AD_HOC


def _has_falsy_success_entry(node: ast.Dict) -> bool:
    """Return True when the dict literal has a ``"success"`` key with a falsy value."""
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == SUCCESS_KEY and _is_falsy_success(value):
            return True
    return False


def _has_status_entry(node: ast.Dict) -> bool:
    """Return True when the dict literal has a literal ``"status"`` key."""
    return any(isinstance(key, ast.Constant) and key.value == STATUS_KEY for key in node.keys)


def _has_partial_success_flag(node: ast.Dict) -> bool:
    """Return True when the dict literal carries an additive partial-success flag."""
    return any(isinstance(key, ast.Constant) and key.value in PARTIAL_SUCCESS_FLAGS for key in node.keys)


def _finding_for_return(path: Path, node: ast.Return) -> Finding | None:
    """Classify one ``return`` statement. None = not a failure-shaped return."""
    value = node.value
    if not isinstance(value, ast.Dict):
        return None

    keys = _literal_keys(value)
    is_failure = _has_falsy_success_entry(value)
    is_status = _has_status_entry(value)
    is_partial = _has_partial_success_flag(value)

    if not (is_failure or is_status or is_partial):
        return None

    if keys is None:
        # A ``**spread`` or computed key hid the full key set. Flag it so a
        # reviewer can eyeball it; treat an obvious carve-out shape as exempt,
        # everything else as AD_HOC (an unknown shape never silently passes).
        looks_like_carve_out = (is_status and not is_failure) or is_partial
        classification = CARVE_OUT_CANDIDATE if looks_like_carve_out else AD_HOC
        return Finding(
            path=path,
            lineno=node.lineno,
            classification=classification,
            keys=("<dynamic keys>",),
            detail="dict with spread/computed keys — verify by hand",
        )

    classification = _classify_keys(keys)
    return Finding(
        path=path,
        lineno=node.lineno,
        classification=classification,
        keys=tuple(keys),
        detail="",
    )


def scan_file(path: Path) -> list[Finding]:
    """Parse *path* and return every failure-shaped return finding in it."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            finding = _finding_for_return(path, node)
            if finding is not None:
                findings.append(finding)
    return findings


def collect_findings(services_dir: Path = SERVICES_DIR) -> list[Finding]:
    """Walk *services_dir* and return every failure-shape finding."""
    findings: list[Finding] = []
    if services_dir.is_dir():
        for path in sorted(services_dir.rglob("*.py")):
            findings.extend(scan_file(path))
    return findings


def _group_by_class(findings: list[Finding]) -> dict[str, list[Finding]]:
    by_class: dict[str, list[Finding]] = {label: [] for label in REPORT_ORDER}
    for finding in findings:
        by_class.setdefault(finding.classification, []).append(finding)
    return by_class


def _print_report(findings: list[Finding]) -> None:
    """Print findings grouped by classification with a count summary."""
    by_class = _group_by_class(findings)

    for label in REPORT_ORDER:
        group = by_class.get(label, [])
        print(f"=== {label} ({len(group)}) ===")
        for finding in group:
            print(f"  {finding.render()}")
        print()

    print("=== SUMMARY ===")
    for label in REPORT_ORDER:
        print(f"  {label}: {len(by_class.get(label, []))}")
    print(f"  TOTAL: {len(findings)}")


def _print_violations(findings: list[Finding]) -> None:
    """Print only the enforce-mode violations, grouped, with a fix hint."""
    violations = [f for f in findings if f.classification in VIOLATION_CLASSES]
    by_class = _group_by_class(violations)
    for label in REPORT_ORDER:
        group = by_class.get(label, [])
        if not group:
            continue
        print(f"=== {label} ({len(group)}) ===")
        for finding in group:
            print(f"  {finding.render()}")
        print()
    print(
        "ERROR: failure-shaped returns in py_modules/services/ must carry "
        "'reason' + 'message' and must not carry 'error' / 'error_code' "
        "(CLAUDE.md → Callable response shapes; lib/list_result.py)."
    )


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0

    enforce = "--check" in argv
    findings = collect_findings(SERVICES_DIR)

    if enforce:
        violations = [f for f in findings if f.classification in VIOLATION_CLASSES]
        if violations:
            _print_violations(findings)
            return 1
        print(f"OK: no failure-shape dialect violations in {SERVICES_DIR.relative_to(REPO_ROOT)}.")
        return 0

    _print_report(findings)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
