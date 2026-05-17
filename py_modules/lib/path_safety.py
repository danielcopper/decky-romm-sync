"""Path containment predicates for guarding destructive filesystem ops.

Stateless safety checks that answer "is path X safely inside configured
root Y?". Uses ``os.path.realpath`` to resolve symlinks before comparing,
so callers cannot escape the configured root via a symlink — which means
this is not pure compute (``realpath`` is an ``lstat`` syscall) and
belongs in ``lib/`` rather than ``domain/``. The source of the
configured root (e.g. the ``RetroDeckPaths`` Protocol) stays in
``services/``; this module only consumes the resolved string.
"""

from __future__ import annotations

import os


def is_safe_rom_path(path: str, roms_base: str) -> bool:
    """Return True when ``path`` is safely inside ``roms_base``.

    Two properties must hold:

    1. ``os.path.realpath(path)`` lies strictly inside
       ``os.path.realpath(roms_base) + os.sep`` — equality with the base
       is rejected and symlinks escaping the base are rejected.
    2. The resolved path is at least two segments below the base, so a
       bare platform directory (e.g. ``roms_base/gb/``) does not qualify
       while a file beneath one (e.g. ``roms_base/gb/file.zip``) does.
    """
    resolved = os.path.realpath(path)
    real_base = os.path.realpath(roms_base)
    if not resolved.startswith(real_base + os.sep):
        return False
    rel = os.path.relpath(resolved, real_base)
    parts = rel.split(os.sep)
    return len(parts) >= 2
