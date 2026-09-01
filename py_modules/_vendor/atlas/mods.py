"""The mods family's packaged knowledge — what no read of a machine recovers.

Two questions share this file, because they are two halves of one subject: where
a mod for this game goes, and which patch files the frontend applies to the
content before any emulator sees it. What they have in common is the boundary
rule — the roots and the switches are read live, and only what is written
nowhere on a machine is packaged here, marked and cited.

What it carries:

- **The mod cards**, in the shape :mod:`atlas.textures` established for texture
  packs and for the same reasons — a card names a root *kind* and the fragment
  below it, never a directory; it names the option that governs the feature but
  never that option's value; and it is refused without provenance. Two things
  are this family's own. A card states one **or several** trees, because an
  emulator may read mods from directories that are different mechanisms rather
  than alternatives (FBNeo's ``patched`` / ``ips`` / ``romdata``), and a tree
  then names its role. And a card may record the **default** of its option: a
  core that registers its options too late for any probe to capture leaves the
  machine stating nothing, and the value written down here is what stands then —
  pinned to the build it was read from, so an update cannot carry it silently.

- **Which patch formats a RetroArch build attempts.** Soft patching as a whole
  is ``HAVE_PATCH`` and its ``.xdelta`` applier ``HAVE_XDELTA``
  (``Makefile.common:260-267`` at RetroArch a79435a), both decided when the
  binary was compiled. No setting, no log and no file on a running machine
  states either, so this is not a live read for any arrangement — it is a fact
  about a shipped build, and the only honest way to carry it is per arrangement,
  pinned to the version of that arrangement it was proven against.

That pin is the same device the texture cards' ``absent_switch`` uses and it is
here for the same reason: a claim about a build must not survive the build
changing. An arrangement whose machine states a different version gets the claim
with ``unverified-version`` beside it; an arrangement with no record at all gets
no claim, and its answer says which formats were left unestablished rather than
assuming the upstream build defaults.

Facts in data, interpretation in code: this module only loads and indexes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ._data import packaged_text
from .placement import KEYINGS, PATCH_FORMATS, ROOT_KINDS, Keying
from .textures import SO_SUFFIX, XDG_BASES, expect_table_anchors, path_segments

# Packaged-data schema version. The loader is strict for the same reason every
# other packaged-data loader here is: a malformed build fails loudly instead of
# resolving with knowledge nobody can place.
MODS_SCHEMA = 1


# This check exists verbatim in every packaged-data loader
# (:func:`atlas.oddities._expect_str`, :func:`atlas.textures._expect_str`,
# :func:`atlas.evidence._expect_str`). The repetition is the deliberate cost of
# keeping the loaders independent: each reads its one file and shares no
# machinery with the others, so a defect in one table can never fail the load of
# another.
def _expect_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _expect_str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{where}: expected a list of non-empty strings, got {value!r}")
    return tuple(value)


def _expect_subdir(value: object, where: str) -> str:
    """The fragment below the root: relative, and no path trickery in it.

    Joined onto a directory resolved from a config, so an absolute fragment
    would silently discard that root and a ``..`` would climb out of it — both
    of which reach a caller as an ordinary-looking answer pointing somewhere the
    emulator never reads.
    """
    subdir = _expect_str(value, where)
    if subdir.startswith("/"):
        raise ValueError(
            f"{where}: {subdir!r} is absolute — a card states the fragment BELOW a root it does not "
            "know, and an absolute one would replace that root instead of extending it"
        )
    if ".." in subdir.split("/"):
        raise ValueError(f"{where}: {subdir!r} climbs out of the root with '..'")
    return subdir


@dataclass(frozen=True, slots=True)
class ModSetting:
    """A configuration key whose *value* is a directory, with what governs without it.

    The card says where the value lives and what the emulator falls back to;
    reading it is code beside the card, because reading a configuration always
    is. ``default`` is the emulator's own compiled fallback, spelled the way
    the emulator spells it, so a card never states twice what a missing key
    means.
    """

    section: str
    key: str
    default: str
    citation: str


@dataclass(frozen=True, slots=True)
class ModTreeSpec:
    """One directory a card states mods are read from.

    Stated one of two ways, and exactly one: ``subdir`` for a tree the
    emulator opens at a fixed place below the card's XDG base, or
    ``directory`` for one it opens wherever its own configuration points —
    the shape an emulator needs when the root the tree hangs off is not a
    fixed XDG join at all. A card carrying any ``directory`` tree needs a
    resolver registered beside it in :mod:`atlas.installations` and fails the
    load without one, exactly as a texture card does.

    ``role`` is the emulator's own word for this tree and is ``None`` on a card
    that states one — see :class:`~atlas.placement.ModTree` for why the field
    exists at all. ``keying`` follows the cited-or-absent rule of every recorded
    keying here.
    """

    subdir: str | None
    directory: ModSetting | None
    role: str | None
    keying: Keying | None
    keying_citation: str | None


@dataclass(frozen=True, slots=True)
class OptionDefault:
    """The value a core's option takes when nothing on the machine states one.

    Written down only where the machine *cannot* answer: a core that registers
    its options after ``retro_set_environment`` gives a probe nothing to read,
    so an options file with no entry leaves the switch unresolvable and the
    answer would drop a fact upstream states plainly. LRPS2 and FBNeo are both
    in that position.

    The live core still wins wherever it speaks — a default the probe *does*
    capture is a fact about this machine's binary and outranks any record. So
    this value is the fallback, and it is pinned like every other claim about a
    build: ``verified_core`` names the generation it was read at, and a machine
    running another one gets the value with ``unverified-version`` beside it.
    """

    value: str
    verified_core: str
    citation: str


@dataclass(frozen=True, slots=True)
class SoftPatchBuild:
    """Which patch formats one arrangement's shipped RetroArch was built to try.

    ``formats`` is the established set — a format outside it is one this build
    does **not** attempt, which is a claim as strong as the positive one and
    rests on the same read. An empty set is therefore a legal record (a build
    with patching compiled out), not a missing one: a build nobody examined has
    no record at all and answers ``None`` for every format.

    ``verified_arrangement`` pins the version the binary was read at, and
    ``citation`` says how. Both are required for the reason the texture cards'
    absent switch requires its own: the strongest statements in this project are
    the ones about a *build*, and a build is exactly what an update replaces.
    """

    kind: str
    formats: frozenset[str]
    verified_arrangement: str
    citation: str

    def attempts(self) -> dict[str, bool]:
        """Per known format, whether this build tries it — the whole vocabulary, decided."""
        return {fmt: fmt in self.formats for fmt in PATCH_FORMATS}


def _soft_patch_build(kind: str, entry: Any) -> SoftPatchBuild:
    """One arrangement's build record — validated, never coerced."""
    where = f"soft-patching record {kind!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    formats = entry.get("formats")
    if not isinstance(formats, list) or not all(isinstance(f, str) for f in formats):
        raise ValueError(f"{where}: formats must be a list of format names, got {formats!r}")
    unknown = sorted(set(formats) - set(PATCH_FORMATS))
    if unknown:
        # It reaches the caller as a candidate's ``attempted``, so a format
        # atlas does not compose a path for could only ever be a claim about a
        # file nobody looks for.
        raise ValueError(
            f"{where}: formats must be drawn from {sorted(PATCH_FORMATS)}, got {unknown}"
        )
    return SoftPatchBuild(
        kind=kind,
        formats=frozenset(formats),
        verified_arrangement=_expect_str(
            entry.get("verified_arrangement"), f"{where}: verified_arrangement"
        ),
        citation=_expect_str(entry.get("citation"), f"{where}: citation"),
    )


