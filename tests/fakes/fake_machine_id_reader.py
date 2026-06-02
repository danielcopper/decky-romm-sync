"""In-memory ``MachineIdReader`` implementation for service tests."""

from __future__ import annotations


class FakeMachineIdReader:
    """In-memory ``MachineIdReader`` for tests.

    Returns the ``machine_id`` value configured at construction (``None``
    models an unreadable ``/etc/machine-id``). Tests that assert on the
    registration fingerprint read the same value back through the service.
    """

    def __init__(self, machine_id: str | None = "test-machine-id") -> None:
        self.machine_id = machine_id

    def get(self) -> str | None:
        return self.machine_id
