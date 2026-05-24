"""Pure schema-migration functions for plugin state files.

Each function accepts a raw dict (as loaded from disk) and returns
the same dict promoted to the current schema version.  No I/O —
reading and writing is the caller's responsibility.
"""

from __future__ import annotations


def migrate_settings(data: dict) -> dict:
    """Bring *data* from any older settings schema to the current version.

    Value semantics — the caller's dict is never mutated.
    """
    new_data = dict(data)
    version = new_data.get("version", 0)
    if version < 1:
        # v0 → v1: rename deprecated boolean keys
        if new_data.pop("disable_steam_input", None):
            new_data["steam_input_mode"] = "force_off"
        if new_data.pop("debug_logging", None):
            new_data["log_level"] = "debug"
        new_data["version"] = 1
    if version < 3:
        # v<3 → v3: split flat ``enabled_collections`` dict into a nested
        # dict keyed by collection kind (user/smart/franchise). Numeric
        # string keys came from the user-collection endpoint; the rest
        # (base64-shaped) came from the virtual/franchise endpoint. The
        # smart bucket starts empty because smart collections did not
        # exist before this version. Skip the split if the value is
        # already nested (defensive — guards against re-migration of a
        # half-stamped file). A partial-nested value (e.g. only the
        # ``user`` bucket present) is normalized to the full three-bucket
        # shape rather than re-split as if it were flat.
        flat = new_data.get("enabled_collections")
        if isinstance(flat, dict):
            if _is_nested_collections(flat):
                pass  # already correct shape
            elif _is_partial_nested_collections(flat):
                new_data["enabled_collections"] = _fill_missing_buckets(flat)
            else:
                nested: dict[str, dict[str, bool]] = {"user": {}, "smart": {}, "franchise": {}}
                for key, value in flat.items():
                    if isinstance(key, str) and key.lstrip("-").isdigit():
                        nested["user"][key] = bool(value)
                    else:
                        nested["franchise"][str(key)] = bool(value)
                new_data["enabled_collections"] = nested
        new_data["version"] = 3
    return new_data


_BUCKET_KEYS = ("user", "smart", "franchise")


def _is_nested_collections(value: dict) -> bool:
    """Return True if *value* already has the full nested-by-kind shape."""
    if not isinstance(value, dict) or set(value.keys()) != set(_BUCKET_KEYS):
        return False
    return all(isinstance(v, dict) for v in value.values())


def _is_partial_nested_collections(value: dict) -> bool:
    """Return True if *value* is a non-empty subset of bucket keys with dict values."""
    if not isinstance(value, dict) or not value:
        return False
    keys = set(value.keys())
    if not keys.issubset(set(_BUCKET_KEYS)):
        return False
    return all(isinstance(v, dict) for v in value.values())


def _fill_missing_buckets(value: dict) -> dict[str, dict[str, bool]]:
    """Return a complete three-bucket dict, filling missing buckets with ``{}``."""
    return {kind: dict(value.get(kind, {})) for kind in _BUCKET_KEYS}


def migrate_state(data: dict) -> dict:
    """Bring *data* from any older state schema to the current version."""
    # No migrations at v1 — infrastructure for future changes
    return data
