"""In-memory ``PluginMetadataReader`` implementation for service tests."""

from __future__ import annotations


class FakePluginMetadataReader:
    """In-memory ``PluginMetadataReader`` for tests.

    Returns the version string configured at construction. Tests that
    don't care about the value can rely on the ``"0.0.0"`` default —
    matches the production adapter's fallback when ``package.json`` is
    unreadable. ``read_version`` records the last ``plugin_dir`` it was
    called with so tests can assert wiring.
    """

    def __init__(self, version: str = "0.0.0") -> None:
        self.version = version
        self.last_plugin_dir: str | None = None
        self.read_count = 0

    def read_version(self, plugin_dir: str) -> str:
        self.last_plugin_dir = plugin_dir
        self.read_count += 1
        return self.version
