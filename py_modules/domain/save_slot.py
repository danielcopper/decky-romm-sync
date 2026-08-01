"""Slot-addressing primitives for the RomM save wire — pure compute, stdlib only.

The single source of truth for how a slot name maps onto a RomM ``?slot=``
query and how a server save is matched back to a slot. The legacy slot has
two equivalent spellings on our side — ``None`` and the empty string ``""`` —
and RomM stores legacy saves as ``slot: null``. RomM filters ``slot=`` by exact
literal string match, so no ``slot=`` value can address a ``null`` save: legacy
operations MUST omit the param and filter client-side. Anything that decides a
slot query value or whether a save belongs to a slot belongs here, so the wire
contract for the legacy slot is enforced in one place.
"""

from __future__ import annotations

from typing import Any


def normalize_slot(slot: str | None) -> str | None:
    """Collapse the two legacy-slot spellings to one: ``""`` and ``None`` both become ``None``."""
    return slot or None


def slot_query_param(slot: str | None) -> str | None:
    """Return the value for RomM's ``?slot=``: ``None`` (omit the param) for the legacy slot, else the name.

    RomM stores legacy saves as ``slot=null``, which no param value can address
    (#1061), so the legacy slot must omit ``slot=`` entirely and the caller must
    filter the result with :func:`save_in_slot`.
    """
    return None if normalize_slot(slot) is None else slot


def save_in_slot(server_save: dict[str, Any], slot: str | None) -> bool:
    """Whether ``server_save`` belongs to ``slot``. The legacy slot matches a save whose slot is ``null`` or ``""``."""
    return normalize_slot(server_save.get("slot")) == normalize_slot(slot)


def filter_saves_to_slot(server_saves: list[dict[str, Any]], active_slot: str | None) -> list[dict[str, Any]]:
    """Filter server saves to *active_slot* by exact slot membership.

    A legacy (``slot:null`` / ``""``) save belongs ONLY to the legacy slot — it
    is never surfaced under a named slot. Sharing :func:`save_in_slot` keeps the
    sync matrix, the status display, and rollback consistent with the per-slot
    read/delete paths (#1061): the legacy save is visible and syncable only in
    legacy mode, so it can't bleed into a named slot's status or get downloaded
    into it.
    """
    return [ss for ss in server_saves if save_in_slot(ss, active_slot)]
