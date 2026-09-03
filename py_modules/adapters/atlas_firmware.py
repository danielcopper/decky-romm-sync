"""Atlas firmware adapters — the seam through which firmware questions reach the resolver.

The single place the vendored `emu-atlas <https://github.com/danielcopper/emu-atlas>`_
resolver is asked what the installed emulators want. Services see a
:class:`domain.firmware_wants.FirmwareCatalogue` or a
:class:`domain.firmware_wants.FolderVerdict` and never an atlas type — which is
not a stylistic choice: ``domain/`` may not import ``_vendor`` at all (the
``domain-stdlib-only`` contract), so the vocabulary and the resolver have to
meet at an adapter.

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

Two questions, and their scopes are the cost model. ``firmware_inventory()``
answers the whole machine unverified in 115-325 ms and memoises nothing, so
asking it once per platform would multiply that by the platform count;
``firmware_for_core(..., verify=True)`` reads the candidate files inside one
core's declared folder, which is affordable per core and is not what the
inventory would do under verification — there the sweep takes in every
unclaimed file under the BIOS root as well. Nothing is cached in either — a
firmware answer is about files on disk that the user is actively adding and
removing, and a cached one would outlive the download that changed it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from _vendor.atlas import CAVEAT_FIRMWARE_IMAGE_IDENTIFIED, CAVEAT_FIRMWARE_IMAGE_UNLISTED, detect

from domain.firmware_wants import (
    DECLARED_DIRECTORY,
    DECLARED_FILE,
    FirmwareCatalogue,
    FirmwarePlacement,
    FirmwareWant,
    FolderVerdict,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

# Declaration states in which the core stated what it wants. ``read`` is its own
# ``.info`` off the machine; ``packaged`` is a rule card for an emulator that
# ships none. Every other state — ``unreadable``, ``absent``, ``unsupported`` —
# means the emulator was NOT asked, which is what ``unread_cores`` collects.
_STATED_DECLARATIONS = frozenset({"read", "packaged"})

_CORE_SO_SUFFIX = ".so"

# The two codes that name an image the core would boot. ``unlisted`` counts as
# much as ``identified``: both mean the file passed the header check the core
# makes itself, and they differ only in whether the packaged identity table also
# files those bytes — the table lists what System.dat lists, so an uncatalogued
# dump is the ordinary case rather than a lesser answer.
_IDENTIFIED_IMAGE_CODES = frozenset({CAVEAT_FIRMWARE_IMAGE_IDENTIFIED, CAVEAT_FIRMWARE_IMAGE_UNLISTED})


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


class AtlasFolderVerdictAdapter:
    """Reads what one core's declared FOLDERS hold, verified, through the vendored resolver.

    The second firmware question, and it is asked per core because of what it
    costs. A folder declaration is satisfied by a file *inside* the folder, so
    the only reading that can answer it is one that opens the candidates and
    reads them the way the core does — 0.26 s for one core on the reference
    machine, against 0.24 s for the whole machine's unverified inventory. The
    same verification over the whole machine sweeps every unclaimed file under
    the BIOS root as well, and this plugin resolves the whole machine on every
    game-page open; so the inventory stays unverified and this seam is asked
    only for the cores whose folder row is still unanswered.

    It shares :class:`AtlasFirmwareAdapter`'s two properties and answers them
    the same way: the resolver raises on its own invariant violations, so every
    call is wrapped and a failure comes back as no verdict at all — which reads
    downstream as "nothing could be established", never as a folder holding
    nothing. And it never logs, so its caveats are traced through the injected
    debug logger.
    """

    def __init__(self, *, user_home: str, log_debug: Callable[[str], None]) -> None:
        self._user_home = user_home
        self._log_debug = log_debug

    def __call__(self, core_so: str) -> Mapping[str, FolderVerdict]:
        """*core_so*'s folder verdicts by file name, or nothing where none was established."""
        try:
            return self._read_folders(core_so)
        except Exception as exc:
            self._log_debug(f"[firmware] verified folder read failed for {core_so}: {exc!r}")
            return {}

    def _read_folders(self, core_so: str) -> dict[str, FolderVerdict]:
        installations = detect(self._user_home)
        if not installations:
            return {}
        answer = installations[0].firmware_for_core(f"{core_so}{_CORE_SO_SUFFIX}", verify=True)
        if answer.root is None:
            return {}
        caveats = _deduplicated(_every_caveat(answer))
        verdicts: dict[str, FolderVerdict] = {}
        for core in answer.cores:
            for requirement in _requirement_entries(core):
                if requirement.declared_kind != DECLARED_DIRECTORY:
                    continue
                verdicts.setdefault(requirement.file_name, _folder_verdict(requirement, caveats))
        self._log_debug(f"[firmware] {core_so} folders: {[(n, v.satisfied) for n, v in verdicts.items()]}")
        return verdicts


