"""Pure helpers for SteamGridDB asset-type maps, endpoint paths, and app-id math.

Owns the stateless compute SteamGridService needs to translate the
plugin's internal asset-type vocabulary into SteamGridDB API endpoints
and to convert unsigned Steam app IDs into the signed int32 form
``shortcuts.vdf`` records use.
"""

from __future__ import annotations

import struct

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
