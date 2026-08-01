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
# verbatim in the tree's command line); ``BASENAME_DISCRIMINATOR`` is the
# deliberately narrow fallback below.
PATH_DISCRIMINATOR = "path"
BASENAME_DISCRIMINATOR = "basename"


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


def match_instance_for_launch_path(instances: Sequence[GameInstance], launch_path: str) -> InstanceMatch | None:
    """Return the instance whose command line runs *launch_path*, or None.

    *launch_path* is the resolved absolute launch target baked into the ROM's
    Steam shortcut. An instance matches when that exact path is one of its argv
    tokens; failing that, when one of its argv tokens has the same **basename**.

    The basename fallback exists because the command lines being matched are read
    from inside the flatpak sandbox, whose mount namespace may expose the ROM
    under a different absolute path than the host one the launch command was
    baked from. It is deliberately narrow — whole-basename equality on a single
    token, never a substring test, so ``a.bin`` cannot match ``aaa.bin`` — and
    the exact-path pass always wins, so a correct absolute match is never
    displaced by a coincidental filename.

    ``None`` means no instance is running this ROM: the caller must signal
    nothing rather than fall back to "some instance", because the tree it would
    hit is another game whose save is being held open.
    """
    if not launch_path:
        return None
    for instance in instances:
        if launch_path in instance.argv:
            return InstanceMatch(instance=instance, discriminator=PATH_DISCRIMINATOR)
    basename = os.path.basename(launch_path)
    if not basename:
        return None
    for instance in instances:
        if any(os.path.basename(token) == basename for token in instance.argv):
            return InstanceMatch(instance=instance, discriminator=BASENAME_DISCRIMINATOR)
    return None
