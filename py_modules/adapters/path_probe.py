"""Concrete ``PathExistsReader`` adapter — generic filesystem existence probe."""

from __future__ import annotations

import os


class PathProbeAdapter:
    """Thin wrapper over ``os.path.exists`` for the ``PathExistsReader`` Protocol."""

    def exists(self, path: str) -> bool:
        return os.path.exists(path)
