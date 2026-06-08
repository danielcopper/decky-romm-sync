"""Health classification for RetroDECK path resolution from ``retrodeck.json``.

Cross-cutting enum shared by the adapter that reads ``retrodeck.json``
(``adapters/retrodeck_paths.py``), the Protocol services depend on
(``services/protocols/paths.py``), and the ``main.py`` callable that
surfaces the state to the frontend banner. It lives in ``lib/`` because
all three layers must import it and ``import-linter`` forbids the
adapter→service and service→adapter directions; ``lib/`` is the only
namespace importable from every layer.
"""

from __future__ import annotations

from enum import StrEnum


class RetroDeckConfigHealth(StrEnum):
    """How trustworthy the resolved RetroDECK roots are right now.

    The path getters are always best-effort and never raise; this enum
    is the loud signal that lets the frontend warn the user when the
    resolved roots are likely wrong.
    """

    OK = "ok"
    """``retrodeck.json`` read successfully AND the resolved RetroDECK home exists on disk."""

    ABSENT = "absent"
    """``retrodeck.json`` not found — the legitimate fresh-install fallback to ``~/retrodeck``. Stays quiet."""

    UNREADABLE = "unreadable"
    """``retrodeck.json`` exists but cannot be read or parsed — we know RetroDECK is configured but not where. Loud."""

    ROOT_MISSING = "root_missing"
    """``retrodeck.json`` read OK, but the resolved home is missing on disk (e.g. SD card ejected). Loud."""
