"""System UUID adapter — concrete ``UuidGen`` Protocol implementation.

Wraps :mod:`uuid` so services can stay free of direct ``uuid`` imports
when they need fresh identifiers.
"""

from __future__ import annotations

import uuid


class SystemUuidGen:
    """Real-entropy ``UuidGen`` backed by :func:`uuid.uuid4`."""

    def uuid4(self) -> str:
        return str(uuid.uuid4())
