"""Texture cards — where each emulator reads texture packs, minus the live part.

The cards live in ``data/texture_packs.json`` — world knowledge under the
boundary rule, and the split it makes is sharper than the save cards':

- **The root is not here.** A card names a root *kind* and the resolver resolves
  it live, the same way the save routes resolve theirs (the system directory as
  the core is handed it, the save root as it stands). A directory written down
  would be stale on the first user click.
- **The option's value is not here either.** A card names the option that
  governs replacement and which of its values mean *on*; which value is set is
  read from the options file RetroArch would read first, or from the default the
  installed core registers — both live reads.
- **What is here is what no machine states**: the fragment below the root, the
  option's identity, and the per-game keying of the tree.

Cards are keyed by the core's canonical short name (the ``.so`` basename without
``_libretro.so``), which is where the ``.so`` name comes from — derived, not
restated, so the two cannot disagree. ``identifiers.library_name`` carries the
display name the binary reports, so lookup works from either side. Both
conventions are :mod:`atlas.oddities`'s, deliberately: a maintainer editing one
table should not have to learn a second dialect.

Every recorded fact is refused without provenance. That is the one rule this
loader adds over the save cards', and it is the one decision 4 of the family's
design rests on: a keying is stated only where a citation backs it, so the field
is absent rather than derived wherever the evidence stops. A card that recorded
a keying and cited nothing would put a guess into an answer under the same field
name a cited one uses, which no client could tell apart.

Facts in data, interpretation in code: this module only loads and indexes; the
resolver in :mod:`atlas.installations` resolves the root and joins it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ._data import packaged_text
from .placement import KEYINGS, ROOT_KINDS, Keying

# Packaged-data schema version. The loader is strict for the same reason every
# other packaged-data loader here is: a malformed build fails loudly instead of
# resolving with knowledge nobody can place.
TEXTURE_PACKS_SCHEMA = 1

# How a card's ``.so`` is spelled — the card key plus this suffix, exactly as
# :data:`atlas.oddities.SO_SUFFIX` spells it for the save cards.
SO_SUFFIX = "_libretro.so"

# The roots a card may anchor at. The placement's own vocabulary, imported
# rather than respelled: a value that only looked right would be joined onto a
# resolved directory and stated as fact.
_KNOWN_ROOTS = set(ROOT_KINDS)
_KNOWN_KEYINGS = set(KEYINGS)

# The XDG base a standalone emulator's tree hangs off. Two values, because two
# are what the emulators use: a data home for content-like trees (Dolphin's
# ``Load``, Cemu's ``graphicPacks``) and a config home for settings-adjacent
# ones (PPSSPP's memory stick, DuckStation's data directory). *Which* base an
# emulator uses is world knowledge; where that base is on this machine is the
# arrangement's to resolve, and a flatpak pins it.
XDG_DATA = "data"
XDG_CONFIG = "config"
XDG_BASES = (XDG_DATA, XDG_CONFIG)


# This check exists verbatim in every packaged-data loader
# (:func:`atlas.oddities._expect_str`, :func:`atlas.evidence._expect_str`,
# :func:`atlas.systems._expect_str`). The repetition is the deliberate cost of
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

    It is joined onto a directory that was resolved from a config, so an
    absolute fragment would silently discard that root and a ``..`` would climb
    out of it — both of which reach a caller as an ordinary-looking answer
    pointing somewhere the emulator never reads.
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
class ReplacementOption:
    """The core option that switches texture replacement on, and what its values mean.

    ``setting`` is the core option's own name — spelled ``setting`` rather than
    ``key`` so it matches :class:`AbsentSwitch`, which says the same thing about
    a switch that does not exist. ``values`` maps each value the card knows to
    whether it means *enabled*. A
    value outside the map leaves :attr:`~atlas.placement.TexturePlacement.enabled`
    unanswered with a stated caveat — the record lags the core's generation, and
    reading an unknown word as "off" would be the guess the boundary rule
    refuses.
    """

    setting: str
    values: Mapping[str, bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class AbsentSwitch:
    """A feature this build compiles in and offers no way to switch.

    The third thing a card can say about the switch, beside naming a core option
    and saying nothing. Here the setting exists in the emulator's own
    vocabulary, its value is established, and **nothing in the shipped build
    writes it** — so the state is a fact about the binary rather than a reading
    of any file, and it cannot change until the build does.

    That is why ``citation`` is required, and why ``verified_core`` is: "no
    writer anywhere" is the strongest negative in this file, and the only honest
    way to carry it is pinned to the binary it was proven against. The pin lives
    **here** rather than being read off ``core_audit.json``, and the difference
    is not cosmetic: that record's ``core_library_version`` moves whenever a
    live round re-verifies a core's *save* behaviour, and a bump for an
    unrelated reason would silently re-validate this claim against a build
    nobody examined for it. A field of its own moves only when someone
    re-examines the build for this.
    """

    setting: str
    enabled: bool
    verified_core: str
    citation: str


@dataclass(frozen=True, slots=True)
class TextureCard:
    """One core's texture-pack rule card: where below which root, keyed how, gated by what."""

    key: str
    library_names: tuple[str, ...]
    root: str
    subdir: str
    keying: Keying | None
    keying_citation: str | None
    option: ReplacementOption | None
    absent_switch: AbsentSwitch | None
    provenance: str

    @property
    def so_name(self) -> str:
        """The ``.so`` basename this card describes — the key plus the suffix."""
        return f"{self.key}{SO_SUFFIX}"

    def matches(self, *, so_basename: str | None, library_name: str | None) -> bool:
        if so_basename is not None and so_basename == self.so_name:
            return True
        return library_name is not None and library_name in self.library_names


