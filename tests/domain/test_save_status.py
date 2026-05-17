"""Tests for domain.save_status — save sync display computation."""

from __future__ import annotations

from domain.save_status import SaveSyncDisplay, compute_save_sync_display


class TestComputeSaveSyncDisplay:
    def test_none_input(self):
        result = compute_save_sync_display(None, None)
        assert result == SaveSyncDisplay(status="none", label="No saves", last_sync_check_at=None)

    def test_empty_files(self):
        result = compute_save_sync_display([], None)
        assert result == SaveSyncDisplay(status="none", label="No saves", last_sync_check_at=None)

    def test_has_conflict(self):
        files = [{"status": "conflict", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, None)
        assert result == SaveSyncDisplay(status="conflict", label="Conflict", last_sync_check_at=None)

    def test_has_local_files_no_last_check(self):
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, None)
        assert result == SaveSyncDisplay(status="synced", label="Not synced", last_sync_check_at=None)

    def test_synced_with_last_check_passes_through_timestamp(self):
        """Time-relative formatting is the frontend's job — backend passes the timestamp through."""
        iso = "2026-02-17T10:31:00+00:00"
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, iso)
        assert result == SaveSyncDisplay(status="synced", label=None, last_sync_check_at=iso)

    def test_synced_with_naive_timestamp_passes_through(self):
        """Naive (no-tz) timestamps pass through verbatim — frontend handles parsing."""
        iso = "2026-02-17T10:31:00"
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, iso)
        assert result == SaveSyncDisplay(status="synced", label=None, last_sync_check_at=iso)

    def test_files_without_local_path_or_synced(self):
        """Files that are only 'download' or 'skip' with no local_path = no local saves."""
        files = [{"status": "download", "local_path": None}]
        result = compute_save_sync_display(files, None)
        assert result == SaveSyncDisplay(status="none", label="No local saves", last_sync_check_at=None)

    def test_upload_status_counts_as_local(self):
        files = [{"status": "upload", "local_path": None}]
        result = compute_save_sync_display(files, None)
        assert result == SaveSyncDisplay(status="synced", label="Not synced", last_sync_check_at=None)

    def test_local_path_present_counts_as_local(self):
        files = [{"status": "skip", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, None)
        assert result == SaveSyncDisplay(status="synced", label="Not synced", last_sync_check_at=None)

    def test_malformed_timestamp_passes_through_unchanged(self):
        """Backend no longer parses the timestamp; an unparseable value is shipped as-is."""
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, "not-a-date")
        assert result == SaveSyncDisplay(status="synced", label=None, last_sync_check_at="not-a-date")

    def test_server_query_failed_overrides_everything(self):
        """server_query_failed=True collapses to 'Server unreachable' regardless of files."""
        files = [{"status": "synced", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, "2026-01-01T00:00:00Z", server_query_failed=True)
        assert result == SaveSyncDisplay(status="none", label="Server unreachable", last_sync_check_at=None)

    def test_server_query_failed_with_empty_files(self):
        """server_query_failed=True also wins over the empty-files branch."""
        result = compute_save_sync_display([], None, server_query_failed=True)
        assert result == SaveSyncDisplay(status="none", label="Server unreachable", last_sync_check_at=None)

    def test_server_query_failed_default_false_preserves_legacy(self):
        """Default value (False) preserves the pre-fix behavior."""
        result = compute_save_sync_display(None, None)
        assert result == SaveSyncDisplay(status="none", label="No saves", last_sync_check_at=None)
