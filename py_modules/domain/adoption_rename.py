"""What must move so an adopted candidate becomes indistinguishable from a download.

Owns the plan: every source → target pair the rename consists of, and — given
what is already occupied — which of those pairs collide. Pure, and computed in
full before a single file is touched, because that ordering is the requirement.
Renaming as you go and asking at the first collision leaves half the set moved
when the question appears.

Why the saves move at all is a lifecycle argument. ``compute_local_save_target``
derives a save's filename from the **local** ROM's basename, deliberately,
because that is the string RetroArch uses to look up SRAM. An uninstall drops the
ROM and its row but never the saves (ADR-0007), so a game adopted under the
user's own name and later uninstalled leaves saves under that name for a
re-download that will look for the canonical one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ROM = "rom"
SAVE = "save"
SAVESTATE = "savestate"

OVERWRITE = "overwrite"
KEEP = "keep"


@dataclass(frozen=True)
class RenamePair:
    """One file the adoption has to carry, and where it has to end up."""

    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class CompanionDir:
    """A directory holding files named after the ROM: where they are, where they go, what is there.

    *source_dir* and *target_dir* differ whenever the resolved directory itself
    depends on the ROM's name — RetroArch's content sorting names the subdirectory
    after the folder the ROM sits in, so renaming a multi-file ROM's directory
    moves its whole save subdirectory while leaving each filename alone. For a
    single-file ROM the two are equal and the filenames are what change. One rule
    covers both.
    """

    kind: str
    source_dir: str
    target_dir: str
    names: tuple[str, ...]


def rename_pairs(
    *,
    rom_source: str,
    rom_target: str,
    stem_source: str,
    stem_target: str,
    companions: tuple[CompanionDir, ...],
) -> tuple[RenamePair, ...]:
    """Every source → target pair this adoption consists of, ROM first.

    *stem_source* and *stem_target* are the ROM's identity from RetroArch's
    perspective — the launch file's basename without its extension — and are
    passed in rather than derived from *rom_source*: for a multi-file ROM the
    launch file sits **inside** the directory being renamed, so the directory's
    name and the stem are two different strings.

    A companion file belongs to this ROM when its name is the stem or the stem
    followed by a dot, which is what tells ``Game (U).state.auto`` apart from
    ``Game (U) 2.srm`` and keeps ``Example Quest`` from claiming
    ``Example Quest - Second Journey``'s saves. An empty *stem_source* matches
    nothing — as a prefix it would claim every file in the directory.

    Pairs whose source and target are the same path are dropped: there is nothing
    to move, and a self-rename staged as a hardlink would fail on its own target.
    Two companion directories that resolve to the same place contribute each file
    once.
    """
    pairs: list[RenamePair] = []
    seen: set[str] = set()
    for source, target, kind in _raw_pairs(rom_source, rom_target, stem_source, stem_target, companions):
        if source == target or source in seen:
            continue
        seen.add(source)
        pairs.append(RenamePair(source=source, target=target, kind=kind))
    return tuple(pairs)


def _raw_pairs(
    rom_source: str,
    rom_target: str,
    stem_source: str,
    stem_target: str,
    companions: tuple[CompanionDir, ...],
):
    """Yield ``(source, target, kind)`` for the ROM and every companion file it owns."""
    yield (rom_source, rom_target, ROM)
    if not stem_source:
        return
    for companion in companions:
        for name in companion.names:
            if name != stem_source and not name.startswith(stem_source + "."):
                continue
            renamed = stem_target + name[len(stem_source) :]
            yield (
                os.path.join(companion.source_dir, name),
                os.path.join(companion.target_dir, renamed),
                companion.kind,
            )


def split_collisions(
    pairs: tuple[RenamePair, ...], occupied: frozenset[str]
) -> tuple[tuple[RenamePair, ...], tuple[RenamePair, ...]]:
    """Split *pairs* into the ones whose target is free and the ones whose target is taken.

    *occupied* is the set of target paths that already exist, observed for the
    **whole** plan before anything moved. Both halves are returned so the caller
    can state exactly what collides while still knowing what would move either
    way.
    """
    clear = tuple(pair for pair in pairs if pair.target not in occupied)
    colliding = tuple(pair for pair in pairs if pair.target in occupied)
    return (clear, colliding)


def pairs_for_choice(
    clear: tuple[RenamePair, ...], colliding: tuple[RenamePair, ...], choice: str
) -> tuple[RenamePair, ...] | None:
    """The pairs to move under the user's answer to the collision, or ``None`` if it is not one.

    ``overwrite`` moves everything and the caller removes the occupied targets
    first. ``keep`` moves only what is clear: the occupied targets stay and the
    old-named files stay where they are — nothing is lost, but they are orphaned,
    which is the caller's to say out loud rather than imply the move was clean.
    """
    if choice == OVERWRITE:
        return clear + colliding
    if choice == KEEP:
        return clear
    return None


def collision_refusal(colliding: tuple[RenamePair, ...]) -> dict[str, object]:
    """The refusal an adoption returns when a name it needs is already taken.

    Returned **before** a single file has been touched, with every collision in
    it, so the one decision the dialog asks for covers the whole set. Asking at
    the first collision would mean asking with half the set already moved.
    """
    return {
        "success": False,
        "reason": "rename_collisions",
        "message": (
            f"'{os.path.basename(colliding[0].target)}' already exists"
            if len(colliding) == 1
            else "Some of this game's files already exist under the name it would take"
        ),
        "collisions": [
            {"name": os.path.basename(pair.target), "path": pair.target, "kind": pair.kind} for pair in colliding
        ],
    }
