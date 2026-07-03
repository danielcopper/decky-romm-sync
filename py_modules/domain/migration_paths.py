"""Pure logic for the RetroDECK pending-home set — change detection and remap.

A RetroDECK home change is "pending" until the user migrates or dismisses.
When the user changes the home again *before* migrating, the plugin must
remember **every** home it has left behind, not just the most recent one, or
files stranded under an intermediate home are lost (#1042). The pending set is
the ordered list ``[oldest, …, newest]`` of homes that may still hold plugin
files; the live home is never a member.

Anything that decides how that set transitions on a home change, or maps a
tracked path from whichever pending home it lives under to the current home,
belongs here. All functions are pure, stdlib-only compute over strings and
lists; the service owns the kv reads/writes, the ``exists`` probes, and the
file moves.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingHomeTransition:
    """Outcome of one RetroDECK home-change detection pass.

    ``kind`` tells the service what to persist and emit:

    - ``"first_run"`` — no home was stored yet; store ``home``, write no
      pending markers, emit nothing.
    - ``"unchanged"`` — the live home equals the stored home; no writes,
      no emit.
    - ``"cleared"`` — the user reverted to the sole pending home; store
      ``home``, delete the pending markers, emit ``retrodeck_path_changed``
      with ``cleared=True``.
    - ``"changed"`` — the home changed (possibly again before migrating);
      store ``home`` and the new ``pending`` set, emit
      ``retrodeck_path_changed``.

    ``pending`` is the new pending set (oldest→newest); its head is the
    ``retrodeck_home_path_previous`` marker every existing consumer reads.
    ``emit_old`` / ``emit_new`` are the ``old_path`` / ``new_path`` payload
    fields; ``emit_cleared`` flags the auto-clear emit.
    """

    kind: str
    home: str
    pending: tuple[str, ...]
    emit_old: str
    emit_new: str
    emit_cleared: bool

    @property
    def previous(self) -> str:
        """The oldest pending home (the ``_previous`` marker), or ``""``."""
        return self.pending[0] if self.pending else ""


def _dedupe_exclude(homes: list[str], exclude: str) -> tuple[str, ...]:
    """Return *homes* with duplicates and *exclude* removed, order preserved.

    First occurrence wins, so the oldest position of a repeated home is kept.
    """
    seen: set[str] = set()
    result: list[str] = []
    for home in homes:
        if not home or home == exclude or home in seen:
            continue
        seen.add(home)
        result.append(home)
    return tuple(result)


def compute_pending_home_transition(
    stored_home: str,
    current_home: str,
    pending: list[str],
) -> PendingHomeTransition:
    """Decide how the pending-home set transitions for one detected home change.

    *stored_home* is the last-seen ``retrodeck_home_path`` (``""`` on first
    run); *current_home* is the live home (the caller has already confirmed it
    is non-empty and a real directory); *pending* is the current pending set
    (oldest→newest, ``[]`` when no migration is pending).

    The rules:

    - First run (*stored_home* empty) → ``first_run``.
    - No change (*current_home* == *stored_home*) → ``unchanged``.
    - Simple revert (*current_home* is the sole pending home) → ``cleared``:
      the one home we left is the one we came back to, so nothing is pending.
      This preserves the shipped single-hop revert UX.
    - Otherwise → ``changed``: the new pending set is the old set plus the home
      we are leaving (*stored_home*), deduped, with the home we are arriving at
      (*current_home*) removed. This covers a fresh change, a change chained on
      top of a pending one (#1042), a revert onto an older pending home while
      later hops remain, and moving back onto an existing hop — all uniformly.
    """
    if not stored_home:
        return PendingHomeTransition("first_run", current_home, (), "", "", False)
    if current_home == stored_home:
        return PendingHomeTransition("unchanged", stored_home, tuple(pending), "", "", False)

    previous = pending[0] if pending else ""
    hops = pending[1:]
    if current_home == previous and not hops:
        return PendingHomeTransition("cleared", current_home, (), previous, current_home, True)

    new_pending = _dedupe_exclude([*pending, stored_home], current_home)
    return PendingHomeTransition("changed", current_home, new_pending, new_pending[0], current_home, False)


def pending_homes_from_kv(previous: str, hops_raw: str | None) -> list[str]:
    """Reassemble the pending-home list from its two kv_config values.

    *previous* is ``retrodeck_home_path_previous`` (the oldest pending home, or
    ``""`` when no migration is pending); *hops_raw* is the JSON array stored
    under ``retrodeck_home_path_hops`` (the additional pending homes,
    oldest→newest), or ``None`` when absent (the common single-hop case).
    Returns ``[]`` when nothing is pending.

    A corrupt or unexpectedly-shaped ``_hops`` value degrades to "no hops"
    (the ``previous`` marker is still honoured) rather than propagating — see
    :func:`_decode_hops`.
    """
    if not previous:
        return []
    return [previous, *_decode_hops(hops_raw)]


def _decode_hops(hops_raw: str | None) -> list[str]:
    """Decode the ``_hops`` JSON array defensively into a list of homes.

    This runs on every plugin startup (the detect path and the install prune),
    so a hand-edited or truncated kv value must never crash the load. Returns
    ``[]`` on any of: absent value, JSON decode error, a decoded value that is
    not a list, or a list carrying a non-string / empty-string entry. In
    particular a bare JSON string (``'"str"'``) decodes to a str and is
    rejected here rather than being spread into single characters upstream.
    """
    if not hops_raw:
        return []
    try:
        value = json.loads(hops_raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list) or not all(isinstance(home, str) and home for home in value):
        return []
    return value


def match_pending_base(path: str, pending_homes: list[str]) -> str | None:
    """Return the longest pending home that is a path-prefix of *path*, else None.

    Longest-prefix wins so a home nested inside another (``/a`` and ``/a/b``)
    maps a path to its most specific base. The ``os.sep`` guard rejects a
    false prefix match (``/foo`` does not match ``/foobar/x``). A *path* under
    the current home (or under no pending home) returns ``None`` — the caller
    skips it.
    """
    best: str | None = None
    for home in pending_homes:
        if home and path.startswith(home + os.sep) and (best is None or len(home) > len(best)):
            best = home
    return best


def remap_under_current(path: str, base: str, current_home: str) -> str:
    """Map *path* (living under *base*) to the same relative spot under *current_home*."""
    return os.path.join(current_home, os.path.relpath(path, base))


def stranded_source_candidates(rel: str, matched_base: str, pending_homes: list[str]) -> list[str]:
    """Ordered source paths to probe for a record missing at its stored path.

    When a tracked record's stored path is gone on disk (e.g. an interrupted
    earlier migration left the file under an intermediate home), the file may
    still sit at ``<home>/rel`` under another pending home. This returns those
    candidate paths across the pending homes other than *matched_base*,
    newest-first (*pending_homes* is oldest→newest). Pure ordering only — the
    service performs the ``exists`` probes and takes the first hit.
    """
    return [os.path.join(home, rel) for home in reversed(pending_homes) if home and home != matched_base]
