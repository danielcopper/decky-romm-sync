"""Unit tests for the ``Playtime`` aggregate."""

from __future__ import annotations

import pytest

from domain.playtime import (
    PendingPlaySession,
    Playtime,
    is_ingestable_session,
    rejected_session_indices,
)


class TestIsIngestableSession:
    def test_multi_hour_window_is_ingestable(self):
        assert is_ingestable_session("2026-07-04T10:00:00Z", "2026-07-04T11:00:00Z") is True

    def test_one_second_window_is_ingestable(self):
        assert is_ingestable_session("2026-07-04T10:00:00Z", "2026-07-04T10:00:01Z") is True

    def test_same_second_sub_ms_window_is_rejected(self):
        # The #1312 poison: start and end in the same wall-clock second.
        assert is_ingestable_session("2026-07-04T10:00:00.100Z", "2026-07-04T10:00:00.900Z") is False

    def test_identical_timestamps_are_rejected(self):
        assert is_ingestable_session("2026-07-04T10:00:00Z", "2026-07-04T10:00:00Z") is False

    def test_end_before_start_is_rejected(self):
        assert is_ingestable_session("2026-07-04T10:00:05Z", "2026-07-04T10:00:00Z") is False

    def test_sub_second_but_cross_second_boundary_is_ingestable(self):
        # 0.6s of real time, but the flooring lands on two different seconds — RomM
        # accepts it (00 < 01), so we do too (mirror RomM exactly, never over-drop).
        assert is_ingestable_session("2026-07-04T10:00:00.900Z", "2026-07-04T10:00:01.500Z") is True

    def test_fully_suspended_long_window_is_ingestable(self):
        # A multi-second window whose duration_ms would be ~0 (fully suspended) is
        # still a valid window — the kernel judges the window, not the duration.
        assert is_ingestable_session("2026-07-04T10:00:00Z", "2026-07-04T10:05:00Z") is True

    def test_unparseable_start_is_not_ingestable(self):
        assert is_ingestable_session("not-a-date", "2026-07-04T10:00:01Z") is False

    def test_unparseable_end_is_not_ingestable(self):
        assert is_ingestable_session("2026-07-04T10:00:00Z", "garbage") is False

    def test_naive_aware_mismatch_is_not_ingestable(self):
        assert is_ingestable_session("2026-07-04T10:00:00", "2026-07-04T11:00:00Z") is False

    def test_empty_strings_are_not_ingestable(self):
        assert is_ingestable_session("", "") is False


class TestRejectedSessionIndices:
    def _detail(self, *indices: int) -> list[dict[str, object]]:
        return [{"loc": ["body", "sessions", i], "msg": "end_time must be after start_time"} for i in indices]

    def test_extracts_the_flagged_indices_sorted(self):
        assert rejected_session_indices(self._detail(5, 2), 10) == [2, 5]

    def test_single_index(self):
        assert rejected_session_indices(self._detail(2), 10) == [2]

    def test_dedupes_repeated_indices(self):
        assert rejected_session_indices(self._detail(2, 2, 2), 10) == [2]

    def test_out_of_range_indices_are_dropped(self):
        assert rejected_session_indices(self._detail(0, 99, -1), 3) == [0]

    def test_field_level_loc_still_extracts_the_item_index(self):
        detail = [{"loc": ["body", "sessions", 4, "end_time"], "msg": "bad"}]
        assert rejected_session_indices(detail, 10) == [4]

    def test_none_detail_yields_empty(self):
        assert rejected_session_indices(None, 10) == []

    def test_non_list_detail_yields_empty(self):
        assert rejected_session_indices({"detail": "oops"}, 10) == []

    def test_empty_detail_yields_empty(self):
        assert rejected_session_indices([], 10) == []

    def test_loc_without_sessions_segment_is_ignored(self):
        detail = [{"loc": ["body", "device_id"], "msg": "bad"}]
        assert rejected_session_indices(detail, 10) == []

    def test_non_int_index_is_ignored(self):
        detail = [{"loc": ["body", "sessions", "two"], "msg": "bad"}]
        assert rejected_session_indices(detail, 10) == []

    def test_bool_index_is_excluded(self):
        # bool is an int subclass — True must NOT be read as index 1.
        detail = [{"loc": ["body", "sessions", True], "msg": "bad"}]
        assert rejected_session_indices(detail, 10) == []

    def test_non_dict_items_are_skipped(self):
        detail = ["oops", None, {"loc": ["body", "sessions", 1]}]
        assert rejected_session_indices(detail, 10) == [1]

    def test_missing_loc_is_skipped(self):
        detail = [{"msg": "no loc here"}, {"loc": ["body", "sessions", 3]}]
        assert rejected_session_indices(detail, 10) == [3]


class TestBeginSession:
    def test_begin_session_sets_start_and_monotonic(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=123.5)
        assert playtime.last_session_start == "2026-05-28T10:00:00"
        assert playtime.last_session_start_monotonic == 123.5


