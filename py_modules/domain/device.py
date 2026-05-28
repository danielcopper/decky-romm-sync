"""Device identity — the single registered device this plugin instance runs on.

Owns the server-issued device identity and its optional display name. Anything
about *which* device this plugin is registered as belongs here. Per-device sync
state, playtime, and saves are separate aggregates keyed by their own roots.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class Device:
    """The registered device this plugin instance runs on (singleton).

    Identity is ``device_id`` — the server-issued id RomM assigns on
    registration. ``device_name`` is a mutable display label.
    """

    device_id: str
    device_name: str | None = None

    @classmethod
    def register(cls, device_id: str, name: str | None = None) -> Device:
        """Register the device under its server-issued ``device_id``."""
        if not device_id:
            raise ValueError("device_id is required")
        return cls(device_id=device_id, device_name=name)

    def rename(self, new_name: str) -> None:
        """Change the device's display name."""
        self.device_name = new_name
