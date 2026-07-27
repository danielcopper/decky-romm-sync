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

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, str]] = {}

    def read_metadata(self, plugin_dir: str) -> tuple[str, str]:
        cached = self._cache.get(plugin_dir)
        if cached is not None:
            return cached
        payload = self._read(plugin_dir)
        raw_name = payload.get("name")
        raw_version = payload.get("version")
        value = (
            raw_name if isinstance(raw_name, str) and raw_name else "decky-plugin",
            raw_version if isinstance(raw_version, str) and raw_version else "0.0.0",
        )
        self._cache[plugin_dir] = value
        return value

    def read_version(self, plugin_dir: str) -> str:
        return self.read_metadata(plugin_dir)[1]

    def read_name(self, plugin_dir: str) -> str:
        return self.read_metadata(plugin_dir)[0]

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
