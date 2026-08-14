"""Adapter-produced shapes for ROM adoption.

The filesystem answers the adoption question in several forms: one ``stat`` of
the path a download would write to, — when that path is a directory — the
per-file inventory a manifest comparison walks, — when it is an archive — the
inventory its central directory states, the platform directory's top level the
candidate search reads, and the truthful outcome of carrying an adopted
candidate to its canonical name. All cross a file-store seam, so their shapes
live here rather than in ``domain/`` (which may not import ``models``) or inside
the adapters.
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


class TopLevelName(TypedDict):
    """One top-level entry as the directory read alone described it.

    Name, full path, and whether it is a directory — what a name match reads, and
    what ``scandir`` hands over without a further syscall. The listing
    deliberately does not descend: a single multi-file install can hold tens of
    thousands of files, and a user's own subfolders are their filing.
    """

    name: str
    path: str
    is_dir: bool


class TopLevelEntry(TopLevelName):
    """A top-level entry plus what a ``stat`` per entry added.

    ``size_bytes`` is ``0`` for a directory — a directory's recursive total is not
    something this read pays for, for the reason above. ``modified_at`` is POSIX
    epoch seconds.
    """

    size_bytes: int
    modified_at: float


class MoveOutcome(TypedDict):
    """Truthful result of carrying a set of files to new names.

    The three lists partition the pairs that were attempted, by where the content
    ended up:

    ``moved`` — target paths that now hold the content, with the source gone.
    ``stranded`` — source paths that survive **beside** a completed target. One
    inode under two names: nothing is lost and a re-run finishes the job, which
    is why this is not a failure.
    ``unmoved`` — source paths still holding the content where they were, with no
    target created. The move did not happen for these.

    ``error`` is the empty string when nothing failed. A non-empty ``error`` with
    an empty ``unmoved`` means the content all arrived and something untidy
    remains behind it.
    """

    moved: list[str]
    stranded: list[str]
    unmoved: list[str]
    error: str
