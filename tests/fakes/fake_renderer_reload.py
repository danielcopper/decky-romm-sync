"""In-memory ``RendererReloadFn`` implementation for service tests."""

from __future__ import annotations


class FakeRendererReload:
    """In-memory ``RendererReloadFn`` for tests.

    Returns the ``result`` configured at construction (default ``True`` — the
    reload was accepted) and records ``calls`` so a test can assert the "free
    Steam memory" action fired the reload.
    """

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.result
