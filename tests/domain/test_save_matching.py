"""Tests for domain.save_sync.match_local_to_server_saves — pure function, no I/O."""

from domain.save_sync import match_local_to_server_saves


def _local(fn="pokemon.srm", path="/saves/gba/pokemon.srm"):
    return {"filename": fn, "path": path}


def _server(sid, fn="pokemon.srm", slot: str | None = "default", updated="2026-03-24T15:00:00", **kw):
    return {"id": sid, "file_name": fn, "slot": slot, "updated_at": updated, "file_size_bytes": 1024, **kw}


class TestTrackedIdMatch:
    def test_matches_by_tracked_save_id(self):
        local = [_local()]
        server = [_server(42, fn="pokemon [timestamp].srm")]
        files_state = {"pokemon.srm": {"tracked_save_id": 42}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        assert len(result.matched) == 1
        assert result.matched[0].match_method == "tracked_id"
        assert result.matched[0].server_save is not None
        assert result.matched[0].server_save["id"] == 42
        assert result.matched[0].filename == "pokemon.srm"

    def test_tracked_id_not_on_server_falls_through(self):
        local = [_local()]
        server = [_server(99, fn="pokemon.srm")]
        files_state = {"pokemon.srm": {"tracked_save_id": 42}}  # 42 not on server

        result = match_local_to_server_saves(local, server, files_state, "default")
        assert result.matched[0].match_method == "filename"
        assert result.matched[0].server_save is not None
        assert result.matched[0].server_save["id"] == 99


class TestFilenameMatch:
    def test_matches_by_filename(self):
        local = [_local()]
        server = [_server(10, fn="pokemon.srm")]
        result = match_local_to_server_saves(local, server, {}, "default")
        assert result.matched[0].match_method == "filename"

    def test_no_match_when_names_differ(self):
        local = [_local()]
        server = [_server(10, fn="other.srm")]
        result = match_local_to_server_saves(local, server, {}, "default")
        # Local file has no match, server file is server-only
        local_match = [m for m in result.matched if m.local_file is not None]
        assert local_match[0].match_method == "slot_fallback" or local_match[0].server_save is not None


class TestSlotFallback:
    def test_fallback_to_newest_in_active_slot(self):
        local = [_local()]
        server = [
            _server(10, fn="pokemon [old].srm", slot="default", updated="2026-03-24T10:00:00"),
            _server(20, fn="pokemon [new].srm", slot="default", updated="2026-03-24T15:00:00"),
        ]
        result = match_local_to_server_saves(local, server, {}, "default")
        assert result.matched[0].match_method == "slot_fallback"
        assert result.matched[0].server_save is not None
        assert result.matched[0].server_save["id"] == 20
        assert result.new_tracked_ids["pokemon.srm"] == 20

    def test_fallback_marks_all_slot_candidates_as_matched(self):
        local = [_local()]
        server = [
            _server(10, fn="a [old].srm", slot="default", updated="2026-03-24T10:00:00"),
            _server(20, fn="a [new].srm", slot="default", updated="2026-03-24T15:00:00"),
        ]
        result = match_local_to_server_saves(local, server, {}, "default")
        assert 10 in result.matched_server_ids
        assert 20 in result.matched_server_ids

    def test_no_fallback_to_different_slot(self):
        local = [_local()]
        server = [_server(10, fn="pokemon [ts].srm", slot="portable")]
        result = match_local_to_server_saves(local, server, {}, "default")
        local_match = [m for m in result.matched if m.local_file is not None]
        # Should NOT match — different slot
        assert local_match[0].server_save is None or local_match[0].match_method != "slot_fallback"


class TestServerOnly:
    def test_downloads_newest_with_local_filename(self):
        server = [
            _server(
                16,
                fn="Mario Golf [2026-03-24_15-18-50].srm",
                slot="default",
                updated="2026-03-24T15:18:50",
                file_name_no_tags="Mario Golf",
                file_extension="srm",
            ),
            _server(
                17,
                fn="Mario Golf [2026-03-24_15-19-15].srm",
                slot="default",
                updated="2026-03-24T15:19:15",
                file_name_no_tags="Mario Golf",
                file_extension="srm",
            ),
            _server(
                18,
                fn="Mario Golf [2026-03-24_15-19-26].srm",
                slot="default",
                updated="2026-03-24T15:19:26",
                file_name_no_tags="Mario Golf",
                file_extension="srm",
            ),
        ]
        result = match_local_to_server_saves([], server, {}, "default", rom_name="Mario Golf")
        server_only = [m for m in result.matched if m.match_method == "server_only"]
        assert len(server_only) == 1
        assert server_only[0].server_save is not None
        assert server_only[0].server_save["id"] == 18
        assert server_only[0].filename == "Mario Golf.srm"

    def test_all_versions_marked_as_matched(self):
        server = [
            _server(
                16,
                fn="a [old].srm",
                slot="default",
                updated="2026-03-24T10:00:00",
                file_name_no_tags="a",
                file_extension="srm",
            ),
            _server(
                18,
                fn="a [new].srm",
                slot="default",
                updated="2026-03-24T15:00:00",
                file_name_no_tags="a",
                file_extension="srm",
            ),
        ]
        result = match_local_to_server_saves([], server, {}, "default")
        assert 16 in result.matched_server_ids
        assert 18 in result.matched_server_ids


class TestOlderVersionSkipping:
    def test_older_versions_not_shown_when_tracked(self):
        """Tracked file matched to id=18 → id=16/17 must not appear as server-only."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(16, fn="pokemon [old].srm", slot="default", updated="2026-03-24T10:00:00"),
            _server(17, fn="pokemon [mid].srm", slot="default", updated="2026-03-24T12:00:00"),
            _server(18, fn="pokemon [new].srm", slot="default", updated="2026-03-24T15:00:00"),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 18}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        server_only = [m for m in result.matched if m.match_method == "server_only"]
        assert len(server_only) == 0

    def test_different_slot_filtered_out(self):
        """Save in different slot should be filtered out entirely."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(10, fn="pokemon.srm", slot="default", updated="2026-03-24T15:00:00"),
            _server(
                20,
                fn="pokemon [portable].srm",
                slot="portable",
                updated="2026-03-20T10:00:00",
                file_name_no_tags="pokemon",
                file_extension="srm",
            ),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 10}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        server_only = [m for m in result.matched if m.match_method == "server_only"]
        assert len(server_only) == 0


