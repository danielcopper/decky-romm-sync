"""Tests for domain.platform_prefs — the per-platform "all enabled" defaulting.

Hand-enumerated cases for the pure kernel shared by the display path
(``get_platforms``) and the filter path (``_fetch_enabled_platforms``).
The property-based companion lives in ``test_platform_prefs_property.py``.
"""

from __future__ import annotations

from domain.platform_prefs import materialize_enabled_platforms, resolve_sync_enabled


class TestResolveSyncEnabled:
    """resolve_sync_enabled — absent-id default depends on whether prefs exist."""

    def test_absent_id_defaults_true_when_map_empty(self):
        assert resolve_sync_enabled({}, "1") is True

    def test_absent_id_defaults_false_when_map_non_empty(self):
        # The #1007 bug condition: one explicit entry means every absent id is
        # disabled — so a complete map can be read literally.
        assert resolve_sync_enabled({"1": True}, "2") is False

    def test_present_id_true_is_returned(self):
        assert resolve_sync_enabled({"1": True}, "1") is True

    def test_present_id_false_is_returned(self):
        assert resolve_sync_enabled({"1": False}, "1") is False

    def test_present_false_id_returned_even_when_others_present(self):
        assert resolve_sync_enabled({"1": False, "2": True}, "1") is False


class TestMaterializeEnabledPlatforms:
    """materialize_enabled_platforms — empty-map sentinel → explicit all-True map."""

    def test_empty_map_with_ids_yields_full_all_true_map(self):
        assert materialize_enabled_platforms({}, ["1", "2", "3"]) == {
            "1": True,
            "2": True,
            "3": True,
        }

    def test_non_empty_map_yields_none(self):
        # Already holds explicit prefs — nothing to materialize.
        assert materialize_enabled_platforms({"1": False}, ["1", "2"]) is None

    def test_empty_map_with_no_ids_yields_none(self):
        # No platforms to enumerate (e.g. server returned an empty / all-empty
        # library) — leave the sentinel untouched so the safety floor survives.
        assert materialize_enabled_platforms({}, []) is None

    def test_non_empty_map_with_no_ids_yields_none(self):
        assert materialize_enabled_platforms({"1": True}, []) is None

    def test_single_id_empty_map_yields_single_true(self):
        assert materialize_enabled_platforms({}, ["42"]) == {"42": True}
