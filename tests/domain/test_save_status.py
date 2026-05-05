"""Tests for domain.save_status — save sync display computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain.save_status import compute_save_sync_display


class TestComputeSaveSyncDisplay:
    def test_none_input(self):
        result = compute_save_sync_display(None, None)
        assert result == {"status": "none", "label": "No saves"}

    def test_empty_files(self):
        result = compute_save_sync_display([], None)
        assert result == {"status": "none", "label": "No saves"}

    def test_has_conflict(self):
        files = [{"status": "conflict", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, None)
        assert result == {"status": "conflict", "label": "Conflict"}

    def test_has_local_files_no_last_check(self):
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, None)
        assert result == {"status": "synced", "label": "Not synced"}

    def test_synced_just_now(self):
        now = datetime.now(UTC).isoformat()
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, now)
        assert result == {"status": "synced", "label": "Just now"}

    def test_synced_minutes_ago(self):
        past = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, past)
        assert result == {"status": "synced", "label": "15m ago"}

    def test_synced_hours_ago(self):
        past = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, past)
        assert result == {"status": "synced", "label": "3h ago"}

    def test_synced_days_ago(self):
        past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, past)
        assert result == {"status": "synced", "label": "2d ago"}

    def test_files_without_local_path_or_synced(self):
        """Files that are only 'download' or 'skip' with no local_path = no local saves."""
        files = [{"status": "download", "local_path": None}]
        result = compute_save_sync_display(files, None)
        assert result == {"status": "none", "label": "No local saves"}

    def test_upload_status_counts_as_local(self):
        files = [{"status": "upload", "local_path": None}]
        result = compute_save_sync_display(files, None)
        assert result == {"status": "synced", "label": "Not synced"}

    def test_local_path_present_counts_as_local(self):
        files = [{"status": "skip", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, None)
        assert result == {"status": "synced", "label": "Not synced"}

    def test_unparseable_last_check_falls_back_to_not_synced(self):
        """Malformed timestamp -> _format_time_ago returns None -> 'Not synced' fallback."""
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, "not-a-date")
        assert result == {"status": "synced", "label": "Not synced"}

    def test_naive_timestamp_treated_as_utc(self):
        """A naive ISO timestamp (no tzinfo) is treated as UTC, not crashed on."""
        # Recent enough that we get a sane label rather than something like '20000d ago'.
        recent_naive = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, recent_naive)
        assert result["status"] == "synced"
        # Allow a small tolerance band — clock-edge flake protection.
        assert result["label"] in {"4m ago", "5m ago", "6m ago"}
