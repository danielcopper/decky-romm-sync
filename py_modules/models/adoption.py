"""Adapter-produced shapes for ROM adoption.

The filesystem answers the adoption question in two forms: one ``stat`` of the
path a download would write to, and — when that path is a directory — the
per-file inventory a manifest comparison walks. Both cross the
``DownloadFileStore`` seam, so their shapes live here rather than in ``domain/``
(which may not import ``models``) or inside the adapter.
"""

from __future__ import annotations

from typing import TypedDict


class ExistingContent(TypedDict):
    """What one ``stat`` found at an occupied ROM target path.

    ``size_bytes`` is the file's own size, or the recursive total of a
    directory's contents so it is comparable with the server's ``fs_size_bytes``
    for a multi-file ROM. ``modified_at`` is POSIX epoch seconds.
    """

    path: str
    is_dir: bool
    size_bytes: int
    modified_at: float
