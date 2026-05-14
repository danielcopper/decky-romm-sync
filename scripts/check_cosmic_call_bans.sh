#!/usr/bin/env bash
# Cosmic Python call-ban check.
# Services must inject Clock / UuidGen / Sleeper Protocols instead of
# calling datetime.now() / time.time() / time.monotonic() / asyncio.sleep() /
# uuid.uuid4() / random.* directly.
#
# Limitation: grep-based. Aliased module imports (``import asyncio as aio; aio.sleep()``)
# and direct function imports (``from asyncio import sleep; sleep()``) bypass the regex.
# Reviewers catch those workarounds.

set -euo pipefail

readonly SERVICES_DIR="py_modules/services"

readonly PATTERNS=(
    'datetime\.now\('
    'asyncio\.sleep\('
    'time\.time\('
    'time\.monotonic\('
    'uuid\.uuid4\('
    '(^|[^a-zA-Z_.])random\.[a-zA-Z_]'
)

found_any=0
for pattern in "${PATTERNS[@]}"; do
    # `|| true` keeps `set -e` happy when grep returns 1 on no-match;
    # checking `$matches` directly avoids a false-positive that the
    # assignment-as-if-test form triggered in this loop construct.
    matches=$(grep -rnE "$pattern" "$SERVICES_DIR" 2>/dev/null || true)
    if [[ -n "$matches" ]]; then
        echo "Forbidden Cosmic Python call '$pattern' in $SERVICES_DIR:"
        echo "$matches"
        echo
        found_any=1
    fi
done

if [[ $found_any -ne 0 ]]; then
    echo "ERROR: services must inject Clock / UuidGen / Sleeper Protocols (CLAUDE.md)."
    exit 1
fi

echo "OK: no forbidden calls in $SERVICES_DIR."
