"""Pure parser for RetroArch .info files.

RetroArch ships a ``<core>.info`` file next to every ``<core>.so`` in its
cores directory. The file format is INI-like: ``key = "value"`` pairs,
``#`` comments, and blank lines. This module parses the text into a
``dict[str, str]`` without touching the filesystem.

All values are returned as strings — the caller decides how to interpret
each field (``corename``, ``supported_extensions``, ``firmware_count``,
etc.). Outer double-quotes are stripped; whitespace around keys and
values is trimmed.
"""

from __future__ import annotations


def parse_core_info(text: str) -> dict[str, str]:
    """Parse a RetroArch .info file's content into a key-value dict.

    Accepts the raw file text and returns a ``dict[str, str]`` of all
    ``key = "value"`` pairs found. Lines starting with ``#`` are treated
    as comments and ignored; blank lines are ignored; lines without an
    ``=`` sign are ignored. If a value is wrapped in matching double
    quotes, the quotes are removed. Values are otherwise returned
    unchanged (including embedded whitespace).

    The parser is intentionally permissive: unknown keys, mixed line
    endings, trailing whitespace, and Unicode content are all accepted.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        result[key] = value
    return result
