"""Pure naming + retention rules for the local ``.romm-backup`` quarantine.

The matrix executor owns the I/O; the collision-free name and the prune
set are decided here so both rules are unit-testable without touching the
filesystem.
"""

from __future__ import annotations

import os
import re

BACKUP_DIR_NAME = ".romm-backup"
"""Name of the per-saves-directory quarantine folder every removed save is moved into."""

_TS = r"\d{8}_\d{6}"


def backup_name(filename: str, ts: str, existing: set[str]) -> str:
    """Return a collision-free backup name for *filename* at timestamp *ts*.

    Builds ``<name>_<ts><ext>`` from *filename* and *ts*. When that name is
    already present in *existing*, a ``_<n>`` counter (``_1``, ``_2``, …) is
    appended before the extension until the name is unused — so a multi-file
    slot whose files all back up in the same second never overwrites an earlier
    backup (#974).
    """
    name, ext = os.path.splitext(filename)
    candidate = f"{name}_{ts}{ext}"
    n = 1
    while candidate in existing:
        candidate = f"{name}_{ts}_{n}{ext}"
        n += 1
    return candidate


def select_backups_to_prune(filename: str, existing: list[str], keep: int) -> list[str]:
    """Return the oldest backups of *filename* to delete so only *keep* remain.

    Considers only entries in *existing* that are a backup of *filename* —
    ``<name>_<ts>[_<n>]<ext>`` — because the ``.romm-backup`` directory is
    shared across every save file under the saves directory, so a sibling
    file's backups must be ignored. The oldest ``len(matches) - keep`` entries
    (by a plain lexicographic sort) are returned and the newest *keep* are
    retained; a *keep* of ``0`` or less disables pruning and returns ``[]``.

    Sort caveat: the fixed-width zero-padded ``<ts>`` makes the sort
    chronological *across* seconds. *Within* one second the ``_<n>`` collision
    counter is **not** zero-padded (``_10`` sorts before ``_2``), so same-second
    ordering beyond the first collision is only approximate — immaterial, since
    those copies share a second. One consequence matters to callers: a base
    name (``<name>_<ts><ext>``) that was freed by an earlier prune and then
    reused for a *newer* backup sorts *oldest* (``.`` < ``_``), so a caller that
    prunes in the same call that creates a backup MUST exclude the just-created
    name — otherwise the cap would delete the live save it just quarantined
    (see :meth:`MatrixExecutor.quarantine_local_file`).
    """
    if keep <= 0:
        return []
    name, ext = os.path.splitext(filename)
    pattern = re.compile(rf"^{re.escape(name)}_{_TS}(_\d+)?{re.escape(ext)}$")
    matches = sorted(b for b in existing if pattern.match(b))
    if len(matches) <= keep:
        return []
    return matches[: len(matches) - keep]


def is_backup_for(filename: str, candidate: str) -> bool:
    """Return whether *candidate* is a quarantine backup of *filename*."""
    name, ext = os.path.splitext(filename)
    return re.fullmatch(rf"{re.escape(name)}_{_TS}(?:_\d+)?{re.escape(ext)}", candidate) is not None
