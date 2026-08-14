"""Which entry already in the platform directory could be the ROM about to be downloaded.

Owns the search's two judgements: whether two filenames denote the same *game*
once their version tags are stripped, and — among several that do — what the
evidence for each rests on. Pure: the directory listing, the install rows and an
archive's central directory all arrive as values.

The search deliberately looks for the game and not the dump. Whether the bytes
are the same dump is what the digest answers, and answering it is the adopt
dialog's content check (ADR-0028) — never this filter's job.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TypeVar

# Ranked strongest first: what a row rests on decides where it sits in the list.
# ``crc32`` is a checksum the ZIP's own central directory hands over for free and
# ``size`` a number ``stat`` already returned, so neither costs a read — and
# neither is proof. ADR-0028 is explicit that cheap agreement never earns the
# strong claim; it earns a place at the top of the list, which is the user's cue
# to press Check Against Server.
CRC32_MATCH = "crc32"
SIZE_MATCH = "size"
NAME_MATCH = "name"

_EVIDENCE_RANK = {CRC32_MATCH: 2, SIZE_MATCH: 1, NAME_MATCH: 0}

_EVIDENCE_DETAIL = {
    CRC32_MATCH: "Its checksum matches the server's, read from the archive's index",
    SIZE_MATCH: "Exactly the size the server would send",
    NAME_MATCH: "Matched on name only",
}

# How many rows the dialog is willing to show. Measured across every populated
# platform folder on a real device the search returns 1 to 5 entries with zero
# normalized-name collisions, so this is a guard against a pathological folder
# rather than a working limit — and when it bites the caller says so on screen,
# because a silent truncation reads as "that is all there is".
CANDIDATE_LIMIT = 10


@dataclass(frozen=True)
class LocalName:
    """One entry at the platform directory's top level, as ``readdir`` alone saw it.

    Everything :func:`matching_entries` reads and nothing more, which is why it
    is its own type: the game-detail read pays a bare listing for it, while the
    click-time search stats each entry as well and puts the richer
    :class:`LocalEntry` through the same filter. Splitting them is what makes
    "both sides share one filter" a property of the types rather than a promise
    in a docstring — the lean side cannot reach a field it never read, and the
    ranking below cannot be handed entries whose size nobody measured.
    """

    name: str
    path: str
    is_dir: bool


@dataclass(frozen=True)
class LocalEntry(LocalName):
    """A top-level entry plus what a ``stat`` added — what ranking needs.

    ``size_bytes`` is ``0`` for a directory: the search never descends, because a
    single multi-file install can hold tens of thousands of files, so a
    directory's total is not evidence this filter can afford.
    """

    size_bytes: int
    modified_at: float


EntryT = TypeVar("EntryT", bound=LocalName)


@dataclass(frozen=True)
class AdoptionCandidate:
    """One entry the search is willing to offer, and what its offer rests on.

    ``detail`` is the whole sentence the row states, so the list reads as one
    line per candidate. It names the evidence rather than passing a verdict —
    nothing here has read a byte of content.
    """

    name: str
    path: str
    is_dir: bool
    size_bytes: int
    modified_at: float
    evidence: str
    detail: str


def normalize_rom_name(name: str) -> str:
    """Reduce *name* to the game it denotes: no extension, no tags, no punctuation.

    ``Mario Golf - Advance Tour (Rev 1) (USA).zip`` and
    ``Mario Golf - Advance Tour (U).zip`` both become
    ``mario golf advance tour``. Bracketed groups go with their contents,
    everything that is not alphanumeric collapses to a single space, and the
    result is lowercased and trimmed. ``str.isalnum`` is Unicode-aware, so
    ``Pokémon`` keeps its ``é`` and a non-Latin title survives intact.

    The extension is removed with one ``splitext`` on both sides of the
    comparison rather than against the system's accept-list. The blunt rule is
    symmetric — a name the server spells ``Sonic 3.0`` loses its ``.0`` wherever
    it is read — while an accept-list rule would need a second argument and would
    still have to guess for a directory, which carries no extension at all unless
    ES-DE's collapse put one there.

    Returns ``""`` for a name that is nothing but tags. Callers must treat that
    as "matches nothing": read as a value it would match every other empty
    normalization.
    """
    stem = os.path.splitext(name)[0]
    spaced = "".join(char if char.isalnum() else " " for char in _strip_bracketed(stem))
    return " ".join(spaced.split()).lower()


def _strip_bracketed(name: str) -> str:
    """Drop every ``(...)`` and ``[...]`` group from *name*, contents included.

    One depth counter for both bracket kinds, so ``Game (Rev [1])`` nests
    correctly. An unmatched opener swallows the rest of the name and an unmatched
    closer is dropped: both are malformed, and the alternative — keeping the tag
    text — would put a region code into the game's name.
    """
    kept: list[str] = []
    depth = 0
    for char in name:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(depth - 1, 0)
        elif depth == 0:
            kept.append(char)
    return "".join(kept)


def matching_entries(
    entries: tuple[EntryT, ...],
    *,
    wanted_name: str,
    want_dir: bool,
    accepted_extensions: frozenset[str],
    covered_paths: frozenset[str],
) -> tuple[EntryT, ...]:
    """The entries that could be the ROM *wanted_name* names, cheapest test first.

    Four filters, each of which alone would be too weak:

    * **Shape** — a ROM the server serves as one file can only be a file here,
      and one it serves as a directory can only be a directory. The adopt dialog
      already refuses the mismatched shape as unusable, so offering it would be
      offering a row the user cannot take. An entry that matches on everything
      *but* shape is not nothing, though: the caller asks a second time with
      ``want_dir`` inverted and reports it through :func:`shape_conflict_refusal`
      rather than downloading a second copy beside it without a word.
    * **Extension** — for a file, a positive test against *accepted_extensions*
      (ES-DE's live per-system ``<extension>`` list). ``systeminfo.txt``,
      ``.directory`` and every other frontend's bookkeeping fall out without a
      blacklist anyone has to maintain. An **empty** set means the source could
      not answer, and the test is skipped rather than turned into a refusal —
      the same default-safe reading every other consumer of that list applies.
      A directory is never extension-tested: it usually carries none, and
      excluding it would exclude the whole multi-file case.
    * **Install rows** — anything a ``rom_installs`` row already accounts for is
      another ROM's content, and the plugin's claim on it is that row.
    * **Name** — the normalized names must be equal. An empty normalization
      matches nothing, on either side.

    *covered_paths* holds the ``file_path`` and ``rom_dir`` of every install row;
    the path a download of *this* ROM would write to belongs in there too, so a
    free target is never offered back as a candidate for itself.
    """
    if not wanted_name:
        return ()
    return tuple(
        entry
        for entry in entries
        if entry.is_dir == want_dir
        and entry.path not in covered_paths
        and (entry.is_dir or not accepted_extensions or _extension_of(entry.name) in accepted_extensions)
        and normalize_rom_name(entry.name) == wanted_name
    )


def _extension_of(name: str) -> str:
    """The entry's extension as ES-DE spells it in its accept-list: lowercased, dot-led."""
    return os.path.splitext(name)[1].lower()


def rank_candidates(
    matches: tuple[LocalEntry, ...],
    *,
    server_size: int,
    server_crc32: str,
    member_crc32s: dict[str, tuple[str, ...]],
    limit: int = CANDIDATE_LIMIT,
) -> tuple[tuple[AdoptionCandidate, ...], bool]:
    """Order *matches* by what each one's claim rests on, and say whether the list was cut.

    Returns ``(candidates, truncated)``. Strongest evidence first, then by name so
    two rows resting on the same thing keep a stable order.

    *member_crc32s* holds, per entry path, the CRC32 of every member an archive's
    central directory listed. A **single**-member archive is the only one whose
    member CRC32 can be held against *server_crc32*: RomM's file-level digest for
    an archive describes the content inside it, and over one member every hashing
    rule it has used reduces to that member (ADR-0028). Over several the number
    is a composite this plugin cannot attribute, so those rank on size or name
    like anything else.
    """
    candidates = sorted(
        (_describe(entry, server_size, server_crc32, member_crc32s.get(entry.path, ())) for entry in matches),
        key=lambda candidate: (-_EVIDENCE_RANK[candidate.evidence], candidate.name),
    )
    return (tuple(candidates[:limit]), len(candidates) > limit)


def _describe(entry: LocalEntry, server_size: int, server_crc32: str, members: tuple[str, ...]) -> AdoptionCandidate:
    """Attach the strongest evidence *entry* supports to it."""
    evidence = _evidence_for(entry, server_size, server_crc32, members)
    return AdoptionCandidate(
        name=entry.name,
        path=entry.path,
        is_dir=entry.is_dir,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
        evidence=evidence,
        detail=_EVIDENCE_DETAIL[evidence],
    )


def _evidence_for(entry: LocalEntry, server_size: int, server_crc32: str, members: tuple[str, ...]) -> str:
    """The strongest of the three claims *entry* supports without a byte being read."""
    if server_crc32 and len(members) == 1 and members[0] == server_crc32:
        return CRC32_MATCH
    if server_size and not entry.is_dir and entry.size_bytes == server_size:
        return SIZE_MATCH
    return NAME_MATCH


def candidates_refusal(
    candidates: tuple[AdoptionCandidate, ...],
    *,
    truncated: bool,
    incoming_name: str,
    incoming_size: int,
) -> dict[str, object]:
    """The refusal a download returns when this game is already on disk under another name.

    The same shape as the occupied-target refusal it sits beside: nothing was
    written, no transfer started, and both sides of the comparison cross the wire
    so the dialog can be rendered off the reply. *truncated* is stated rather than
    implied — a list silently cut short reads as "that is all there is".
    """
    return {
        "success": False,
        "reason": "adoption_candidates",
        "message": (
            f"'{candidates[0].name}' is already on this device"
            if len(candidates) == 1
            else f"{len(candidates)} files on this device could be this game"
        ),
        "incoming": {"name": incoming_name, "size_bytes": incoming_size},
        "candidates": [
            {
                "name": candidate.name,
                "path": candidate.path,
                "is_dir": candidate.is_dir,
                "size_bytes": candidate.size_bytes,
                "modified_at": candidate.modified_at,
                "evidence": candidate.evidence,
                "detail": candidate.detail,
            }
            for candidate in candidates
        ],
        "truncated": truncated,
    }


# Both halves of the sentence the shape refusal has to say out loud: what is
# lying there, and what the server would send. Singular and plural are spelled
# rather than derived, because "single files" is not "single file" + "s".
_SHAPE_WORD = {True: "folder", False: "single file"}
_SHAPE_WORD_PLURAL = {True: "folders", False: "single files"}


def shape_conflict_refusal(
    entries: tuple[LocalName, ...],
    *,
    served_dir: bool,
    incoming_name: str,
    incoming_size: int,
    limit: int = CANDIDATE_LIMIT,
) -> dict[str, object]:
    """The refusal a download returns for a namesake it cannot offer to take over.

    Raised when the platform folder holds an entry whose normalized name is this
    game's but whose **shape** is the other one — a loose file where the server
    serves a folder, or a folder where it serves one file. Nothing on disk can be
    adopted, so there is no candidate; downloading regardless would drop a second
    copy of the game beside the first with no word said, after a button that may
    well have read *Use Existing Files*.

    So it is asked instead: the caller's dialog names both outcomes, and the
    download proceeds only on the user's word. The list is capped like the
    candidate list is, and says so when it was cut — a list silently cut short
    reads as "that is all there is".
    """
    shown = entries[:limit]
    found_dir = not served_dir
    return {
        "success": False,
        "reason": "shape_conflict",
        "message": (
            f"'{shown[0].name}' has this game's name but is a {_SHAPE_WORD[found_dir]}"
            if len(shown) == 1
            else f"{len(shown)} entries here have this game's name but are {_SHAPE_WORD_PLURAL[found_dir]}"
        )
        + f", and the server sends this game as a {_SHAPE_WORD[served_dir]}",
        "incoming": {"name": incoming_name, "size_bytes": incoming_size},
        "existing": [{"name": entry.name, "path": entry.path, "is_dir": entry.is_dir} for entry in shown],
        "served_is_dir": served_dir,
        "truncated": len(entries) > limit,
    }
