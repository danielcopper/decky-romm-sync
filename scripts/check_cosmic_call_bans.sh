#!/usr/bin/env bash
# Cosmic Python call-ban check.
#
# Services must inject Protocols instead of touching infrastructure directly:
#   - Clock / UuidGen / Sleeper        for time, IDs, and async sleeps
#   - File-store / cache adapters      for any filesystem-touch operation
#   - HTTP / socket adapters           for any network I/O
#
# Two pattern groups are enforced in ``py_modules/services/``:
#
#   1. Clock / randomness / sleep call sites
#      (``datetime.now()`` / ``time.time()`` / ``time.monotonic()`` /
#       ``asyncio.sleep()`` / ``uuid.uuid4()`` / ``random.*``)
#
#   2. Filesystem-touch + network-I/O patterns
#      (``os.*`` filesystem helpers, raw ``open(...)``, pathlib filesystem
#      methods, and side-effecting module imports like ``shutil`` /
#      ``subprocess`` / ``urllib`` / ``fcntl`` / ``requests`` / ``httpx`` /
#      ``socket`` / ``aiofiles`` / ``aiohttp`` / ``tempfile``)
#
# Allowed in services (path algebra only — no filesystem touch):
#   os.path.join / os.path.relpath / os.path.splitext /
#   os.path.basename / os.path.dirname / os.path.normpath / os.sep
#
# Allowed Protocol-adapter call shape: ``self._files.exists(path)`` etc.
# Those take a path argument and route to a Protocol-typed adapter. The
# pathlib-method patterns below match the no-arg call form (``path.exists()``)
# so they do not collide with the adapter call shape.
#
# Limitations:
#   - grep-based. Aliased module imports (``import asyncio as aio; aio.sleep()``)
#     and direct function imports (``from asyncio import sleep; sleep()``)
#     bypass the regex. Reviewers catch those workarounds.
#   - Docstring mentions of banned names inside double-backticks (e.g.
#     ``os.replace`` in the ``FileStore`` Protocol docs) are filtered out
#     via the DOCSTRING_BACKTICK_FILTER below. Plain-text mentions in
#     comments are NOT filtered — keep service comments free of literal
#     forbidden call snippets, or wrap them in backticks.

set -euo pipefail

readonly SERVICES_DIR="py_modules/services"

# Lines where the pattern appears only inside a ``...`` markdown span
# (docstring code reference) are not real call sites. Strip them out.
readonly DOCSTRING_BACKTICK_FILTER='``[^`]*``'

readonly CLOCK_RANDOM_SLEEP_PATTERNS=(
    'datetime\.now\('
    'asyncio\.sleep\('
    'time\.time\('
    'time\.monotonic\('
    'uuid\.uuid4\('
    '(^|[^a-zA-Z_.])random\.[a-zA-Z_]'
)

