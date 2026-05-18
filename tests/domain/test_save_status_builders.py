"""Tests for domain/save_status_builders — pure file-status DTO builders."""

from domain.save_status_builders import resolve_chosen_server, status_from_action


class TestBuildersDefensiveBranches:
    """Direct coverage of the defensive fallbacks in save_status_builders."""

    def test_status_from_action_unknown_type_defaults_to_synced(self):
        """An action that matches none of Skip/Upload/Download/Conflict falls back to "synced"."""
        assert status_from_action(object()) == "synced"

    def test_resolve_chosen_server_empty_candidates_returns_none(self):
        """Skip-branch with no server candidates yields no chosen server save."""
        # A bare object is not Download/Conflict/Upload, so the function falls
        # through to the candidates check; an empty list returns None.
        assert resolve_chosen_server(object(), []) is None