def _folder_verdict(requirement: Any, caveats: tuple[Any, ...]) -> FolderVerdict:
    """One folder declaration's verdict, with the caveats that speak for it.

    A caveat belongs to this folder when it names the folder itself (``dir``, or
    a ``path`` at the folder) or a file inside it (a ``path`` one level down) —
    the folder read states its findings per file, and the requirement carries no
    identifier for them to point back at. One level down is a place, not a role,
    so a caveat about a DECLARED file sitting directly in the folder is taken
    the same way. That needs one core declaring both a folder and a file in it;
    LRPS2 declares ``pcsx2/bios`` and ``pcsx2/resources/GameIndex.yaml``, which
    is two levels down, so no shape on a reference machine collides.

    The images are the descriptions of what the read identified, which is the
    core's own option-label text. Only a satisfied verdict has any: an image the
    core's own test rejected, or one it never got to read, is not something the
    folder holds for the purpose of this row.
    """
    here = [caveat for caveat in caveats if _names_destination(caveat.data, requirement.path)]
    return FolderVerdict(
        satisfied=requirement.satisfied,
        images=tuple(
            description
            for caveat in here
            if caveat.code in _IDENTIFIED_IMAGE_CODES and (description := caveat.data.get("description"))
        ),
        caveats=tuple(dict.fromkeys(caveat.code for caveat in here)),
    )


def _names_destination(data: Mapping[str, Any], destination: str) -> bool:
    """Does this caveat's data name *destination*, or something directly inside it?"""
    if data.get("dir") == destination:
        return True
    path = data.get("path")
    return isinstance(path, str) and destination in (path, os.path.dirname(path))


def _deduplicated(caveats: tuple[Any, ...]) -> tuple[Any, ...]:
    """*caveats* with byte-identical repeats of one statement collapsed to the first.

    RetroDECK's ES-DE catalogue lists ``pcsx2_libretro.so`` under two PS2
    entries, so a caveat about that core can be stated twice with identical data
    (emu-atlas #361). Keyed on ``(code, data)`` rather than on the code alone:
    two statements of one code about two different files are two findings.
    """
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    kept: list[Any] = []
    for caveat in caveats:
        key = (caveat.code, tuple(sorted((k, str(v)) for k, v in caveat.data.items())))
        if key not in seen:
            seen.add(key)
            kept.append(caveat)
    return tuple(kept)


def _caveat_codes(answer: Any) -> tuple[str, ...]:
    """Every stable caveat code the answer states, at the answer and at each core."""
    return tuple(caveat.code for caveat in _every_caveat(answer))


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
    at_path, in_folder = _caveats_by_destination(answer)
    by_name: dict[str, list[Any]] = {}
    for core in answer.cores:
        for requirement in _requirement_entries(core):
            by_name.setdefault(requirement.file_name, []).append((core, requirement))

    placements: list[FirmwarePlacement] = []
    for file_name, pairs in by_name.items():
        first = pairs[0][1]
        location = _declared_location(first, root)
        supplied = first.supplied_by if location is not None else None
        directory = first.declared_kind == DECLARED_DIRECTORY
        folder = _settled_folder(first) if directory and location is not None else None
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
                declared_kind=DECLARED_DIRECTORY if directory else DECLARED_FILE,
                caveats=_row_caveats(first, at_path, in_folder, settled=folder) if location is not None else (),
                folder=folder,
                supplied_by=supplied.label if supplied is not None else None,
            )
        )
    return tuple(sorted(placements, key=lambda placement: placement.file_name))


