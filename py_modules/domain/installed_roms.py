"""Pure logic for installed_roms state — pre-migration path detection.

Functions that classify or transform ``installed_roms`` entries without
touching the filesystem belong here. Anything that probes disk or
mutates state lives in the corresponding service or adapter.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def is_pending_migration_path(file_path: str, rom_dir: str | None, pending_homes: Sequence[str]) -> bool:
    """Return True when an installed_roms entry lives under any pending home.

    *pending_homes* is the set of previous ``retrodeck_home_path`` values held
    in state while a RetroDECK migration is pending (the ``_previous`` marker
    plus any additional hops, #1042); pass an empty sequence when no migration
    is pending and the function will return ``False``. *rom_dir* is the
    install's dedicated per-ROM directory, or ``None`` for a single-file ROM
    that owns no folder.

    The check uses the platform path separator so prefix false-matches
    like ``"/foo"`` matching ``"/foobar/x"`` are rejected.
    """
    for pending_home in pending_homes:
        if not pending_home:
            continue
        prefix = pending_home + os.sep
        if file_path.startswith(prefix) or bool(rom_dir and rom_dir.startswith(prefix)):
            return True
    return False