readonly FILESYSTEM_PATTERNS=(
    # os.* filesystem helpers (path algebra like os.path.join is allowed
    # and not listed here).
    '(^|[^a-zA-Z_])os\.path\.exists\('
    '(^|[^a-zA-Z_])os\.path\.isfile\('
    '(^|[^a-zA-Z_])os\.path\.isdir\('
    '(^|[^a-zA-Z_])os\.stat\('
    '(^|[^a-zA-Z_])os\.listdir\('
    '(^|[^a-zA-Z_])os\.scandir\('
    '(^|[^a-zA-Z_])os\.makedirs\('
    '(^|[^a-zA-Z_])os\.unlink\('
    '(^|[^a-zA-Z_])os\.remove\('
    '(^|[^a-zA-Z_])os\.rename\('
    '(^|[^a-zA-Z_])os\.replace\('
    '(^|[^a-zA-Z_])os\.walk\('
    '(^|[^a-zA-Z_])os\.mkdir\('
    '(^|[^a-zA-Z_])os\.rmdir\('
    '(^|[^a-zA-Z_])os\.chmod\('
    '(^|[^a-zA-Z_])os\.chown\('
    '(^|[^a-zA-Z_])os\.symlink\('
    '(^|[^a-zA-Z_])os\.readlink\('

    # Raw file-open patterns. ``open(`` matches both ``open(...)`` and
    # ``with open(...)``. Negative left-context avoids matching attribute
    # access like ``self.open(...)`` (no project case today, but cheap).
    '(^|[^a-zA-Z_.])open\('
    '(^|[^a-zA-Z_])io\.open\('

    # pathlib filesystem-touch methods. Match the no-arg call form so
    # Protocol-adapter calls like ``self._files.exists(path)`` (which carry
    # a path argument) are not flagged.
    '\.exists\(\s*\)'
    '\.is_file\(\s*\)'
    '\.is_dir\(\s*\)'
    '\.iterdir\(\s*\)'
    '\.read_text\('
    '\.write_text\('
    '\.read_bytes\(\s*\)'
    '\.write_bytes\('
    '\.unlink\(\s*\)'
    '\.rmdir\(\s*\)'
    '\.touch\(\s*\)'
    '\.lstat\(\s*\)'
    # Path.stat() is filesystem; the adapter equivalent uses descriptive
    # names (e.g. ``size_bytes``), so the no-arg form is a safe ban.
    '\.stat\(\s*\)'
    # ``.mkdir(`` and ``.glob(`` always touch the filesystem in pathlib.
    # The Protocol-adapter equivalent is ``make_dirs`` (with underscore),
    # so ``.mkdir(`` is safe to ban outright.
    '\.mkdir\('
    '\.glob\('

    # Side-effecting module imports. Anchored at line start with optional
    # indentation so both top-level and nested imports are caught.
    '^[[:space:]]*import shutil\b'
    '^[[:space:]]*from shutil\b'
    '^[[:space:]]*import subprocess\b'
    '^[[:space:]]*from subprocess\b'
    '^[[:space:]]*import urllib\b'
    '^[[:space:]]*from urllib\b'
    '^[[:space:]]*import fcntl\b'
    '^[[:space:]]*from fcntl\b'
    '^[[:space:]]*import requests\b'
    '^[[:space:]]*from requests\b'
    '^[[:space:]]*import httpx\b'
    '^[[:space:]]*from httpx\b'
    '^[[:space:]]*import socket\b'
    '^[[:space:]]*from socket\b'
    '^[[:space:]]*import aiofiles\b'
    '^[[:space:]]*from aiofiles\b'
    '^[[:space:]]*import aiohttp\b'
    '^[[:space:]]*from aiohttp\b'
    '^[[:space:]]*import tempfile\b'
    '^[[:space:]]*from tempfile\b'
)

found_any=0

check_patterns() {
    local label="$1"
    shift
    local patterns=("$@")

    for pattern in "${patterns[@]}"; do
        # `|| true` keeps `set -e` happy when grep returns 1 on no-match;
        # checking `$matches` directly avoids a false-positive that the
        # assignment-as-if-test form triggered in this loop construct.
        # The second grep strips lines where the match only appears inside
        # a ``...`` markdown span (docstring code reference).
        matches=$(
            grep -rnE "$pattern" "$SERVICES_DIR" 2>/dev/null \
                | grep -vE "$DOCSTRING_BACKTICK_FILTER" \
                || true
        )
        if [[ -n "$matches" ]]; then
            echo "Forbidden $label call '$pattern' in $SERVICES_DIR:"
            echo "$matches"
            echo
            found_any=1
        fi
    done
}

check_patterns "Cosmic Python" "${CLOCK_RANDOM_SLEEP_PATTERNS[@]}"
check_patterns "filesystem/network" "${FILESYSTEM_PATTERNS[@]}"

if [[ $found_any -ne 0 ]]; then
    echo "ERROR: services must inject Protocol-typed adapters for I/O,"
    echo "       and Clock / UuidGen / Sleeper for time / IDs / sleeps (CLAUDE.md)."
    exit 1
fi

echo "OK: no forbidden calls in $SERVICES_DIR."