@dataclass(frozen=True, slots=True)
class ModOption:
    """The option that switches this emulator's mod loading on, and what its values mean.

    ``values`` maps each value the card knows to whether it means *enabled*; a
    value outside the map leaves the answer's ``enabled`` unstated with a
    caveat, because reading an unknown word as "off" is the guess the boundary
    rule refuses. ``default`` is the recorded fallback for a core whose options
    no probe can capture — ``None`` on every card whose core states its own.
    """

    setting: str
    values: Mapping[str, bool]
    default: OptionDefault | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class EmulatorConfig:
    """A settings file that would answer the switch, named but never read.

    Two shapes, because two kinds of emulator keep one. A standalone emulator's
    sits below an XDG base (``base`` names which); a libretro core that ports
    one keeps its ini inside the user tree it builds under the root RetroArch
    hands it, and then ``base`` is ``None`` and ``path`` is relative to that
    root. Either way the file is named and not read, and
    ``emulator-config-unread`` carries the pair.
    """

    path: str
    base: str | None = None


@dataclass(frozen=True, slots=True)
class ModCard:
    """One core's mod rule card: which trees below which root, keyed how, gated by what."""

    key: str
    library_names: tuple[str, ...]
    root: str
    trees: tuple[ModTreeSpec, ...]
    option: ModOption | None
    config: EmulatorConfig | None
    provenance: str

    @property
    def so_name(self) -> str:
        """The ``.so`` basename this card describes — the key plus the suffix."""
        return f"{self.key}{SO_SUFFIX}"

    def matches(self, *, so_basename: str | None, library_name: str | None) -> bool:
        if so_basename is not None and so_basename == self.so_name:
            return True
        return library_name is not None and library_name in self.library_names


