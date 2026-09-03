"""Atlas firmware adapter — the seam through which firmware questions reach the resolver.

The single place the vendored `emu-atlas <https://github.com/danielcopper/emu-atlas>`_
resolver is asked what the installed emulators want. It implements the
``FirmwareResolver`` Protocol, so services see a :class:`domain.firmware_wants.FirmwareCatalogue`
and never an atlas type — which is not a stylistic choice: ``domain/`` may not
import ``_vendor`` at all (the ``domain-stdlib-only`` contract), so the vocabulary
and the resolver have to meet at an adapter.

Two properties of the resolver decide this module's shape:

- **It raises on its own invariant violations** rather than returning a
  degraded answer — a ``ValueError`` out of an answer's ``__post_init__``, a
  strict packaged-data loader — and promises nothing in writing about not
  raising. So every call is wrapped, and a failure becomes a catalogue with no
  placements and ``resolved`` clear. That reads downstream as "nothing could be
  established", never as "nothing is needed": the second would clear a real
  BIOS warning off a game that cannot launch without the file.
- **It never logs.** Caveats are its whole degradation channel, and their
  ``code`` is the stable half of that contract while ``message`` is prose that
  may change freely. The codes are carried on the catalogue and traced through
  the injected debug logger; nothing here parses a message.

One question per call, machine-wide: ``firmware_inventory()`` costs 115-325 ms
and memoises nothing, so asking it once per platform would multiply that by the
platform count. Nothing is cached here — a firmware answer is about files on
disk that the user is actively adding and removing, and a cached one would
outlive the download that changed it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from _vendor.atlas import KIND_DIRECTORY, detect

from domain.firmware_wants import FirmwareCatalogue, FirmwarePlacement, FirmwareWant

if TYPE_CHECKING:
    from collections.abc import Callable

# Declaration states in which the core stated what it wants. ``read`` is its own
# ``.info`` off the machine; ``packaged`` is a rule card for an emulator that
# ships none. Every other state — ``unreadable``, ``absent``, ``unsupported`` —
# means the emulator was NOT asked, which is what ``unread_cores`` collects.
_STATED_DECLARATIONS = frozenset({"read", "packaged"})

_CORE_SO_SUFFIX = ".so"


def _plugin_core_so(core_so: str | None) -> str | None:
    """Atlas's ``mgba_libretro.so`` in the plugin's own identifier space.

    Every core identifier the plugin holds — the resolved active core, the
    ``platform_cores`` override, an ES-DE ``<command>``'s core — is the bare
    ``.so`` basename without its extension. Comparing the two spellings without
    normalising here would silently match nothing.
    """
    if core_so is None:
        return None
    return core_so[: -len(_CORE_SO_SUFFIX)] if core_so.endswith(_CORE_SO_SUFFIX) else core_so


class AtlasFirmwareAdapter:
    """Reads what the installed emulators want, live, through the vendored resolver."""

    def __init__(self, *, user_home: str, log_debug: Callable[[str], None]) -> None:
        self._user_home = user_home
        self._log_debug = log_debug

    def __call__(self) -> FirmwareCatalogue:
        """The whole machine's firmware demand, or an empty unresolved catalogue."""
        try:
            return self._read_catalogue()
        except Exception as exc:
            # Deliberately broad: the resolver's failure modes are its own
            # invariant assertions and its packaged-data loaders, neither of
            # which is an exception type this adapter should enumerate. The
            # honest answer to "we could not ask" is the same whatever raised.
            self._log_debug(f"[firmware] resolver failed, answering unresolved: {exc!r}")
            return FirmwareCatalogue(placements=(), unread_cores=frozenset(), resolved=False)

    def _read_catalogue(self) -> FirmwareCatalogue:
        installations = detect(self._user_home)
        if not installations:
            self._log_debug("[firmware] no emulator installation detected")
            return FirmwareCatalogue(placements=(), unread_cores=frozenset(), resolved=False)

        # Detection returns the arrangements it found highest-priority first and
        # never picks a winner itself; the plugin is a RetroDECK plugin, and
        # RetroDECK leads that order where it is present.
        installation = installations[0]
        answer = installation.firmware_inventory()
        self._trace(installation.kind, answer)

        if answer.root is None:
            return FirmwareCatalogue(
                placements=(),
                unread_cores=frozenset(),
                resolved=False,
                caveats=_caveat_codes(answer),
            )
        return FirmwareCatalogue(
            placements=_placements(answer),
            unread_cores=_unread_cores(answer),
            resolved=True,
            caveats=_caveat_codes(answer),
        )

    def _trace(self, kind: str, answer: Any) -> None:
        """Trace the answer's shape and its stable caveat codes."""
        self._log_debug(
            f"[firmware] {kind}: root={answer.root!r} cores={len(answer.cores)} "
            f"requirements={len(answer.requirements)} caveats={sorted(set(_caveat_codes(answer)))}"
        )


