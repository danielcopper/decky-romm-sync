"""The path a ROM's content occupies — the one type this package's parts share.

Both halves of adoption speak in terms of it: the service resolves it from RomM's
payload and decides what may happen there, the renamer derives every save and
savestate path from it. It lives here rather than in either so neither imports
the other for it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """Where a ROM's content belongs, and what the plugin expects to find there.

    ``manifest_name`` is the name the server's manifest uses for the single-file
    case, which need not equal the on-disk name the download derives from
    ``fs_name``; for a directory it is the directory's own name and unused.
    """

    path: str
    system: str
    is_multi: bool
    manifest_name: str
