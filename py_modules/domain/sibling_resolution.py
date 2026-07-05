"""Deterministic sibling-group representative resolution (ADR-0021 §3).

A sibling group is one game with several released dumps (region / language /
revision variants). When one version of the group must be chosen — the version
its Steam shortcut binds to, or the picker's preselect — this module answers
"which one" with a total, shuffle-stable order:

    installed sibling > existing binding > RomM default (is_main_sibling) >
    alphabetical fs_name_no_ext

The last leg is exactly RomM's own grouped-view fallback, so an ungroomed
library resolves identically to the RomM web UI. Ties inside any leg break on
``fs_name_no_ext`` (case-folded) then ``rom_id`` so the choice never depends on
fetch order. Pure compute, stdlib only — no I/O, no service/adapter imports.
"""

from __future__ import annotations

from typing import Any


def _tie_break_key(member: dict[str, Any]) -> tuple[str, int]:
    """Stable within-leg order: case-folded ``fs_name_no_ext``, then ``rom_id``.

    ``fs_name_no_ext`` is RomM's own alphabetical fallback key; ``rom_id`` is the
    final disambiguator so two dumps with an identical filename stem still order
    deterministically.
    """
    # ``.lower()`` must stay: RomM's own grouped-view ordering runs over MariaDB's
    # default case-insensitive collation, so case-folding here IS RomM parity — a
    # case-sensitive sort would pick a different representative than the web UI.
    return (str(member.get("fs_name_no_ext") or "").lower(), int(member["rom_id"]))


def resolve_group_representative(
    members: list[dict[str, Any]],
    installed_rom_ids: set[int],
    bound_rom_ids: set[int],
) -> int:
    """Return the representative ``rom_id`` for one sibling group's *members*.

    *members* are the group's fetched ROM dicts (each carrying ``rom_id``,
    ``is_main_sibling`` and ``fs_name_no_ext``). The resolution chain (ADR-0021
    §3): the first non-empty leg wins, and inside a leg the ``_tie_break_key``
    order decides —

    1. an **installed** sibling (``rom_id`` in *installed_rom_ids*),
    2. else a sibling with an **existing binding** (``rom_id`` in *bound_rom_ids*),
    3. else RomM's per-user **default** (``is_main_sibling`` truthy),
    4. else the alphabetically-first ``fs_name_no_ext``.

    Raises ``ValueError`` on an empty *members* — a group always has at least one
    fetched member at the call sites here.
    """
    if not members:
        raise ValueError("cannot resolve a representative for an empty sibling group")

    for leg in (
        [m for m in members if int(m["rom_id"]) in installed_rom_ids],
        [m for m in members if int(m["rom_id"]) in bound_rom_ids],
        [m for m in members if m.get("is_main_sibling")],
        members,
    ):
        if leg:
            return int(min(leg, key=_tie_break_key)["rom_id"])

    # Unreachable — the final leg is the full member list, always non-empty here.
    raise AssertionError("resolution chain fell through a non-empty member list")
