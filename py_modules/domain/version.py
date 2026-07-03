"""Version string parsing and minimum-version comparison."""

from __future__ import annotations

import re

_VERSION_RE = re.compile(
    r"^(\d+(?:\.\d+)*)(?:-(alpha|beta)(?:\.\d+)?)?$",
    re.IGNORECASE,
)


def _parse_version(version_str: str | None) -> tuple[tuple[int, ...], bool] | None:
    """Parse *version_str* into ``(core_tuple, has_prerelease)``.

    Returns ``None`` when *version_str* is not a supported RomM version shape.
    """
    if not isinstance(version_str, str):
        return None
    match = _VERSION_RE.match(version_str)
    if match is None:
        return None
    try:
        core = tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None
    has_prerelease = match.group(2) is not None
    return core, has_prerelease


def meets_min_version(version_str: str | None, minimum: tuple[int, ...]) -> bool:
    """Return True when *version_str* is SemVer-compatible with ``>= minimum``.

    *version_str* is a dot-separated numeric string such as ``"4.8.1"``, optionally
    followed by a RomM pre-release suffix ``-alpha`` or ``-beta`` (case-insensitive)
    with an optional ``.N`` build number (e.g. ``"5.0.0-alpha.1"``, ``"4.9.0-beta"``).

    Pre-releases rank **below** their own release: at floor ``(4, 9, 0)``,
    ``4.9.0-beta.3`` is rejected while ``4.9.1-beta`` passes because its numeric
    core is genuinely above the floor. When the numeric core is below *minimum*,
    the result is ``False`` regardless of suffix.

    Returns ``False`` for any input that cannot be parsed (empty string, non-numeric
    parts, unsupported pre-release tags, ``None``, or any non-``str`` type). The input
    is server-controlled, so a numeric or structured value is rejected by the
    ``isinstance`` guard in :func:`_parse_version` rather than raising. Non-numeric
    sentinel strings like ``"development"`` also return ``False`` — callers that want
    to bypass the check for development builds must test for them before invoking this
    function.
    """
    parsed = _parse_version(version_str)
    if parsed is None:
        return False
    core, has_prerelease = parsed
    if core < minimum:
        return False
    if core > minimum:
        return True
    return not has_prerelease
