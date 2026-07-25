"""Validation and decoding for vanished-ROM cleanup wire requests."""

from __future__ import annotations

import json
from typing import Any, Literal

from services.prune._models import PruneOptions

_MAX_PREVIEW_PAGE = 100
_MAX_STEAM_SNAPSHOT_BYTES = 64 * 1024
_MAX_SELECTION_PAGE = 100


def parse_preview_request(
    request: object,
) -> tuple[Literal["bulk", "rom"], int | None, str | None, int, int] | dict[str, Any]:
    """Validate one local preview page request."""
    if not isinstance(request, dict):
        return _failure("invalid_request", "Preview request must be an object.")
    scope = request.get("scope")
    if scope not in {"bulk", "rom"}:
        return _failure("invalid_scope", "Preview scope must be bulk or rom.")
    explicit: int | None = None
    if scope == "rom":
        raw = request.get("rom_id")
        if type(raw) is not int or raw <= 0:
            return _failure("invalid_rom_id", "A positive ROM id is required.")
        explicit = raw
    preview_id = request.get("preview_id")
    if preview_id is not None and not isinstance(preview_id, str):
        return _failure("invalid_preview_id", "Preview id must be a string or null.")
    offset = request.get("offset", 0)
    limit = request.get("limit", 50)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 0 <= limit <= _MAX_PREVIEW_PAGE:
        return _failure("invalid_page", f"Offset must be non-negative and limit 0-{_MAX_PREVIEW_PAGE}.")
    return scope, explicit, preview_id, offset, limit


def parse_options(request: dict[str, Any], include_installed_rom_ids: frozenset[int]) -> PruneOptions | dict[str, Any]:
    """Validate explicit one-run destructive options."""
    keys = ("repoint_shortcuts", "remove_rows", "remove_fully_vanished", "create_recovery_bundle")
    if any(type(request.get(key)) is not bool for key in keys):
        return _failure("invalid_options", "Every cleanup option must be explicitly true or false.")
    if not request["create_recovery_bundle"] and include_installed_rom_ids:
        return _failure("invalid_options", "Installed content cannot be selected when recovery is disabled.")
    return PruneOptions(
        repoint_shortcuts=request["repoint_shortcuts"],
        remove_rows=request["remove_rows"],
        remove_fully_vanished=request["remove_fully_vanished"],
        create_recovery_bundle=request["create_recovery_bundle"],
        include_installed_rom_ids=include_installed_rom_ids,
    )


def parse_selection_page(request: object) -> tuple[str, str | None, list[int], bool] | dict[str, Any]:
    """Validate one bounded page of a preview-bound installed-content selection."""
    if not isinstance(request, dict):
        return _failure("invalid_request", "Installed-content selection must be an object.")
    preview_id = request.get("preview_id")
    selection_id = request.get("selection_id")
    rom_ids = request.get("rom_ids")
    final = request.get("final")
    if not isinstance(preview_id, str) or not preview_id:
        return _failure("invalid_preview_id", "Preview id must be a non-empty string.")
    if selection_id is not None and (not isinstance(selection_id, str) or not selection_id):
        return _failure("invalid_selection_id", "Selection id must be a non-empty string or null.")
    if (
        not isinstance(rom_ids, list)
        or len(rom_ids) > _MAX_SELECTION_PAGE
        or any(type(value) is not int or value <= 0 for value in rom_ids)
    ):
        return _failure(
            "invalid_selection", f"Each selection page may contain 0-{_MAX_SELECTION_PAGE} positive ROM ids."
        )
    if type(final) is not bool:
        return _failure("invalid_selection", "Selection final must be explicitly true or false.")
    return preview_id, selection_id, rom_ids, final


def valid_snapshot(snapshot: object, expected_app_id: int | None) -> bool:
    """Accept only a bounded complete Steam snapshot for the pending appId."""
    if not isinstance(snapshot, dict) or type(snapshot.get("app_id")) is not int:
        return False
    if expected_app_id is None or snapshot["app_id"] != expected_app_id:
        return False
    if any(not isinstance(snapshot.get(key), str) for key in ("name", "exe", "start_dir", "launch_options")):
        return False
    if not snapshot["name"] or not snapshot["exe"].rstrip('"').endswith("/bin/rom-launcher"):
        return False
    if any(
        key not in snapshot for key in ("minutes_playtime_forever", "minutes_playtime_last_two_weeks", "last_played")
    ):
        return False
    if any(
        value is not None and type(value) is not int
        for value in (
            snapshot.get("minutes_playtime_forever"),
            snapshot.get("minutes_playtime_last_two_weeks"),
            snapshot.get("last_played"),
        )
    ):
        return False
    collections = snapshot.get("collections")
    if not isinstance(collections, list):
        return False
    if any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str)
        for item in collections
    ):
        return False
    try:
        encoded = json.dumps(snapshot, ensure_ascii=True)
    except (TypeError, ValueError):
        return False
    if len(encoded.encode("utf-8")) > _MAX_STEAM_SNAPSHOT_BYTES:
        return False
    return not _contains_base64(snapshot)


def _contains_base64(value: object) -> bool:
    if isinstance(value, dict):
        return any("base64" in str(key).lower() or _contains_base64(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_base64(item) for item in value)
    return False


def _failure(reason: str, message: str) -> dict[str, Any]:
    return {"success": False, "reason": reason, "message": message}
