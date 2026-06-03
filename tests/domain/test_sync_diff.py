"""Tests for domain.sync_diff — pure delta computations for the sync engine."""

from typing import Any

from domain.sync_diff import (
    ClassificationResult,
    classify_roms,
    compute_collection_diff,
    compute_platform_collection_diff,
    should_include_in_platform_collection,
)


def _make_sd(
    rom_id,
    name="Game",
    platform_name="N64",
    platform_slug="n64",
    fs_name="game.z64",
    igdb_id=None,
    sgdb_id=None,
):
    """Build a shortcut_data dict matching build_shortcuts_data output."""
    return {
        "rom_id": rom_id,
        "name": name,
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "fs_name": fs_name,
        "igdb_id": igdb_id,
        "sgdb_id": sgdb_id,
    }


def _reg(name="Game", platform_name="N64", platform_slug="n64", fs_name="game.z64", app_id=1001):
    """Build a registry entry dict matching build_registry_entry output."""
    return {
        "app_id": app_id,
        "name": name,
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "fs_name": fs_name,
    }


class TestClassifyRoms:
    """classify_roms() — bucketing fetched ROMs against the saved registry."""

    def test_all_new_empty_registry(self):
        sd = [_make_sd(1, "Game A"), _make_sd(2, "Game B")]
        new, changed, unchanged_ids, stale, disabled = classify_roms(sd, {}, {"N64"})
        assert len(new) == 2
        assert changed == []
        assert unchanged_ids == []
        assert stale == []
        assert disabled == 0

    def test_all_unchanged(self):
        registry = {
            "1": _reg(name="Game A", fs_name="gamea.z64", app_id=1001),
            "2": _reg(name="Game B", fs_name="gameb.z64", app_id=1002),
        }
        sd = [
            _make_sd(1, "Game A", fs_name="gamea.z64"),
            _make_sd(2, "Game B", fs_name="gameb.z64"),
        ]
        new, changed, unchanged_ids, stale, _ = classify_roms(sd, registry, {"N64"})
        assert new == []
        assert changed == []
        assert set(unchanged_ids) == {1, 2}
        assert stale == []

    def test_mixed_new_changed_unchanged(self):
        registry = {
            "1": _reg(name="Game A", fs_name="gamea.z64", app_id=1001),
            "2": _reg(name="Old Name", fs_name="gameb.z64", app_id=1002),
        }
        sd = [
            _make_sd(1, "Game A", fs_name="gamea.z64"),  # unchanged
            _make_sd(2, "New Name", fs_name="gameb.z64"),  # changed (name)
            _make_sd(3, "Game C", fs_name="gamec.z64"),  # new
        ]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert len(new) == 1
        assert new[0]["rom_id"] == 3
        assert len(changed) == 1
        assert changed[0]["rom_id"] == 2
        assert changed[0]["existing_app_id"] == 1002
        assert unchanged_ids == [1]

    def test_stale_detection(self):
        registry = {
            "1": _reg(name="Game A", fs_name="", platform_slug="n64", app_id=1001),
            "99": {"app_id": 1099, "name": "Deleted Game", "platform_name": "N64"},
        }
        sd = [_make_sd(1, "Game A", fs_name="")]
        _, _, _, stale, disabled = classify_roms(sd, registry, {"N64"})
        assert 99 in stale
        assert disabled == 0  # N64 is in fetched_platform_names

    def test_disabled_platform_stale_count(self):
        registry = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "SNES"},
        }
        sd: list[dict[str, Any]] = []  # nothing fetched
        _, _, _, stale, disabled = classify_roms(sd, registry, {"N64"})
        assert 1 in stale
        assert disabled == 1  # SNES not in {"N64"}

    def test_name_change_detected(self):
        registry = {
            "1": _reg(name="Old Title", app_id=1001),
        }
        sd = [_make_sd(1, "New Title")]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert len(changed) == 1
        assert changed[0]["existing_app_id"] == 1001
        assert new == []
        assert unchanged_ids == []

    def test_platform_name_change_detected(self):
        registry = {
            "1": _reg(name="Game A", platform_name="Nintendo 64", app_id=1001),
        }
        sd = [_make_sd(1, "Game A", platform_name="N64")]
        _, changed, _, _, _ = classify_roms(sd, registry, {"N64"})
        assert len(changed) == 1
        assert changed[0]["rom_id"] == 1

    def test_fs_name_change_detected(self):
        registry = {
            "1": _reg(name="Game A", fs_name="old.z64", app_id=1001),
        }
        sd = [_make_sd(1, "Game A", fs_name="new.z64")]
        _, changed, _, _, _ = classify_roms(sd, registry, {"N64"})
        assert len(changed) == 1
        assert changed[0]["rom_id"] == 1

    def test_igdb_id_change_no_false_positive(self):
        registry = {
            "1": _reg(name="Game A", app_id=1001),
        }
        sd = [_make_sd(1, "Game A", igdb_id=999, sgdb_id=888)]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert unchanged_ids == [1]
        assert changed == []
        assert new == []

    def test_registry_without_app_id_is_new(self):
        registry = {
            "1": {"name": "Game A", "platform_name": "N64"},
        }
        sd = [_make_sd(1, "Game A")]
        new, changed, _, _, _ = classify_roms(sd, registry, {"N64"})
        assert len(new) == 1
        assert new[0]["rom_id"] == 1
        assert changed == []

    def test_first_sync_empty_registry_all_new(self):
        sd = [_make_sd(i, f"Game {i}") for i in range(1, 6)]
        new, changed, unchanged_ids, stale, disabled = classify_roms(sd, {}, {"N64"})
        assert len(new) == 5
        assert changed == []
        assert unchanged_ids == []
        assert stale == []
        assert disabled == 0

    def test_no_changes(self):
        registry = {
            "1": _reg(name="Game A", app_id=1001),
        }
        sd = [_make_sd(1, "Game A")]
        new, changed, unchanged_ids, stale, _ = classify_roms(sd, registry, {"N64"})
        assert len(new) == 0
        assert len(changed) == 0
        assert len(stale) == 0
        assert len(unchanged_ids) == 1

    def test_all_stale_disabled_platforms(self):
        registry = {
            "1": {"app_id": 1001, "name": "Game A", "platform_name": "GBA"},
            "2": {"app_id": 1002, "name": "Game B", "platform_name": "SNES"},
        }
        sd: list[dict[str, Any]] = []
        _, _, _, stale, disabled = classify_roms(sd, registry, {"N64"})
        assert len(stale) == 2
        assert disabled == 2

    def test_returns_classification_result_namedtuple(self):
        """Result supports both positional unpacking and attribute access."""
        result = classify_roms([_make_sd(1)], {}, {"N64"})
        assert isinstance(result, ClassificationResult)
        # Attribute access
        assert result.new[0]["rom_id"] == 1
        assert result.changed == []
        assert result.unchanged_ids == []
        assert result.stale == []
        assert result.disabled_count == 0
        # Positional unpacking still works
        new, changed, unchanged_ids, stale, disabled = result
        assert new == result.new
        assert changed == result.changed
        assert unchanged_ids == result.unchanged_ids
        assert stale == result.stale
        assert disabled == result.disabled_count

    def test_does_not_mutate_input_shortcuts_data(self):
        """Changed ROMs are returned as fresh dicts; caller's input is untouched."""
        registry = {
            "1": _reg(name="Old Title", app_id=1001),
        }
        sd_item = _make_sd(1, "New Title")
        sd_snapshot = dict(sd_item)
        sd = [sd_item]
        _, changed, _, _, _ = classify_roms(sd, registry, {"N64"})
        # Caller's dict is unchanged — no existing_app_id leaked in
        assert sd_item == sd_snapshot
        assert "existing_app_id" not in sd_item
        # The returned changed entry does carry existing_app_id
        assert changed[0]["existing_app_id"] == 1001


