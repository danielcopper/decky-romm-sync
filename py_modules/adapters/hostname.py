"""Hostname adapter — concrete ``HostnameReader`` Protocol implementation.

Wraps :func:`socket.gethostname` so services can stay free of direct
``socket`` imports for the sole purpose of reading the local hostname.
"""

from __future__ import annotations

import socket


class HostnameAdapter:
    """Real ``HostnameReader`` backed by :func:`socket.gethostname`."""

    def get(self) -> str:
        return socket.gethostname()
