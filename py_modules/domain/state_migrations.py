"""Pure schema-migration functions for plugin state files.

Each function accepts a raw dict (as loaded from disk) and returns
the same dict promoted to the current schema version.  No I/O —
reading and writing is the caller's responsibility.
"""

from __future__ import annotations

from typing import Any


def migrate_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Bring *data* from any older settings schema to the current version.

    Value semantics — the caller's dict is never mutated.
    """
    new_data = dict(data)
    version = new_data.get("version", 0)
    if version < 1:
        new_data = _migrate_v0_to_v1(new_data)
    if version < 3:
        new_data = _migrate_v2_to_v3(new_data)
    if version < 4:
        new_data = _migrate_v3_to_v4(new_data)
    if version < 5:
        new_data = _migrate_v4_to_v5(new_data)
    if version < 6:
        new_data = _migrate_v5_to_v6(new_data)
    return new_data


_SAVE_SYNC_KNOBS = (
    "save_sync_enabled",
    "sync_before_launch",
    "sync_after_exit",
    "default_slot",
    "autocleanup_limit",
)


def fold_legacy_save_sync_settings(settings: dict[str, Any], save_sync_raw: dict[str, Any] | None) -> dict[str, Any]:
    """Fold the legacy save-sync knobs + ``device_name`` into *settings*.

    Returns a new dict — the inputs are never mutated. The five feature
    knobs are copied out of ``save_sync_raw["settings"]`` (per-key, only
    for keys actually present) and ``device_name`` out of the top level,
    overwriting the ``DEFAULT_SETTINGS`` placeholders. Device identity
    (``device_id`` / ``server_device_id``) is left in the save-sync file.
    A falsy *save_sync_raw* (``None`` or empty) returns *settings*
    unchanged.
    """
    if not save_sync_raw:
        return dict(settings)
    folded = dict(settings)
    raw_knobs = save_sync_raw.get("settings")
    if isinstance(raw_knobs, dict):
        for key in _SAVE_SYNC_KNOBS:
            if key in raw_knobs:
                folded[key] = raw_knobs[key]
    if "device_name" in save_sync_raw:
        folded["device_name"] = save_sync_raw["device_name"]
    return folded


def _migrate_v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """v0 → v1: rename deprecated boolean keys."""
    if data.pop("disable_steam_input", None):
        data["steam_input_mode"] = "force_off"
    if data.pop("debug_logging", None):
        data["log_level"] = "debug"
    data["version"] = 1
    return data


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """v<3 → v3: normalize ``enabled_collections`` to nested-by-kind shape.

    Splits a flat dict into user/smart/franchise buckets. Numeric string
    keys came from the user-collection endpoint; the rest (base64-shaped)
    came from the virtual/franchise endpoint. The smart bucket starts
    empty because smart collections did not exist before this version.
    Already-nested values pass through; partial-nested values are
    filled out rather than re-split.
    """
    flat = data.get("enabled_collections")
    if isinstance(flat, dict):
        data["enabled_collections"] = _normalize_enabled_collections(flat)
    data["version"] = 3
    return data


def _normalize_enabled_collections(flat: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """Coerce *flat* to the full three-bucket shape."""
    if _is_nested_collections(flat):
        return flat
    if _is_partial_nested_collections(flat):
        return _fill_missing_buckets(flat)
    return _split_flat_to_buckets(flat)


def _split_flat_to_buckets(flat: dict[Any, Any]) -> dict[str, dict[str, bool]]:
    """Split a pre-v3 flat enabled_collections dict into user/franchise buckets."""
    nested: dict[str, dict[str, bool]] = {"user": {}, "smart": {}, "franchise": {}}
    for key, value in flat.items():
        if isinstance(key, str) and key.lstrip("-").isdigit():
            nested["user"][key] = bool(value)
        else:
            nested["franchise"][str(key)] = bool(value)
    return nested


_BUCKET_KEYS = ("user", "smart", "franchise")


def _is_nested_collections(value: object) -> bool:
    """Return True if *value* already has the full nested-by-kind shape."""
    if not isinstance(value, dict) or set(value.keys()) != set(_BUCKET_KEYS):
        return False
    return all(isinstance(v, dict) for v in value.values())


def _is_partial_nested_collections(value: object) -> bool:
    """Return True if *value* is a non-empty subset of bucket keys with dict values."""
    if not isinstance(value, dict) or not value:
        return False
    keys = set(value.keys())
    if not keys.issubset(set(_BUCKET_KEYS)):
        return False
    return all(isinstance(v, dict) for v in value.values())


def _fill_missing_buckets(value: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """Return a complete three-bucket dict, filling missing buckets with ``{}``."""
    return {kind: dict(value.get(kind, {})) for kind in _BUCKET_KEYS}


def _migrate_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    """v<4 → v4: stamp the version after the save-sync fold.

    The cross-file lift of the save-sync knobs + ``device_name`` from
    ``save_sync_state.json`` into ``settings.json`` is orchestrated in
    ``bootstrap`` (domain code cannot do I/O); this step only advances
    the schema version so the fold runs exactly once.
    """
    data["version"] = 4
    return data


def _migrate_v4_to_v5(data: dict[str, Any]) -> dict[str, Any]:
    """v<5 → v5: seed the Client API Token slots.

    Introduces ``romm_api_token`` / ``romm_api_token_id`` as ``None``
    placeholders so post-migration reads find the keys. Minting the
    token from any stored legacy credentials is a network side effect
    that lives in the service layer (``ConnectionService``); this step
    only advances the schema.
    """
    data.setdefault("romm_api_token", None)
    data.setdefault("romm_api_token_id", None)
    data["version"] = 5
    return data


def _migrate_v5_to_v6(data: dict[str, Any]) -> dict[str, Any]:
    """v<6 → v6: drop legacy credentials once a Client API Token exists.

    A stored token fully supersedes ``romm_user`` / ``romm_pass`` — nothing
    reads the credentials at runtime once a token is present, so they are
    plaintext-at-rest with no purpose. When a token is set, both credential
    keys are removed; without a token the credentials are kept untouched so
    the startup ``migrate_legacy_credentials`` path can still mint from them.
    """
    if data.get("romm_api_token"):
        data.pop("romm_user", None)
        data.pop("romm_pass", None)
    data["version"] = 6
    return data
