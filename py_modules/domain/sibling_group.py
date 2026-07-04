"""Sibling-group key derivation — mirrors RomM's per-platform metadata grouping.

Two RomM ROMs are the same game (siblings) when they matched the same external-
metadata id, coalesced in a fixed source order (IGDB → ScreenScraper → Moby → RA
→ Hasheous → LaunchBox → TGDB → Flashpoint) and scoped per platform. An unmatched
ROM falls back to its own id — a solo group, exactly as on the server. This module
derives that group key client-side over a fetched RomM ROM dict so grouping never
needs the server's ``group_by_meta_id`` fetch (see ADR-0019). Pure compute, no
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
