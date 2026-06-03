"""Settings-derived save-sync toggles, read from the live ``settings.json`` dict.

The five save-sync feature toggles and the device label live in
``settings.json`` (ADR-0003), not in SQLite. These pure readers apply the same
coercions the legacy on-disk parse did so every consumer (SaveService facade,
SyncEngine, sub-services) interprets the dict identically. No I/O — callers pass
the live settings dict; persistence flows through ``SettingsPersister``.
"""

from __future__ import annotations

from typing import Any


def save_sync_enabled(settings: dict[str, Any]) -> bool:
    """Whether the save-sync feature toggle is on."""
    return bool(settings.get("save_sync_enabled", False))


def sync_before_launch(settings: dict[str, Any]) -> bool:
    """Whether the pre-launch download is enabled."""
    return bool(settings.get("sync_before_launch", True))


def sync_after_exit(settings: dict[str, Any]) -> bool:
    """Whether the post-exit upload is enabled."""
    return bool(settings.get("sync_after_exit", True))


def resolve_default_slot(settings: dict[str, Any]) -> str | None:
    """The configured default slot, collapsing empty/None to ``None`` (legacy mode)."""
    raw_slot = settings.get("default_slot", "default")
    if raw_slot is None:
        return None
    slot_str = str(raw_slot)
    return slot_str if slot_str else None


def autocleanup_limit(settings: dict[str, Any]) -> int:
    """The auto-cleanup retention limit, guarded against ``0`` / ``None``."""
    return int(settings.get("autocleanup_limit", 10) or 10)


def save_sync_settings_view(settings: dict[str, Any]) -> dict[str, Any]:
    """Build the frontend-facing dict of the five save-sync knobs."""
    return {
        "save_sync_enabled": save_sync_enabled(settings),
        "sync_before_launch": sync_before_launch(settings),
        "sync_after_exit": sync_after_exit(settings),
        "default_slot": resolve_default_slot(settings),
        "autocleanup_limit": autocleanup_limit(settings),
    }


def sanitize_setting(key: str, value: object) -> tuple[object, bool]:
    """Validate and coerce a single save-sync settings key/value pair.

    Returns ``(coerced_value, skip)`` where ``skip=True`` means the value should
    be discarded. Mirrors the legacy ``StateService`` coercions: empty/whitespace
    slot names collapse to ``None`` (legacy mode), ``autocleanup_limit`` is
    clamped to a positive int, and the three booleans pass through ``bool(...)``.
    """
    if key == "default_slot":
        if value is None:
            return None, False  # None = legacy mode
        coerced = str(value).strip()
        return (coerced if coerced else None), False  # empty -> None
    if key == "autocleanup_limit":
        return max(1, int(value)), False  # type: ignore[arg-type]
    if key in ("save_sync_enabled", "sync_before_launch", "sync_after_exit"):
        return bool(value), False
    return value, False


ALLOWED_SETTINGS_KEYS = frozenset(
    {
        "save_sync_enabled",
        "sync_before_launch",
        "sync_after_exit",
        "default_slot",
        "autocleanup_limit",
    }
)
