from __future__ import annotations

from services.prune._models import PruneOptions
from services.prune.requests import parse_options, parse_preview_request, parse_selection_page, valid_snapshot


def _snapshot(app_id: int = 9001) -> dict[str, object]:
    return {
        "app_id": app_id,
        "name": "Game",
        "exe": "/plugin/bin/rom-launcher",
        "start_dir": "/plugin",
        "launch_options": "launch",
        "minutes_playtime_forever": 10,
        "minutes_playtime_last_two_weeks": None,
        "last_played": 123,
        "collections": [{"id": "favorites", "name": "Favorites"}],
    }


def test_preview_request_defaults_and_rejects_bad_pages() -> None:
    assert parse_preview_request({"scope": "bulk"}) == ("bulk", None, None, 0, 50)
    assert parse_preview_request({"scope": "rom", "rom_id": 7}) == ("rom", 7, None, 0, 50)
    invalid = parse_preview_request({"scope": "bulk", "offset": -1, "limit": 101})
    assert invalid == {
        "success": False,
        "reason": "invalid_page",
        "message": "Offset must be non-negative and limit 0-100.",
    }


def test_options_require_explicit_booleans_and_recovery_for_content_selection() -> None:
    parsed = parse_options(
        {
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": False,
            "create_recovery_bundle": True,
        },
        frozenset({7}),
    )
    assert parsed == PruneOptions(True, True, False, True, frozenset({7}))
    invalid = parse_options(
        {
            "repoint_shortcuts": True,
            "remove_rows": True,
            "remove_fully_vanished": False,
            "create_recovery_bundle": False,
        },
        frozenset({7}),
    )
    assert isinstance(invalid, dict)
    assert invalid["reason"] == "invalid_options"


def test_installed_selection_pages_are_wire_bounded_without_total_selection_cap() -> None:
    page = parse_selection_page(
        {"preview_id": "preview", "selection_id": None, "rom_ids": list(range(1, 101)), "final": False}
    )
    assert page == ("preview", None, list(range(1, 101)), False)
    too_large = parse_selection_page(
        {"preview_id": "preview", "selection_id": None, "rom_ids": list(range(1, 102)), "final": True}
    )
    assert isinstance(too_large, dict)
    assert too_large["reason"] == "invalid_selection"


def test_snapshot_requires_exact_app_complete_shape_and_no_base64() -> None:
    assert valid_snapshot(_snapshot(), 9001) is True
    assert valid_snapshot(_snapshot(9002), 9001) is False
    missing = _snapshot()
    missing.pop("launch_options")
    assert valid_snapshot(missing, 9001) is False
    encoded = _snapshot()
    encoded["cover_base64"] = "AAAA"
    assert valid_snapshot(encoded, 9001) is False
    foreign = _snapshot()
    foreign["exe"] = "/usr/bin/foreign-game"
    assert valid_snapshot(foreign, 9001) is False


def test_snapshot_rejects_oversized_payload() -> None:
    snapshot = _snapshot()
    snapshot["collections"] = [{"id": str(index), "name": "x" * 4096} for index in range(100)]
    assert valid_snapshot(snapshot, 9001) is False
