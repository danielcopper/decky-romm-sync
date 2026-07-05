"""Tests for ``SqlitePlaytimeRepository`` over ``rom_playtime`` + ``rom_playtime_sessions``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.playtime import PendingPlaySession, Playtime
from domain.rom import Rom

if TYPE_CHECKING:
    from adapters.repositories.unit_of_work import SqliteUnitOfWork


def _seed_rom(uow: SqliteUnitOfWork, rom_id: int) -> None:
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug="snes",
            name=f"Game {rom_id}",
            fs_name=f"game_{rom_id}.sfc",
            shortcut_app_id=1000 + rom_id,
            last_synced_at="2026-01-01T00:00:00Z",
        )
    )


def _pending(
    device_id: str = "dev-1",
    end_time: str = "2026-03-03T11:00:00Z",
    duration_ms: int = 3600,
    attempts: int = 0,
) -> PendingPlaySession:
    return PendingPlaySession(device_id=device_id, end_time=end_time, duration_ms=duration_ms, attempts=attempts)


class TestRoundTrip:
    def test_full_playtime_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        playtime = Playtime(
            total_seconds=3600,
            session_count=4,
            last_session_start="2026-03-03T10:00:00Z",
            last_session_start_monotonic=4321.5,
            last_session_duration_sec=900,
            last_played="2026-03-03T10:15:00Z",
            pending_sessions={"2026-03-03T10:00:00Z": _pending()},
        )
        uow.playtime.save(5, playtime)

        assert uow.playtime.get(5) == playtime

    def test_last_session_start_monotonic_round_trips(self, uow: SqliteUnitOfWork):
        """The monotonic-start column (migration 009) survives save → get (#1148)."""
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(last_session_start="2026-03-03T10:00:00Z", last_session_start_monotonic=1234.5))

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.last_session_start_monotonic == 1234.5

    def test_last_played_round_trips(self, uow: SqliteUnitOfWork):
        """The ``last_played`` column (migration 007) survives save → get (#903)."""
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(total_seconds=60, session_count=1, last_played="2026-03-03T11:00:00Z"))

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.last_played == "2026-03-03T11:00:00Z"

    def test_nullable_fields_preserved(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        playtime = Playtime()  # all defaults: open-session/duration/last-played None, empty outbox
        uow.playtime.save(5, playtime)

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.total_seconds == 0
        assert loaded.last_session_start is None
        assert loaded.last_session_start_monotonic is None
        assert loaded.last_session_duration_sec is None
        assert loaded.last_played is None
        assert loaded.pending_sessions == {}


class TestOutbox:
    def test_pending_sessions_round_trip(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        playtime = Playtime(
            pending_sessions={
                "s1": _pending(end_time="e1", duration_ms=100),
                "s2": _pending(end_time="e2", duration_ms=200),
            }
        )
        uow.playtime.save(5, playtime)

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.pending_sessions == {
            "s1": _pending(end_time="e1", duration_ms=100),
            "s2": _pending(end_time="e2", duration_ms=200),
        }

    def test_save_replaces_child_rows(self, uow: SqliteUnitOfWork):
        """A re-save with a dequeued outbox must delete the stale child rows, not merge."""
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(pending_sessions={"s1": _pending(), "s2": _pending()}))
        uow.playtime.save(5, Playtime(pending_sessions={"s2": _pending()}))

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert set(loaded.pending_sessions) == {"s2"}

    def test_save_empty_outbox_clears_child_rows(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(pending_sessions={"s1": _pending()}))
        uow.playtime.save(5, Playtime())  # all sent

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.pending_sessions == {}

    def test_attempts_round_trip(self, uow: SqliteUnitOfWork):
        """The ``attempts`` counter (migration 006) survives save → get."""
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(pending_sessions={"s1": _pending(attempts=3)}))

        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.pending_sessions["s1"].attempts == 3


class TestIterPendingSessions:
    def test_projects_rows_ordered_and_limited(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.playtime.save(
            1, Playtime(pending_sessions={"s2": _pending(duration_ms=20), "s1": _pending(duration_ms=10)})
        )
        uow.playtime.save(2, Playtime(pending_sessions={"s3": _pending(duration_ms=30, attempts=2)}))

        rows = uow.playtime.iter_pending_sessions(10)

        # Ordered by (rom_id, start_time): (1,s1), (1,s2), (2,s3).
        assert [(r.rom_id, r.start_time) for r in rows] == [(1, "s1"), (1, "s2"), (2, "s3")]
        assert rows[0].duration_ms == 10
        assert rows[2].attempts == 2

    def test_limit_caps_across_roms(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.playtime.save(1, Playtime(pending_sessions={f"s{i}": _pending() for i in range(5)}))

        rows = uow.playtime.iter_pending_sessions(3)

        assert len(rows) == 3

    def test_empty_when_no_outbox(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.playtime.save(1, Playtime(total_seconds=10))
        assert uow.playtime.iter_pending_sessions(10) == []


class TestMiss:
    def test_get_absent_returns_none(self, uow: SqliteUnitOfWork):
        assert uow.playtime.get(999) is None


class TestDelete:
    def test_delete_removes_scalar_and_child_rows(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 5)
        uow.playtime.save(5, Playtime(total_seconds=10, pending_sessions={"s1": _pending()}))
        uow.playtime.delete(5)
        assert uow.playtime.get(5) is None
        # Child rows are gone too — a fresh save with no outbox reads back empty.
        uow.playtime.save(5, Playtime())
        loaded = uow.playtime.get(5)
        assert loaded is not None
        assert loaded.pending_sessions == {}

    def test_delete_absent_is_idempotent(self, uow: SqliteUnitOfWork):
        uow.playtime.delete(404)
        assert uow.playtime.get(404) is None


class TestIteration:
    def test_iter_all_yields_rom_id_pairs_with_outbox(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        _seed_rom(uow, 2)
        uow.playtime.save(1, Playtime(total_seconds=10, pending_sessions={"s1": _pending()}))
        uow.playtime.save(2, Playtime(total_seconds=20))

        by_id = dict(uow.playtime.iter_all())
        assert set(by_id) == {1, 2}
        assert by_id[1].total_seconds == 10
        assert set(by_id[1].pending_sessions) == {"s1"}
        assert by_id[2].total_seconds == 20
        assert by_id[2].pending_sessions == {}


class TestUpsert:
    def test_save_existing_overwrites(self, uow: SqliteUnitOfWork):
        _seed_rom(uow, 1)
        uow.playtime.save(1, Playtime(total_seconds=10, session_count=1))
        uow.playtime.save(1, Playtime(total_seconds=50, session_count=3))

        loaded = uow.playtime.get(1)
        assert loaded is not None
        assert loaded.total_seconds == 50
        assert loaded.session_count == 3