def _caveat_codes(answer: Any) -> tuple[str, ...]:
    """Every stable caveat code the answer states, at the answer and at each core."""
    return (
        *(caveat.code for caveat in answer.caveats),
        *(caveat.code for core in answer.cores for caveat in core.caveats),
    )


def _unread_cores(answer: Any) -> frozenset[str]:
    """The cores that did not state what they want, in the plugin's identifier space.

    A refused declaration counts too: the emulator does want something atlas
    would not follow to a destination, so what it wants is no better known than
    for a core whose ``.info`` was missing.
    """
    unread: set[str] = set()
    for core in answer.cores:
        core_so = _plugin_core_so(core.core_so)
        if core_so is None:
            continue
        if core.declaration not in _STATED_DECLARATIONS or core.refused:
            unread.add(core_so)
    return frozenset(unread)


def _requirement_entries(core: Any) -> list[Any]:
    """A core's requirements, flattened out of any per-region alternatives group.

    An alternatives group states that one launch needs exactly one of its
    options, decided by the running disc's region. Which option that is cannot
    be known from a file list, and the question here is per file — "does
    anything want this one" — so every option is an emulator's declared demand
    and belongs in the catalogue.
    """
    entries: list[Any] = []
    for entry in core.requirements:
        options = getattr(entry, "options", None)
        if options is None:
            entries.append(entry)
        else:
            entries.extend(options)
    return entries


def _declared_location(requirement: Any, root: str) -> str | None:
    """The location the emulator declared, or ``None`` when nothing under *root* honours it.

    Two fields of one requirement answer two different questions, and only the
    first belongs here. ``declared`` is the string the emulator spelled and the
    name it will open; ``path`` is where that lands once the kernel has followed
    every symlink, which is what says whether the destination is inside the root
    the plugin owns. Reconstructing the declaration from ``path`` instead —
    ``relpath(path, root)`` — agrees with it only while no link re-roots the
    way: RetroDECK points ``<bios>/pcsx2/bios`` back at ``<bios>``, so LRPS2's
    ``pcsx2/bios`` collapses onto the root and comes back as ``.``.

    ``None`` where there is no location below the root to honour, so the caller
    falls back to its own flat layout. Three shapes reach it. A resolved
    destination outside the root — a standalone emulator's own XDG tree. A
    declaration that is absent, absolute, or climbs out of the root, which names
    a place the caller cannot express as a segment under a root that is its to
    own. And a declaration that normalises to ``.``, which is the root itself:
    that is not a location under it, and a caller joining it would place every
    file of that name at the directory rather than in it.
    """
    relative = os.path.relpath(requirement.path, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    declared = requirement.declared
    if not declared or os.path.isabs(declared):
        return None
    normalised = os.path.normpath(declared)
    if normalised == os.curdir or normalised == os.pardir or normalised.startswith(os.pardir + os.sep):
        return None
    return normalised


def _placements(answer: Any) -> tuple[FirmwarePlacement, ...]:
    """One placement per declared file name, folding every core that declares it.

    Two cores routinely want the same file — gambatte and SameBoy both ask for a
    Game Boy boot ROM — so the file is the key and the cores are the entries
    under it. That is also what keeps one file from reading differently on two
    surfaces: there is one row per name in the whole answer.

    Everything about the destination — where it is, whether anything is there,
    what kind of thing, whose copy — is taken from the first requirement under
    the name, because those are statements about one place and reading them off
    different requirements would describe two places as one row.

    And they stand or fall together with the location. Where there is none to
    honour, the caller places the file by its own flat default instead, which is
    a different place from the one that was read — a standalone emulator's own
    XDG tree holding the file says nothing about the BIOS root. So the reading
    is dropped with the location rather than travelling on to describe somewhere
    the caller will never write.
    """
    root = answer.root
    by_name: dict[str, list[Any]] = {}
    for core in answer.cores:
        for requirement in _requirement_entries(core):
            by_name.setdefault(requirement.file_name, []).append((core, requirement))

    placements: list[FirmwarePlacement] = []
    for file_name, pairs in by_name.items():
        first = pairs[0][1]
        location = _declared_location(first, root)
        supplied = first.supplied_by if location is not None else None
        placements.append(
            FirmwarePlacement(
                file_name=file_name,
                relative_path=location,
                description=first.description,
                wants=tuple(
                    FirmwareWant(
                        core_so=_plugin_core_so(core.core_so),
                        required=requirement.need == "required",
                    )
                    for core, requirement in pairs
                ),
                present=first.present if location is not None else None,
                is_directory=location is not None and first.found == KIND_DIRECTORY,
                supplied_by=supplied.distribution if supplied is not None else None,
            )
        )
    return tuple(sorted(placements, key=lambda placement: placement.file_name))
