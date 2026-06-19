"""Per-platform sync-enabled defaulting — the empty-map "all enabled" rule.

Single source of truth for how an absent ``enabled_platforms`` entry is
resolved, shared by the display path (``get_platforms``) and the filter
path (``_fetch_enabled_platforms``) so the two can never drift. The empty
map means "all platforms enabled by default"; once any platform is
explicitly toggled, the map is the full per-platform preference set.

Pure compute — no I/O, no state mutation.
"""

from __future__ import annotations


def resolve_sync_enabled(stored: dict[str, bool], pid: str) -> bool:
    """Resolve whether platform ``pid`` is sync-enabled given the stored map.

    An absent id defaults to enabled **only while the map is empty** (the
    "all enabled by default" sentinel). Once any preference is recorded, an
    absent id resolves to disabled — so a complete map can be read literally.
    """
    return stored.get(pid, len(stored) == 0)


def materialize_enabled_platforms(stored: dict[str, bool], platform_ids: list[str]) -> dict[str, bool] | None:
    """Resolve the empty-map sentinel into an explicit all-True map.

    Returns a full ``{pid: True}`` map when ``stored`` is the empty sentinel
    and at least one platform id is given; otherwise ``None`` (no
    materialization needed — the map already holds explicit preferences, or
    there are no platforms to enumerate). The caller persists a non-``None``
    result so every later single-key write is a true partial update of a
    complete map, and "len == 1" can never be misread as "the rest disabled".
    """
    if stored or not platform_ids:
        return None
    return dict.fromkeys(platform_ids, True)
