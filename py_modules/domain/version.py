"""Version string parsing and minimum-version comparison."""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:-(alpha|beta)(?:\.\d+)?)?$")


def _parse_core_version(version_str: str) -> tuple[int, ...] | None:
    """Parse the numeric core from *version_str*, ignoring any pre-release suffix."""
    match = _VERSION_RE.match(version_str)
    if match is None:
        return None
    try:
        return tuple(int(p) for p in match.group(1).split("."))
    except ValueError:
        return None


def meets_min_version(version_str: str, minimum: tuple[int, ...]) -> bool:
    """Return True when *version_str* parses to a version >= *minimum*.

    *version_str* is a dot-separated numeric string such as ``"4.8.1"``, optionally
    followed by a RomM pre-release suffix ``-alpha`` or ``-beta`` with an optional
    ``.N`` build number (e.g. ``"5.0.0-alpha.1"``, ``"4.9.0-beta"``). Only the
    numeric core is compared against *minimum*; pre-release tags are accepted for
    parsing but do not affect the comparison.

    Returns ``False`` for any input that cannot be parsed (empty string, non-numeric
    parts, unsupported pre-release tags, ``None``). Non-numeric sentinel strings like
    ``"development"`` also return ``False`` — callers that want to bypass the check
    for development builds must test for them before invoking this function.
    """
    parts = _parse_core_version(version_str)
    if parts is None:
        return False
    return parts >= minimum
