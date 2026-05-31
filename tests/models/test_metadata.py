"""Tests for models.metadata dataclasses."""

from dataclasses import asdict

from models.metadata import AchievementSummary


class TestAchievementSummary:
    def test_construction(self):
        a = AchievementSummary(earned=5, total=20, earned_hardcore=3, cached_at=9999.0)
        assert a.earned == 5
        assert a.total == 20

    def test_asdict(self):
        a = AchievementSummary(earned=10, total=10, earned_hardcore=10, cached_at=1.0)
        d = asdict(a)
        assert d == {"earned": 10, "total": 10, "earned_hardcore": 10, "cached_at": 1.0}
