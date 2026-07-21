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
    if version < 7:
        new_data = _migrate_v6_to_v7(new_data)
    if version < 8:
        new_data = _migrate_v7_to_v8(new_data)
    if version < 9:
        new_data = _migrate_v8_to_v9(new_data)
    if version < 10:
        new_data = _migrate_v9_to_v10(new_data)
    if version < 11:
        new_data = _migrate_v10_to_v11(new_data)
    if version < 12:
        new_data = _migrate_v11_to_v12(new_data)
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


def _migrate_v6_to_v7(data: dict[str, Any]) -> dict[str, Any]:
    """v<7 → v7: seed the empty per-platform core map.

    Introduces ``platform_cores`` (RomM platform slug → core label) as an
    empty ``{}`` placeholder so post-migration reads find the key. No
    existing per-platform selection is imported — the map starts empty and
    the user re-applies any platform-wide core via the System page; nothing
    is read out of the retired ES-DE gamelist.
    """
    data.setdefault("platform_cores", {})
    data["version"] = 7
    return data


def _migrate_v7_to_v8(data: dict[str, Any]) -> dict[str, Any]:
    """v<8 → v8: seed the token's minting origin slot.

    Introduces ``romm_api_token_origin`` as a ``None`` placeholder so
    post-migration reads find the key. ``None`` means "origin unknown" — a
    token minted before host-binding — and is treated as legacy by the auth
    guard (still attached, never blocked). The origin is stamped on the next
    sign-in; no existing token's origin is inferred here.
    """
    data.setdefault("romm_api_token_origin", None)
    data["version"] = 8
    return data


def _migrate_v8_to_v9(data: dict[str, Any]) -> dict[str, Any]:
    """v<9 → v9: purge the retired ``romm_user`` / ``romm_pass`` keys.

    The Client API Token superseded stored credentials, and the password is
    never persisted at sign-in — but ``DEFAULT_SETTINGS`` used to re-seed both
    keys as ``""``, so token-based installs kept empty placeholders on disk
    that contradict the "we never store the password" guarantee. Remove them.

    The one case that must survive is a legacy, token-less install whose real
    password is still needed: ``migrate_legacy_credentials`` mints from it on
    startup. So the keys are kept only when there is no token yet **and** a
    non-empty password remains; every other shape (token present, or an empty
    placeholder) is dropped.
    """
    keep_for_legacy_mint = not data.get("romm_api_token") and bool(data.get("romm_pass"))
    if not keep_for_legacy_mint:
        data.pop("romm_user", None)
        data.pop("romm_pass", None)
    data["version"] = 9
    return data


def _migrate_v9_to_v10(data: dict[str, Any]) -> dict[str, Any]:
    """v<10 → v10: stamp the token-provenance slot.

    Introduces ``romm_api_token_source`` — ``"minted"`` for a token the plugin
    minted from credentials, ``"user"`` for a token the user pasted in. Before
    this version every stored token came from the credential-mint path, so a
    truthy ``romm_api_token`` is stamped ``"minted"`` and a token-less install
    is stamped ``None``. The provenance decides whether re-auth may DELETE the
    old token server-side (a pasted ``"user"`` token belongs to the user and is
    never deleted); no existing token's provenance is inferred beyond this.
    """
    data["romm_api_token_source"] = "minted" if data.get("romm_api_token") else None
    data["version"] = 10
    return data


def _migrate_v10_to_v11(data: dict[str, Any]) -> dict[str, Any]:
    """v<11 → v11: rename the ``enabled_collections`` ``franchise`` bucket to ``virtual``.

    The single ownerless virtual-collection kind is now ``virtual`` (it carries
    a ``virtual_type`` sub-field distinguishing IGDB ``franchise`` from IGDB
    ``collection``), so the on-disk enabled bucket keyed ``franchise`` is renamed
    to ``virtual`` with every enabled id preserved — a previously-enabled
    franchise collection stays enabled and keeps syncing.

    Value semantics: the nested ``enabled_collections`` dict is rebuilt rather
    than mutated in place. A file that somehow already carries a ``virtual``
    bucket has the franchise entries merged into it (franchise ids win on a key
    clash) rather than dropped — the two buckets cannot legitimately both exist
    before v11, so the union is the loss-free reconciliation.

    The historical v2→v3 step still produces the ``franchise`` bucket; this step
    renames it afterwards, so the whole chain (v2→v3 → … → v10→v11) lands a
    v2-origin install on the ``virtual`` bucket without rewriting frozen history.
    """
    collections = data.get("enabled_collections")
    if isinstance(collections, dict) and "franchise" in collections:
        rebuilt = {k: v for k, v in collections.items() if k != "franchise"}
        franchise_bucket = collections["franchise"]
        existing_virtual = rebuilt.get("virtual", {})
        merged = dict(existing_virtual) if isinstance(existing_virtual, dict) else {}
        if isinstance(franchise_bucket, dict):
            merged.update(franchise_bucket)
        rebuilt["virtual"] = merged
        data["enabled_collections"] = rebuilt
    data["version"] = 11
    return data


def _migrate_v11_to_v12(data: dict[str, Any]) -> dict[str, Any]:
    """v<12 → v12: align the default save slot with the ecosystem's ``autosave``.

    ``default_slot`` — the slot a brand-new, never-configured ROM adopts on its
    first sync — moves from ``"default"`` to ``"autosave"``, the slot name the
    official RomM clients (Argosy, Grout) use, so a save synced from one client
    lands in the slot the others read. Only a stored value of exactly
    ``"default"`` is rewritten; a user-chosen slot name is left untouched, and an
    absent key is left absent (``DEFAULT_SETTINGS`` seeds the new default on
    load). Existing tracked ROMs are unaffected — each syncs to its persisted
    ``active_slot``, never to this setting.
    """
    if data.get("default_slot") == "default":
        data["default_slot"] = "autosave"
    data["version"] = 12
    return data
