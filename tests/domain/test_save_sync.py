"""Unit tests for domain.save_sync — v4.7-aware sync action logic."""

from __future__ import annotations

from domain.save_sync import check_server_changed_v47, determine_sync_action

# ---------------------------------------------------------------------------
# TestCheckServerChangedV47
# ---------------------------------------------------------------------------


class TestCheckServerChangedV47:
    def test_none_input_returns_none(self):
        assert check_server_changed_v47(None) is None

    def test_is_current_true_returns_false(self):
        assert check_server_changed_v47({"is_current": True}) is False

    def test_is_current_false_returns_true(self):
        assert check_server_changed_v47({"is_current": False}) is True

    def test_missing_is_current_key_returns_none(self):
        """A device_sync_info dict without is_current is treated as indeterminate."""
        assert check_server_changed_v47({}) is None

    def test_is_current_none_value_returns_none(self):
        """is_current=None is treated as indeterminate."""
        assert check_server_changed_v47({"is_current": None}) is None


# ---------------------------------------------------------------------------
# TestDetermineSyncAction
# ---------------------------------------------------------------------------


class TestDetermineSyncAction:
    # --- No server save ---

    def test_no_server_save_not_changed_skips(self):
        result = determine_sync_action(local_changed=False, server_save=None)
        assert result == "skip"

    def test_no_server_save_local_changed_initial_upload(self):
        result = determine_sync_action(local_changed=True, server_save=None)
        assert result == "initial_upload"

    # --- Server save exists, v4.7 device_sync_info ---

    def test_server_exists_neither_changed_skips(self):
        server_save = {"id": 1, "updated_at": "2026-02-17T06:00:00Z", "file_size_bytes": 1024}
        device_sync_info = {"is_current": True}
        result = determine_sync_action(
            local_changed=False,
            server_save=server_save,
            device_sync_info=device_sync_info,
        )
        assert result == "skip"

    def test_only_local_changed_uploads(self):
        server_save = {"id": 1, "updated_at": "2026-02-17T06:00:00Z", "file_size_bytes": 1024}
        device_sync_info = {"is_current": True}
        result = determine_sync_action(
            local_changed=True,
            server_save=server_save,
            device_sync_info=device_sync_info,
        )
        assert result == "upload"

    def test_only_server_changed_v47_downloads(self):
        server_save = {"id": 1, "updated_at": "2026-02-17T08:00:00Z", "file_size_bytes": 2048}
        device_sync_info = {"is_current": False}
        result = determine_sync_action(
            local_changed=False,
            server_save=server_save,
            device_sync_info=device_sync_info,
        )
        assert result == "download"

    def test_both_changed_v47_conflicts(self):
        server_save = {"id": 1, "updated_at": "2026-02-17T08:00:00Z", "file_size_bytes": 2048}
        device_sync_info = {"is_current": False}
        result = determine_sync_action(
            local_changed=True,
            server_save=server_save,
            device_sync_info=device_sync_info,
        )
        assert result == "conflict"

    # --- Safe default: server_save exists, no device_sync_info, no file_state ---

    def test_server_exists_no_sync_info_no_file_state_assumes_server_changed(self):
        """No device_sync_info and no file_state → cannot determine server state.
        Safe default: assume server changed. Local not changed → download."""
        server_save = {"id": 1, "updated_at": "2026-02-17T06:00:00Z", "file_size_bytes": 1024}
        result = determine_sync_action(
            local_changed=False,
            server_save=server_save,
            device_sync_info=None,
            file_state=None,
        )
        assert result == "download"

    def test_server_exists_no_sync_info_no_file_state_local_also_changed_conflicts(self):
        """No device_sync_info and no file_state → assume server changed.
        Local also changed → conflict."""
        server_save = {"id": 1, "updated_at": "2026-02-17T06:00:00Z", "file_size_bytes": 1024}
        result = determine_sync_action(
            local_changed=True,
            server_save=server_save,
            device_sync_info=None,
            file_state=None,
        )
        assert result == "conflict"
