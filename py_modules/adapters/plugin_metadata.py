"""Concrete ``PluginMetadataReader`` adapter — reads ``package.json``.

Owns the raw ``open()`` + ``json.load()`` round-trip behind the
``PluginMetadataReader`` Protocol. A missing or malformed
``package.json`` is not a hard failure — bootstrap must keep wiring
services even when the metadata read fails, so the adapter returns the
documented fallback.
"""

from __future__ import annotations

import json
import os
from typing import Any


class PluginMetadataAdapter:
    """Real ``PluginMetadataReader`` backed by the on-disk ``package.json``."""

    def read_version(self, plugin_dir: str) -> str:
        return self._read(plugin_dir).get("version", "0.0.0")

    def read_name(self, plugin_dir: str) -> str:
        value = self._read(plugin_dir).get("name")
        return value if isinstance(value, str) and value else "decky-plugin"

    @staticmethod
    def _read(plugin_dir: str) -> dict[str, Any]:
        try:
            with open(os.path.join(plugin_dir, "package.json")) as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload
