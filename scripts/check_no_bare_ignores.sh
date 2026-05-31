#!/usr/bin/env bash
# No-bare-ignore guardrail.
#
# Suppression directives must carry a reason — a blanket suppression hides
# the next regression that creeps in under the same line. Every directive
# must name the specific error it silences (Python) or carry a human-readable
# justification (TypeScript).
#
# Enforced conventions:
#
#   Python (py_modules/, main.py, tests/ — _vendor/ excluded):
#     - `# type: ignore` must be scoped: `# type: ignore[code]`. A bare
#       `# type: ignore` (not immediately followed by `[`) is rejected.
#     - `# noqa` must name a rule: `# noqa: CODE`. A bare `# noqa`
#       (not followed by `:`) is rejected.
#
#   TypeScript (src/):
#     - `eslint-disable` / `eslint-disable-next-line` / `eslint-disable-line`
#       must carry the ESLint native description separator ` -- reason`.
#     - `@ts-ignore` is rejected outright — use `@ts-expect-error` with a
#       trailing description instead (it fails when the error disappears,
#       so it can't silently rot).
#
# Generated / vendored trees are out of scope: _vendor, dist, node_modules,
# .worktrees.
#
# Limitations:
#   - grep-based. It matches the literal directive text on a line; it does
#     not parse comments, so a directive inside a string literal would be
#     flagged. No project case today.

set -euo pipefail

readonly PY_PATHS=("py_modules" "main.py" "tests")
readonly TS_DIR="src"

# Shared exclusions for generated / vendored trees.
readonly EXCLUDE_DIRS=(--exclude-dir=_vendor --exclude-dir=dist --exclude-dir=node_modules --exclude-dir=.worktrees)

found_any=0

report() {
    local label="$1"
    local matches="$2"
    echo "Bare $label (missing required reason / error code):"
    echo "$matches"
    echo
    found_any=1
}

# --- Python -----------------------------------------------------------------
# Bare `# type: ignore` — flag when NOT immediately followed by `[`.
py_type_ignore=$(
    grep -rnE '#\s*type:\s*ignore([^[]|$)' "${PY_PATHS[@]}" \
        --include='*.py' "${EXCLUDE_DIRS[@]}" 2>/dev/null || true
)
if [[ -n "$py_type_ignore" ]]; then
    report "# type: ignore (use # type: ignore[code])" "$py_type_ignore"
fi

# Bare `# noqa` — flag when NOT followed by `:`.
py_noqa=$(
    grep -rnE '#\s*noqa([^:]|$)' "${PY_PATHS[@]}" \
        --include='*.py' "${EXCLUDE_DIRS[@]}" 2>/dev/null || true
)
if [[ -n "$py_noqa" ]]; then
    report "# noqa (use # noqa: CODE)" "$py_noqa"
fi

# --- TypeScript -------------------------------------------------------------
# eslint-disable* without the ` -- ` native description separator.
ts_eslint=$(
    grep -rnE 'eslint-disable(-next-line|-line)?' "$TS_DIR" \
        --include='*.ts' --include='*.tsx' "${EXCLUDE_DIRS[@]}" 2>/dev/null \
        | grep -vF ' -- ' || true
)
if [[ -n "$ts_eslint" ]]; then
    report "eslint-disable (use 'eslint-disable... -- reason')" "$ts_eslint"
fi

# @ts-ignore is rejected outright (use @ts-expect-error with a description).
ts_ignore=$(
    grep -rnF '@ts-ignore' "$TS_DIR" \
        --include='*.ts' --include='*.tsx' "${EXCLUDE_DIRS[@]}" 2>/dev/null || true
)
if [[ -n "$ts_ignore" ]]; then
    report "@ts-ignore (use @ts-expect-error with a trailing description)" "$ts_ignore"
fi

if [[ $found_any -ne 0 ]]; then
    echo "ERROR: suppression directives must carry a reason (CLAUDE.md / issue #838)."
    exit 1
fi

echo "OK: no bare suppression directives."
