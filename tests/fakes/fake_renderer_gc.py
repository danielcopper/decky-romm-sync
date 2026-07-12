"""In-memory ``RendererGcFn`` implementation for service tests."""

from __future__ import annotations


class FakeRendererGc:
    """In-memory ``RendererGcFn`` for tests.

    Returns the ``result`` configured at construction (default ``False`` — a
    no-op GC, matching the fail-open real adapter when the debugger is
    unreachable) and records ``calls`` so a test can assert the gate fired the GC
    before measuring RSS.
    """

    def __init__(self, result: bool = False) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.result