class TestComputeCollectionDiff:
    """compute_collection_diff() — diff enabled collections vs last-synced set."""

    def test_first_sync_with_collections_has_changes(self):
        result = compute_collection_diff({"Favorites": [1, 2]}, [])
        assert result["has_changes"] is True
        assert result["added"] == ["Favorites"]
        assert result["removed"] == []

    def test_empty_current_and_previous_no_changes(self):
        result = compute_collection_diff({}, [])
        assert result["has_changes"] is False
        assert result["added"] == []
        assert result["removed"] == []

    def test_added_collection_detected(self):
        result = compute_collection_diff({"Favorites": [1], "RPG": [2]}, ["Favorites"])
        assert result["has_changes"] is True
        assert result["added"] == ["RPG"]
        assert result["removed"] == []

    def test_removed_collection_detected(self):
        result = compute_collection_diff({"Favorites": [1]}, ["Favorites", "RPG"])
        assert result["has_changes"] is True
        assert result["added"] == []
        assert result["removed"] == ["RPG"]

    def test_unchanged_collections_still_has_changes_when_current_nonempty(self):
        """has_changes is True even with no add/remove if current is non-empty."""
        result = compute_collection_diff({"Favorites": [1]}, ["Favorites"])
        assert result["has_changes"] is True
        assert result["added"] == []
        assert result["removed"] == []

    def test_added_and_removed_sorted(self):
        result = compute_collection_diff(
            {"Zelda": [1], "Mario": [2], "Pokemon": [3]},
            ["Sonic", "Kirby"],
        )
        assert result["added"] == ["Mario", "Pokemon", "Zelda"]
        assert result["removed"] == ["Kirby", "Sonic"]


