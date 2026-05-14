"""Tests for domain.save_attribution — pure uploader attribution flag."""

from __future__ import annotations

from domain.save_attribution import compute_uploaded_by_us


class TestComputeUploadedByUs:
    def test_server_save_none_returns_none(self) -> None:
        """No server save record to attribute → None."""
        assert compute_uploaded_by_us(None, [1, 2, 3]) is None

    def test_own_upload_ids_none_returns_none(self) -> None:
        """Legacy ROM state lacks own_upload_ids → attribution unknown → None."""
        assert compute_uploaded_by_us({"id": 1}, None) is None

    def test_both_none_returns_none(self) -> None:
        assert compute_uploaded_by_us(None, None) is None

    def test_server_save_missing_id_returns_none(self) -> None:
        """A server-save record without an id can't be attributed."""
        assert compute_uploaded_by_us({"file_name": "x.srm"}, [1, 2, 3]) is None

    def test_server_save_id_explicitly_none_returns_none(self) -> None:
        """Explicit None ``id`` is treated identically to missing key."""
        assert compute_uploaded_by_us({"id": None}, [1, 2, 3]) is None

    def test_id_in_own_uploads_returns_true(self) -> None:
        """The server-save id matches one we POSTed → True."""
        assert compute_uploaded_by_us({"id": 2}, [1, 2, 3]) is True

    def test_id_not_in_own_uploads_returns_false(self) -> None:
        """The server-save id does not match any we POSTed → False."""
        assert compute_uploaded_by_us({"id": 99}, [1, 2, 3]) is False

    def test_empty_own_upload_ids_returns_false(self) -> None:
        """An empty list means we know we POSTed nothing → False, not None.

        The presence of the ``own_upload_ids`` field (even empty) means
        attribution is known; only ``None`` means "legacy ROM state".
        """
        assert compute_uploaded_by_us({"id": 1}, []) is False

    def test_id_zero_in_own_uploads_returns_true(self) -> None:
        """Edge: ``id`` 0 is a legitimate value and must match by membership."""
        assert compute_uploaded_by_us({"id": 0}, [0, 1]) is True
