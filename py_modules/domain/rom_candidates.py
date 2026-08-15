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
from typing import Literal, TypeVar

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

# How many rows the dialog is willing to show. Normalized-name collisions
# concentrate inside a sibling group — the region, language and revision variants
# of one game — and this search covers one platform folder rather than a library,
# so the expected answer is zero or one entry and a handful at most. The cap
# therefore guards a pathological folder rather than an ordinary one, and when it
# bites the caller says so on screen, because a silent truncation reads as "that
# is all there is".
CANDIDATE_LIMIT = 10

# What an entry *is*, judged without following it. The whole vocabulary: an entry
# is one of these three or it is not reported at all.
#
# A symlink is its own kind rather than whatever it points at, because following
# it answers a different question from the one adoption asks. An install row must
# be removable — every uninstall goes through ``claim_source``, which refuses a
# symlink outright — so a link that resolves to a perfectly good ROM still cannot
# become one. Judging the entry itself also costs nothing: the directory read
# already carries the type.
#
# Anything else a filesystem can hold — a FIFO, a socket, a device node — has no
# truthful value in a vocabulary of "file or directory", and inventing one is
# what let a named pipe be offered as a game.
#
# The vocabulary is a ``Literal`` rather than three ``str`` constants so the type
# checker holds it closed. "There is no fourth value" was a docstring promise
# through three review rounds while the code kept acquiring one.
Kind = Literal["file", "dir", "link"]

