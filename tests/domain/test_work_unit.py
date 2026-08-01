"""Tests for domain/work_unit.py — event-payload shape + estimate weights (#1382)."""

from domain.work_unit import WorkUnit, collection_units


def _platform_unit(**overrides):
    kwargs = {"type": "platform", "id": 1, "name": "N64", "slug": "n64", "rom_count": 5}
    kwargs.update(overrides)
    return WorkUnit(**kwargs)


class TestToEventPayload:
    """Optional fields ride the payload only when known — absent, not null."""

    def test_platform_payload_omits_unset_estimate_fields(self):
        payload = _platform_unit().to_event_payload()
        assert payload == {"type": "platform", "id": 1, "name": "N64", "slug": "n64", "rom_count": 5}
        assert "predicted_skip" not in payload
        assert "collapsed_count" not in payload
        assert "bound_count" not in payload
        assert "new_shortcut_count" not in payload
        assert "collection_kind" not in payload

    def test_platform_payload_carries_estimate_fields_when_set(self):
        payload = _platform_unit(
            predicted_skip=True, collapsed_count=3, bound_count=2, new_shortcut_count=1
        ).to_event_payload()
        assert payload["predicted_skip"] is True
        assert payload["collapsed_count"] == 3
        assert payload["bound_count"] == 2
        assert payload["new_shortcut_count"] == 1

    def test_zero_bound_count_is_still_emitted(self):
        """0 is knowledge ("nothing mirrored yet, price it all as creates"),
        distinct from absent ("unknown, fall back to the old all-creates read")."""
        payload = _platform_unit(bound_count=0).to_event_payload()
        assert payload["bound_count"] == 0

    def test_zero_new_shortcut_count_is_still_emitted(self):
        """0 is knowledge ("nothing left to mint, price it all as updates") —
        exactly the Force Full Sync shape. Absent means "unknown", which prices
        creates by subtraction instead and over-reads a sibling-heavy platform."""
        payload = _platform_unit(new_shortcut_count=0).to_event_payload()
        assert payload["new_shortcut_count"] == 0

    def test_predicted_skip_false_is_still_emitted(self):
        """False is knowledge ("will not skip"), distinct from absent ("unknown")."""
        payload = _platform_unit(predicted_skip=False).to_event_payload()
        assert payload["predicted_skip"] is False
        assert "collapsed_count" not in payload
        assert "bound_count" not in payload
        assert "new_shortcut_count" not in payload

    def test_collection_payload_carries_kind_but_no_estimate_fields(self):
        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=4, collection_kind="standard")
        payload = unit.to_event_payload()
        assert payload["collection_kind"] == "standard"
        assert "predicted_skip" not in payload
        assert "collapsed_count" not in payload
        assert "bound_count" not in payload
        assert "new_shortcut_count" not in payload

    def test_virtual_type_defaults_to_none_and_stays_off_the_wire(self):
        """virtual_type is reporter-internal (#1539): defaults None, never in the payload."""
        unit = WorkUnit(type="collection", id="7", name="Faves", slug="", rom_count=4, collection_kind="standard")
        assert unit.virtual_type is None
        assert "virtual_type" not in unit.to_event_payload()

    def test_virtual_type_carried_when_set(self):
        unit = WorkUnit(
            type="collection",
            id="vc-1",
            name="coll-fr",
            slug="",
            rom_count=3,
            collection_kind="virtual",
            virtual_type="franchise",
        )
        assert unit.virtual_type == "franchise"
        # Still internal — the sync_plan / sync_apply_unit payload carries only kind.
        assert "virtual_type" not in unit.to_event_payload()


class TestEstimatedItems:
    """The unit's weight in the plan's skip-aware totals (estimate-only, ADR-0023)."""

    def test_predicted_skip_weighs_zero(self):
        assert _platform_unit(predicted_skip=True, collapsed_count=3).estimated_items() == 0

    def test_collapsed_count_wins_over_raw_rom_count(self):
        assert _platform_unit(predicted_skip=False, collapsed_count=3).estimated_items() == 3

    def test_falls_back_to_raw_rom_count_when_collapsed_unknown(self):
        assert _platform_unit(predicted_skip=False).estimated_items() == 5

    def test_unknown_prediction_falls_back_to_raw_rom_count(self):
        """Collections / failed estimate reads carry None everywhere → raw weight."""
        assert _platform_unit().estimated_items() == 5

    def test_zero_collapsed_count_weighs_zero_not_raw(self):
        """0 is a real collapsed count (edge), never confused with the None fallback."""
        assert _platform_unit(predicted_skip=False, collapsed_count=0).estimated_items() == 0


class TestCollectionUnits:
    def test_builds_a_unit_per_enabled_collection_only(self):
        listing = [
            {"id": 1, "name": "Shooters", "slug": "shooters", "rom_count": 3, "updated_at": "2026-07-20T06:27:12"},
            {"id": 2, "name": "Puzzles", "slug": "puzzles", "rom_count": 9},
        ]
        units = collection_units(listing, {"1"}, "standard")
        assert [(u.id, u.name, u.rom_count, u.collection_kind) for u in units] == [("1", "Shooters", 3, "standard")]
        assert units[0].collection_updated_at == "2026-07-20T06:27:12"

    def test_rom_count_falls_back_to_the_member_id_list(self):
        listing = [{"id": 7, "name": "Faves", "slug": "faves", "rom_ids": [1, 2, 3, 4]}]
        assert collection_units(listing, {"7"}, "smart")[0].rom_count == 4

    def test_owner_scope_drops_a_foreign_collection_even_when_enabled(self):
        listing = [
            {"id": 1, "name": "Mine", "slug": "mine", "rom_count": 1, "user_id": 5},
            {"id": 2, "name": "Theirs", "slug": "theirs", "rom_count": 1, "user_id": 9},
        ]
        units = collection_units(listing, {"1", "2"}, "standard", own_user_id=5, filter_to_own=True)
        assert [u.id for u in units] == ["1"]

    def test_a_virtual_collection_has_no_owner_and_always_survives_the_scope(self):
        listing = [{"id": "franchise-1", "name": "Mario", "slug": "mario", "rom_count": 2}]
        units = collection_units(
            listing, {"franchise-1"}, "virtual", virtual_type="franchise", own_user_id=5, filter_to_own=True
        )
        assert [(u.id, u.virtual_type) for u in units] == [("franchise-1", "franchise")]

    def test_nothing_enabled_yields_no_units(self):
        listing = [{"id": 1, "name": "Shooters", "slug": "shooters", "rom_count": 3}]
        assert collection_units(listing, set(), "standard") == []
        assert collection_units([], {"1"}, "standard") == []

    def test_a_missing_name_falls_back_to_the_id_and_the_slug_to_empty(self):
        units = collection_units([{"id": 4}], {"4"}, "standard")
        assert (units[0].name, units[0].slug, units[0].rom_count) == ("4", "", 0)
