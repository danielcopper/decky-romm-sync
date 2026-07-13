"""Tests for domain.sync_diff — pure delta computations for the sync engine."""

from typing import Any

from domain.sync_diff import (
    BIND_ROM_ID_KEY,
    ClassificationResult,
    classify_roms,
    collapse_sibling_groups,
    compute_collection_diff,
    compute_platform_collection_diff,
    select_stale_removals,
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
    launch_options="",
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
        "launch_options": launch_options,
    }


def _reg(
    name="Game",
    platform_name="N64",
    platform_slug="n64",
    fs_name="game.z64",
    app_id=1001,
    applied_launch_options: str | None = "",
):
    """Build a registry entry dict matching _read_preview_baseline output.

    ``applied_launch_options`` defaults to ``""`` — an uninstalled ROM whose
    empty placeholder was recorded — so a same-identity fetch with the default
    ``_make_sd`` launch_options ("") reads as unchanged (#1383).
    """
    return {
        "app_id": app_id,
        "name": name,
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "fs_name": fs_name,
        "applied_launch_options": applied_launch_options,
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

    def test_launch_options_change_detected_with_identical_identity(self):
        # Identity (name/fs_name/platform) is unchanged but the built target
        # launch_options differs from the recorded applied value — a just-installed
        # ROM whose shortcut still carries "" — so the item is "changed" (#1383).
        registry = {"1": _reg(name="Game A", fs_name="gamea.z64", app_id=1001, applied_launch_options="")}
        sd = [_make_sd(1, "Game A", fs_name="gamea.z64", launch_options="flatpak run … /gamea.z64")]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert new == []
        assert [c["rom_id"] for c in changed] == [1]
        assert changed[0]["existing_app_id"] == 1001
        assert unchanged_ids == []

    def test_matching_launch_options_stays_unchanged(self):
        # Identity AND launch_options both match the recorded applied value — the
        # shortcut is already correct, so the item is genuinely unchanged (skipped).
        cmd = "flatpak run … /gamea.z64"
        registry = {"1": _reg(name="Game A", fs_name="gamea.z64", app_id=1001, applied_launch_options=cmd)}
        sd = [_make_sd(1, "Game A", fs_name="gamea.z64", launch_options=cmd)]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert new == []
        assert changed == []
        assert unchanged_ids == [1]

    def test_null_applied_launch_options_forces_changed(self):
        # A pre-migration-015 row (or a freshly created row not yet recorded) has
        # applied_launch_options = None (unknown). It never matches a target
        # string, so the item is always "changed" and re-applied once — the "no
        # skip on unknown state / no data invented" contract (#1383). Even when the
        # target launch_options is "" (uninstalled), None != "" holds.
        registry = {"1": _reg(name="Game A", fs_name="gamea.z64", app_id=1001, applied_launch_options=None)}
        sd = [_make_sd(1, "Game A", fs_name="gamea.z64", launch_options="")]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert new == []
        assert [c["rom_id"] for c in changed] == [1]
        assert unchanged_ids == []

    def test_platform_name_divergence_not_classified_as_changed(self):
        """A derived-display-only divergence never counts as changed (#1292).

        ``platform_name`` is a derived, non-persisted display field. A ROM
        riding an enabled collection whose platform is *disabled* resolves to
        the bare slug ("gba") on the registry side (the enabled-only
        slug→name map has no entry, so it falls back to the slug) but to
        RomM's full display name ("Game Boy Advance") on the fetch side. With
        every persisted field (name/platform_slug/fs_name) equal, that
        divergence must NOT classify as changed — the apply upsert persists
        only platform_slug/name/fs_name, so it could never be healed and
        produced a permanent phantom "1 updated" preview delta.
        """
        registry = {
            "1": _reg(name="Game A", platform_name="gba", platform_slug="gba", fs_name="game.gba", app_id=1001),
        }
        sd = [_make_sd(1, "Game A", platform_name="Game Boy Advance", platform_slug="gba", fs_name="game.gba")]
        new, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert changed == []
        assert new == []
        assert unchanged_ids == [1]

    def test_platform_slug_change_detected(self):
        """A genuine platform change (persisted platform_slug differs) is changed."""
        registry = {
            "1": _reg(name="Game A", platform_name="Game Boy Advance", platform_slug="gba", app_id=1001),
        }
        sd = [_make_sd(1, "Game A", platform_name="Game Boy Color", platform_slug="gbc")]
        _, changed, unchanged_ids, _, _ = classify_roms(sd, registry, {"N64"})
        assert len(changed) == 1
        assert changed[0]["rom_id"] == 1
        assert changed[0]["existing_app_id"] == 1001
        assert unchanged_ids == []

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


class TestSelectStaleRemovals:
    """``select_stale_removals`` — drop any stale candidate whose appId this run re-bound (#1036)."""

    def test_excludes_resynced_appid(self):
        """An old colliding rom_id whose appId was re-bound this run is NOT removed.

        rom 1 (old) and rom 2 (new) both resolve to appId 5000; the run bound
        5000 to rom 2, so rom 1 looks stale but its appId is live → excluded."""
        candidate_stale = [(1, 5000)]
        result = select_stale_removals(candidate_stale, {5000})
        assert result == []

    def test_keeps_genuinely_stale_non_resynced_appid(self):
        """A stale ROM whose appId was NOT bound this run is still removed."""
        candidate_stale = [(99, 9900)]
        result = select_stale_removals(candidate_stale, {5000})
        assert result == [(99, 9900)]

    def test_mixed_keeps_only_non_resynced(self):
        """The collision row drops; the genuinely-stale row stays."""
        candidate_stale = [(1, 5000), (99, 9900)]
        result = select_stale_removals(candidate_stale, {5000})
        assert result == [(99, 9900)]

    def test_empty_synced_app_ids_is_passthrough(self):
        """No appId bound this run → every candidate survives unchanged."""
        candidate_stale = [(1, 5000), (99, 9900)]
        result = select_stale_removals(candidate_stale, set())
        assert result == [(1, 5000), (99, 9900)]

    def test_empty_candidates_is_empty(self):
        assert select_stale_removals([], {5000}) == []

    def test_preserves_candidate_order(self):
        candidate_stale = [(3, 3000), (1, 1000), (2, 2000)]
        result = select_stale_removals(candidate_stale, set())
        assert result == [(3, 3000), (1, 1000), (2, 2000)]


def _gsd(
    rom_id,
    *,
    name="Game",
    group_key="g1",
    fs_name="game.z64",
    fs_name_no_ext="game",
    is_main_sibling=False,
    platform_slug="n64",
    launch_options="",
    regions=(),
    revision="",
    tags=(),
):
    """A built shortcut entry as build_shortcuts_data shapes it, with the
    sibling-group fields the collapse + resolver read."""
    return {
        "rom_id": rom_id,
        "name": name,
        "fs_name": fs_name,
        "fs_name_no_ext": fs_name_no_ext,
        "platform_name": "N64",
        "platform_slug": platform_slug,
        "launch_options": launch_options,
        "cover_path": "",
        "sibling_group_key": group_key,
        "is_main_sibling": is_main_sibling,
        "regions": list(regions),
        "revision": revision,
        "tags": list(tags),
        "igdb_id": None,
        "sgdb_id": None,
        "ra_id": None,
    }


def _greg(
    rom_id,
    *,
    app_id,
    name="Game",
    group_key: str | None = "g1",
    fs_name="game.z64",
    platform_slug="n64",
    applied_launch_options="",
):
    """A bound-registry entry as _read_apply_registry / _read_preview_baseline shape it.

    ``applied_launch_options`` defaults to ``""`` — the recorded empty placeholder —
    so a same-identity re-fetch with the default ``_gsd`` launch_options ("") reads
    as unchanged (#1383).
    """
    return {
        "app_id": app_id,
        "name": name,
        "fs_name": fs_name,
        "platform_slug": platform_slug,
        "platform_name": "N64",
        "sibling_group_key": group_key,
        "applied_launch_options": applied_launch_options,
    }


class TestCollapseSiblingGroups:
    """``collapse_sibling_groups`` — one Steam shortcut per sibling group (ADR-0021)."""

    def test_new_group_emits_single_representative(self):
        # 3 siblings, no binding anywhere; rom 2 is the RomM default → the one rep.
        members = [
            _gsd(1, name="Game (USA)", fs_name_no_ext="game_usa"),
            _gsd(2, name="Game (JP)", fs_name_no_ext="game_jp", is_main_sibling=True),
            _gsd(3, name="Game (EU)", fs_name_no_ext="game_eu"),
        ]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [2]

    def test_new_group_representative_and_name_follow_hardened_ranking(self):
        # A New group with a (USA) (Beta) prerelease + a (Japan) retail final: the
        # 1G1R ranking (prerelease demotion before region) picks the Japan retail
        # dump as BOTH the emitted representative (bind target) and the canonical
        # shortcut name — the region-preferred USA dump is a prerelease, so it loses.
        members = [
            _gsd(1, name="Game (USA) (Beta)", fs_name_no_ext="game_usa_beta", regions=["USA"], tags=["Beta"]),
            _gsd(2, name="Game (Japan)", fs_name_no_ext="game_japan", regions=["Japan"]),
        ]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [2]
        assert emitted[0]["name"] == "Game (Japan)"

    def test_installed_sibling_wins_representative(self):
        members = [
            _gsd(1, is_main_sibling=True, fs_name_no_ext="a"),
            _gsd(2, fs_name_no_ext="z"),
        ]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids={2}, complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [2]

    def test_unbound_siblings_not_counted_as_new(self):
        # The #1292-class phantom fix: a group with one bound sibling + two unbound
        # siblings collapses to the bound entry — classify reads it as unchanged,
        # NOT the unbound siblings as perpetual "new".
        members = [
            _gsd(1, name="Game", fs_name="game.z64"),
            _gsd(2, name="Game (JP)", fs_name="game_jp.z64"),
            _gsd(3, name="Game (EU)", fs_name="game_eu.z64"),
        ]
        registry = {"1": _greg(1, app_id=1001, name="Game", fs_name="game.z64")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [1]
        result = classify_roms(emitted, registry, {"N64"})
        assert result.new == []
        assert result.unchanged_ids == [1]
        assert result.stale == []

    def test_grandfathered_multiple_bound_siblings_all_kept(self):
        # Two DIFFERENT-name siblings each already carry a shortcut (a pre-ADR-0021
        # library). Both are still fetched → both stay emitted; no new shortcut for
        # the unbound third sibling.
        members = [
            _gsd(1, name="Game", fs_name="game.z64"),
            _gsd(2, name="Game JP", fs_name="game_jp.z64"),
            _gsd(3, name="Game EU", fs_name="game_eu.z64"),
        ]
        registry = {
            "1": _greg(1, app_id=1001, name="Game", fs_name="game.z64"),
            "2": _greg(2, app_id=1002, name="Game JP", fs_name="game_jp.z64"),
        }
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert sorted(e["rom_id"] for e in emitted) == [1, 2]
        assert 3 not in {e["rom_id"] for e in emitted}

    def test_whole_group_vanished_is_stale(self):
        # The bound row's group has NO fetched member at all → collapse emits
        # nothing for it, and classify flags it stale.
        members = [_gsd(9, name="Other", group_key="g2")]
        registry = {"1": _greg(1, app_id=1001, name="Gone", group_key="g1")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert 1 not in {e["rom_id"] for e in emitted}
        result = classify_roms(emitted, registry, {"N64"})
        assert result.stale == [1]

    def test_vanished_bound_sibling_rebinds_to_representative(self):
        # rom 1 is the group's bound sibling but vanished from the server; roms 2/3
        # survive. Collapse emits ONE rebind entry keyed to rom 1 (frontend reuses
        # its shortcut) carrying bind_rom_id → the surviving representative.
        members = [
            _gsd(
                2,
                name="Game (JP)",
                fs_name="game_jp.z64",
                fs_name_no_ext="game_jp",
                is_main_sibling=True,
                launch_options="run /jp.z64",
            ),
            _gsd(3, name="Game (EU)", fs_name="game_eu.z64", fs_name_no_ext="game_eu"),
        ]
        registry = {"1": _greg(1, app_id=1001, name="Game (USA)", fs_name="game_usa.z64")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert len(emitted) == 1
        entry = emitted[0]
        # Keyed to the vanished bound sibling (sticky identity) so the frontend
        # reuses its shortcut and the preview reads it as unchanged.
        assert entry["rom_id"] == 1
        assert entry["name"] == "Game (USA)"
        # The binding moves onto the representative (rom 2 = the RomM default), and
        # the representative's launch bake rides along.
        assert entry[BIND_ROM_ID_KEY] == 2
        assert entry["launch_options"] == "run /jp.z64"
        result = classify_roms(emitted, registry, {"N64"})
        # The rebind carries the representative's launch bake, which differs from
        # the vanished sibling's recorded applied state (""), so the shortcut must
        # be re-touched to move the binding — classify reads it as changed, not
        # unchanged (#1383). The apply path force-keeps rebinds regardless, so a
        # rebind is never skipped even when classify would call it unchanged.
        assert result.unchanged_ids == []
        assert [e["rom_id"] for e in result.changed] == [1]
        assert result.stale == []
        assert result.new == []

    def test_rebind_keeps_smallest_rom_id_binding_others_stale(self):
        # A group with TWO bound siblings (grandfathered), both vanished. One appId
        # is kept (smallest rom_id) and rebound; the other goes stale.
        members = [_gsd(5, name="Game (EU)", fs_name_no_ext="game_eu")]
        registry = {
            "1": _greg(1, app_id=1001, name="Game (USA)"),
            "2": _greg(2, app_id=1002, name="Game (JP)"),
        }
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert len(emitted) == 1
        assert emitted[0]["rom_id"] == 1
        assert emitted[0][BIND_ROM_ID_KEY] == 5
        result = classify_roms(emitted, registry, {"N64"})
        assert result.stale == [2]

    def test_legacy_null_key_bound_row_grandfathered_via_fetched_key(self):
        # A bound row whose stored sibling_group_key is still NULL (pre-capture) is
        # grouped by its FETCHED key, so it is grandfathered (kept), never churned
        # into a delete + recreate.
        members = [_gsd(1, name="Game", group_key="igdb:42:7")]
        registry = {"1": _greg(1, app_id=1001, name="Game", group_key=None)}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [1]
        result = classify_roms(emitted, registry, {"N64"})
        assert result.unchanged_ids == [1]

    def test_solo_unmatched_group_degrades_to_per_rom(self):
        # An unmatched ROM is a solo group — collapse emits it exactly as before.
        members = [_gsd(7, name="Homebrew", group_key="romm:7:1")]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [7]


class TestCollapseSiblingGroupsCanonicalNaming:
    """Mint entries carry the region-canonical name; bound lanes keep the persisted name (ADR-0021 §2/§3)."""

    def test_mint_binds_region_representative_and_names_it_canonically(self):
        # The Pokémon fix: no binding anywhere, USA + Japan dumps. Region priority
        # binds USA AND names the shortcut after USA — not the alphabetically-first
        # Japanese dump.
        members = [
            _gsd(1, name="ポケットモンスターファイアレッド", fs_name_no_ext="game_japan", regions=["Japan"]),
            _gsd(2, name="Pokemon FireRed", fs_name_no_ext="game_usa", regions=["USA"]),
        ]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert len(emitted) == 1
        assert emitted[0]["rom_id"] == 2
        assert emitted[0]["name"] == "Pokemon FireRed"

    def test_mint_name_decoupled_from_bound_target_when_default_forces_it(self):
        # RomM default = the Japanese dump, so the BIND target is Japan, but the
        # canonical NAME still follows region priority (USA) — name decoupled from
        # the bound version (ADR-0021 "name can lag the active version").
        members = [
            _gsd(1, name="Japan Default", fs_name_no_ext="game_japan", regions=["Japan"], is_main_sibling=True),
            _gsd(2, name="USA Name", fs_name_no_ext="game_usa", regions=["USA"]),
        ]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert len(emitted) == 1
        assert emitted[0]["rom_id"] == 1  # bind the RomM default (Japan)
        assert emitted[0]["name"] == "USA Name"  # but name it canonically (USA)

    def test_mint_does_not_mutate_the_source_member(self):
        # The emitted entry is a copy: the original member dict (also staged in
        # pending_all_roms for the per-sibling identity upsert) keeps its own name.
        japan = _gsd(1, name="Japan Name", fs_name_no_ext="game_japan", regions=["Japan"], is_main_sibling=True)
        members = [japan, _gsd(2, name="USA Name", fs_name_no_ext="game_usa", regions=["USA"])]
        collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=True)
        assert japan["name"] == "Japan Name"

    def test_mint_respects_preferred_region_override(self):
        members = [
            _gsd(1, name="Euro Name", fs_name_no_ext="game_eu", regions=["Europe"]),
            _gsd(2, name="German Name", fs_name_no_ext="game_de", regions=["Germany"]),
        ]
        emitted = collapse_sibling_groups(
            members, registry={}, installed_rom_ids=set(), complete_group_view=True, preferred_region="Germany"
        )
        assert emitted[0]["rom_id"] == 2
        assert emitted[0]["name"] == "German Name"

    def test_grandfathered_bound_sibling_keeps_persisted_name_no_rename(self):
        # A bound group must never be renamed by canonical naming — the emitted
        # grandfathered entry carries the fetched member's own name, matching the
        # persisted registry name, so classify reads it as unchanged (no churn).
        members = [
            _gsd(1, name="Japan Name", fs_name_no_ext="game_japan", regions=["Japan"]),
            _gsd(2, name="USA Name", fs_name_no_ext="game_usa", regions=["USA"]),
        ]
        registry = {"1": _greg(1, app_id=1001, name="Japan Name", fs_name="game.z64")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert [e["rom_id"] for e in emitted] == [1]
        assert emitted[0]["name"] == "Japan Name"
        result = classify_roms(emitted, registry, {"N64"})
        assert result.unchanged_ids == [1]
        assert result.changed == []

    def test_rebind_entry_keeps_persisted_name_not_canonical(self):
        # A rebinding group is already bound → sticky: the rebind entry carries the
        # vanished sibling's PERSISTED name, never the region-canonical name, so the
        # live shortcut is not renamed.
        members = [
            _gsd(2, name="USA Name", fs_name_no_ext="game_usa", regions=["USA"]),
            _gsd(3, name="Euro Name", fs_name_no_ext="game_eu", regions=["Europe"]),
        ]
        registry = {"1": _greg(1, app_id=1001, name="Original Bound Name", fs_name="game_x.z64")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=True)
        assert len(emitted) == 1
        assert emitted[0]["rom_id"] == 1
        assert emitted[0]["name"] == "Original Bound Name"
        # The binding still moves onto the region representative (USA outranks
        # Europe in the build-time order → rom 2).
        assert emitted[0][BIND_ROM_ID_KEY] == 2


class TestCollapseSiblingGroupsPartialView:
    """``collapse_sibling_groups`` under a PARTIAL group view (collection units, #1296).

    A collection unit spans platforms and fetches only its own members, so the
    whole-registry read surfaces bound siblings the unit never fetched. Absence
    from that partial fetch is NOT absence from the server, so the collapse must
    never rebind (which would move a live installed game's shortcut onto an
    uninstalled sibling) — it only grandfathers.
    """

    def test_bound_sibling_absent_from_partial_fetch_is_grandfathered_not_rebound(self):
        # The worked #1296 failure in miniature: rom 1 is bound + installed but on
        # a platform this collection unit never fetched; the collection fetches
        # only the UNBOUND sibling rom 2. A partial view must leave the binding
        # untouched — emit nothing, never a rebind entry onto the uninstalled rom 2.
        members = [_gsd(2, name="Game (JP)", fs_name="game_jp.z64", fs_name_no_ext="game_jp")]
        registry = {"1": _greg(1, app_id=1001, name="Game (USA)", fs_name="game_usa.z64")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids={1}, complete_group_view=False)
        assert emitted == []
        # And the contrast: the SAME inputs under a complete view DO rebind — the
        # only thing separating "grandfather" from "rebind" is view-completeness.
        rebound = collapse_sibling_groups(members, registry, installed_rom_ids={1}, complete_group_view=True)
        assert len(rebound) == 1
        assert rebound[0]["rom_id"] == 1
        assert rebound[0][BIND_ROM_ID_KEY] == 2

    def test_partial_view_group_with_no_binding_anywhere_mints_representative(self):
        # A group with NO binding in the whole registry IS a genuinely new game —
        # a collection-only unit still mints its single representative among the
        # fetched members (RomM default rom 2 here).
        members = [
            _gsd(1, name="Game (USA)", fs_name_no_ext="game_usa"),
            _gsd(2, name="Game (JP)", fs_name_no_ext="game_jp", is_main_sibling=True),
        ]
        emitted = collapse_sibling_groups(members, registry={}, installed_rom_ids=set(), complete_group_view=False)
        assert [e["rom_id"] for e in emitted] == [2]

    def test_partial_view_fetched_bound_sibling_still_emits_as_update(self):
        # A bound sibling that IS in the partial fetch still emits (as an update
        # entry) so its identity/launch refresh; the unbound fetched sibling does
        # not mint a second shortcut. No rebind either — the group is grandfathered.
        members = [
            _gsd(1, name="Game", fs_name="game.z64"),
            _gsd(2, name="Game (JP)", fs_name="game_jp.z64"),
        ]
        registry = {"1": _greg(1, app_id=1001, name="Game", fs_name="game.z64")}
        emitted = collapse_sibling_groups(members, registry, installed_rom_ids=set(), complete_group_view=False)
        assert [e["rom_id"] for e in emitted] == [1]
        assert all(BIND_ROM_ID_KEY not in e for e in emitted)