def _replacement_option(value: object, where: str) -> ReplacementOption | None:
    """The governing option, or ``None`` where the card records none.

    A card with no option is a core whose replacement is not switchable, and
    the answer then leaves ``enabled`` unanswered rather than claiming *on*.
    """
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
        # An option whose every value means the same thing governs nothing, and
        # a card recording one would report a feature as permanently on or
        # permanently off while the machine could say otherwise.
        raise ValueError(
            f"{where}.values: must name at least one value that means enabled and one that means "
            f"disabled — got {sorted(values)} meaning {sorted(set(values.values()))}"
        )
    return ReplacementOption(
        setting=_expect_str(value.get("setting"), f"{where}.setting"), values=values
    )


def _keying(value: object, where: str) -> tuple[Keying | None, str | None]:
    """How the tree below the root is divided per game — cited, or not stated.

    The citation is required rather than encouraged. A keying is the one field
    of this table that no read of any machine can contradict, so an uncited one
    would be indistinguishable from a cited one at the point a client acts on
    it, and the honest alternative — saying nothing — is always available.
    """
    if value is None:
        return None, None
    if not isinstance(value, dict) or set(value) != {"value", "citation"}:
        raise ValueError(f"{where}: expected {{'value': …, 'citation': …}} or null, got {value!r}")
    keying = _expect_str(value.get("value"), f"{where}.value")
    if keying not in _KNOWN_KEYINGS:
        # It reaches the caller as the contractual TexturePlacement.keying, so a
        # misspelling here would be stated as this tree's actual shape.
        raise ValueError(f"{where}.value: must be one of {sorted(_KNOWN_KEYINGS)}, got {keying!r}")
    citation = _expect_str(value.get("citation"), f"{where}.citation")
    return keying, citation


def _absent_switch(value: object, where: str) -> AbsentSwitch | None:
    """A switch this build does not offer — or ``None`` where the card claims none."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "setting",
        "enabled",
        "verified_core",
        "citation",
    }:
        raise ValueError(
            f"{where}: expected {{'setting': …, 'enabled': …, 'verified_core': …, 'citation': …}} "
            f"or null, got {value!r}"
        )
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        # It reaches the caller as TexturePlacement.enabled, stated as fact
        # rather than as a reading — and bool("false") is True in Python.
        raise ValueError(f"{where}.enabled: must be a JSON boolean")
    return AbsentSwitch(
        setting=_expect_str(value.get("setting"), f"{where}.setting"),
        enabled=enabled,
        verified_core=_expect_str(value.get("verified_core"), f"{where}.verified_core"),
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


# The byte tripwire's data half, shared with the mods table (issue #105).
# Every name a row records — path segments, option settings, config file
# names — is either pinned to the raw bytes it was read from, or opted out
# with the reason that says what does stand behind it. Raw-byte containment
# rather than NUL-delimited runs, because a tail-merged literal ("load/"
# living only inside "…-emu/load/") is invisible to every token pass; the
# encoding travels with the anchor because one shipped name exists only as
# UTF-32LE. The block is validated here and dropped — audit machinery, not
# an answer — and the byte check itself is a test.
ANCHOR_ENCODINGS = ("utf-8", "utf-16le", "utf-32le")


def path_segments(*paths: str | None) -> list[str]:
    """The recorded words of one or more slash-joined paths, in order."""
    return [segment for path in paths if path for segment in path.split("/") if segment]


def _expect_anchor_binary(value: object, *, at: str, binary_required: bool) -> None:
    """The ``binary`` field's one rule per row kind: stated exactly where nothing derives it."""
    if binary_required and (not isinstance(value, str) or not value):
        raise ValueError(f"{at}: a standalone row names the component binary its literals were read from")
    if not binary_required and value is not None:
        raise ValueError(f"{at}: a core row's binary is derived from its key — a restated one could only disagree")


