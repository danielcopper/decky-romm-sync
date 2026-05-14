"""Version string parsing and minimum-version comparison."""

from __future__ import annotations


def meets_min_version(version_str: str, minimum: tuple[int, ...]) -> bool:
    """Return True when *version_str* parses to a version >= *minimum*.

    *version_str* is a dot-separated numeric string such as ``"4.8.1"``.
    Returns ``False`` for any input that cannot be parsed as all-integer
    components (empty string, non-numeric parts, ``None``). Non-numeric
    sentinel strings like ``"development"`` also return ``False`` —
    callers that want to bypass the check for development builds must
    test for them before invoking this function.
    """
    try:
        parts = tuple(int(p) for p in version_str.split("."))
    except (ValueError, AttributeError):
        return False
    return parts >= minimum
