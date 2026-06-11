#!/usr/bin/env bash
# Create a .worktrees/<type>/<slug> worktree, trust its mise config, and install
# its dependencies — so tests, ruff, basedpyright and the editor LSP work inside
# the worktree immediately (a fresh worktree gets its own empty .venv otherwise).
#
# After this finishes, re-root the Claude session into the worktree with
# EnterWorktree({ path: ".worktrees/<type>/<slug>" }) for clean diagnostics.
#
# Usage: mise run worktree:new <type> <slug> [base]   (base defaults to main)
set -euo pipefail

type="${1:?usage: mise run worktree:new <type> <slug> [base=main]}"
slug="${2:?usage: mise run worktree:new <type> <slug> [base=main]}"
base="${3:-main}"
wt=".worktrees/${type}/${slug}"

git worktree add "$wt" -b "${type}/${slug}" "$base"
mise trust "$wt/mise.toml"
mise -C "$wt" run setup

echo
echo "✓ worktree ready: $wt  (branch ${type}/${slug})"
echo "  next: EnterWorktree({ path: \"$wt\" })  — re-roots the session for clean LSP/tooling"
