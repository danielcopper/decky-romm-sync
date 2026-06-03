"""RetroAchievements data shaping from RomM API payloads.

Pure transforms over RomM ROM-detail and user-progression dicts: pick the
right metadata branch, normalize achievement entries, and reduce per-game
progression into a flat summary. Anything that fetches, caches, or reads
the wall clock belongs in ``services/achievements.py``, not here.
"""

from __future__ import annotations

from typing import Any


def extract_achievements_from_rom(rom_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the normalized achievement list from a RomM ROM detail dict.

    Prefers ``ra_metadata``; falls back to ``merged_ra_metadata`` (which has
    resolved badge paths) when the primary branch is empty or missing.
    """
    ra_metadata = rom_data.get("ra_metadata") or {}
    if not ra_metadata:
        ra_metadata = rom_data.get("merged_ra_metadata") or {}
    achievements = ra_metadata.get("achievements") or []
    return [
        {
            "ra_id": a.get("ra_id"),
            "title": a.get("title", ""),
            "description": a.get("description", ""),
            "points": a.get("points", 0),
            "badge_id": a.get("badge_id", ""),
            "badge_url": a.get("badge_url", ""),
            "badge_url_lock": a.get("badge_url_lock", ""),
            "display_order": a.get("display_order", 0),
            "type": a.get("type", ""),
            "num_awarded": a.get("num_awarded", 0),
            "num_awarded_hardcore": a.get("num_awarded_hardcore", 0),
        }
        for a in achievements
    ]


def extract_game_progress(
    ra_progression: dict[str, Any] | None,
    ra_id: int,
    total: int,
    cached_at: float,
) -> dict[str, Any]:
    """Reduce a user's RA progression payload into a per-game progress dict.

    Looks up the entry whose ``rom_ra_id`` matches ``ra_id``. Returns a
    zero-stub (with ``total`` and ``cached_at`` carried through) when the
    game is absent or ``ra_progression`` is empty. ``cached_at`` is
    propagated verbatim — callers stamp it from their own clock.
    """
    results = (ra_progression or {}).get("results") or []
    game_progress = next((entry for entry in results if entry.get("rom_ra_id") == ra_id), None)

    if not game_progress:
        return {
            "earned": 0,
            "earned_hardcore": 0,
            "total": total,
            "earned_achievements": [],
            "cached_at": cached_at,
        }
    return {
        "earned": game_progress.get("num_awarded", 0) or 0,
        "earned_hardcore": game_progress.get("num_awarded_hardcore", 0) or 0,
        "total": game_progress.get("max_possible", total) or total,
        "earned_achievements": game_progress.get("earned_achievements", []),
        "cached_at": cached_at,
    }