class TestShouldIncludeInPlatformCollection:
    """should_include_in_platform_collection() — toggle-aware membership predicate."""

    def test_sc5b_should_include_helper_excludes_collection_only_rom(self):
        """Returns False for collection-only ROM when toggle is OFF."""
        platform_rom_ids = {1, 2}  # ROM 3 is collection-only
        assert should_include_in_platform_collection(1, platform_rom_ids, False) is True
        assert should_include_in_platform_collection(3, platform_rom_ids, False) is False

    def test_sc5b_should_include_helper_includes_all_when_toggle_on(self):
        """Returns True for all ROMs when toggle is ON."""
        platform_rom_ids = {1, 2}
        assert should_include_in_platform_collection(1, platform_rom_ids, True) is True
        assert should_include_in_platform_collection(3, platform_rom_ids, True) is True

    def test_sc5b_should_include_helper_excludes_all_when_no_platforms_enabled(self):
        """Empty set = no platforms enabled -> exclude all (toggle OFF)."""
        assert should_include_in_platform_collection(1, set(), False) is False

    def test_sc5b_should_include_helper_includes_all_when_no_tracking_data(self):
        """None = legacy sync without platform tracking -> include all."""
        assert should_include_in_platform_collection(1, None, False) is True

    def test_sc5b_should_include_helper_includes_all_empty_set_when_toggle_on(self):
        """Empty set + toggle ON -> include all."""
        assert should_include_in_platform_collection(1, set(), True) is True

    def test_should_include_helper_includes_all_when_none_and_toggle_on(self):
        """None + toggle ON -> include all."""
        assert should_include_in_platform_collection(1, None, True) is True


class TestComputePlatformCollectionDiff:
    """compute_platform_collection_diff() — diff future platform groups vs last-synced."""

    def test_first_sync_adds_all_platforms(self):
        sd = [
            _make_sd(1, platform_name="Game Boy Advance"),
            _make_sd(2, platform_name="Nintendo 64"),
        ]
        result = compute_platform_collection_diff(sd, {1, 2}, [], False)
        assert result["has_changes"] is True
        assert result["added_count"] == 2
        assert result["removed_count"] == 0

    def test_no_changes_when_platforms_match_last_sync(self):
        sd = [_make_sd(1, platform_name="Game Boy Advance")]
        result = compute_platform_collection_diff(sd, {1}, ["Game Boy Advance"], False)
        assert result["has_changes"] is False
        assert result["added_count"] == 0
        assert result["removed_count"] == 0

    def test_removed_platform_detected(self):
        sd = [_make_sd(1, platform_name="Game Boy Advance")]
        result = compute_platform_collection_diff(
            sd,
            {1},
            ["Game Boy Advance", "Nintendo 64"],
            False,
        )
        assert result["has_changes"] is True
        assert result["added_count"] == 0
        assert result["removed_count"] == 1

    def test_collection_only_rom_excluded_when_toggle_off(self):
        """ROM not in platform_rom_ids doesn't contribute its platform when toggle is OFF."""
        sd = [
            _make_sd(1, platform_name="Game Boy Advance"),
            _make_sd(2, platform_name="PlayStation"),  # collection-only
        ]
        result = compute_platform_collection_diff(sd, {1}, [], False)
        # Only GBA gets added; PSX is filtered out
        assert result["added_count"] == 1

    def test_collection_only_rom_included_when_toggle_on(self):
        """create_platform_groups=True forces every ROM's platform into the diff."""
        sd = [
            _make_sd(1, platform_name="Game Boy Advance"),
            _make_sd(2, platform_name="PlayStation"),  # collection-only
        ]
        result = compute_platform_collection_diff(sd, {1}, [], True)
        # Both platforms qualify
        assert result["added_count"] == 2

    def test_none_platform_rom_ids_treats_all_as_qualifying(self):
        """platform_rom_ids=None (legacy sync) includes every ROM regardless of toggle."""
        sd = [_make_sd(1, platform_name="Game Boy Advance")]
        result = compute_platform_collection_diff(sd, None, [], False)
        assert result["added_count"] == 1

    def test_empty_platform_name_is_skipped(self):
        sd = [
            _make_sd(1, platform_name=""),
            _make_sd(2, platform_name="Nintendo 64"),
        ]
        result = compute_platform_collection_diff(sd, {1, 2}, [], False)
        assert result["added_count"] == 1
