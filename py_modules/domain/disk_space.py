"""Whether what a download needs still fits in what the card has left.

Pure arithmetic over numbers the caller measures — the size the server states,
the free space it read, the bytes sibling downloads have already claimed, and
the bytes a resumable transfer already wrote. Reading them is the download
service's concern; what they add up to, and the two figures a refusal shows the
user, are decided here.
"""

from __future__ import annotations

from dataclasses import dataclass

# Headroom a download must leave unclaimed. An emulator writes saves, states and
# shader caches beside the ROM it just fetched, so a transfer that fills the card
# to its last byte breaks the thing it was fetched for.
_HEADROOM_BYTES = 100 * 1024 * 1024

_BYTES_PER_MB = 1024 * 1024


@dataclass(frozen=True)
class DiskSpaceVerdict:
    """Whether a download fits, what it claims while it runs, and what a refusal says.

    *needed_bytes* is the figure the caller reserves against concurrent
    pre-flights, so what a download claims is exactly what it was weighed
    against. The megabyte figures are what the refusal is read in, derived from
    the same numbers as *fits* — a refusal can never quote a free space that
    would have passed.
    """

    fits: bool
    needed_bytes: int
    free_mb: int

    @property
    def needed_mb(self) -> int:
        """*needed_bytes* in the unit the refusal states it in."""
        return self.needed_bytes // _BYTES_PER_MB


def disk_space_verdict(
    *,
    file_size: int,
    free_space: int,
    reserved_bytes: int,
    multi_file: bool,
    already_on_disk: int = 0,
) -> DiskSpaceVerdict:
    """Decide whether *file_size* still fits once every other claim is counted.

    A multi-file ROM is counted **twice**: it arrives as a ZIP and is extracted
    beside it, so both exist at once. *reserved_bytes* is what sibling downloads
    have claimed but not yet written — without it two concurrent pre-flights
    could each pass on space only one of them fits (#1053). *already_on_disk* is
    what a resumable transfer already wrote, which is not needed a second time,
    so a near-complete resume is not refused for the full size.

    A ROM whose size the server did not state (*file_size* of 0) always fits:
    there is nothing to weigh, and refusing on an absent number would be a claim
    the plugin cannot make.
    """
    needed = max((file_size * 2 if multi_file else file_size) + _HEADROOM_BYTES - already_on_disk, 0)
    available = free_space - reserved_bytes
    return DiskSpaceVerdict(
        fits=not file_size or available >= needed,
        needed_bytes=needed,
        free_mb=max(available, 0) // _BYTES_PER_MB,
    )
