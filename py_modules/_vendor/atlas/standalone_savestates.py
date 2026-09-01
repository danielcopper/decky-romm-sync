"""Standalone savestate cards — which emulators atlas can answer the savestate question for.

The savefile family's twin, one question over (#225). A standalone emulator is
handed nothing by a frontend: where its states land is its own — a compiled
join below one of its XDG trees, or a directory its own configuration names —
and what a state file is called is its own too. The card here is the thin,
versioned half of that knowledge — which emulator, which catalogue systems it
answers for, where the tree hangs or which key moves it, how the files inside
are named, and the citations behind all of it — while the reading itself is
code beside it in :mod:`atlas.installations`, exactly the split the save cards
make (:mod:`atlas.standalone_saves`): a card states what *can* be, the code
reads what *is* on this machine, and neither guesses.

The cards are keyed by the ``%EMULATOR_…%`` token the ES-DE catalogue names in
a launch command, because for a standalone entry that token is the only
identifier there is — the same key every other standalone card family uses. A
card whose token has no resolver function registered is a marker selecting
nothing, and fails at dispatch the way a rule card without a rule does.

A card makes its statement one of five ways, and exactly one. Two state a
directory the way the standalone texture cards established
(:mod:`atlas.textures`):

- ``base`` plus ``subdir`` for an emulator whose states tree is a compiled
  join below its own directory (Dolphin's ``StateSaves``, PPSSPP's
  ``PSP/PPSSPP_STATE``, RPCS3's ``savestates``, Azahar's ``states``) — no
  configuration can move it, so no file is read for the root;
- ``directory`` — the configuration key whose *value* is the directory — for
  one that opens whatever its settings name (PCSX2's ``[Folders] Savestates``,
  melonDS's ``[Instance0] SavestatePath``, DuckStation's
  ``[Folders] SaveStates``), with the compiled default beside it.

Three joined with #284, because finishing the column meant stating facts the
two directory shapes cannot spell:

- ``inside_image`` — the states are entries INSIDE a disk image a settings
  key names (xemu's snapshots are QEMU internal snapshots in the qcow2 at
  ``[sys.files] hdd_path``) — there is no directory and no file per state, so
  the card names the key and the answer names the image;
- ``launch_ini`` — the directory is a key in an ini whose *address* comes
  from the launch command rather than from any fixed XDG location (MAME's
  ``state_directory`` in the mame.ini the ``-inipath`` flag names), the one
  case the ``emulator_settings`` table deliberately cannot state;
- ``absent`` — the emulator has no savestates at all, as a cited fact. An
  answer, not a refusal (#284): Cemu 2.6 ships no state serializer, the
  Ryujinx lineage never had one, and a column is only finished when that too
  is stated with its evidence instead of hiding inside the generic
  ``standalone-unsupported``. An absence card states no names, no settings
  and no anchors — there is no tree to describe and no word to pin.

``names`` is the one field with no analogue on the save side, and it exists
because the savestate question has the opposite naming problem: a core's save
extensions are unknowable world knowledge, while every standalone emulator
names its states itself, from an identity of the running game — a disc serial,
a game id, a title id — that no content path derives (melonDS alone derives
its from the loaded file's name). So the card states the pattern, cited, and
the resolver hands it to the caller in the caveat that says why the files
below cannot be listed by name.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._data import packaged_text
from .textures import XDG_BASES, expect_table_anchors, path_segments

SAVESTATES_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class SavestateSetting:
    """The configuration key whose value is the states directory, with its default.

    The texture cards' ``TextureSetting`` shape, restated here rather than
    imported: the loaders stay independent so a defect in one table can never
    fail the load of another (the same deliberate repetition every packaged
    loader carries). ``default`` is the string the emulator falls back to — the
    compiled directory name for PCSX2 and DuckStation, and the empty string for
    melonDS, whose empty value routes the state beside the ROM.
    """

    section: str
    key: str
    default: str
    citation: str


@dataclass(frozen=True, slots=True)
class SavestateImage:
    """The settings key whose value is the disk image the states live inside.

    xemu's shape (#284): a snapshot is an entry written into the qcow2 the
    key names, not a file in any directory, so the card states the key and
    the citation for the inside-the-image fact and the resolver names the
    image file itself.
    """

    key: str
    citation: str


@dataclass(frozen=True, slots=True)
class SavestateIniKey:
    """One launch-ini key the resolver reads: its compiled default, cited."""

    default: str
    citation: str


@dataclass(frozen=True, slots=True)
class SavestateLaunchIni:
    """A directory key in an ini whose address the launch command names.

    MAME's shape (#284): the governing mame.ini is wherever ``-inipath``
    points (falling back to the OSD default search path), so no
    ``emulator_settings`` row can state the address — the card states the
    file's *name*, the keys the resolver reads with their compiled defaults,
    and the citation for the address rule itself.
    """

    file: str
    keys: Mapping[str, SavestateIniKey]
    citation: str


@dataclass(frozen=True, slots=True)
class SavestateAbsenceStatement:
    """The card's stated no: this emulator has no savestates, with evidence.

    ``citation`` is the spans and scans that establish the absence — it rides
    into the answer contractually, because a stated no without its evidence
    is indistinguishable from a guess. ``build_unestablished`` is the
    sentence naming why no shipped build pins the claim (nothing ships
    Ryubing; PICO-8's binary is the user's own), and its presence is what
    puts the ``unverified-version`` caveat on the answer; ``None`` for an
    emulator whose shipped build was read.
    """

    citation: str
    build_unestablished: str | None


@dataclass(frozen=True, slots=True)
class StandaloneSavestateCard:
    """One standalone emulator's savestate knowledge: tree, names, systems, citations.

    ``settings`` names the governing configuration file by **name**, its
    address stated once in ``atlas/data/emulator_settings.json`` — the same
    rule every card family follows, for the same reason (#250, #256). It is
    ``None`` for an emulator whose states tree is fixed by the build rather
    than by any file. ``systems`` is the closed list of catalogue systems this
    card answers for. There is deliberately no ``flatpak`` field: which app id
    an arrangement installs an emulator as is recorded once, in
    ``atlas/data/emulator_settings.json``
    (:meth:`atlas.installations.EmuDeck._homes_for_token` reads it there), and
    a copy here could only ever drift from it.

    ``citations`` are the source references the **resolver** speaks, keyed by
    the slot the code asks for; they live on the card because one resolver can
    serve two emulators that are not the same source (PrimeHack is a Dolphin
    fork read by Dolphin's states resolver, and every inherited file sits at
    different lines). The reserved ``installations`` key states one set per
    flatpak app id, because a citation belongs to the **build**: the PrimeHack
    revision RetroDECK builds and the one Flathub ships are three years apart.
    """

    token: str
    settings: str | None
    systems: tuple[str, ...]
    base: str | None
    subdir: str | None
    directory: SavestateSetting | None
    # The three #284 shapes: exactly one of the five is ever non-None/stated,
    # which the loader enforces. ``names`` is None exactly on an absence card
    # — no state exists to have a name.
    inside_image: SavestateImage | None
    launch_ini: SavestateLaunchIni | None
    absent: SavestateAbsenceStatement | None
    names: str | None
    names_citation: str | None
    provenance: str
    citations: Mapping[str, str] = field(default_factory=dict)
    citation_installations: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def cite(self, slot: str, *, flatpak: str | None) -> str:
        """The card's citation for one slot, in the build this launch runs.

        *flatpak* has no default for the reason the save card's has none: a
        reading that forgot it would name the arrangement's own build's lines
        for an answer about somebody else's, and look exactly like a verified
        one. Raises for a slot the card does not state — the card and the code
        shipped out of step.
        """
        stated = self.citation_installations.get(flatpak or "", self.citations)
        citation = stated.get(slot)
        if citation is None:
            raise ValueError(
                f"standalone savestate card {self.token!r} states no {slot!r} citation for "
                f"{flatpak or 'the arrangement own build'} — the resolver reading it names "
                "that source in its answer, and the card and the code shipped out of step"
            )
        return citation


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def recorded_savestate_emulator_words(entry: Mapping[str, Any]) -> frozenset[str]:
    """Every name a savestate card states as this emulator's own.

    The anchor vocabulary (#263): the fixed subpath's segments, the settings
    file's name, for a config-stated directory the section and key it reads
    and the non-empty default it falls back to, for an inside-image card the
    key naming the image, and for a launch-ini card the file's name and the
    keys the resolver reads (their defaults stay out — ``sta`` and ``%g`` are
    too short to be bytes worth pinning, and the option-name literals are the
    words the binary really carries). The ``names`` pattern stays out: it is
    a template full of holes, cited to source lines rather than pinned to
    bytes. An absence card contributes nothing — there is no word to pin.
    """
    savestates = entry.get("savestates", {})
    words = path_segments(savestates.get("subdir"))
    settings = savestates.get("settings")
    if isinstance(settings, str):
        words.append(settings)
    directory = savestates.get("directory")
    if isinstance(directory, dict):
        for key in ("section", "key", "default"):
            value = directory.get(key)
            if isinstance(value, str) and value:
                words.append(value)
    inside_image = savestates.get("inside_image")
    if isinstance(inside_image, dict) and isinstance(inside_image.get("key"), str):
        words.append(inside_image["key"])
    launch_ini = savestates.get("launch_ini")
    if isinstance(launch_ini, dict):
        if isinstance(launch_ini.get("file"), str):
            words.append(launch_ini["file"])
        keys = launch_ini.get("keys")
        if isinstance(keys, dict):
            words.extend(key for key in keys if isinstance(key, str))
    return frozenset(words)


def _directory_setting(value: Any, where: str) -> SavestateSetting:
    if not isinstance(value, dict) or set(value) != {"section", "key", "default", "citation"}:
        raise ValueError(f"{where}: expected exactly section/key/default/citation, got {value!r}")
    default = value["default"]
    if not isinstance(default, str):
        # The empty string is a real default (melonDS routes it beside the ROM),
        # so this is not _expect_str.
        raise ValueError(f"{where}.default: expected a string, got {default!r}")
    return SavestateSetting(
        section=_expect_str(value.get("section"), f"{where}.section"),
        key=_expect_str(value.get("key"), f"{where}.key"),
        default=default,
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _inside_image_statement(value: Any, where: str) -> SavestateImage:
    if not isinstance(value, dict) or set(value) != {"key", "citation"}:
        raise ValueError(f"{where}: expected exactly key/citation, got {value!r}")
    return SavestateImage(
        key=_expect_str(value.get("key"), f"{where}.key"),
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _launch_ini_statement(value: Any, where: str) -> SavestateLaunchIni:
    if not isinstance(value, dict) or set(value) != {"file", "keys", "citation"}:
        raise ValueError(f"{where}: expected exactly file/keys/citation, got {value!r}")
    keys = value.get("keys")
    if not isinstance(keys, dict) or not keys:
        raise ValueError(f"{where}.keys: expected a non-empty object of ini keys, got {keys!r}")
    parsed: dict[str, SavestateIniKey] = {}
    for key, stated in keys.items():
        _expect_str(key, f"{where}.keys key")
        if not isinstance(stated, dict) or set(stated) != {"default", "citation"}:
            raise ValueError(
                f"{where}.keys[{key!r}]: expected exactly default/citation, got {stated!r}"
            )
        default = stated["default"]
        if not isinstance(default, str):
            raise ValueError(f"{where}.keys[{key!r}].default: expected a string, got {default!r}")
        parsed[key] = SavestateIniKey(
            default=default,
            citation=_expect_str(stated.get("citation"), f"{where}.keys[{key!r}].citation"),
        )
    return SavestateLaunchIni(
        file=_expect_str(value.get("file"), f"{where}.file"),
        keys=parsed,
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _absence_statement(value: Any, where: str) -> SavestateAbsenceStatement:
    if not isinstance(value, dict) or not set(value) <= {"citation", "build_unestablished"}:
        raise ValueError(
            f"{where}: expected citation (and optionally build_unestablished), got {value!r}"
        )
    unpinned = value.get("build_unestablished")
    if unpinned is not None and (not isinstance(unpinned, str) or not unpinned):
        raise ValueError(
            f"{where}.build_unestablished: expected a non-empty sentence or nothing, got {unpinned!r}"
        )
    return SavestateAbsenceStatement(
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
        build_unestablished=unpinned,
    )


def _subdir(value: Any, where: str) -> str:
    subdir = _expect_str(value, where)
    if subdir.startswith("/"):
        raise ValueError(
            f"{where}: {subdir!r} is absolute — a card states the fragment BELOW the emulator's "
            "own directory, and an absolute one would replace that root instead of extending it"
        )
    if ".." in subdir.split("/"):
        raise ValueError(f"{where}: {subdir!r} climbs out of the root with '..'")
    return subdir


def _citations(
    savestates: Mapping[str, Any], where: str
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """The default citation set and the per-installation overrides, validated whole."""
    stated = savestates.get("citations", {})
    if not isinstance(stated, dict):
        raise ValueError(f"{where}: expected a 'savestates.citations' object, got {stated!r}")
    # Copied rather than popped: the caller's parsed document is theirs, and a
    # loader that empties a key out of it makes a second load of the same
    # object see a card without its overrides.
    installations = stated.get("installations", {})
    citations = {
        _expect_str(slot, f"{where}: savestates.citations key"): _expect_str(
            citation, f"{where}: savestates.citations[{slot!r}]"
        )
        for slot, citation in stated.items()
        if slot != "installations"
    }
    if not isinstance(installations, dict):
        raise ValueError(
            f"{where}: expected a 'savestates.citations.installations' object, got {installations!r}"
        )
    overrides: dict[str, dict[str, str]] = {}
    for app_id, per_build in installations.items():
        _expect_str(app_id, f"{where}: savestates.citations.installations key")
        if not isinstance(per_build, dict) or set(per_build) != set(citations):
            raise ValueError(
                f"{where}: savestates.citations.installations[{app_id!r}] must state the same "
                f"slots as the default set {sorted(citations)} — a partial override reads as one "
                "build's evidence and answers with another's"
            )
        overrides[app_id] = {
            slot: _expect_str(
                citation, f"{where}: savestates.citations.installations[{app_id!r}][{slot!r}]"
            )
            for slot, citation in per_build.items()
        }
    return citations, overrides


def _settings_name(savestates: Mapping[str, Any], where: str) -> str | None:
    """The governing file's name, or ``None`` for a tree fixed by the build."""
    settings = savestates.get("settings")
    if settings is None:
        return None
    return _expect_str(settings, f"{where}: savestates.settings")


def _systems(savestates: Mapping[str, Any], where: str) -> tuple[str, ...]:
    """The closed list of catalogue systems this card answers for."""
    systems = savestates.get("systems")
    if not isinstance(systems, list) or not systems:
        raise ValueError(f"{where}: savestates.systems must be a non-empty list, got {systems!r}")
    return tuple(_expect_str(s, f"{where}: savestates.systems[]") for s in systems)


_SHAPES = ("base+subdir", "directory", "inside_image", "launch_ini", "absent")


def _stated_shape(savestates: Mapping[str, Any], where: str) -> str:
    """Which of the five statements this card makes — exactly one, or a refusal."""
    stated = [
        shape
        for shape, present in (
            (
                "base+subdir",
                savestates.get("base") is not None or savestates.get("subdir") is not None,
            ),
            ("directory", savestates.get("directory") is not None),
            ("inside_image", savestates.get("inside_image") is not None),
            ("launch_ini", savestates.get("launch_ini") is not None),
            ("absent", savestates.get("absent") is not None),
        )
        if present
    ]
    if len(stated) != 1:
        raise ValueError(
            f"{where}: state exactly one of {list(_SHAPES)} — got {stated or 'none of them'}"
        )
    return stated[0]


def _tree_shape(
    savestates: Mapping[str, Any], where: str, *, shape: str, settings: str | None
) -> tuple[str | None, str | None, SavestateSetting | None]:
    """The two directory statements: base+subdir, or a settings key."""
    if shape == "directory":
        if settings is None:
            raise ValueError(
                f"{where}: a directory setting is read out of a settings file, and the card "
                "names none — nothing could ever read the key it states"
            )
        return (
            None,
            None,
            _directory_setting(savestates["directory"], f"{where}: savestates.directory"),
        )
    base = _expect_str(savestates.get("base"), f"{where}: savestates.base")
    if base not in XDG_BASES:
        raise ValueError(
            f"{where}: savestates.base must be one of {sorted(XDG_BASES)}, got {base!r}"
        )
    return base, _subdir(savestates.get("subdir"), f"{where}: savestates.subdir"), None


def _names_statement(savestates: Mapping[str, Any], where: str) -> tuple[str, str]:
    """How this emulator names a state — the cited (pattern, citation) pair."""
    names = savestates.get("names")
    if not isinstance(names, dict) or set(names) != {"pattern", "citation"}:
        raise ValueError(
            f"{where}: savestates.names must be {{'pattern': …, 'citation': …}} — how this "
            f"emulator names a state is the card's to state, cited; got {names!r}"
        )
    return (
        _expect_str(names.get("pattern"), f"{where}: savestates.names.pattern"),
        _expect_str(names.get("citation"), f"{where}: savestates.names.citation"),
    )


def _provenance_source(entry: Mapping[str, Any], where: str) -> str:
    """The card's own evidence prose — required, like every packaged loader's."""
    provenance = entry.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError(f"{where}: expected a 'provenance' object, got {provenance!r}")
    return _expect_str(provenance.get("source"), f"{where}: provenance.source")


@dataclass(frozen=True, slots=True)
class _CardStatement:
    """One card's parsed statement — whichever of the five it makes.

    The intermediate the shape helpers hand back to :func:`_card`, so the
    assembly reads as composition instead of five branches mutating one pile
    of locals.
    """

    base: str | None = None
    subdir: str | None = None
    directory: SavestateSetting | None = None
    inside_image: SavestateImage | None = None
    launch_ini: SavestateLaunchIni | None = None
    absent: SavestateAbsenceStatement | None = None
    names: str | None = None
    names_citation: str | None = None


def _absence_card_statement(
    savestates: Mapping[str, Any], entry: Mapping[str, Any], where: str
) -> _CardStatement:
    """The stated no, with the refusals of everything a no cannot have.

    A stated no describes no tree: names, settings and anchors would each
    pretend one exists, so each is refused rather than ignored.
    """
    absent = _absence_statement(savestates["absent"], f"{where}: savestates.absent")
    for field_name in ("names", "settings"):
        if savestates.get(field_name) is not None:
            raise ValueError(
                f"{where}: an absence card states no {field_name} — there is no state "
                "to name and no file to read"
            )
    if entry.get("anchors") is not None:
        raise ValueError(
            f"{where}: an absence card records no emulator words, so there is nothing "
            "an anchor could pin"
        )
    return _CardStatement(absent=absent)


def _shaped_card_statement(
    shape: str, savestates: Mapping[str, Any], where: str, *, settings: str | None
) -> _CardStatement:
    """A tree-describing statement — the four shapes that name something on disk."""
    names, names_citation = _names_statement(savestates, where)
    if shape == "inside_image":
        if settings is None:
            raise ValueError(
                f"{where}: the image is a settings value, and the card names no settings "
                "file — nothing could ever read the key it states"
            )
        inside = _inside_image_statement(
            savestates["inside_image"], f"{where}: savestates.inside_image"
        )
        return _CardStatement(inside_image=inside, names=names, names_citation=names_citation)
    if shape == "launch_ini":
        if settings is not None:
            raise ValueError(
                f"{where}: a launch-ini card's file is addressed by the launch command, "
                "never by the settings table — stating both is a contradiction"
            )
        stated = _launch_ini_statement(
            savestates["launch_ini"], f"{where}: savestates.launch_ini"
        )
        return _CardStatement(launch_ini=stated, names=names, names_citation=names_citation)
    base, subdir, directory = _tree_shape(savestates, where, shape=shape, settings=settings)
    return _CardStatement(
        base=base,
        subdir=subdir,
        directory=directory,
        names=names,
        names_citation=names_citation,
    )


def _crossed_citations(
    savestates: Mapping[str, Any], where: str, *, names_citation: str | None
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """The citation sets, crossed against the names statement they must agree with."""
    citations, overrides = _citations(savestates, where)
    if "names" in citations and citations["names"] != names_citation:
        raise ValueError(
            f"{where}: the 'names' citation slot and savestates.names.citation state the "
            "same fact for the default build and disagree — one of the two is the span "
            "somebody re-read, and a reader cannot tell which"
        )
    return citations, overrides


def _card(token: str, entry: Any) -> StandaloneSavestateCard:
    """One emulator's card — validated, never coerced, one helper per grammar rule."""
    where = f"standalone savestate card {token!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    savestates = entry.get("savestates")
    if not isinstance(savestates, dict):
        raise ValueError(f"{where}: expected a 'savestates' object, got {savestates!r}")
    if "flatpak" in entry:
        raise ValueError(
            f"{where}: which app id an arrangement installs this emulator as is stated once in "
            "atlas/data/emulator_settings.json — a copy here could only ever drift from it"
        )
    settings = _settings_name(savestates, where)
    shape = _stated_shape(savestates, where)
    statement = (
        _absence_card_statement(savestates, entry, where)
        if shape == "absent"
        else _shaped_card_statement(shape, savestates, where, settings=settings)
    )
    citations, overrides = _crossed_citations(
        savestates, where, names_citation=statement.names_citation
    )
    if shape != "absent" and entry.get("anchors") is not None:
        expect_table_anchors(
            entry["anchors"],
            where=where,
            vocabulary=recorded_savestate_emulator_words(entry),
            binary_required=True,
        )
    return StandaloneSavestateCard(
        token=token,
        settings=settings,
        systems=_systems(savestates, where),
        base=statement.base,
        subdir=statement.subdir,
        directory=statement.directory,
        inside_image=statement.inside_image,
        launch_ini=statement.launch_ini,
        absent=statement.absent,
        names=statement.names,
        names_citation=statement.names_citation,
        provenance=_provenance_source(entry, where),
        citations=citations,
        citation_installations=overrides,
    )


def load_standalone_savestates(text: str | None = None) -> tuple[StandaloneSavestateCard, ...]:
    """Load the packaged standalone savestate cards (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("standalone_savestates.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != SAVESTATES_SCHEMA:
        raise ValueError(
            f"standalone_savestates: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {SAVESTATES_SCHEMA})"
        )
    return tuple(_card(token, entry) for token, entry in raw.get("emulators", {}).items())


_PACKAGED: tuple[StandaloneSavestateCard, ...] | None = None


def lookup_standalone_savestate_card(token: str | None) -> StandaloneSavestateCard | None:
    """The packaged card for one emulator token, or ``None`` — no fuzzy matching."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_standalone_savestates()
    if token is None:
        return None
    return next((card for card in _PACKAGED if card.token == token), None)
