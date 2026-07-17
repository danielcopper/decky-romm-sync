"""Cover conditional-request (revalidation) outcome."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoverRevalidation:
    """The outcome of a (possibly conditional) cover GET (#1454).

    ``not_modified`` is ``True`` only when a conditional request (``If-None-Match``
    / ``If-Modified-Since``) drew a ``304 Not Modified`` — the destination file was
    left untouched and the cached bytes are still current. ``False`` means the
    server sent a ``200`` and the destination now holds fresh bytes (an
    unconditional download always reports ``False``).

    ``etag`` / ``last_modified`` are the validators the response carried, or
    ``None`` when the server (or an intermediary proxy) sent none — the signal to
    fall back to an unconditional download next time.
    """

    not_modified: bool
    etag: str | None
    last_modified: str | None