def _settled_folder(requirement: Any) -> FolderVerdict | None:
    """The folder verdict this unverified reading already settles, or ``None``.

    Read off the resolver's own three-valued answer rather than off a list of
    shapes, because the shapes that settle without a content check are the
    resolver's business and not this adapter's to enumerate: today an absent
    folder, a plain file where the folder belongs, and a folder holding no file
    of a size the core would even open are all settled by a stat. ``None`` is
    the question a content read has to answer, and it is the only shape the
    caller pays a verified per-core resolve for.
    """
    return None if requirement.satisfied is None else FolderVerdict(satisfied=requirement.satisfied)


def _row_caveats(
    requirement: Any,
    at_path: dict[str, tuple[str, ...]],
    in_folder: dict[str, tuple[str, ...]],
    *,
    settled: FolderVerdict | None,
) -> tuple[str, ...]:
    """The codes one row carries: what was found AT its destination, and IN it.

    The second half rides only on a folder declaration the unverified reading
    SETTLED, and both halves of that are load-bearing. A file declaration must
    not pick up a listing's findings at all — on a linked root the listed folder
    IS the firmware root, which is the resolved destination of any declaration
    that collapses onto it. And an unsettled folder is one the verified read is
    there to answer, its caveats folded in by ``merge_folder_verdicts``;
    carrying the unverified statement on as well would leave the row saying its
    contents were not checked beside the verdict of the check.

    The verified read is not the only thing that words an unsettled folder,
    though, because ``at_path`` is carried either way. The inventory's unclaimed
    sweep states a directory it could not list under ``path``, and on a linked
    root that directory IS the folder declaration's destination — so an
    unlistable folder hands the row ``firmware-scan-incomplete`` even where the
    verified read never runs, which is a better sentence than the fallback it
    would otherwise get. (The sweep globs the escaped spelling, so a BIOS path
    containing ``*``, ``?`` or ``[`` would not match; no normal one does.)

    Both indexes are keyed by **place**, so requirements resolving to one place
    share its statements — which is right for two cores declaring one file, and
    is the residual for two folder declarations landing on one directory: they
    would each carry the other's codes. Discriminating on the caveat's own
    ``core_so`` would close that and is not done, because three of the four
    folder codes carry one and ``firmware-scan-incomplete`` does not — the rule
    would drop exactly the code a broken listing needs.
    """
    codes = at_path.get(requirement.path, ())
    if settled is None:
        return codes
    return tuple(dict.fromkeys((*codes, *in_folder.get(requirement.path, ()))))


def _caveats_by_destination(answer: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """The answer's caveat codes, indexed by the destination each one names.

    Two indexes, because the resolver names a destination two ways and they are
    different statements. ``path`` is the thing the caveat is ABOUT, so the row
    is the requirement resolving there. ``dir`` is a directory that was listed,
    and the caveat is about what the listing found IN it, which is what
    :func:`_row_caveats` keys on.

    In the two questions this module asks, every ``dir`` is a folder
    declaration's own: both answer through the resolver's libretro core walk,
    and the folder route is the only thing there that states one. It is not the
    only *lister* — ``firmware_inventory`` also sweeps for unclaimed files,
    globbing the firmware root and every directory a declared file sits in —
    but that sweep records a listing it could not make under ``path``, so its
    statements land in the other index.
    The resolver states ``dir`` more widely again: a standalone emulator's
    search directory carries one too, so a third question put to it
    (``firmware_for_system``) would need this keying revisited before a search
    finding could land on a folder row.

    Deduplicated on the code within one destination, because the same statement
    arrives more than once: RetroDECK's ES-DE catalogue lists
    ``pcsx2_libretro.so`` under two PS2 entries, so every caveat about that core
    is stated twice with byte-identical data (emu-atlas #361).
    """
    at_path: dict[str, list[str]] = {}
    in_folder: dict[str, list[str]] = {}
    for caveat in _every_caveat(answer):
        for key, index in (("path", at_path), ("dir", in_folder)):
            named = caveat.data.get(key)
            if isinstance(named, str):
                index.setdefault(named, []).append(caveat.code)
    return _deduplicated_codes(at_path), _deduplicated_codes(in_folder)


def _deduplicated_codes(by_destination: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    """One destination's codes, first occurrence kept and repeats dropped."""
    return {destination: tuple(dict.fromkeys(codes)) for destination, codes in by_destination.items()}


def _every_caveat(answer: Any) -> tuple[Any, ...]:
    """Every caveat the answer states, at the answer and at each core."""
    return (*answer.caveats, *(caveat for core in answer.cores for caveat in core.caveats))
