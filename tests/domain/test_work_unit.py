"""Tests for domain/work_unit.py — event-payload shape + estimate weights (#1382)."""

from domain.work_unit import WorkUnit


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
