"""Unit tests for the ``Playtime`` aggregate."""

from __future__ import annotations

import pytest

from domain.playtime import PendingPlaySession, Playtime


class TestBeginSession:
    def test_begin_session_sets_start(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        assert playtime.last_session_start == "2026-05-28T10:00:00"


class TestRecordSession:
    def test_happy_path_folds_duration_into_totals(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        playtime.record_session("2026-05-28T11:00:00")
        assert playtime.total_seconds == 3600
        assert playtime.session_count == 1
        assert playtime.last_session_duration_sec == 3600
        assert playtime.last_session_start is None

    def test_stamps_last_played_with_ended_at(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        playtime.record_session("2026-05-28T11:00:00")
        assert playtime.last_played == "2026-05-28T11:00:00"

    def test_second_cycle_advances_last_played(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        playtime.record_session("2026-05-28T11:00:00")
        playtime.begin_session("2026-05-28T12:00:00")
        playtime.record_session("2026-05-28T12:30:00")
        assert playtime.last_played == "2026-05-28T12:30:00"

    def test_two_cycles_accumulate(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        playtime.record_session("2026-05-28T11:00:00")
        playtime.begin_session("2026-05-28T12:00:00")
        playtime.record_session("2026-05-28T12:30:00")
        assert playtime.total_seconds == 3600 + 1800
        assert playtime.session_count == 2
        assert playtime.last_session_duration_sec == 1800

    def test_no_open_session_raises(self):
        playtime = Playtime()
        with pytest.raises(ValueError, match="no open session to record"):
            playtime.record_session("2026-05-28T11:00:00")

    def test_unparseable_start_raises(self):
        playtime = Playtime()
        playtime.begin_session("not-a-date")
        with pytest.raises(ValueError, match="unparseable session timestamps"):
            playtime.record_session("2026-05-28T11:00:00")

    def test_upper_clamp_caps_at_24h(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        playtime.record_session("2026-05-30T10:00:00")
        assert playtime.last_session_duration_sec == 86400
        assert playtime.total_seconds == 86400
        assert playtime.session_count == 1

    def test_lower_clamp_end_before_start(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T11:00:00")
        playtime.record_session("2026-05-28T10:00:00")
        assert playtime.last_session_duration_sec == 0
        assert playtime.total_seconds == 0
        assert playtime.session_count == 1
        assert playtime.last_session_start is None

    def test_mixed_naive_aware_timestamps_raise(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")  # naive
        with pytest.raises(ValueError, match="inconsistent session timestamps"):
            playtime.record_session("2026-05-28T11:00:00Z")  # aware (Z -> +00:00)

    def test_suspend_subtracted_from_duration(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        # 3600s elapsed minus 600s suspended -> 3000s counted.
        playtime.record_session("2026-05-28T11:00:00", suspended_seconds=600)
        assert playtime.last_session_duration_sec == 3000
        assert playtime.total_seconds == 3000
        assert playtime.session_count == 1
        assert playtime.last_session_start is None

    def test_default_suspend_is_zero(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        # No suspend arg → full elapsed counted (unchanged behavior).
        playtime.record_session("2026-05-28T11:00:00")
        assert playtime.last_session_duration_sec == 3600

    def test_over_subtraction_clamps_to_zero(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        # 30s elapsed minus 60s suspended -> clamped to 0, never negative.
        playtime.record_session("2026-05-28T10:00:30", suspended_seconds=60)
        assert playtime.last_session_duration_sec == 0
        assert playtime.total_seconds == 0
        assert playtime.session_count == 1

    def test_24h_cap_applies_after_subtraction(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        # 90000s elapsed minus 3600s suspended = 86400s, still capped at 24h.
        playtime.record_session("2026-05-29T11:00:00", suspended_seconds=3600)
        assert playtime.last_session_duration_sec == 86400
        assert playtime.total_seconds == 86400


class TestEnqueueSession:
    def test_enqueue_adds_pending_session_keyed_by_start(self):
        playtime = Playtime()
        playtime.enqueue_session(
            device_id="dev-1",
            start_time="2026-05-28T10:00:00",
            end_time="2026-05-28T11:00:00",
            duration_ms=3_600_000,
        )
        assert playtime.pending_sessions == {
            "2026-05-28T10:00:00": PendingPlaySession(
                device_id="dev-1", end_time="2026-05-28T11:00:00", duration_ms=3_600_000
            )
        }

    def test_enqueue_same_start_overwrites(self):
        """Re-enqueuing the same window overwrites rather than duplicates (start is the dedup key)."""
        playtime = Playtime()
        playtime.enqueue_session(device_id="dev-1", start_time="s", end_time="e1", duration_ms=100)
        playtime.enqueue_session(device_id="dev-1", start_time="s", end_time="e2", duration_ms=200)
        assert len(playtime.pending_sessions) == 1
        assert playtime.pending_sessions["s"].duration_ms == 200

    def test_two_distinct_starts_coexist(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="dev-1", start_time="s1", end_time="e1", duration_ms=100)
        playtime.enqueue_session(device_id="dev-1", start_time="s2", end_time="e2", duration_ms=200)
        assert set(playtime.pending_sessions) == {"s1", "s2"}

    def test_record_session_end_can_be_enqueued(self):
        """The typical flow: record folds duration, then enqueue captures the window."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00")
        playtime.record_session("2026-05-28T10:30:00")
        playtime.enqueue_session(
            device_id="dev-1",
            start_time="2026-05-28T10:00:00",
            end_time="2026-05-28T10:30:00",
            duration_ms=(playtime.last_session_duration_sec or 0) * 1000,
        )
        assert playtime.pending_sessions["2026-05-28T10:00:00"].duration_ms == 1_800_000


class TestMarkSessionsSent:
    def test_dequeues_named_starts(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.enqueue_session(device_id="d", start_time="s2", end_time="e", duration_ms=1)
        playtime.mark_sessions_sent(["s1"])
        assert set(playtime.pending_sessions) == {"s2"}

    def test_unknown_start_is_ignored(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.mark_sessions_sent(["missing"])
        assert set(playtime.pending_sessions) == {"s1"}

    def test_dequeue_all_empties_outbox(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.enqueue_session(device_id="d", start_time="s2", end_time="e", duration_ms=1)
        playtime.mark_sessions_sent(["s1", "s2"])
        assert playtime.pending_sessions == {}


class TestRecordIngestFailure:
    def test_increments_attempts_on_named_rows(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.record_ingest_failure(["s1"])
        assert playtime.pending_sessions["s1"].attempts == 1
        playtime.record_ingest_failure(["s1"])
        assert playtime.pending_sessions["s1"].attempts == 2

    def test_preserves_other_fields(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="dev-9", start_time="s1", end_time="e9", duration_ms=42)
        playtime.record_ingest_failure(["s1"])
        row = playtime.pending_sessions["s1"]
        assert (row.device_id, row.end_time, row.duration_ms, row.attempts) == ("dev-9", "e9", 42, 1)

    def test_unknown_start_is_ignored(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.record_ingest_failure(["missing"])
        assert playtime.pending_sessions["s1"].attempts == 0

    def test_only_named_rows_are_bumped(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.enqueue_session(device_id="d", start_time="s2", end_time="e", duration_ms=1)
        playtime.record_ingest_failure(["s1"])
        assert playtime.pending_sessions["s1"].attempts == 1
        assert playtime.pending_sessions["s2"].attempts == 0


class TestQuarantineSessions:
    def test_drops_named_rows(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.enqueue_session(device_id="d", start_time="s2", end_time="e", duration_ms=1)
        playtime.quarantine_sessions(["s1"])
        assert set(playtime.pending_sessions) == {"s2"}

    def test_unknown_start_is_ignored(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.quarantine_sessions(["missing"])
        assert set(playtime.pending_sessions) == {"s1"}


class TestDropRejectedSessions:
    def test_drops_named_rows(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.enqueue_session(device_id="d", start_time="s2", end_time="e", duration_ms=1)
        playtime.drop_rejected_sessions(["s1"])
        assert set(playtime.pending_sessions) == {"s2"}

    def test_unknown_start_is_ignored(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s1", end_time="e", duration_ms=1)
        playtime.drop_rejected_sessions(["missing"])
        assert set(playtime.pending_sessions) == {"s1"}


class TestPendingPlaySessionAttempts:
    def test_defaults_to_zero(self):
        row = PendingPlaySession(device_id="d", end_time="e", duration_ms=1)
        assert row.attempts == 0

    def test_enqueue_creates_row_with_zero_attempts(self):
        playtime = Playtime()
        playtime.enqueue_session(device_id="d", start_time="s", end_time="e", duration_ms=1)
        assert playtime.pending_sessions["s"].attempts == 0


class TestReconcileTotal:
    def test_raises_total_to_larger_value(self):
        playtime = Playtime(total_seconds=100)
        playtime.reconcile_total(300)
        assert playtime.total_seconds == 300

    def test_ignores_smaller_value(self):
        playtime = Playtime(total_seconds=500)
        playtime.reconcile_total(200)
        assert playtime.total_seconds == 500

    def test_equal_value_is_a_noop(self):
        playtime = Playtime(total_seconds=250)
        playtime.reconcile_total(250)
        assert playtime.total_seconds == 250


class TestReconcileSessionCount:
    def test_raises_count_to_larger_value(self):
        playtime = Playtime(session_count=2)
        playtime.reconcile_session_count(5)
        assert playtime.session_count == 5

    def test_ignores_smaller_value(self):
        playtime = Playtime(session_count=5)
        playtime.reconcile_session_count(2)
        assert playtime.session_count == 5

    def test_equal_value_is_a_noop(self):
        playtime = Playtime(session_count=3)
        playtime.reconcile_session_count(3)
        assert playtime.session_count == 3

    def test_from_zero_adopts_server_count(self):
        playtime = Playtime()  # session_count defaults to 0
        playtime.reconcile_session_count(4)
        assert playtime.session_count == 4


class TestReconcileLastPlayed:
    def test_adopts_strictly_newer_timestamp(self):
        playtime = Playtime(last_played="2026-05-28T10:00:00Z")
        playtime.reconcile_last_played("2026-05-28T12:00:00Z")
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_ignores_older_timestamp(self):
        playtime = Playtime(last_played="2026-05-28T12:00:00Z")
        playtime.reconcile_last_played("2026-05-28T10:00:00Z")
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_equal_instant_is_a_noop(self):
        playtime = Playtime(last_played="2026-05-28T12:00:00Z")
        playtime.reconcile_last_played("2026-05-28T12:00:00Z")
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_none_incoming_is_ignored(self):
        playtime = Playtime(last_played="2026-05-28T12:00:00Z")
        playtime.reconcile_last_played(None)
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_unparseable_incoming_is_ignored(self):
        playtime = Playtime(last_played="2026-05-28T12:00:00Z")
        playtime.reconcile_last_played("not-a-date")
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_unset_local_adopts_any_parseable_incoming(self):
        playtime = Playtime()  # last_played defaults to None
        playtime.reconcile_last_played("2026-05-28T12:00:00Z")
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_unparseable_local_is_overwritten(self):
        playtime = Playtime(last_played="garbage")
        playtime.reconcile_last_played("2026-05-28T12:00:00Z")
        assert playtime.last_played == "2026-05-28T12:00:00Z"

    def test_compares_by_instant_not_lexically(self):
        """A newer instant that sorts EARLIER as a raw string is still adopted.

        current 10:00+02:00 == 08:00 UTC; incoming 09:00Z == 09:00 UTC is later,
        yet "2026-05-28T09..." < "2026-05-28T10..." as strings — a lexical compare
        would wrongly reject it. Parsing to an instant gets the order right.
        """
        playtime = Playtime(last_played="2026-05-28T10:00:00+02:00")
        playtime.reconcile_last_played("2026-05-28T09:00:00Z")
        assert playtime.last_played == "2026-05-28T09:00:00Z"

    def test_naive_aware_mismatch_keeps_current(self):
        """An uncomparable naive/aware pair keeps the current value (never regress)."""
        playtime = Playtime(last_played="2026-05-28T10:00:00")  # naive
        playtime.reconcile_last_played("2026-05-29T10:00:00Z")  # aware
        assert playtime.last_played == "2026-05-28T10:00:00"
