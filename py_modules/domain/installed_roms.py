"""Pure logic for installed_roms state — pre-migration path detection.

Functions that classify or transform ``installed_roms`` entries without
touching the filesystem belong here. Anything that probes disk or
mutates state lives in the corresponding service or adapter.
"""

from __future__ import annotations

import os


def is_pending_migration_path(file_path: str, rom_dir: str | None, pending_home: str) -> bool:
    """Return True when an installed_roms entry lives under a pre-migration home.

    *pending_home* is the previous ``retrodeck_home_path`` value held in
    state while a RetroDECK migration is pending; pass an empty string
    when no migration is pending and the function will return ``False``.
    *rom_dir* is the install's dedicated per-ROM directory, or ``None`` for a
    single-file ROM that owns no folder.

    The check uses the platform path separator so prefix false-matches
    like ``"/foo"`` matching ``"/foobar/x"`` are rejected.
    """
    if not pending_home:
        return False
    prefix = pending_home + os.sep
    return file_path.startswith(prefix) or bool(rom_dir and rom_dir.startswith(prefix))
