"""Tests for domain.save_status — save sync display computation."""

from __future__ import annotations

from domain.save_status import (
    MultiFileSlot,
    SaveSyncDisplay,
    compute_multi_file_slot,
    compute_save_sync_display,
)


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

    def test_upload_status_is_pending_local_changes(self):
        """An upload-pending file counts as local but is NOT synced — it reports
        pending/'Local changes' so a failed post-exit upload can't read as synced (#1334)."""
        files = [{"status": "upload", "local_path": None}]
        result = compute_save_sync_display(files, None)
        assert result == SaveSyncDisplay(status="pending", label="Local changes", last_sync_check_at=None)

    def test_upload_does_not_collapse_to_synced_even_with_recent_check(self):
        """The pre-#1334 bug: a local file pending upload + a recent sync check
        collapsed to a green 'synced'. It must now report pending/'Local changes'."""
        files = [{"status": "upload", "local_path": "/saves/test.srm"}]
        result = compute_save_sync_display(files, "2026-02-17T10:31:00+00:00")
        assert result == SaveSyncDisplay(status="pending", label="Local changes", last_sync_check_at=None)

    def test_download_with_local_is_pending_server_newer(self):
        """A slot with local saves where the server is newer reports pending/'Server newer',
        not a green 'synced' (#1334)."""
        files = [
            {"status": "synced", "local_path": "/saves/a.srm"},
            {"status": "download", "local_path": "/saves/b.srm"},
        ]
        result = compute_save_sync_display(files, "2026-02-17T10:31:00+00:00")
        assert result == SaveSyncDisplay(status="pending", label="Server newer", last_sync_check_at=None)

    def test_upload_wins_over_download_in_mixed_slot(self):
        """When both an upload and a download are pending, 'Local changes' takes precedence."""
        files = [
            {"status": "upload", "local_path": "/saves/a.srm"},
            {"status": "download", "local_path": "/saves/b.srm"},
        ]
        result = compute_save_sync_display(files, "2026-02-17T10:31:00+00:00")
        assert result == SaveSyncDisplay(status="pending", label="Local changes", last_sync_check_at=None)

    def test_display_statuses_enumerated(self):
        """Every display status the frontend must handle, driven from representative inputs."""
        cases = {
            "none": compute_save_sync_display([], None),
            "conflict": compute_save_sync_display([{"status": "conflict", "local_path": "/s/a.srm"}], None),
            "pending": compute_save_sync_display([{"status": "upload", "local_path": "/s/a.srm"}], None),
            "synced": compute_save_sync_display([{"status": "synced", "local_path": "/s/a.srm"}], None),
        }
        assert {expected: got.status for expected, got in cases.items()} == {
            "none": "none",
            "conflict": "conflict",
            "pending": "pending",
            "synced": "synced",
        }

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


class TestComputeMultiFileSlot:
    """compute_multi_file_slot — single vs. multi-file detection from target filenames."""

    def test_empty_is_single_file(self):
        """No files (e.g. ROM not installed / empty slot) is not multi-file."""
        result = compute_multi_file_slot([])
        assert result == MultiFileSlot(is_multi_file=False, component_files=[])

    def test_single_file_is_not_multi_file(self):
        result = compute_multi_file_slot(["pokemon.srm"])
        assert result == MultiFileSlot(is_multi_file=False, component_files=["pokemon.srm"])

    def test_multiple_distinct_files_is_multi_file(self):
        """Saturn cartridge save: .bkr + .bcr + .smpc = one game state across three files."""
        result = compute_multi_file_slot(["rally.bkr", "rally.bcr", "rally.smpc"])
        assert result.is_multi_file is True
        # Sorted set of the component filenames.
        assert result.component_files == ["rally.bcr", "rally.bkr", "rally.smpc"]

    def test_duplicate_filenames_collapse_to_single(self):
        """Repeated identical filenames are one distinct file, not multi-file."""
        result = compute_multi_file_slot(["pokemon.srm", "pokemon.srm"])
        assert result == MultiFileSlot(is_multi_file=False, component_files=["pokemon.srm"])

    def test_component_files_are_deduped_and_sorted(self):
        result = compute_multi_file_slot(["b.sav", "a.srm", "b.sav", "a.srm"])
        assert result.is_multi_file is True
        assert result.component_files == ["a.srm", "b.sav"]
