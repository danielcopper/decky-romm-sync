"""Flatpak install-root resolution for RetroDECK's app files tree.

Owns the read-only filesystem knowledge of *where* a flatpak app's
``files`` tree lives across the install roots flatpak supports. Both the
ES-DE config reader and the RetroArch core-info reader probe the same
``<root>/app/<app_id>/current/active/files`` directory; this module is the
single source of that path layout so the two adapters stay consistent.

Two install roots are probed, in priority order: the system-wide
``/var/lib/flatpak`` and the per-user ``<user_home>/.local/share/flatpak``.
Custom installations declared under ``/etc/flatpak/installations.d`` are
deliberately out of scope — "RetroDECK on SD" in practice means the game
*data* lives on the SD card (resolved via ``retrodeck.json``), not the
flatpak app itself.
"""

from __future__ import annotations

import os

_DEFAULT_APP_ID = "net.retrodeck.retrodeck"

# System-wide flatpak install root. Module-level so tests can repoint it at a
# fabricated tmp tree (the per-user root derives from ``user_home``, which is
# already injectable).
SYSTEM_FLATPAK_ROOT = "/var/lib/flatpak"

_USER_FLATPAK_SUFFIX = os.path.join(".local", "share", "flatpak")

# Within a flatpak install root, the active version's files tree lives under
# ``app/<app_id>/current/active/files`` (``current/active`` is a flatpak-
# maintained symlink to the live deployment).
_APP_FILES_RELATIVE = os.path.join("current", "active", "files")


def flatpak_app_files_dirs(user_home: str, app_id: str = _DEFAULT_APP_ID) -> list[str]:
    """Return existing ``<root>/app/<app_id>/current/active/files`` dirs across flatpak roots.

    Roots probed, in priority order:
      1. system : ``/var/lib/flatpak``
      2. user   : ``<user_home>/.local/share/flatpak``

    ``current/active`` is a symlink flatpak maintains, so existence is checked
    via :func:`os.path.exists` (which follows symlinks). Only dirs that exist on
    disk are returned; an empty list means the app's flatpak is not installed.
    """
    roots = [
        SYSTEM_FLATPAK_ROOT,
        os.path.join(user_home, _USER_FLATPAK_SUFFIX),
    ]
    files_dirs = []
    for root in roots:
        files_dir = os.path.join(root, "app", app_id, _APP_FILES_RELATIVE)
        if os.path.exists(files_dir):
            files_dirs.append(files_dir)
    return files_dirs
