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

from typing import Literal, TypedDict

# The entry-kind vocabulary, spelled a second time. ``domain.rom_candidates``
# owns it and states why it is closed, but ``models`` may not import ``domain``
# (the layer contract in ``.importlinter``), so the two declarations sit side by
# side. They are not held together by convention: every value crossing this
# boundary is checked against both spellings — the adapter assigns a domain
# ``Kind`` into these dicts, and the search reads them back into ``LocalName`` —
# so a fourth value added to one and not the other fails the type check.
Kind = Literal["file", "dir", "link"]


class ExistingContent(TypedDict):
    """What occupies a ROM target path, and enough about it to ask the user.

    ``size_bytes`` is the file's own size, or the recursive total of a
    directory's contents so it is comparable with the server's ``fs_size_bytes``
    for a multi-file ROM. ``modified_at`` is POSIX epoch seconds.

    ``kind`` is what is in the way, judged without following it, and it is what
    separates content that can be adopted from content that cannot: only a file
    or a directory can become an install row, because a row has to be removable
    and the uninstall path refuses a link.

    It is the one place in this module where the kind may be **absent**. The
    listings simply leave out what is neither file, directory nor link; this
    describes one named path, and something that is there must never be reported
    as nothing — that is how a download came to replace a symlink in silence.
    ``None`` says "occupied, by something this plugin has no word for", which is
    a refusal to describe rather than a fourth kind.
    """

    path: str
    kind: Kind | None
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

    ``kind`` is what the entry **is**, judged without following it. There is no
    fourth value and no absent one — a FIFO, a socket or a device node is not
    listed at all, because "file or directory" has no truthful answer for one.

    The listing deliberately does not descend: a single multi-file install can
    hold tens of thousands of files, and a user's own subfolders are their
    filing.
    """

    name: str
    path: str
    kind: Kind


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