FILE: Kind = "file"
DIR: Kind = "dir"
LINK: Kind = "link"


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

    ``kind`` is one of :data:`FILE`, :data:`DIR` or :data:`LINK`. There is no
    fourth value and no "unknown": an entry that is none of the three is not
    reported, so nothing downstream has to carry a case for it.
    """

    name: str
    path: str
    kind: Kind


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

    ``Example Quest - Second Journey (Rev 1) (USA).zip`` and
    ``Example Quest - Second Journey (U).zip`` both become
    ``example quest second journey``. Bracketed groups go with their contents,
    everything that is not alphanumeric collapses to a single space, and the
    result is lowercased and trimmed. ``str.isalnum`` is Unicode-aware, so an
    accented title keeps its accents and a non-Latin one survives intact.

    The extension is removed with one ``splitext`` on both sides of the
    comparison rather than against the system's accept-list. The blunt rule is
    symmetric — a name the server spells ``Example Quest 3.0`` loses its ``.0``
    wherever it is read — while an accept-list rule would need a second argument
    and would still have to guess for a directory, which carries no extension at
    all unless ES-DE's collapse put one there.

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
    wanted_names: frozenset[str],
    accepted_extensions: frozenset[str],
    covered_paths: frozenset[str],
) -> tuple[EntryT, ...]:
    """Every entry that could be the ROM *wanted_names* names, cheapest test first.

    *wanted_names* is a set because one ROM can be known under more than one
    normalized name: the game-detail page matches ``roms.fs_name`` while the
    download derives its own local filename, and for a ROM the server serves as
    a folder around a differently-named file the two are different strings. The
    user's copy is named after the game, so a search under the derived name
    alone would never find it. Empty names are dropped, and an empty set matches
    nothing — read as a value, an empty normalization equals every other one.

    Shape is deliberately **not** filtered here. Every namesake comes back and
    the caller decides what each one means, because "it is the wrong shape" and
    "it is a link" are things to tell the user about rather than reasons to
    pretend an entry is not there.

    Three filters, each of which alone would be too weak:

    * **Extension** — for anything that is not a directory, a positive test
      against *accepted_extensions* (ES-DE's live per-system ``<extension>``
      list). ``systeminfo.txt``, ``.directory`` and every other frontend's
      bookkeeping fall out without a blacklist anyone has to maintain. An
      **empty** set means the source could not answer, and the test is skipped
      rather than turned into a refusal — the same default-safe reading every
      other consumer of that list applies. A directory is never
      extension-tested: it usually carries none, and excluding it would exclude
      the whole multi-file case.
    * **Install rows** — anything a ``rom_installs`` row already accounts for is
      another ROM's content, and the plugin's claim on it is that row.
    * **Name** — the normalized names must be equal. An empty normalization
      matches nothing, on either side.

    *covered_paths* holds the ``file_path`` and ``rom_dir`` of every install row;
    the path a download of *this* ROM would write to belongs in there too, so a
    free target is never offered back as a candidate for itself.
    """
    wanted = frozenset(name for name in wanted_names if name)
    if not wanted:
        return ()
    return tuple(
        entry
        for entry in entries
        if entry.path not in covered_paths
        and (entry.kind == DIR or not accepted_extensions or _extension_of(entry.name) in accepted_extensions)
        and normalize_rom_name(entry.name) in wanted
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
        is_dir=entry.kind == DIR,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
        evidence=evidence,
        detail=_EVIDENCE_DETAIL[evidence],
    )


def _evidence_for(entry: LocalEntry, server_size: int, server_crc32: str, members: tuple[str, ...]) -> str:
    """The strongest of the three claims *entry* supports without a byte being read."""
    if server_crc32 and len(members) == 1 and members[0] == server_crc32:
        return CRC32_MATCH
    if server_size and entry.kind != DIR and entry.size_bytes == server_size:
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


# What the refusal's own sentence calls each thing. It has to read as a sentence
# on its own: the dialog renders its own copy from the payload, but this message
# is what a toast shows if that dialog never opens.
_SERVED_WORD = {True: "folder", False: "single file"}
_KIND_WORD = {FILE: "a single file", DIR: "a folder", LINK: "a shortcut to somewhere else"}
_KIND_PLURAL = {FILE: "single files", DIR: "folders", LINK: "shortcuts to somewhere else"}


def _namesake_message(shown: tuple[LocalName, ...], *, count: int, served_dir: bool) -> str:
    """One sentence naming what was found and why none of it can be this game.

    *count* is how many were found and *shown* is the ones that were looked at,
    which is why both are needed and nothing else is: whether the list was cut is
    the difference between *count* and the length of *shown*.

    The two halves are chosen separately because the reasons are not the same
    reason. A wrong shape is only wrong *against what the server sends*, so that
    clause has to name the served shape. A link is unusable on its own terms, and
    appending "and the server sends this game as a single file" to one invites
    exactly the wrong reading — that the shortcut would have been fine had the
    server served a folder.

    What was found is stated by kind where the kind is known for all of it: a
    capped list saw only the first few, so it says "entries" rather than claiming
    the ones it never looked at were folders too.
    """
    kinds = {entry.kind for entry in shown}
    only_kind = next(iter(kinds)) if len(kinds) == 1 and count == len(shown) else None
    if count == 1:
        subject = f"'{shown[0].name}' has this game's name but is {_KIND_WORD[shown[0].kind]}"
    else:
        what = _KIND_PLURAL[only_kind] if only_kind else "entries"
        subject = f"{count} {what} here have this game's name"
    if only_kind == LINK:
        return f"{subject}, which cannot be used as this game whatever it points at"
    if only_kind is None:
        return f"{subject}, and none of them can be used as this game"
    return f"{subject}, and the server sends this game as a {_SERVED_WORD[served_dir]}"


def unusable_namesake_refusal(
    entries: tuple[LocalName, ...],
    *,
    served_dir: bool,
    incoming_name: str,
    incoming_size: int,
    limit: int = CANDIDATE_LIMIT,
) -> dict[str, object]:
    """The refusal a download returns for a namesake it cannot offer to take over.

    Two things reach it, and they are one answer because the user's choice is the
    same for both: an entry whose **shape** is the other one — a loose file where
    the server serves a folder, or a folder where it serves one file — and a
    **symlink**, which is never adoptable whatever it points at, because an
    install row has to be removable and the uninstall path refuses a link.

    Nothing here can be adopted, so there is no candidate; downloading regardless
    would drop a second copy of the game beside the first with no word said,
    after a button that may well have read *Use Existing Files*. So it is asked
    instead: the caller's dialog names both outcomes, and the download proceeds
    only on the user's word.

    The list is capped like the candidate list is, and says so when it was cut —
    a list silently cut short reads as "that is all there is". The sentence
    counts what was **found**, not what is shown, so a capped list never
    understates the folder.

    Raises ``ValueError`` on an empty *entries*: there is no sentence for "no
    unusable namesake", and a caller that reached here without one has a bug the
    refusal must not paper over.
    """
    if not entries:
        raise ValueError("unusable_namesake_refusal needs at least one entry")
    shown = entries[:limit]
    return {
        "success": False,
        "reason": "unusable_namesake",
        "message": _namesake_message(shown, count=len(entries), served_dir=served_dir),
        "incoming": {"name": incoming_name, "size_bytes": incoming_size},
        "existing": [{"name": entry.name, "path": entry.path, "kind": entry.kind} for entry in shown],
        "served_is_dir": served_dir,
        "truncated": len(entries) > len(shown),
    }


def vanished_candidate_refusal(*, incoming_name: str, incoming_size: int) -> dict[str, object]:
    """The refusal a download returns when the game page found a copy and this search did not.

    The backstop, and the last answer in the chain. The page and the click-time
    search read the same folder from different knowledge and have diverged four
    times over — shape, platform directory, matched name, and the listing itself
    — so the button's promise no longer rests on them agreeing. It rests on this:
    if the page said a copy was here and nothing specific can be said now, the
    download stops and says so rather than starting behind a label that read
    *Use Existing Files*.

    The sentence claims no cause, because none is known. What the page found is
    either gone or no longer matches, and both readings are true of the ordinary
    race where the file was deleted between opening the page and pressing.
    """
    return {
        "success": False,
        "reason": "candidate_vanished",
        "message": "What was found on this device is no longer there, or can no longer be matched to this game",
        "incoming": {"name": incoming_name, "size_bytes": incoming_size},
    }