def _expect_one_anchor(anchor: object, *, here: str) -> None:
    """One anchor: a literal with an optional encoding, or an opt-out with a reason."""
    if not isinstance(anchor, dict) or set(anchor) - {"encoding"} not in ({"literal"}, {"unprotected"}):
        raise ValueError(
            f"{here}: expected {{'literal': …, 'encoding'?: …}} or {{'unprotected': …}}, got {anchor!r}"
        )
    if "unprotected" in anchor:
        if "encoding" in anchor:
            raise ValueError(f"{here}: an encoding belongs to a literal — an opt-out reads no bytes")
        _expect_str(anchor["unprotected"], f"{here}.unprotected")
        return
    _expect_str(anchor["literal"], f"{here}.literal")
    encoding = anchor.get("encoding", "utf-8")
    if encoding not in ANCHOR_ENCODINGS:
        raise ValueError(f"{here}.encoding: must be one of {ANCHOR_ENCODINGS}, got {encoding!r}")


def expect_table_anchors(
    value: object,
    *,
    where: str,
    vocabulary: frozenset[str],
    binary_required: bool,
    extra_keys: frozenset[str] = frozenset(),
) -> None:
    """Validate one row's ``anchors`` block — shape here, bytes in the tests.

    A standalone row names the component binary its literals were read from
    (``binary``, relative to the components tree), because nothing derives
    it; a core row's binary is derived from its key, so a restated one could
    only ever disagree and is refused. *extra_keys* are the keys a caller's
    own block may carry beside those two — the settings table's directory
    anchors name a ``flatpak``, because the build that spells the name lives
    inside that app rather than below the components tree (#246).
    """
    at = f"{where}: anchors"
    allowed = {"binary", "names"} | set(extra_keys)
    if not isinstance(value, dict) or set(value) - allowed or "names" not in value:
        raise ValueError(f"{at}: expected {{'binary'?: …, 'names': …}}, got {value!r}")
    _expect_anchor_binary(value.get("binary"), at=at, binary_required=binary_required)
    names = value["names"]
    if not isinstance(names, dict):
        raise ValueError(f"{at}: 'names' must be an object of recorded name -> anchor, got {names!r}")
    for name, anchor in names.items():
        here = f"{at}[{name!r}]"
        if name not in vocabulary:
            raise ValueError(
                f"{here}: anchors a name this row does not record — an anchor for nothing outlives "
                "the name it was written for and silently protects the next typo instead"
            )
        _expect_one_anchor(anchor, here=here)


def recorded_texture_core_words(entry: Mapping[str, Any]) -> frozenset[str]:
    """Every word a core texture row states as this core's own."""
    textures = entry.get("textures", {})
    words = path_segments(textures.get("subdir"))
    for switch in (textures.get("replacement_option"), textures.get("absent_switch")):
        if isinstance(switch, dict) and isinstance(switch.get("setting"), str):
            words.append(switch["setting"])
    return frozenset(words)


def recorded_texture_emulator_words(entry: Mapping[str, Any]) -> frozenset[str]:
    """Every word a standalone texture row states as this emulator's own.

    A config-stated row records the emulator's own vocabulary too — the
    section and key it reads, and the default it falls back to — so the
    anchors gate proves those against the shipped binary the way it proves a
    fixed subpath's segments.
    """
    textures = entry.get("textures", {})
    words = path_segments(textures.get("subdir"))
    # The settings file is named rather than addressed here, and the name is
    # the emulator's own word for it — so it is anchored exactly as the
    # address's last segment used to be.
    settings = textures.get("settings")
    if isinstance(settings, str):
        words.append(settings)
    # The directory setting's default is a path segment the emulator opens, so
    # it is anchored like one; a switch's default is the spelling of a boolean
    # and names nothing, so it is not.
    for setting, fields in (
        (textures.get("directory"), ("section", "key", "default")),
        (textures.get("switch"), ("section", "key")),
    ):
        if not isinstance(setting, dict):
            continue
        for field in fields:
            value = setting.get(field)
            if isinstance(value, str) and value:
                words.append(value)
    return frozenset(words)