@dataclass(frozen=True, slots=True)
class StandaloneModCard:
    """Where a standalone emulator reads mods, below which XDG base.

    The standalone twin of :class:`ModCard`, keyed by the ``%EMULATOR_…%`` token
    the frontend's launch command names — for a standalone entry that token is
    the only identifier there is.

    ``config`` is optional here, and that is this family's one departure from
    the texture cards: naming a file as "the one that would answer the switch"
    is itself a claim, and for one row nobody has established that any switch
    exists. A card that names none leaves ``enabled`` unstated **without** the
    caveat that points somewhere — an honest silence rather than a signpost to
    a file that may govern nothing.
    """

    token: str
    base: str | None
    trees: tuple[ModTreeSpec, ...]
    settings: str | None
    provenance: str

    @property
    def configured(self) -> bool:
        """Does any tree hang off a configuration value rather than a fixed join?"""
        return any(tree.directory is not None for tree in self.trees)


def _keying(value: object, where: str) -> tuple[Keying | None, str | None]:
    """How the tree below the root is divided per game — cited, or not stated.

    The citation is required rather than encouraged, exactly as it is for the
    texture cards: a keying is the one field no read of any machine can
    contradict, so an uncited one would be indistinguishable from a cited one at
    the point a client acts on it, and saying nothing is always available.
    """
    if value is None:
        return None, None
    if not isinstance(value, dict) or set(value) != {"value", "citation"}:
        raise ValueError(f"{where}: expected {{'value': …, 'citation': …}} or null, got {value!r}")
    keying = _expect_str(value.get("value"), f"{where}.value")
    if keying not in set(KEYINGS):
        raise ValueError(f"{where}.value: must be one of {sorted(KEYINGS)}, got {keying!r}")
    return keying, _expect_str(value.get("citation"), f"{where}.citation")