class TestRecordSession:
    def test_monotonic_delta_is_the_counted_duration(self):
        """Wall span 13min but only 3min awake (monotonic) → 180s counted (#1148)."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=1000.0)
        # 13 min of wall elapsed, but the monotonic clock advanced only 3 min —
        # 10 min suspended (the monotonic clock paused).
        playtime.record_session("2026-05-28T10:13:00", monotonic_end=1180.0)
        assert playtime.last_session_duration_sec == 180
        assert playtime.total_seconds == 180
        assert playtime.session_count == 1
        assert playtime.last_session_start is None
        assert playtime.last_session_start_monotonic is None

    def test_no_suspend_counts_full_span(self):
        """Monotonic delta == wall span (no suspend) → the full span counts."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=1000.0)
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=4600.0)  # +3600 both clocks
        assert playtime.last_session_duration_sec == 3600
        assert playtime.total_seconds == 3600
        assert playtime.session_count == 1

    def test_stamps_last_played_with_ended_at(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=3600.0)
        assert playtime.last_played == "2026-05-28T11:00:00"

    def test_second_cycle_advances_last_played(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=3600.0)
        playtime.begin_session("2026-05-28T12:00:00", monotonic=10000.0)
        playtime.record_session("2026-05-28T12:30:00", monotonic_end=11800.0)
        assert playtime.last_played == "2026-05-28T12:30:00"

    def test_two_cycles_accumulate(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=3600.0)
        playtime.begin_session("2026-05-28T12:00:00", monotonic=10000.0)
        playtime.record_session("2026-05-28T12:30:00", monotonic_end=11800.0)
        assert playtime.total_seconds == 3600 + 1800
        assert playtime.session_count == 2
        assert playtime.last_session_duration_sec == 1800

    def test_no_open_session_raises(self):
        playtime = Playtime()
        with pytest.raises(ValueError, match="no open session to record"):
            playtime.record_session("2026-05-28T11:00:00", monotonic_end=0.0)

    def test_unparseable_start_raises(self):
        playtime = Playtime()
        playtime.begin_session("not-a-date", monotonic=0.0)
        with pytest.raises(ValueError, match="unparseable session timestamps"):
            playtime.record_session("2026-05-28T11:00:00", monotonic_end=10.0)

    def test_upper_clamp_caps_at_24h(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        # 48h wall, 48h monotonic (no suspend) → capped at 24h.
        playtime.record_session("2026-05-30T10:00:00", monotonic_end=172_800.0)
        assert playtime.last_session_duration_sec == 86400
        assert playtime.total_seconds == 86400
        assert playtime.session_count == 1

    def test_lower_clamp_end_before_start(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T11:00:00", monotonic=100.0)
        playtime.record_session("2026-05-28T10:00:00", monotonic_end=100.0)
        assert playtime.last_session_duration_sec == 0
        assert playtime.total_seconds == 0
        assert playtime.session_count == 1
        assert playtime.last_session_start is None

    def test_mixed_naive_aware_timestamps_raise(self):
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)  # naive
        with pytest.raises(ValueError, match="inconsistent session timestamps"):
            playtime.record_session("2026-05-28T11:00:00Z", monotonic_end=3600.0)  # aware (Z -> +00:00)

    def test_fully_suspended_session_counts_zero(self):
        """A session that was suspended the entire time (monotonic did not advance) counts 0."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=500.0)
        # 1h wall elapsed but the monotonic clock never moved (asleep throughout).
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=500.0)
        assert playtime.last_session_duration_sec == 0
        assert playtime.total_seconds == 0
        assert playtime.session_count == 1


class TestRecordSessionMonotonicFallback:
    """When the monotonic delta is unusable, the counted span falls back to wall (pre-#1148 behavior)."""

    def test_absent_monotonic_start_falls_back_to_wall(self):
        """A constructor-seeded / pre-migration row (no monotonic start) counts the full wall span."""
        playtime = Playtime(last_session_start="2026-05-28T10:00:00")  # last_session_start_monotonic is None
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=999.0)
        assert playtime.last_session_duration_sec == 3600  # full wall span; monotonic_end ignored
        assert playtime.total_seconds == 3600

    def test_negative_delta_reboot_falls_back_to_wall(self):
        """A monotonic counter reset mid-session (reboot) yields a negative delta → wall fallback."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=5000.0)
        # The counter reset lower after a reboot, so end < start.
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=10.0)
        assert playtime.last_session_duration_sec == 3600  # wall span, not the negative garbage

    def test_delta_far_above_wall_falls_back_to_wall(self):
        """A monotonic delta more than the tolerance above the wall span is untrusted → wall fallback."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        # Awake can never exceed elapsed: a 2h monotonic delta over a 1h wall span
        # cannot belong to this session, so the wall span is used.
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=7200.0)
        assert playtime.last_session_duration_sec == 3600

    def test_delta_within_tolerance_above_wall_is_clamped_to_wall(self):
        """A monotonic delta a hair above wall (read jitter) is clamped to the wall span, never over-counts."""
        playtime = Playtime()
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        # 3600s wall, 3601.5s monotonic (within the 2s tolerance) → clamped to 3600.
        playtime.record_session("2026-05-28T11:00:00", monotonic_end=3601.5)
        assert playtime.last_session_duration_sec == 3600

    def test_wall_fallback_still_respects_24h_cap(self):
        """The 24h cap still applies when the monotonic delta is discarded and wall is used."""
        playtime = Playtime(last_session_start="2026-05-28T10:00:00")  # no monotonic start → wall fallback
        # 25h wall span, capped at 24h.
        playtime.record_session("2026-05-29T11:00:00", monotonic_end=0.0)
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
        playtime.begin_session("2026-05-28T10:00:00", monotonic=0.0)
        playtime.record_session("2026-05-28T10:30:00", monotonic_end=1800.0)
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