class TestLocalOnly:
    def test_local_file_no_server(self):
        local = [_local()]
        result = match_local_to_server_saves(local, [], {}, "default")
        assert len(result.matched) == 1
        assert result.matched[0].match_method == "local_only"
        assert result.matched[0].server_save is None

    def test_empty_both_sides(self):
        result = match_local_to_server_saves([], [], {}, "default")
        assert len(result.matched) == 0


class TestNewerSaveInSlot:
    def test_detects_newer_save_in_same_slot(self):
        """Tracked save id=18 in 'default' slot, newer save id=25 in same slot."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(18, fn="pokemon [2026-03-24].srm", slot="default", updated="2026-03-24T15:00:00"),
            _server(25, fn="pokemon [2026-03-25].srm", slot="default", updated="2026-03-25T10:00:00"),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 18}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        assert len(result.matched) == 1
        matched = result.matched[0]
        assert matched.match_method == "tracked_id"
        assert matched.server_save is not None
        assert matched.server_save["id"] == 18
        assert matched.newer_save_in_slot is not None
        assert matched.newer_save_in_slot["id"] == 25

    def test_no_flag_when_tracked_is_newest(self):
        """Only one save, it's the tracked one — newer_save_in_slot is None."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(18, fn="pokemon [2026-03-24].srm", slot="default", updated="2026-03-24T15:00:00"),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 18}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        assert result.matched[0].newer_save_in_slot is None

    def test_ignores_saves_in_different_slot(self):
        """Newer save exists but in a different slot — newer_save_in_slot is None."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(18, fn="pokemon [2026-03-24].srm", slot="default", updated="2026-03-24T15:00:00"),
            _server(25, fn="pokemon [2026-03-25].srm", slot="portable", updated="2026-03-25T10:00:00"),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 18}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        assert result.matched[0].newer_save_in_slot is None

    def test_no_flag_for_non_tracked_id_match(self):
        """No tracked_save_id in state, matches by filename — newer_save_in_slot is None."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(18, fn="pokemon.srm", slot="default", updated="2026-03-24T15:00:00"),
            _server(25, fn="pokemon [2026-03-25].srm", slot="default", updated="2026-03-25T10:00:00"),
        ]
        # No tracked_save_id in state
        files_state = {}

        result = match_local_to_server_saves(local, server, files_state, "default")
        matched = next(m for m in result.matched if m.local_file is not None)
        assert matched.match_method == "filename"
        assert matched.newer_save_in_slot is None

    def test_does_not_flag_saves_from_own_device(self):
        """Newer save has device_syncs entry showing our device is_current=True → not flagged."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(18, fn="pokemon [2026-03-24].srm", slot="default", updated="2026-03-24T15:00:00"),
            _server(
                25,
                fn="pokemon [2026-03-25].srm",
                slot="default",
                updated="2026-03-25T10:00:00",
                device_syncs=[{"device_id": "deck-001", "is_current": True}],
            ),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 18}}

        result = match_local_to_server_saves(local, server, files_state, "default", device_id="deck-001")
        assert len(result.matched) == 1
        matched = result.matched[0]
        assert matched.match_method == "tracked_id"
        assert matched.newer_save_in_slot is None

    def test_no_phantom_server_only_with_multiple_newer_in_slot(self):
        """3 saves (tracked=oldest, two newer) — no server_only entries, newer_save_in_slot = newest."""
        local = [_local(fn="pokemon.srm")]
        server = [
            _server(10, fn="pokemon [2026-03-22].srm", slot="default", updated="2026-03-22T10:00:00"),
            _server(20, fn="pokemon [2026-03-23].srm", slot="default", updated="2026-03-23T12:00:00"),
            _server(30, fn="pokemon [2026-03-24].srm", slot="default", updated="2026-03-24T15:00:00"),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 10}}

        result = match_local_to_server_saves(local, server, files_state, "default")
        server_only = [m for m in result.matched if m.match_method == "server_only"]
        assert len(server_only) == 0
        matched = result.matched[0]
        assert matched.newer_save_in_slot is not None
        assert matched.newer_save_in_slot["id"] == 30


class TestSlotFiltering:
    """Tests for active_slot filtering in match_local_to_server_saves."""

    def test_filename_match_filtered_by_active_slot(self):
        """Save with matching filename but in wrong slot should not match."""
        local = [_local()]
        server = [_server(10, fn="pokemon.srm", slot="default")]
        result = match_local_to_server_saves(local, server, {}, "other")
        assert result.matched[0].match_method == "local_only"
        assert result.matched[0].server_save is None

    def test_tracked_id_filtered_by_active_slot(self):
        """Tracked save in wrong slot should not match."""
        local = [_local()]
        server = [_server(42, fn="pokemon [ts].srm", slot="default")]
        files_state = {"pokemon.srm": {"tracked_save_id": 42}}
        result = match_local_to_server_saves(local, server, files_state, "other")
        assert result.matched[0].match_method == "local_only"
        assert result.matched[0].server_save is None

    def test_server_only_filtered_by_active_slot(self):
        """Server-only saves from other slots should not appear."""
        server = [
            _server(10, fn="pokemon.srm", slot="default", file_name_no_tags="pokemon", file_extension="srm"),
            _server(20, fn="zelda.srm", slot="portable", file_name_no_tags="zelda", file_extension="srm"),
        ]
        result = match_local_to_server_saves([], server, {}, "default")
        server_only = [m for m in result.matched if m.match_method == "server_only"]
        assert len(server_only) == 1
        assert server_only[0].server_save is not None
        assert server_only[0].server_save["id"] == 10

    def test_null_slot_server_save_visible_from_any_active_slot(self):
        """Server save with slot=None should match from any active slot (pre-slot legacy)."""
        local = [_local()]
        server = [_server(10, fn="pokemon.srm", slot=None)]
        result = match_local_to_server_saves(local, server, {}, "desktop")
        assert result.matched[0].match_method == "filename"
        assert result.matched[0].server_save is not None
        assert result.matched[0].server_save["id"] == 10

    def test_active_slot_none_only_matches_unslotted_saves(self):
        """When active_slot is None (legacy), only saves without a slot are matched."""
        local = [_local()]
        server = [_server(10, fn="pokemon.srm", slot="default")]
        result = match_local_to_server_saves(local, server, {}, None)
        assert result.matched[0].match_method == "local_only"
        assert result.matched[0].server_save is None

    def test_active_slot_none_matches_unslotted_server_save(self):
        """When active_slot is None (legacy), saves with slot=None are matched."""
        local = [_local()]
        server = [_server(10, fn="pokemon.srm", slot=None)]
        result = match_local_to_server_saves(local, server, {}, None)
        assert result.matched[0].match_method == "filename"
        assert result.matched[0].server_save is not None
        assert result.matched[0].server_save["id"] == 10

    def test_active_slot_empty_string_only_matches_unslotted_saves(self):
        """Empty string active_slot treated same as None — only unslotted saves match."""
        local = [_local()]
        server = [_server(10, fn="pokemon.srm", slot="default")]
        result = match_local_to_server_saves(local, server, {}, "")
        assert result.matched[0].match_method == "local_only"
        assert result.matched[0].server_save is None

    def test_newer_in_slot_only_from_active_slot(self):
        """Newer-in-slot detection should not flag saves from other slots."""
        local = [_local()]
        server = [
            _server(18, fn="pokemon [old].srm", slot="default", updated="2026-03-24T15:00:00"),
            _server(25, fn="pokemon [new].srm", slot="default", updated="2026-03-25T10:00:00"),
            _server(30, fn="pokemon [portable].srm", slot="portable", updated="2026-03-26T10:00:00"),
        ]
        files_state = {"pokemon.srm": {"tracked_save_id": 18}}
        result = match_local_to_server_saves(local, server, files_state, "default")
        matched = result.matched[0]
        assert matched.newer_save_in_slot is not None
        assert matched.newer_save_in_slot["id"] == 25  # from default, not 30 from portable