def _texture_card(key: str, entry: Any) -> TextureCard:
    """One core's card — validated, never coerced."""
    where = f"texture card {key!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    identifiers = entry.get("identifiers", {})
    if "so" in identifiers:
        raise ValueError(
            f"{where}: identifiers.so is derived from the card key ({key + SO_SUFFIX!r}) and not "
            "read — a restated one could only ever disagree with it"
        )
    textures = entry.get("textures")
    if not isinstance(textures, dict):
        raise ValueError(f"{where}: expected a 'textures' object, got {textures!r}")
    root = _expect_str(textures.get("root"), f"{where}: textures.root")
    if root not in _KNOWN_ROOTS:
        raise ValueError(f"{where}: textures.root must be one of {sorted(_KNOWN_ROOTS)}, got {root!r}")
    keying, citation = _keying(textures.get("keying"), f"{where}: textures.keying")
    option = _replacement_option(
        textures.get("replacement_option"), f"{where}: textures.replacement_option"
    )
    absent = _absent_switch(textures.get("absent_switch"), f"{where}: textures.absent_switch")
    if option is not None and absent is not None:
        # A switch that exists and a switch that does not are contradictory
        # claims about one build, and the answer would have to pick one.
        raise ValueError(
            f"{where}: a card states either the option that governs replacement or that this build "
            "offers no way to switch it — never both"
        )
    if entry.get("anchors") is not None:
        expect_table_anchors(
            entry["anchors"],
            where=where,
            vocabulary=recorded_texture_core_words(entry),
            binary_required=False,
        )
    return TextureCard(
        key=key,
        library_names=_expect_str_list(
            identifiers.get("library_name", []), f"{where}: identifiers.library_name"
        ),
        root=root,
        subdir=_expect_subdir(textures.get("subdir"), f"{where}: textures.subdir"),
        keying=keying,
        keying_citation=citation,
        option=option,
        absent_switch=absent,
        provenance=_expect_str(
            entry.get("provenance", {}).get("source"), f"{where}: provenance.source"
        ),
    )


@dataclass(frozen=True, slots=True)
class TextureSetting:
    """One configuration key a card names, with the emulator's compiled default.

    The card states where the value lives and what governs without it; the
    resolver registered for the token reads it the way the emulator does.
    ``default`` is the string the emulator falls back to — for a switch, the
    spelling of its boolean default — so a card never has to say what a
    missing key means twice.
    """

    section: str
    key: str
    default: str
    citation: str


@dataclass(frozen=True, slots=True)
class StandaloneTextureCard:
    """Where a standalone emulator reads texture packs, below which XDG base.

    The standalone twin of :class:`TextureCard`, and the difference is exactly
    the one that makes these rows answerable without modelling an emulator: a
    libretro core is handed its root by RetroArch, while a standalone emulator
    opens its own default directory below an XDG base — a fixed subpath the
    emulator itself decides. So a card names the base and the subpath, and the
    arrangement resolves where that base is (a flatpak pins it).

    Keyed by the ``%EMULATOR_…%`` token the frontend's launch command names,
    because for a standalone entry that token is the only identifier there is.

    A card states its directory one of two ways, and exactly one: ``base`` plus
    ``subdir`` for an emulator that opens a fixed default, or ``directory`` —
    the configuration key whose *value* is the directory, for one that opens
    whatever its settings name (PCSX2's ``[Folders] Textures``). The second
    shape needs a resolver registered beside it in :mod:`atlas.installations`
    and fails the load without one, the same way a save card does: reading a
    configuration is code, never a card DSL.

    ``config`` names the settings file both shapes hang off. Where no
    ``switch`` is stated it is never read and ``enabled`` stays ``None`` with
    ``emulator-config-unread`` naming it; where one is, that key is the live
    read behind ``enabled``. ``keying`` follows the same cited-or-absent rule
    as everywhere in this file.
    """

    token: str
    base: str | None
    subdir: str | None
    directory: TextureSetting | None
    switch: TextureSetting | None
    keying: Keying | None
    keying_citation: str | None
    settings: str
    provenance: str


def _settings_name(value: object, where: str) -> str:
    """The settings file a standalone card names — required, and never read here.

    Required because the answer it belongs to always states
    ``emulator-config-unread``: a caveat that named no file would tell a client
    the switch is unknown and leave it nowhere to go. It is a **name** rather
    than an address, and that is the whole point: where the file lives is
    stated once, in ``atlas/data/emulator_settings.json``, because up to four
    questions of one emulator open the same file and each carrying its own
    copy is how two of them came to disagree in shipped releases.
    """
    return _expect_str(value, where)


