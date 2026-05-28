"""Save-sync feature settings (singleton).

The user-toggleable knobs that govern save synchronisation: whether sync is on,
whether it runs around launch/exit, the default slot, and the autocleanup
retention limit. Distinct from ``settings.json`` (general plugin settings, which
stay JSON per the persistence epic) — this aggregate owns only save-sync state.
"""

from __future__ import annotations

from domain._aggregate import cosmic_aggregate


@cosmic_aggregate
class SyncSettings:
    """User-controlled save-sync feature settings."""

    save_sync_enabled: bool = False
    sync_before_launch: bool = True
    sync_after_exit: bool = True
    default_slot: str | None = "default"
    autocleanup_limit: int = 10

    def enable_save_sync(self) -> None:
        """Turn save sync on."""
        self.save_sync_enabled = True

    def disable_save_sync(self) -> None:
        """Turn save sync off."""
        self.save_sync_enabled = False

    def set_sync_before_launch(self, enabled: bool) -> None:
        """Set whether saves sync down before a game launches."""
        self.sync_before_launch = enabled

    def set_sync_after_exit(self, enabled: bool) -> None:
        """Set whether saves sync up after a game exits."""
        self.sync_after_exit = enabled

    def set_default_slot(self, name: str | None) -> None:
        """Set the default save slot. An empty name means 'no slots' mode (None)."""
        self.default_slot = name or None

    def set_autocleanup_limit(self, limit: int) -> None:
        """Set how many save versions to retain. Must be non-negative."""
        if limit < 0:
            raise ValueError("autocleanup_limit must be >= 0")
        self.autocleanup_limit = limit
