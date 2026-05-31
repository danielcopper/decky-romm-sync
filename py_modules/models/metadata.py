"""Achievement summary dataclass for badge rendering."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AchievementSummary:
    """Cached achievement progress summary for badge rendering."""

    earned: int
    total: int
    earned_hardcore: int
    cached_at: float
