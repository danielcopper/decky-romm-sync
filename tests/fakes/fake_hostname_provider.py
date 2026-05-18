"""In-memory ``HostnameProvider`` implementation for service tests."""

from __future__ import annotations


class FakeHostnameProvider:
    """In-memory ``HostnameProvider`` for tests.

    Returns the ``hostname`` value configured at construction. Tests
    that need to assert on the registered device name read the same
    string back through the service.
    """

    def __init__(self, hostname: str = "test-host") -> None:
        self.hostname = hostname

    def get(self) -> str:
        return self.hostname
