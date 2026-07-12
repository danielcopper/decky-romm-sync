"""In-memory ``RendererRssFn`` implementation for service tests."""

from __future__ import annotations


class FakeRendererRss:
    """In-memory ``RendererRssFn`` for tests.

    Returns the ``rss_kb`` value configured at construction (``None`` models an
    unavailable reading — no renderer process — so the session-budget gate skips,
    fail-open). ``rss_kb`` can be reassigned mid-test to drive the gate across a
    threshold, and ``calls`` records how many times the reading was requested.
    """

    def __init__(self, rss_kb: int | None = None) -> None:
        self.rss_kb = rss_kb
        self.calls = 0

    def __call__(self) -> int | None:
        self.calls += 1
        return self.rss_kb
