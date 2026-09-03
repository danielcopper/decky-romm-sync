"""ES-DE catalogue parsing — which emulators can launch which system, live.

``es_systems.xml`` is the *choice* source of the emulator catalogue: per system,
the launch entries in declared order, first entry = ES-DE's default. Two layers
are read live through the machine seam and merged:

1. the bundled file (inside the frontend's installation), then
2. the user overlay ``<ES-DE home>/custom_systems/es_systems.xml`` — a system
   defined there **replaces** the bundled system of the same name (ES-DE
   USERGUIDE, "Game System Customizations"); new names are added.

Unless the overlay opts out of the merge altogether: a document-level
``<loadExclusive/>`` in the custom file makes ES-DE skip the bundled file
wholesale (``SystemData::loadConfig``, ``es-app/src/SystemData.cpp:858-895``,
ES-DE v3.4.1), so the custom layer *is* the catalogue then. The parser states
the tag's presence on the layer (:class:`CatalogueLayer`); honoring it — only
ever for the custom layer, exactly as ES-DE does — is the callers' work.

A command containing ``*_libretro.so`` is a libretro entry (the ``.so`` basename
is extracted); anything else is a standalone entry. Classification only — no
path knowledge is derived from the command text.

Honest degradations: a missing layer is skipped; a malformed layer is skipped
the same way (recorded per answer as its absence — structured catalogue error
reporting is on the task list). The user's saved per-system emulator choice
(``es_settings.xml``) is **not** read yet: its key format is unverified — the
declared order is the answer until then (task list).

Pure text in, entries out. No I/O.

Parsing uses the stdlib deliberately: the input is local config from the user's
own machine (not attacker-controlled in this threat model), modern expat
(Python ≥ 3.11) rejects entity-expansion attacks — surfacing as ``ParseError``,
i.e. an honestly skipped layer — and ``dependencies = []`` is a design contract
(DESIGN.md, consumption), so ``defusedxml`` is not an option. What the reads go
through is :mod:`atlas._xml`, that same expat with ElementTree's shape around
it: the wrapper package ``xml.etree`` is an assumption a frozen consumer runtime
breaks (issue #339), while the parser underneath is the one ElementTree itself
has always parsed with.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from . import _xml as ET

# The core ``.so`` inside a launch command. The name run is bounded by
# NAME_MAX (255): an unbounded quantifier in front of the ``_libretro`` suffix
# rescans a long name from every position it fails at, and no run longer than
# a file name can be one anyway.
_CORE_SO_RE = re.compile(r"([A-Za-z0-9_\-\[\]]{1,255}_libretro\.so)")

# The emulator ES-DE would run, as its command names it: ``%EMULATOR_DOLPHIN%``,
# ``%EMULATOR_DOSBOX-STAGING%``. For a standalone entry this token is the only
# identifier there is — no ``.so`` basename exists — and it is the frontend's
# own vocabulary read off the machine, not a name atlas invented. Bounded like
# the run above, and for the same reason.
_EMULATOR_TOKEN_RE = re.compile(r"%EMULATOR_([A-Za-z0-9_-]{1,255})%")

KIND_LIBRETRO = "libretro"
KIND_STANDALONE = "standalone"


def emulator_token(command: str) -> str | None:
    """The ``%EMULATOR_…%`` token a launch command names, or ``None``.

    ES-DE substitutes the token from its own find rules, so what it *resolves*
    to is the frontend's business; what the token identifies — which emulator
    this entry launches — is a fact the catalogue states, and the only handle a
    standalone entry offers. A command that names none (a bare path, a shell
    line) identifies no emulator, and ``None`` says exactly that.
    """
    match = _EMULATOR_TOKEN_RE.search(command)
    return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class EmulatorSpec:
    """One launch entry of one system, as declared in ``es_systems.xml``.

    ``core_so`` is the extracted ``.so`` basename for libretro entries, ``None``
    for standalone ones. ``provenance`` names the file layer that defined the
    system (bundled or custom overlay).
    """

    system: str
    label: str
    kind: str
    core_so: str | None
    command: str
    provenance: str
    selection: str | None = None


def _launch_entries(system_el: ET.Element, *, system: str, provenance: str) -> tuple[EmulatorSpec, ...]:
    """The launch entries one ``<system>`` declares, in declared order.

    A command naming a ``*_libretro.so`` is a libretro entry (the basename is
    extracted); anything else is standalone. Classification only.
    """
    entries: list[EmulatorSpec] = []
    for command_el in system_el.findall("command"):
        command = (command_el.text or "").strip()
        if not command:
            continue
        match = _CORE_SO_RE.search(command)
        entries.append(
            EmulatorSpec(
                system=system,
                label=(command_el.get("label") or "").strip(),
                kind=KIND_LIBRETRO if match else KIND_STANDALONE,
                core_so=match.group(1) if match else None,
                command=command,
                provenance=provenance,
            )
        )
    return tuple(entries)


def _read_list(text: str) -> tuple[str, ...]:
    """Split a catalogue list the way ES-DE's ``readList`` does.

    The delimiter set is exactly ES-DE's — space, tab, CR, LF and comma
    (``readList``, SystemData.cpp:795 @ v3.4.1) — and nothing else: Python's
    bare ``split()`` would also break on vertical tabs and form feeds, which
    ES-DE keeps inside a token.
    """
    tokens: list[str] = []
    current: list[str] = []
    for char in text:
        if char in " \t\r\n,":
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def esde_extension(path: str) -> str:
    """The extension ES-DE derives from *path* — the token its accept-list is matched against.

    ``Utils::FileSystem::getExtension`` (FileSystemUtil.cpp:630-645 @ v3.4.1):
    the file name from its **last** dot inclusive, case preserved — the scan's
    comparison against the declared list is an exact string match
    (SystemData.cpp:669), which is why real catalogues list ``.z64`` and
    ``.Z64`` separately. A name without a dot answers ``"."``, the same
    sentinel ES-DE uses, which matches only a declared ``"."`` token.
    """
    name = os.path.basename(path)
    if name == ".":
        return name
    offset = name.rfind(".")
    if offset != -1:
        return name[offset:]
    return "."


def _platform_tokens(text: str) -> tuple[str, ...]:
    """The ``<platform>`` list the way ES-DE reads it — tokens, not judgments.

    ``SystemData.cpp:1074-1092`` @ v3.4.1: the text is lowercased
    (``Utils::String::toLower`` — the tags are ASCII in every shipped
    catalogue), split by the same ``readList`` the extension list uses, and an
    ``ignore`` token clears everything before it and stops the read — the
    system's sole platform becomes ``ignore``. What this deliberately does
    *not* replicate is ``getPlatformId``'s validity judgment (an unknown token
    is warned about and dropped upstream): whether a token is in the platform
    vocabulary is the crosswalk's question, answered where the vocabulary
    lives — the parser states what the file says.
    """
    tokens = _read_list(text.lower())
    if "ignore" in tokens:
        return ("ignore",)
    return tokens


@dataclass(frozen=True, slots=True)
class SystemDeclaration:
    """One ``<system>`` as declared: what launches it, where it lives, what counts.

    ``rom_path`` is the ``<path>`` text **verbatim**, still carrying whatever
    tokens the file wrote (``%ROMPATH%/n64``) — resolving them needs a setting
    this parser does not read, so substituting here would mean guessing it.
    ``extensions`` is the ``<extension>`` list split the way ES-DE's own
    ``readList`` splits it (space, tab, CR, LF **and comma** —
    SystemData.cpp:795 @ v3.4.1), each token exactly as declared: ES-DE lists
    both cases separately (``.z64 .Z64``), and normalizing would state a
    vocabulary the file does not. ``platforms`` is the ``<platform>`` list the
    way ES-DE reads it (see :func:`_platform_tokens`) — the tag that connects
    a system to the platform vocabulary, and through it to the public platform
    identities.
    """

    entries: tuple[EmulatorSpec, ...] = ()
    rom_path: str | None = None
    extensions: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()


# The two reads ES-DE refuses its whole catalogue load on — the values
# :attr:`CatalogueLayer.invalid` may carry, and therefore the values a health
# finding's ``data["problem"]`` states.
INVALID_PARSE = "parse-error"
INVALID_NO_SYSTEMLIST = "missing-systemlist"


@dataclass(frozen=True, slots=True)
class CatalogueLayer:
    """One ``es_systems.xml`` layer as its document declares it.

    ``systems`` is the layer's ``{system: declaration}``; ``load_exclusive``
    whether a document-level ``<loadExclusive/>`` is present. The flag is a
    *statement about this layer*, not a decision: ES-DE acts on it only when
    the layer is the custom file, and ignores it in the bundled one with a
    LogWarning (``SystemData.cpp:884-895``, v3.4.1) — so the caller that
    knows which layer it read is the one that honors it.

    ``invalid`` states a read ES-DE refuses the **whole load** on — not just
    this layer: a file that does not parse aborts ``loadConfig`` outright
    (``SystemData.cpp:879-882``), and so does one carrying no document-level
    ``<systemList>`` (``:900-903``); either way the caller in ``main.cpp``
    turns it into ``INVALID_FILE`` (``:483-486``) and the frontend runs with
    no systems at all. The layer states the fact; the caller — who knows the
    file's path and place in the load order — decides what the catalogue as
    a whole then is. ``systems`` is empty whenever ``invalid`` is set.
    """

    systems: Mapping[str, SystemDeclaration]
    load_exclusive: bool = False
    invalid: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "systems", MappingProxyType(dict(self.systems)))


def parse_es_systems(text: str, *, provenance: str) -> CatalogueLayer:
    """Parse one ``es_systems.xml`` layer the way ES-DE's own read sees it.

    ES-DE reads the file at the **document** level, not the root-element
    level: ``loadConfig`` asks pugixml for document children
    (``doc.child("loadExclusive")``, ``doc.child("systemList")`` —
    ``es-app/src/SystemData.cpp:884,898``, v3.4.1), and pugixml does not
    enforce XML's single-root rule. The documented ``<loadExclusive/>``
    placement (INSTALL.md v3.4.1:1722-1737) is therefore a *second
    document-level element* beside ``<systemList>`` — a file an XML parser
    refuses outright. Wrapping in a synthetic root (the ``parse_es_settings``
    / ``parse_gamelist`` pattern) reads it the way the frontend does; the
    XML declaration is stripped first because inside a wrapper it would sit
    mid-document.

    Mirroring ``doc.child`` exactly is the point twice over: the systems come
    from the **first** document-level ``<systemList>`` (``doc.child`` returns
    the first match, so a second list is invisible to the frontend and stays
    invisible here), and ``load_exclusive`` states a *document-level*
    ``<loadExclusive/>`` in any position — inside ``<systemList>`` it is not
    a document child, has no effect on ES-DE, and is not stated.

    A leading UTF-8 BOM is stripped for :func:`parse_es_settings`'s reason,
    not as a courtesy: pugixml detects the encoding from it and reads such a
    file normally, so its catalogue is the catalogue *in force* — and inside
    the wrapper the mark would sit mid-document and fail a file the frontend
    reads fine.

    Malformed XML — and a document with no ``<systemList>`` — comes back as
    an ``invalid`` layer rather than an empty one, because the frontend does
    not skip such a file: it refuses its whole load on it (see
    :class:`CatalogueLayer`), and an empty layer would spell that as "this
    file declares nothing", which is the opposite claim.
    """
    stripped = text.removeprefix("\ufeff").strip()
    if stripped.startswith("<?"):
        end = stripped.find("?>")
        if end != -1:
            stripped = stripped[end + 2 :]
    try:
        root = ET.fromstring(f"<atlas-wrapper>{stripped}</atlas-wrapper>")
    except ET.ParseError:
        return CatalogueLayer(systems={}, invalid=INVALID_PARSE)
    system_list = root.find("systemList")
    load_exclusive = root.find("loadExclusive") is not None
    if system_list is None:
        return CatalogueLayer(systems={}, load_exclusive=load_exclusive, invalid=INVALID_NO_SYSTEMLIST)
    result: dict[str, SystemDeclaration] = {}
    for system_el in system_list.findall("system"):
        name = (system_el.findtext("name") or "").strip()
        if not name:
            continue
        declared_path = (system_el.findtext("path") or "").strip()
        result[name] = SystemDeclaration(
            entries=_launch_entries(system_el, system=name, provenance=provenance),
            rom_path=declared_path or None,
            extensions=_read_list(system_el.findtext("extension") or ""),
            platforms=_platform_tokens(system_el.findtext("platform") or ""),
        )
    return CatalogueLayer(systems=result, load_exclusive=load_exclusive)


def commented_out_systems(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``(name, platforms)`` of every ``<system>`` living only inside an XML comment.

    ES-DE's parser never sees these — pugixml drops comments like every XML
    reader — so a commented block is **not** a declaration and never reaches
    :func:`parse_es_systems`. It is still a fact about the file worth stating:
    RetroDECK ships 31 systems this way (``xbox360``, ``atarijaguarcd``, …),
    present in the catalogue's text and deliberately off, which is a different
    answer than "this build never carried it".

    Only well-formed ``<system>`` fragments inside comments count — a comment
    is prose until it parses. Order is file order; a name appearing both live
    and commented is reported here too (the caller who knows the declared set
    decides what that means).
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    for comment in re.findall(r"<!--(.*?)-->", text, re.S):
        for fragment in re.findall(r"<system>.*?</system>", comment, re.S):
            try:
                system_el = ET.fromstring(fragment)
            except ET.ParseError:
                continue
            name = (system_el.findtext("name") or "").strip()
            if not name:
                continue
            found.append((name, _platform_tokens(system_el.findtext("platform") or "")))
    return tuple(found)


def merge_layers(
    bundled: Mapping[str, SystemDeclaration],
    custom: Mapping[str, SystemDeclaration],
) -> dict[str, SystemDeclaration]:
    """Merge the custom overlay over the bundled catalogue.

    Per ES-DE semantics a custom system of the same name replaces the bundled
    one entirely; other custom systems are added. Replacement is of the whole
    declaration — an overlay system brings its own path and extensions along
    with its commands, because that is the ``<system>`` element ES-DE ends up
    with.

    The exclusive case never reaches this function: a custom layer carrying a
    document-level ``<loadExclusive/>`` makes ES-DE skip the bundled file
    wholesale (``SystemData.cpp:858-895``, v3.4.1), so the callers honor
    :attr:`CatalogueLayer.load_exclusive` before ever reading a bundled layer
    to merge.
    """
    merged = dict(bundled)
    merged.update(custom)
    return merged


# ES-DE substitutes exactly this token in a system's <path>, and only there;
# every other %TOKEN% in the file belongs to <command>.
ROMPATH_TOKEN = "%ROMPATH%"


def parse_es_settings(text: str) -> dict[str, str] | None:
    """The ``<string name= value=>`` settings of an ``es_settings.xml``, or ``None``.

    **The file is not well-formed XML**, and that is not a defect to route
    around: ES-DE writes a bare sequence of sibling elements with no root, and
    reads it back with pugixml, which does not enforce the single-root rule.
    Handing the text straight to an XML parser fails at the second element
    ("junk after document element") — so a reader that did the obvious thing
    would call every real machine's settings unreadable and then report the ROM
    directory unresolvable everywhere. The document is wrapped in a synthetic
    root to read the fragments the way the writer meant them.

    A leading UTF-8 BOM is stripped for the same reason, not as a courtesy:
    pugixml detects the encoding from it and reads such a file normally, so the
    settings in it are the settings *in force*, and refusing them would make
    atlas answer about a configuration the frontend is not using. It has to go
    before the wrap either way — inside one it would push the XML declaration
    off the front of the document.

    ``None`` is *unparseable even then*, and is why this does not simply answer
    ``{}``: a file that could not be read and a file that sets nothing are the
    same empty mapping and opposite facts, and only one of them means the
    frontend's own defaults apply.
    """
    body = text.removeprefix("\ufeff").lstrip()
    if body.startswith("<?"):
        _, _, body = body.partition("?>")
    try:
        root = ET.fromstring(f"<es-settings>{body}</es-settings>")
    except ET.ParseError:
        return None
    settings: dict[str, str] = {}
    for element in root:
        if element.tag != "string":
            continue
        name = element.get("name")
        if name:
            settings[name] = element.get("value") or ""
    return settings


def _collapse_separators(path: str) -> str:
    """ES-DE's ``//`` collapse — the loop, not one pass.

    ``Utils::String::replace`` re-scans until the pattern is gone
    (``es-core/src/utils/StringUtil.cpp``, ES-DE 3.4.1), so ``a///b`` reaches
    ``a/b`` there. Python's ``str.replace`` is one pass over non-overlapping
    matches and would leave ``a//b`` behind, which is a different directory
    string for anything comparing paths textually.
    """
    while "//" in path:
        path = path.replace("//", "/")
    return path


def expand_home_path(path: str, home: str) -> str:
    """ES-DE's ``expandHomePath``: every ``~`` in *path* replaced with *home*.

    Mirrors ``Utils::FileSystem::expandHomePath``
    (``es-core/src/utils/FileSystemUtil.cpp:663-675``, ES-DE v3.4.1), whose
    whole body for this call is ``Utils::String::replace(path, "~",
    getHomePath())`` — the ``systemHome`` parameter defaults ``false``
    (``FileSystemUtil.h:55``) and the ROMDirectory call site passes one
    argument (``es-app/src/FileData.cpp:289``). That is plain text
    substitution, not a shell's tilde grammar: a bare ``~`` becomes the home,
    ``~user`` looks no user up and becomes the home with ``user`` glued on,
    and a ``~`` in the middle of the value is replaced just the same.
    Mirroring it exactly is the point — for these spellings the substituted
    string *is* the directory the frontend launches from, odd or not, and a
    stricter atlas would refuse a machine ES-DE runs fine on.

    One pass, not the collapse loop: ``Utils::String::replace``
    (``es-core/src/utils/StringUtil.cpp:267-297``) rewrites every occurrence
    left to right, and its outer loop cannot rescan this call — after one
    pass no ``~`` from the input remains, and a *home* that itself carried
    one would hit the endless-loop break (``:293-294``). Python's
    ``str.replace`` has exactly that shape, where the ``//`` collapse above
    needed the loop spelled out.

    The *home* is an argument because establishing it is the caller's work,
    per arrangement: ``getHomePath()`` (``FileSystemUtil.cpp:183-229``)
    answers the ``--home`` a launcher passed (RetroDECK's relocated config
    home) or ``$HOME`` (EmuDeck's plain one), and this module does no I/O.
    """
    return path.replace("~", home)


def resolve_rom_path(declared: str, rom_directory: str | None) -> str | None:
    """A system's declared ``<path>`` with ``%ROMPATH%`` substituted, or ``None``.

    Follows ``SystemData::loadConfig()`` (``es-app/src/SystemData.cpp``, ES-DE
    3.4.1, ~L859-861, line numbers read from the tagged source over the web):
    the token is replaced with the ROM directory, then ``//`` is collapsed —
    **unconditionally**, on a path that carried no token just the same, which is
    why the collapse here is not inside the substitution branch.

    The directory is normalized to exactly one trailing separator first, because
    that is the shape ES-DE substitutes: ``FileData::getROMDirectory()``
    (``es-app/src/FileData.cpp:271-305``, ES-DE 3.4.1) appends one where the
    configured value lacks it (``:291-297``) and returns the empty-setting
    default with one already on (``:283-284``). Doing it here rather than appending unconditionally keeps a
    configured ``…/roms/`` from spelling the answer ``…/roms//n64``.

    A declared path carrying no token needs no directory and resolves to
    itself: ES-DE insists on the token only in ``createSystemDirectories()``
    (~L1366, the placeholder-generation path), never when loading the
    catalogue, so a literal ``<path>`` is a real path here too.

    ``None`` when the token is present and *rom_directory* cannot stand in for
    it: unset, or not absolute. ``~`` expansion is deliberately not this
    function's job — what a ``~`` becomes is ES-DE's own home, which only the
    caller has established, so the handles expand first
    (:func:`expand_home_path`) and a value still carrying one here was never
    expanded. It is refused with the rest of the relative shapes rather than
    guessed at, because a ROM directory guessed wrong is a directory the
    caller would go looking in.
    """
    if ROMPATH_TOKEN not in declared:
        return _collapse_separators(declared) or None
    if not rom_directory or not rom_directory.startswith("/"):
        return None
    return _collapse_separators(declared.replace(ROMPATH_TOKEN, f"{rom_directory.rstrip('/')}/"))


@dataclass(frozen=True, slots=True)
class GamelistSelections:
    """The user's emulator choices stored in a system's ``gamelist.xml``.

    ``system_label`` is the per-system choice (``<alternativeEmulator><label>``,
    beside or inside ``<gameList>`` — see :func:`parse_gamelist`); ``per_game``
    maps each game entry's gamelist-relative path (``./`` stripped; a file
    path, or a folder path for directory entries) to its ``<altemulator>``
    label. Both tag names are verified against the ES-DE binary's strings; the
    per-game hierarchy is game > system > declared.
    """

    system_label: str | None
    per_game: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_game", MappingProxyType(dict(self.per_game)))


def parse_gamelist(text: str) -> GamelistSelections:
    """Parse a ``gamelist.xml`` for emulator selections — both shapes ES-DE reads.

    The per-system selection lives in one of two places, and atlas reads it
    where ES-DE reads it, in ES-DE's own order — the document-level element
    first, the ``<gameList>`` child as the fallback
    (``es-app/src/GamelistFileParser.cpp:190-192``, ES-DE commit ``9207fc77``):

    1. as a **second root element** before ``<gameList>`` — what ES-DE writes
       today (``doc.prepend_child``, same file line 420 when updating an
       existing gamelist and line 444 when creating one), and not well-formed
       XML.
    2. as a **child of ``<gameList>``** — the standards-compliant location
       ES-DE is moving to ("Added forward compatibility for reading the
       alternativeEmulator element from the gameList root element",
       2026-07-29). This is the shape observed live on RetroDECK 0.10.9b,
       whose launcher matches it too (``libexec/run_game.sh:125-135``, an awk
       range over the file).

    Deeper in the tree — inside a ``<game>`` — is not a place ES-DE looks for
    the *system* selection (a game's own choice is ``<altemulator>``), so the
    lookup stays depth-bounded instead of searching the whole document.

    Wrapping in a synthetic root parses the two-root quirk and well-formed
    variants alike; a leading UTF-8 BOM is stripped first, because pugixml
    reads a BOM'd gamelist normally and inside the wrapper the mark would
    fail the whole file — selections ES-DE honors would read as none set.
    Anything unparseable yields empty selections, never a guess.
    """
    stripped = text.removeprefix("\ufeff").strip()
    if stripped.startswith("<?"):
        end = stripped.find("?>")
        if end != -1:
            stripped = stripped[end + 2 :]
    try:
        root = ET.fromstring(f"<atlas-wrapper>{stripped}</atlas-wrapper>")
    except ET.ParseError:
        return GamelistSelections(system_label=None, per_game={})
    selection_el = root.find("alternativeEmulator")
    if selection_el is None:
        game_list = root.find("gameList")
        if game_list is not None:
            selection_el = game_list.find("alternativeEmulator")
    system_label: str | None = None
    if selection_el is not None:
        system_label = (selection_el.findtext("label") or "").strip() or None
    per_game: dict[str, str] = {}
    for game in root.iter("game"):
        path = (game.findtext("path") or "").strip()
        label = (game.findtext("altemulator") or "").strip()
        if path and label:
            # Keep the full gamelist-relative path — basenames alone collide
            # when subdirectories repeat a name (./USA/Game.iso vs ./Japan/…).
            normalized = path.replace("\\", "/").rstrip("/")
            if normalized.startswith("./"):
                normalized = normalized[2:]
            per_game[normalized] = label
    return GamelistSelections(system_label=system_label, per_game=per_game)


def parse_gamelist_alternative(text: str) -> str | None:
    """The per-system ``alternativeEmulator`` label — see :func:`parse_gamelist`."""
    return parse_gamelist(text).system_label
