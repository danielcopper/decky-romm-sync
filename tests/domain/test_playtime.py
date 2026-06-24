"""Unit tests for the ``Playtime`` aggregate."""

from __future__ import annotations

import pytest

from domain.playtime import Playtime, parse_playtime_note_content


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


class TestLinkNote:
    def test_link_note_sets_id(self):
        playtime = Playtime()
        playtime.link_note(42)
        assert playtime.note_id == 42


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


class TestParsePlaytimeNoteContent:
    def test_parse_valid_content(self):
        assert parse_playtime_note_content('{"seconds": 100}') == {"seconds": 100}

    def test_parse_empty(self):
        assert parse_playtime_note_content("") is None

    def test_parse_invalid_json(self):
        assert parse_playtime_note_content("not json") is None

    def test_parse_non_dict(self):
        assert parse_playtime_note_content("[1,2,3]") is None
