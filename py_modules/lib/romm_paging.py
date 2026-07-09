"""Shared page size for RomM list-endpoint pagination.

The RomM adapter (request builder + method defaults), the ``RommRomReader``
Protocol, and the library fetcher's pagination loop must all agree on the page
size requested from RomM's ``/api/roms`` list endpoint. Adapter and service sit
on opposite sides of the layer boundary (a service may not import an adapter, an
adapter may not import a service), so the single source of truth for that value
lives here in ``lib``, importable by both.
"""

from __future__ import annotations

# RomM's list endpoint bounds ``limit`` at ``Query(50, ge=1, le=10_000)``
# (unchanged across the 4.9.x floor), so a page of 500 is well within range.
# Fetch cost is dominated by per-request overhead, not payload size, so a large
# page collapses a multi-thousand-ROM library into a handful of requests — one
# request for a typical platform, seven for a ~3000-ROM one.
LIST_PAGE_SIZE = 500
