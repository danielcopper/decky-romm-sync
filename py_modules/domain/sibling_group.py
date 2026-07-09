"""Sibling-group key derivation — mirrors RomM's per-platform metadata grouping.

Two RomM ROMs are the same game (siblings) when they matched the same external-
metadata id, coalesced in a fixed source order (IGDB → ScreenScraper → Moby → RA
→ Hasheous → LaunchBox → TGDB → Flashpoint) and scoped per platform. An unmatched
ROM falls back to its own id — a solo group, exactly as on the server. This module
derives that group key client-side over a fetched RomM ROM dict so grouping never
needs the server's ``group_by_meta_id`` fetch (see ADR-0021). Pure compute, no
I/O, stdlib only.
"""

from __future__ import annotations

from typing import Any

# (RomM dict field, key-source label) in RomM 4.9.2's coalesce order. The first
# field carrying a non-null id wins; its label prefixes the key so two ROMs that
# matched on different services never collide onto the same group.
_META_ID_SOURCES: tuple[tuple[str, str], ...] = (
    ("igdb_id", "igdb"),
    ("ss_id", "ss"),
    ("moby_id", "moby"),
    ("ra_id", "ra"),
    ("hasheous_id", "hasheous"),
    ("launchbox_id", "launchbox"),
    ("tgdb_id", "tgdb"),
    ("flashpoint_id", "flashpoint"),
)


def compute_sibling_group_key(rom: dict[str, Any]) -> str:
    """Return the per-platform sibling-group key for a fetched RomM *rom* dict.

    Coalesces the external-metadata ids in RomM's fixed order and formats
    ``"{source}:{id}:{platform_id}"`` (e.g. ``"igdb:3404:57"``). When the ROM
    matched no service, falls back to ``"romm:{rom_id}:{platform_id}"`` — its
    own id, a solo group. ``platform_id`` scopes the key so the same metadata id
    on two platforms yields two groups. A missing id is treated as unmatched (its
    field simply carries no non-null value).
    """
    platform_id = rom.get("platform_id")
    for field, source in _META_ID_SOURCES:
        value = rom.get(field)
        if value is not None:
            return f"{source}:{value}:{platform_id}"
    return f"romm:{rom.get('id')}:{platform_id}"


def target_in_sibling_group(
    *,
    bound_group_key: str | None,
    target_group_key: str | None,
    target_is_local: bool,
    target_is_server_sibling: bool,
) -> bool:
    """Whether a switch target belongs to the bound ROM's sibling group.

    The single membership authority shared by the version picker's per-version
    ``switchable`` flag (``get_version_list``) and ``switch_version``'s group
    guard, so the two surfaces can never disagree (ADR-0021). A target is a
    member when it is *eligible* — it has a local row (``target_is_local``) OR
    RomM's symmetric ``sibling_roms`` view lists it (``target_is_server_sibling``)
    — AND its group key matches the bound row's. ``target_group_key`` is the
    target's local ``sibling_group_key`` when it has a local row, else its
    **would-be** key: the key the same sync-time derivation
    (:func:`compute_sibling_group_key`) yields for the server ROM's metadata ids —
    i.e. the group selecting it in the picker would persist it under. A NULL bound
    key (an unbackfilled / solo bound row) can't discriminate, so it accepts any
    eligible target.

    Why the key check matters even for a server-only target: RomM groups by ANY
    shared metadata id (an OR across sources) while the group key is the FIRST
    non-null id in coalesce order, so RomM can bridge two groups (e.g. an
    IGDB-keyed title next to a ScreenScraper-keyed variant that share only the
    ScreenScraper id). A bridged sibling that was never synced locally has no
    local row, but its would-be key still lands it in its OWN group — switching to
    it would bind the shortcut outside the group it was created from. Comparing
    the would-be key to the bound key rejects it (listed but not switchable),
    exactly as a bridged sibling already synced under a different local key is
    rejected (#1360 / #1359).
    """
    if not (target_is_local or target_is_server_sibling):
        return False
    return bound_group_key is None or target_group_key == bound_group_key
