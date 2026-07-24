"""Validation and decoding for vanished-ROM cleanup wire requests."""

from __future__ import annotations

import json
from typing import Any, Literal

from services.prune._models import PruneOptions

_MAX_PREVIEW_PAGE = 100
_MAX_STEAM_SNAPSHOT_BYTES = 64 * 1024


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


def parse_options(request: dict[str, Any]) -> PruneOptions | dict[str, Any]:
    """Validate explicit one-run destructive options."""
    keys = ("repoint_shortcuts", "remove_rows", "remove_fully_vanished", "create_recovery_bundle")
    if any(type(request.get(key)) is not bool for key in keys):
        return _failure("invalid_options", "Every cleanup option must be explicitly true or false.")
    raw_ids = request.get("include_installed_rom_ids", [])
    if not isinstance(raw_ids, list) or any(type(value) is not int or value <= 0 for value in raw_ids):
        return _failure("invalid_options", "Installed-content selections must be positive ROM ids.")
    return PruneOptions(
        repoint_shortcuts=request["repoint_shortcuts"],
        remove_rows=request["remove_rows"],
        remove_fully_vanished=request["remove_fully_vanished"],
        create_recovery_bundle=request["create_recovery_bundle"],
        include_installed_rom_ids=frozenset(raw_ids),
    )


def valid_snapshot(snapshot: object, expected_app_id: int | None) -> bool:
    """Accept only a bounded complete Steam snapshot for the pending appId."""
    if not isinstance(snapshot, dict) or type(snapshot.get("app_id")) is not int:
        return False
    if expected_app_id is None or snapshot["app_id"] != expected_app_id:
        return False
    if any(not isinstance(snapshot.get(key), str) for key in ("name", "exe", "start_dir", "launch_options")):
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
    if not isinstance(collections, list) or len(collections) > 256:
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
