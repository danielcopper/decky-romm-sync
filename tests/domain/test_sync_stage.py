"""Tests for the SyncStage enum — the on-the-wire stage vocabulary."""

import pytest

from domain.sync_stage import SyncStage


class TestSyncStageWireValues:
    """Member values must equal the exact strings the frontend expects."""

    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            (SyncStage.DISCOVERING, "discovering"),
            (SyncStage.FETCHING, "fetching"),
            (SyncStage.APPLYING, "applying"),
            (SyncStage.FINALIZING, "finalizing"),
            (SyncStage.DONE, "done"),
            (SyncStage.CANCELLED, "cancelled"),
            (SyncStage.ERROR, "error"),
        ],
    )
    def test_member_value(self, member, expected):
        assert member.value == expected

    def test_str_mixin_serializes_to_value(self):
        """``str``-based members coerce straight onto the wire dict."""
        assert SyncStage(SyncStage.APPLYING).value == "applying"
        assert SyncStage("done") is SyncStage.DONE

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError):
            SyncStage("roms")
