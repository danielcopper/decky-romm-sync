"""Pure helpers for SteamGridDB asset-type maps, endpoint paths, and app-id math.

Owns the stateless compute SteamGridService needs to translate the
plugin's internal asset-type vocabulary into SteamGridDB API endpoints
and to convert unsigned Steam app IDs into the signed int32 form
``shortcuts.vdf`` records use.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

# Discriminant returned by ``classify_resolution`` describing which
# sgdb_id (if any) should win. RomM is the source of truth.
ResolutionDecision = Literal["use_state", "use_romm", "unresolved"]

# Plugin-internal singular asset-type names mapped to the SGDB endpoint segment.
# The asset-type strings on the right are what the SGDB HTTP API exposes
# (``/heroes/game/{id}``, ``/logos/game/{id}``, ``/grids/game/{id}``,
# ``/icons/game/{id}``).
_ASSET_TYPE_TO_ENDPOINT: dict[str, str] = {
    "hero": "heroes",
    "logo": "logos",
    "grid": "grids",
    "icon": "icons",
}

# Numeric asset-type codes used by the frontend's `get_sgdb_artwork_base64`
# callable. Keep in sync with the frontend's encoding.
_ASSET_TYPE_NUM_TO_NAME: dict[int, str] = {
    1: "hero",
    2: "logo",
    3: "grid",
    4: "icon",
}


def asset_type_name(type_num: int) -> str | None:
    """Return the singular asset-type name for a numeric code, or ``None``."""
    return _ASSET_TYPE_NUM_TO_NAME.get(type_num)


def asset_type_endpoint(asset_type: str) -> str | None:
    """Return the SGDB endpoint segment for a singular asset-type name, or ``None``."""
    return _ASSET_TYPE_TO_ENDPOINT.get(asset_type)


def sgdb_endpoint_path(asset_type: str, sgdb_game_id: int) -> str | None:
    """Build the SGDB API path for fetching artwork of a given type.

    Returns ``None`` when *asset_type* is not a recognised name. The path
    is suffixed with the dimensions query string for the ``grid`` type so
    callers do not need to special-case it.
    """
    endpoint = asset_type_endpoint(asset_type)
    if endpoint is None:
        return None
    path = f"/{endpoint}/game/{sgdb_game_id}"
    if asset_type == "grid":
        path += "?dimensions=460x215,920x430"
    return path


def to_signed_app_id(app_id: int) -> int:
    """Convert an unsigned Steam shortcut app ID to its signed int32 form."""
    return struct.unpack("i", struct.pack("I", app_id & 0xFFFFFFFF))[0]


def build_autocomplete_path(term: str) -> str:
    """Build the SGDB autocomplete search path for a free-text *term*.

    The term is percent-encoded so spaces and other reserved characters
    survive into the query path.
    """
    return f"/search/autocomplete/{quote(term)}"


def parse_autocomplete_results(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalise an SGDB autocomplete response into candidate dicts.

    From the SGDB shape ``{"success": true, "data": [{"id", "name",
    "release_date", ...}]}`` produce ``[{"id": int, "name": str,
    "release_year": int | None}]``. ``release_date`` is a unix timestamp
    in seconds and is converted to a calendar year (UTC). A missing,
    falsy, or malformed payload — or one with ``success: false`` —
    yields an empty list. Individual entries lacking a usable ``id`` or
    ``name`` are skipped.
    """
    if not payload or not payload.get("success"):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    results: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        game_id = entry.get("id")
        name = entry.get("name")
        if not isinstance(game_id, int) or not isinstance(name, str):
            continue
        results.append(
            {
                "id": game_id,
                "name": name,
                "release_year": _release_year(entry.get("release_date")),
            }
        )
    return results


def first_grid_url(payload: dict[str, Any] | None) -> str | None:
    """Return a thumbnail URL for the first grid in a ``/grids/...`` response.

    Prefers the ``thumb`` field, falling back to ``url``. Returns
    ``None`` for a missing/failed payload, an empty ``data`` list, or a
    first entry carrying neither field.
    """
    if not payload or not payload.get("success"):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    thumb = first.get("thumb")
    if isinstance(thumb, str) and thumb:
        return thumb
    url = first.get("url")
    if isinstance(url, str) and url:
        return url
    return None


def classify_resolution(state_id: int | None, romm_id: int | None) -> ResolutionDecision:
    """Decide which sgdb_id wins given the state and RomM values.

    RomM is the source of truth:

    - RomM set → ``"use_romm"`` (regardless of the stored state id)
    - RomM absent, state set → ``"use_state"``
    - both absent → ``"unresolved"``
    """
    if romm_id is not None:
        return "use_romm"
    if state_id is not None:
        return "use_state"
    return "unresolved"


def _release_year(release_date: object) -> int | None:
    """Convert a unix-seconds timestamp to a UTC calendar year, or ``None``."""
    if not isinstance(release_date, int | float) or isinstance(release_date, bool):
        return None
    try:
        return datetime.fromtimestamp(release_date, tz=UTC).year
    except (OverflowError, OSError, ValueError):
        return None
