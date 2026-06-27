"""Tests for py_modules/domain/save_backup.py"""

from __future__ import annotations

from domain.save_backup import backup_name, select_backups_to_prune

# ---------------------------------------------------------------------------
# backup_name
# ---------------------------------------------------------------------------


class TestBackupName:
    TS = "20260101_000000"

    def test_no_collision_returns_plain_name(self) -> None:
        """An empty existing set yields ``<name>_<ts><ext>`` with no counter."""
        assert backup_name("game.srm", self.TS, set()) == f"game_{self.TS}.srm"

    def test_one_collision_appends_1(self) -> None:
        """The plain name already present → the first counter ``_1`` is used."""
        existing = {f"game_{self.TS}.srm"}
        assert backup_name("game.srm", self.TS, existing) == f"game_{self.TS}_1.srm"

    def test_multiple_collisions_increment_counter(self) -> None:
        """Successive same-second backups walk ``_1``, ``_2``, … past every taken name."""
        existing: set[str] = set()
        names = []
        for _ in range(4):
            name = backup_name("game.srm", self.TS, existing)
            names.append(name)
            existing.add(name)
        assert names == [
            f"game_{self.TS}.srm",
            f"game_{self.TS}_1.srm",
            f"game_{self.TS}_2.srm",
            f"game_{self.TS}_3.srm",
        ]

    def test_different_extension_does_not_collide(self) -> None:
        """A backup of a sibling extension never blocks this file's plain name."""
        existing = {f"game_{self.TS}.rtc"}
        assert backup_name("game.srm", self.TS, existing) == f"game_{self.TS}.srm"

    def test_different_stem_does_not_collide(self) -> None:
        """A backup of a different save file never blocks this file's plain name."""
        existing = {f"other_{self.TS}.srm"}
        assert backup_name("game.srm", self.TS, existing) == f"game_{self.TS}.srm"

    def test_filename_with_no_extension(self) -> None:
        """A filename without an extension yields ``<name>_<ts>`` (no trailing dot)."""
        assert backup_name("game", self.TS, set()) == f"game_{self.TS}"
        existing = {f"game_{self.TS}"}
        assert backup_name("game", self.TS, existing) == f"game_{self.TS}_1"


# ---------------------------------------------------------------------------
# select_backups_to_prune
# ---------------------------------------------------------------------------


class TestSelectBackupsToPrune:
    def _backups(self, count: int, stem: str = "game", ext: str = ".srm") -> list[str]:
        """Build *count* chronological backups of *stem* with distinct timestamps."""
        return [f"{stem}_202601{day:02d}_000000{ext}" for day in range(1, count + 1)]

    def test_under_limit_returns_empty(self) -> None:
        """Fewer backups than the keep limit → nothing to prune."""
        assert select_backups_to_prune("game.srm", self._backups(3), keep=10) == []

    def test_exactly_at_limit_returns_empty(self) -> None:
        """Exactly the keep limit → nothing to prune."""
        assert select_backups_to_prune("game.srm", self._backups(10), keep=10) == []

    def test_over_limit_returns_oldest(self) -> None:
        """Over the limit → the oldest ``len - keep`` are returned, newest kept."""
        backups = self._backups(12)
        stale = select_backups_to_prune("game.srm", backups, keep=10)
        # Two oldest dropped; the newest 10 retained.
        assert stale == ["game_20260101_000000.srm", "game_20260102_000000.srm"]
        assert all(b not in stale for b in backups[2:])

    def test_same_second_counter_orders_after_base(self) -> None:
        """A same-second ``_1`` collision sorts AFTER its base, so the base is the
        one pruned when only the newest must survive."""
        backups = [
            "game_20260101_000000.srm",
            "game_20260101_000000_1.srm",
        ]
        # keep=1 → only the newest survives; the base (sorts first) is pruned.
        assert select_backups_to_prune("game.srm", backups, keep=1) == ["game_20260101_000000.srm"]

    def test_other_save_files_ignored(self) -> None:
        """Backups of a different stem in the shared dir are never pruned for this file."""
        backups = self._backups(12) + self._backups(12, stem="other")
        stale = select_backups_to_prune("game.srm", backups, keep=10)
        assert stale == ["game_20260101_000000.srm", "game_20260102_000000.srm"]
        assert all(b.startswith("game_") for b in stale)

    def test_other_extension_ignored(self) -> None:
        """A sibling extension's backups don't count toward this file's retention."""
        backups = self._backups(12) + self._backups(12, ext=".rtc")
        stale = select_backups_to_prune("game.srm", backups, keep=10)
        assert stale == ["game_20260101_000000.srm", "game_20260102_000000.srm"]
        assert all(b.endswith(".srm") for b in stale)

    def test_non_backup_junk_ignored(self) -> None:
        """Entries that don't match the ``<name>_<ts>[_<n>]<ext>`` shape are ignored."""
        backups = [*self._backups(11), "game.srm", "game_backup.srm", "README.txt"]
        stale = select_backups_to_prune("game.srm", backups, keep=10)
        assert stale == ["game_20260101_000000.srm"]

    def test_keep_zero_disables_pruning(self) -> None:
        """A keep of 0 disables pruning entirely."""
        assert select_backups_to_prune("game.srm", self._backups(20), keep=0) == []

    def test_keep_negative_disables_pruning(self) -> None:
        """A negative keep disables pruning entirely."""
        assert select_backups_to_prune("game.srm", self._backups(20), keep=-5) == []

    def test_empty_list_returns_empty(self) -> None:
        """No entries → nothing to prune."""
        assert select_backups_to_prune("game.srm", [], keep=10) == []
