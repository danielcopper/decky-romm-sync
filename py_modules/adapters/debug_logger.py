"""Settings-aware debug logger adapter — concrete ``DebugLogger`` Protocol implementation.

Routes debug-level messages through a standard-library logger only when
the user's QAM-configured ``log_level`` setting is at least ``debug``.
Holds a live reference to the settings dict (so level changes from the
QAM panel take effect on the next call without restart) and the
underlying :class:`logging.Logger` it emits through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    import logging


class SettingsAwareDebugLogger:
    """``DebugLogger`` impl that honors the live ``log_level`` setting.

    The settings dict is bound by reference at construction so QAM-side
    edits are observed without restart. Messages route through
    ``logger.info`` (matching the existing frontend-log surface so users
    see both their own frontend messages and backend debug traces in the
    same stream) when ``log_level`` is ``"debug"``; any other level
    silently drops the message.
    """

    _LOG_LEVELS: ClassVar[dict[str, int]] = {"debug": 0, "info": 1, "warn": 2, "error": 3}

    def __init__(self, *, settings: dict[str, Any], logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger

    def __call__(self, msg: str) -> None:
        configured = self._settings.get("log_level", "warn")
        if self._LOG_LEVELS.get("debug", 0) >= self._LOG_LEVELS.get(configured, 2):
            self._logger.info(msg)