def _texture_setting(value: object, where: str) -> TextureSetting:
    """One configuration key a card names — section, key, default, citation."""
    if not isinstance(value, dict) or set(value) != {"section", "key", "default", "citation"}:
        raise ValueError(
            f"{where}: expected exactly section/key/default/citation, got {value!r}"
        )
    return TextureSetting(
        section=_expect_str(value.get("section"), f"{where}.section"),
        key=_expect_str(value.get("key"), f"{where}.key"),
        # The default may legitimately be the empty string — an emulator whose
        # unset key means "nothing configured" — so it is not _expect_str.
        default=(
            value["default"]
            if isinstance(value["default"], str)
            else _expect_str(value["default"], f"{where}.default")
        ),
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _standalone_card(token: str, entry: Any) -> StandaloneTextureCard:
    """One standalone emulator's card — validated, never coerced."""
    where = f"standalone texture card {token!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    textures = entry.get("textures")
    if not isinstance(textures, dict):
        raise ValueError(f"{where}: expected a 'textures' object, got {textures!r}")
    stated_directory = textures.get("directory")
    fixed = textures.get("base") is not None or textures.get("subdir") is not None
    if fixed == (stated_directory is not None):
        raise ValueError(
            f"{where}: state either base+subdir or a 'directory' setting, never both or neither"
        )
    base: str | None = None
    subdir: str | None = None
    if fixed:
        base = _expect_str(textures.get("base"), f"{where}: textures.base")
        if base not in XDG_BASES:
            raise ValueError(
                f"{where}: textures.base must be one of {sorted(XDG_BASES)}, got {base!r}"
            )
        subdir = _expect_subdir(textures.get("subdir"), f"{where}: textures.subdir")
    keying, citation = _keying(textures.get("keying"), f"{where}: textures.keying")
    if entry.get("anchors") is not None:
        expect_table_anchors(
            entry["anchors"],
            where=where,
            vocabulary=recorded_texture_emulator_words(entry),
            binary_required=True,
        )
    switch = textures.get("switch")
    return StandaloneTextureCard(
        token=token,
        base=base,
        subdir=subdir,
        directory=(
            _texture_setting(stated_directory, f"{where}: textures.directory")
            if stated_directory is not None
            else None
        ),
        switch=(
            _texture_setting(switch, f"{where}: textures.switch") if switch is not None else None
        ),
        keying=keying,
        keying_citation=citation,
        settings=_settings_name(textures.get("settings"), f"{where}: textures.settings"),
        provenance=_expect_str(
            entry.get("provenance", {}).get("source"), f"{where}: provenance.source"
        ),
    )


def load_standalone_texture_packs(text: str | None = None) -> tuple[StandaloneTextureCard, ...]:
    """Load the packaged standalone cards (or *text* when supplied, for tests)."""
    raw = json.loads(text if text is not None else _packaged_text())
    if not isinstance(raw, dict) or raw.get("schema") != TEXTURE_PACKS_SCHEMA:
        raise ValueError(
            f"texture_packs: unsupported schema {raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {TEXTURE_PACKS_SCHEMA})"
        )
    return tuple(_standalone_card(token, entry) for token, entry in raw.get("emulators", {}).items())


def _packaged_text() -> str:
    return packaged_text("texture_packs.json")


def load_texture_packs(text: str | None = None) -> tuple[TextureCard, ...]:
    """Load the packaged texture cards (or *text* when supplied, for tests).

    Reading packaged data is not the machine seam — it is the library reading
    its own bundled world knowledge, which is exactly what the cards are.
    """
    raw = json.loads(text if text is not None else _packaged_text())
    if not isinstance(raw, dict) or raw.get("schema") != TEXTURE_PACKS_SCHEMA:
        raise ValueError(
            f"texture_packs: unsupported schema {raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {TEXTURE_PACKS_SCHEMA})"
        )
    return tuple(_texture_card(key, entry) for key, entry in raw.get("cores", {}).items())


_PACKAGED: tuple[TextureCard, ...] | None = None
_PACKAGED_STANDALONE: tuple[StandaloneTextureCard, ...] | None = None


def lookup_texture_card(*, so_basename: str | None, library_name: str | None) -> TextureCard | None:
    """Find the packaged texture card matching a core, by ``.so`` name or ``library_name``."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_texture_packs()
    for card in _PACKAGED:
        if card.matches(so_basename=so_basename, library_name=library_name):
            return card
    return None


def lookup_standalone_texture_card(token: str | None) -> StandaloneTextureCard | None:
    """Find the packaged card for the emulator a ``%EMULATOR_…%`` token names."""
    global _PACKAGED_STANDALONE
    if _PACKAGED_STANDALONE is None:
        _PACKAGED_STANDALONE = load_standalone_texture_packs()
    if token is None:
        return None
    return next((card for card in _PACKAGED_STANDALONE if card.token == token), None)