def _trees(value: object, where: str) -> tuple[ModTreeSpec, ...]:
    """The directories a card states — one, or several that are different mechanisms.

    A card with several requires a role on each, and the roles must differ:
    they are what a caller holding three directories tells them apart by, and a
    repeated one would make two of them indistinguishable in the answer. A card
    with one states no role, because there is nothing to tell apart.
    """
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where}: expected a non-empty list of trees, got {value!r}")
    trees: list[ModTreeSpec] = []
    for index, entry in enumerate(value):
        at = f"{where}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{at}: expected an object, got {entry!r}")
        role = entry.get("role")
        if role is not None:
            role = _expect_str(role, f"{at}.role")
        keying, citation = _keying(entry.get("keying"), f"{at}.keying")
        subdir = entry.get("subdir")
        directory = entry.get("directory")
        if (subdir is None) == (directory is None):
            raise ValueError(
                f"{at}: state exactly one of 'subdir' and 'directory' — a fixed place below the "
                "card's base, or the configuration key whose value is the directory"
            )
        trees.append(
            ModTreeSpec(
                subdir=None if subdir is None else _expect_subdir(subdir, f"{at}.subdir"),
                directory=None if directory is None else _mod_setting(directory, f"{at}.directory"),
                role=role,
                keying=keying,
                keying_citation=citation,
            )
        )
    roles = [tree.role for tree in trees]
    if len(trees) == 1:
        if roles[0] is not None:
            raise ValueError(
                f"{where}: a card stating one tree names no role — the field tells several trees "
                f"apart, and there is nothing here to tell apart (got {roles[0]!r})"
            )
    elif None in roles:
        raise ValueError(f"{where}: every tree of a multi-tree card names its role")
    elif len(set(roles)) != len(roles):
        raise ValueError(f"{where}: roles must tell the trees apart, got {roles}")
    return tuple(trees)


