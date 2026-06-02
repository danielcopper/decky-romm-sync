"""Machine-id adapter — concrete ``MachineIdReader`` Protocol implementation.

Reads ``/etc/machine-id`` so services can supply the stable, machine-derived
device identity to RomM device registration without touching the filesystem
directly. The single source of truth here is ``/etc/machine-id`` alone — no
fallback chain — so an unreadable or empty file degrades to ``None`` and the
caller falls back to no-fingerprint registration.
"""

from __future__ import annotations

_MACHINE_ID_PATH = "/etc/machine-id"


class MachineIdAdapter:
    """Real ``MachineIdReader`` backed by ``/etc/machine-id``."""

    def get(self) -> str | None:
        """Return the stripped ``/etc/machine-id`` value, or ``None`` when unreadable.

        Returns ``None`` on a missing file, empty/whitespace-only content, or
        any ``OSError`` so registration can degrade to no-fingerprint behaviour
        rather than sending an empty or invalid identity.
        """
        try:
            with open(_MACHINE_ID_PATH, encoding="utf-8") as f:
                machine_id = f.read().strip()
        except OSError:
            return None
        return machine_id or None
