"""The vocabulary a firmware answer arrives in, and the one classification over it.

Which emulator wants which file is read live off the machine by the resolver
behind :class:`services.protocols.FirmwareResolver`; ``domain/`` may not import
the vendored resolver at all, so this module holds only the words the answer
comes back in — the same split :mod:`domain.sync_action` makes for the save-sync
core.

The resolver answers per *file* whether a core needs it or merely accepts it.
"Nothing wants it" and "nothing could be established" are not properties of a
file — they are properties of the **reading**, one level up on the core, which is
why :class:`FirmwareCatalogue` names the cores it could not ask. Our own file
list comes from the RomM server rather than from the resolver, so each server
file is classified by whether the catalogue holds a placement for it, and by
whether the reading was complete for the emulators the caller's platform offers:

``needed`` · ``optional`` · ``not_needed`` (the whole reading succeeded and no
emulator asked for it) · ``unknown`` (something in the reading failed, so no
claim can be made either way).

Collapsing the last two into one value is the defect this vocabulary exists to
prevent: a file nothing wants is a finished answer, and a file we could not ask
about is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

WANTED_NEEDED = "needed"
WANTED_OPTIONAL = "optional"
WANTED_NOT_NEEDED = "not_needed"
WANTED_UNKNOWN = "unknown"

WANTED_VALUES = (WANTED_NEEDED, WANTED_OPTIONAL, WANTED_NOT_NEEDED, WANTED_UNKNOWN)


@dataclass(frozen=True)
class FirmwareWant:
    """One emulator's demand for one firmware file.

    ``core_so`` is ``None`` for an emulator that ships no libretro core — the
    identifier space is the core's ``.so`` basename and inventing one for a
    standalone emulator would collide with it. ``required`` is the resolver's
    own two-valued answer: the core will not run without the file, or it will.

    No display label: what a core is called to a user is ES-DE's answer, read
    per platform off ``es_systems.xml``, and a second spelling arriving with the
    requirement would be a second thing to keep in step with it.
    """

    core_so: str | None
    required: bool


@dataclass(frozen=True)
class FirmwarePlacement:
    """One firmware file the machine asks for: where it goes and who wants it.

    ``relative_path`` is the destination relative to the firmware root, so a
    consumer joins it under the BIOS directory it owns rather than trusting an
    absolute path from outside. ``None`` when the file resolves outside that root
    — a standalone emulator keeping its firmware in its own XDG tree — and the
    consumer then has no placement to honour and falls back to its own layout.

    ``wants`` is never empty: a placement exists because at least one emulator
    declared the file, and a placement without an owning emulator is exactly the
    orphaned entry this model removes.
    """

    file_name: str
    relative_path: str | None
    description: str
    wants: tuple[FirmwareWant, ...]

    @property
    def required_by_any(self) -> bool:
        """Does any emulator refuse to run without this file?"""
        return any(want.required for want in self.wants)

    @property
    def destination(self) -> str:
        """Where the file belongs under the BIOS directory, relative to its root.

        The resolver's own placement where it has one, and the bare file name
        otherwise — the flat default that has always applied to a file with no
        stated subdirectory. Callers still join this under their own root
        through ``safe_join``; it is a path segment, not an address.
        """
        return self.relative_path or self.file_name


@dataclass(frozen=True)
class FirmwareCatalogue:
    """Everything the installed emulators want, and which of them could not be asked.

    ``placements`` is machine-wide: a file is declared, or it is not, and the
    answer does not change with the surface asking. What DOES depend on the
    caller is whether an absence may be read as "nothing wants it", and that is
    :meth:`reading_complete_for` — the emulators that could not be asked are
    named in ``unread_cores``, so a caller scopes the doubt to the emulators its
    platform actually offers instead of letting one unreadable core anywhere
    silence every answer.

    ``resolved`` is ``False`` when the reading did not happen at all — no
    installation found, or the resolver refused. Then nothing may be ruled out
    for anyone, whatever the scope.

    ``caveats`` carries the resolver's stable degradation codes, never its human
    messages: the codes are the contract, the messages are prose.
    """

    placements: tuple[FirmwarePlacement, ...]
    unread_cores: frozenset[str]
    resolved: bool
    caveats: tuple[str, ...] = ()

    def by_file_name(self) -> dict[str, FirmwarePlacement]:
        """The placements indexed by file name — the shape every lookup wants.

        A caller classifying a whole server listing builds this once and reads
        it per file; one built per file would rescan a few hundred placements
        for every row.
        """
        return {placement.file_name: placement for placement in self.placements}

    def reading_complete_for(self, core_sos: Collection[str] | None) -> bool:
        """May an absence be read as "nothing wants it" for the scope *core_sos*?

        ``core_sos`` is the scope the caller is answering for — the libretro
        cores its platform offers — and the question is whether every one of
        them was asked. ``None`` means the caller could not establish its own
        scope, which is itself a reason to rule nothing out.

        **An empty scope is refused too**, though every emulator in it was
        vacuously asked. Read as complete it would license the strongest claim
        this vocabulary can make — "no emulator here wants any of these files" —
        off asking nobody, which is the very collapse the four values exist to
        prevent. A caller reaching this with no core to name has not established
        a reading, whether it says so with ``None`` or with an empty list, so
        both answer the same. That makes the refusal belt-and-braces rather than
        the only guard: ``FirmwareService._core_scope`` already returns ``None``
        for a platform ES-DE offers no libretro core for.
        """
        if not self.resolved or not core_sos:
            return False
        return not self.unread_cores.intersection(core_sos)


def classify_wanted(placement: FirmwarePlacement | None, complete: bool) -> str:
    """Classify one server file against the catalogue — one of :data:`WANTED_VALUES`.

    *placement* is the catalogue's entry for the file, ``None`` when it holds
    none; *complete* is the catalogue's own reading state.
    """
    if placement is not None:
        return WANTED_NEEDED if placement.required_by_any else WANTED_OPTIONAL
    return WANTED_NOT_NEEDED if complete else WANTED_UNKNOWN