def _mod_setting(value: object, where: str) -> ModSetting:
    """The configuration key a tree hangs off — every field required, none inferred."""
    if not isinstance(value, dict) or set(value) != {"section", "key", "default", "citation"}:
        raise ValueError(
            f"{where}: expected exactly section/key/default/citation, got {value!r}"
        )
    return ModSetting(
        section=_expect_str(value.get("section"), f"{where}.section"),
        key=_expect_str(value.get("key"), f"{where}.key"),
        default=_expect_subdir(value.get("default"), f"{where}.default"),
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _option_default(value: object, where: str) -> OptionDefault | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"value", "verified_core", "citation"}:
        raise ValueError(
            f"{where}: expected {{'value': …, 'verified_core': …, 'citation': …}} or null, "
            f"got {value!r}"
        )
    return OptionDefault(
        value=_expect_str(value.get("value"), f"{where}.value"),
        verified_core=_expect_str(value.get("verified_core"), f"{where}.verified_core"),
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _mod_option(value: object, where: str) -> ModOption | None:
    """The governing option, or ``None`` where the card records none."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object or null, got {value!r}")
    values = value.get("values")
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{where}.values: expected a non-empty object of option value -> boolean")
    for option_value, meaning in values.items():
        if not isinstance(option_value, str) or not option_value:
            raise ValueError(f"{where}.values: {option_value!r} is not an option value")
        if not isinstance(meaning, bool):
            # bool("false") is True in Python — never coerce this claim.
            raise ValueError(f"{where}.values[{option_value!r}]: must be a JSON boolean")
    if set(values.values()) != {True, False}:
        # An option whose every value means the same thing governs nothing.
        raise ValueError(
            f"{where}.values: must name at least one value that means enabled and one that means "
            f"disabled — got {sorted(values)} meaning {sorted(set(values.values()))}"
        )
    default = _option_default(value.get("default"), f"{where}.default")
    if default is not None and default.value not in values:
        # A default outside the card's own vocabulary would reach the answer as
        # a value it then refuses to interpret — a record contradicting itself.
        raise ValueError(
            f"{where}.default.value: {default.value!r} is not one of this option's values "
            f"{sorted(values)}"
        )
    return ModOption(
        setting=_expect_str(value.get("setting"), f"{where}.setting"), values=values, default=default
    )


def _core_config(value: object, where: str) -> EmulatorConfig | None:
    """The ini a core keeps inside its own user tree — relative to the card's root."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"path"}:
        raise ValueError(f"{where}: expected {{'path': …}} or null, got {value!r}")
    return EmulatorConfig(path=_expect_subdir(value.get("path"), f"{where}.path"))


def _settings_name(value: object, where: str) -> str | None:
    """The settings file a standalone card names, or ``None`` where none is established.

    A **name**, not an address: where the file lives is stated once in
    ``atlas/data/emulator_settings.json``, since the save, texture, mod and
    firmware answers of one emulator open the same file. ``None`` keeps this
    family's own meaning — for one emulator nobody has established that a
    switch exists at all, and naming a file would signpost one that may govern
    nothing.
    """
    return None if value is None else _expect_str(value, where)


def recorded_mod_words(entry: Mapping[str, Any]) -> frozenset[str]:
    """Every word a mods row states as this emulator's own — both row kinds.

    Tree path segments, the option setting where one governs, and the config
    file's own name where one is named; the same vocabulary rule the texture
    rows follow (issue #105).
    """
    mods = entry.get("mods", {})
    words: list[str] = []
    for tree in mods.get("trees") or ():
        if isinstance(tree, dict):
            words.extend(path_segments(tree.get("subdir")))
            directory = tree.get("directory")
            if isinstance(directory, dict):
                # A configured tree states three names of the emulator's own —
                # the section and key it reads, and the fallback it composes
                # without one — and all three are pinned like any subpath
                # segment. The section counts: a card whose key survives a
                # rename of the section around it reads nothing, and the
                # texture rows have always watched theirs.
                for field in ("section", "key", "default"):
                    if isinstance(directory.get(field), str):
                        words.extend(path_segments(directory[field]))
    option = mods.get("option")
    if isinstance(option, dict) and isinstance(option.get("setting"), str):
        words.append(option["setting"])
    # A standalone row names its settings file rather than addressing it, and
    # the name is the emulator's own word for the file; a core row still gives
    # a path, relative to the user tree the core builds.
    settings = mods.get("settings")
    if isinstance(settings, str):
        words.append(settings)
    config = mods.get("config")
    if isinstance(config, dict) and isinstance(config.get("path"), str):
        words.append(config["path"].rsplit("/", 1)[-1])
    return frozenset(words)


def _mod_card(key: str, entry: Any) -> ModCard:
    """One core's card — validated, never coerced."""
    where = f"mod card {key!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    identifiers = entry.get("identifiers", {})
    if "so" in identifiers:
        raise ValueError(
            f"{where}: identifiers.so is derived from the card key ({key + SO_SUFFIX!r}) and not "
            "read — a restated one could only ever disagree with it"
        )
    mods = entry.get("mods")
    if not isinstance(mods, dict):
        raise ValueError(f"{where}: expected a 'mods' object, got {mods!r}")
    root = _expect_str(mods.get("root"), f"{where}: mods.root")
    if root not in set(ROOT_KINDS):
        raise ValueError(f"{where}: mods.root must be one of {sorted(ROOT_KINDS)}, got {root!r}")
    trees = _trees(mods.get("trees"), f"{where}: mods.trees")
    if any(tree.directory is not None for tree in trees):
        raise ValueError(
            f"{where}: a core is handed its root by RetroArch, so a tree here states the "
            "fragment below it — a configuration key of an emulator's own has nothing to "
            "name on this side"
        )
    if entry.get("anchors") is not None:
        expect_table_anchors(
            entry["anchors"],
            where=where,
            vocabulary=recorded_mod_words(entry),
            binary_required=False,
        )
    return ModCard(
        key=key,
        library_names=_expect_str_list(
            identifiers.get("library_name", []), f"{where}: identifiers.library_name"
        ),
        root=root,
        trees=trees,
        option=_mod_option(mods.get("option"), f"{where}: mods.option"),
        config=_core_config(mods.get("config"), f"{where}: mods.config"),
        provenance=_expect_str(
            entry.get("provenance", {}).get("source"), f"{where}: provenance.source"
        ),
    )


def _standalone_mod_card(token: str, entry: Any) -> StandaloneModCard:
    """One standalone emulator's card — validated, never coerced."""
    where = f"standalone mod card {token!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    mods = entry.get("mods")
    if not isinstance(mods, dict):
        raise ValueError(f"{where}: expected a 'mods' object, got {mods!r}")
    trees = _trees(mods.get("trees"), f"{where}: mods.trees")
    raw_base = mods.get("base")
    fixed = [tree for tree in trees if tree.subdir is not None]
    if raw_base is None and fixed:
        raise ValueError(f"{where}: mods.base is what a tree stating a subdir hangs off")
    if raw_base is not None and not fixed:
        raise ValueError(
            f"{where}: mods.base names a root no tree uses — every tree here states a "
            "configuration key instead"
        )
    base = None if raw_base is None else _expect_str(raw_base, f"{where}: mods.base")
    if base is not None and base not in XDG_BASES:
        raise ValueError(f"{where}: mods.base must be one of {sorted(XDG_BASES)}, got {base!r}")
    if entry.get("anchors") is not None:
        expect_table_anchors(
            entry["anchors"],
            where=where,
            vocabulary=recorded_mod_words(entry),
            binary_required=True,
        )
    return StandaloneModCard(
        token=token,
        base=base,
        trees=trees,
        settings=_settings_name(mods.get("settings"), f"{where}: mods.settings"),
        provenance=_expect_str(
            entry.get("provenance", {}).get("source"), f"{where}: provenance.source"
        ),
    )


def _packaged_raw(text: str | None) -> dict[str, Any]:
    raw = json.loads(text if text is not None else packaged_text("mods.json"))
    if not isinstance(raw, dict) or raw.get("schema") != MODS_SCHEMA:
        raise ValueError(
            f"mods: unsupported schema {raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {MODS_SCHEMA})"
        )
    return raw


def load_mod_cards(text: str | None = None) -> tuple[ModCard, ...]:
    """Load the packaged core cards (or *text* when supplied, for tests)."""
    return tuple(_mod_card(key, entry) for key, entry in _packaged_raw(text).get("cores", {}).items())


def load_standalone_mod_cards(text: str | None = None) -> tuple[StandaloneModCard, ...]:
    """Load the packaged standalone cards (or *text* when supplied, for tests)."""
    return tuple(
        _standalone_mod_card(token, entry)
        for token, entry in _packaged_raw(text).get("emulators", {}).items()
    )


_PACKAGED: tuple[ModCard, ...] | None = None
_PACKAGED_STANDALONE: tuple[StandaloneModCard, ...] | None = None


def lookup_mod_card(*, so_basename: str | None, library_name: str | None) -> ModCard | None:
    """Find the packaged mod card matching a core, by ``.so`` name or ``library_name``."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_mod_cards()
    for card in _PACKAGED:
        if card.matches(so_basename=so_basename, library_name=library_name):
            return card
    return None


def lookup_standalone_mod_card(token: str | None) -> StandaloneModCard | None:
    """Find the packaged card for the emulator a ``%EMULATOR_…%`` token names."""
    global _PACKAGED_STANDALONE
    if _PACKAGED_STANDALONE is None:
        _PACKAGED_STANDALONE = load_standalone_mod_cards()
    if token is None:
        return None
    return next((card for card in _PACKAGED_STANDALONE if card.token == token), None)


def load_soft_patch_builds(text: str | None = None) -> dict[str, SoftPatchBuild]:
    """Load the packaged build records (or *text* when supplied, for tests).

    Reading packaged data is not the machine seam — it is the library reading
    its own bundled world knowledge, which is exactly what these records are.
    """
    return {
        kind: _soft_patch_build(kind, entry)
        for kind, entry in _packaged_raw(text).get("soft_patching", {}).items()
    }


_PACKAGED_BUILDS: dict[str, SoftPatchBuild] | None = None


def lookup_soft_patch_build(kind: str) -> SoftPatchBuild | None:
    """The packaged build record for an installation kind, or ``None`` for none.

    ``None`` is the ordinary state, not an error: most arrangements ship a
    RetroArch nobody has examined for this, and the answer says so.
    """
    global _PACKAGED_BUILDS
    if _PACKAGED_BUILDS is None:
        _PACKAGED_BUILDS = load_soft_patch_builds()
    return _PACKAGED_BUILDS.get(kind)
