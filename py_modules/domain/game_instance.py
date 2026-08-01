"""Which live sandbox instance of an app is running a given ROM.

An app can have several live flatpak instances at once — a second game launched
from another Steam shortcut, or ES-DE opened on its own — and each is an
independent process tree. This module holds the shape one such tree is reported
in and the pure decision that picks the tree belonging to one ROM, so the stop
ladder can signal that tree and nothing else. Gathering the trees is the
process-control adapter's job; nothing here reads a process table.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# How a tree was recognised as this ROM's, reported so the caller can log it.
# ``PATH_DISCRIMINATOR`` is the strong match (the resolved launch path appears
# verbatim in the tree's command line); ``PATH_TAIL_DISCRIMINATOR`` is the
# deliberately narrow fallback below. The two are logged verbatim, so an
# on-device run shows which one a sandboxed command line actually satisfies.
PATH_DISCRIMINATOR = "path"
PATH_TAIL_DISCRIMINATOR = "path-tail"


@dataclass(frozen=True)
class GameInstance:
    """One live sandbox instance of an app: its signal targets and their argv.

    ``pids`` are the tree's processes in the order the ladder must signal them
    (deepest first, sandbox scaffolding excluded). ``argv`` is every command-line
    token read across those processes, flattened — the tree is matched to a ROM
    as a whole, so which of its processes carries the ROM path does not matter.
    A process whose command line could not be read simply contributes nothing.
    """

    pids: tuple[int, ...]
    argv: tuple[str, ...]


@dataclass(frozen=True)
class InstanceMatch:
    """The instance running a ROM, plus how it was recognised."""

    instance: GameInstance
    discriminator: str


def path_tail(path: str) -> str:
    """Return *path*'s last two components — ``<parent>/<file>``.

    ``/home/deck/roms/snes/Aladdin.zip`` → ``snes/Aladdin.zip``. A path with no
    parent component yields the file alone. The ROM's own filename is not
    unique across a library (the same game exists for several platforms, one
    directory each), but the filename **under its platform directory** is — and
    it survives a re-rooted mount, which the bare filename's uniqueness does not
    buy anything extra over.
    """
    parent, name = os.path.split(path)
    return os.path.join(os.path.basename(parent), name)


def match_instance_for_launch_path(instances: Sequence[GameInstance], launch_path: str) -> InstanceMatch | None:
    """Return the instance whose command line runs *launch_path*, or None.

    *launch_path* is the resolved absolute launch target baked into the ROM's
    Steam shortcut. An instance matches when that exact path is one of its argv
    tokens; failing that, when one of its argv tokens has the same **path tail**
    (``<parent>/<file>``, see :func:`path_tail`).

    The tail fallback exists because the command lines being matched are read
    from inside the flatpak sandbox, whose mount namespace may expose the ROM
    under a different absolute path than the host one the launch command was
    baked from. Only the ROOT can differ that way, so comparing the tail rather
    than the whole path is what survives the re-rooting — while still telling
    ``roms/snes/Aladdin.zip`` from ``roms/genesis/Aladdin.zip``, which a bare
    filename cannot, and which matters because the sandbox regime is exactly the
    one where EVERY match is a fallback match. It is component equality, never a
    substring test, so ``a.bin`` cannot match ``aaa.bin``.

    Two refusals guard the fallback, because it is a weaker signal than an exact
    hit. It never runs while an exact hit exists (the whole first pass completes
    first), and it refuses outright when it matches **more than one** instance:
    picking either would be picking by scan order, which is the arbitrary kill
    this function exists to prevent. The exact pass has no such rule — argv
    carrying the identical absolute path IS this ROM, however many trees run it.

    ``None`` means no instance could be attributed to this ROM — nothing matched,
    or too much did. The caller must signal nothing rather than fall back to
    "some instance", because the tree it would hit is another game whose save is
    being held open.
    """
    if not launch_path:
        return None
    for instance in instances:
        if launch_path in instance.argv:
            return InstanceMatch(instance=instance, discriminator=PATH_DISCRIMINATOR)
    if not os.path.basename(launch_path):
        return None
    tail = path_tail(launch_path)
    hits = [instance for instance in instances if any(path_tail(token) == tail for token in instance.argv)]
    if len(hits) != 1:
        return None
    return InstanceMatch(instance=hits[0], discriminator=PATH_TAIL_DISCRIMINATOR)
