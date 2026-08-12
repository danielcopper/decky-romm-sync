"""Adapter-produced shapes for ROM adoption.

The filesystem answers the adoption question in three forms: one ``stat`` of the
path a download would write to, — when that path is a directory — the per-file
inventory a manifest comparison walks, and — when it is an archive — the
inventory its central directory states. All cross the ``DownloadFileStore``
seam, so their shapes live here rather than in ``domain/`` (which may not import
``models``) or inside the adapter.
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


class ArchiveMemberInfo(TypedDict):
    """One entry of an archive's central directory, read without decompressing.

    ``name`` is the member's path inside the archive, ``size_bytes`` its
    uncompressed size and ``crc32`` the CRC32 of its uncompressed bytes as eight
    lowercase hex digits — the same rendering RomM publishes.
    """

    name: str
    size_bytes: int
    crc32: str
