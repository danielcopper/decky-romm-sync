"""Rule cards for cores whose save behaviour deviates from the standard rule.

The cards live in ``data/core_oddities.json`` — world knowledge under the
boundary rule: a card states *which* live config governs a core and what its
values mean; the current value is always read from the machine, never from the
card. Cards are keyed by the core's canonical short name (the ``.so`` basename
without ``_libretro.so``), which is also where the ``.so`` name comes from — it
is derived, not restated, so the two cannot disagree. The ``identifiers`` block
carries the display ``library_name`` the binary reports, so lookup works from
either side.

A card states only what no read of the machine recovers. Everything it does
state about a core's own vocabulary is machine-checked where a machine can
check it: the option key, its default and its value set against the
registration the deployed core makes, and the recorded file names and subdir
fragments against the literals in the shipped binary (the ``anchors`` block —
validated here, asserted in ``tests/test_oddities.py``).

Facts in data, interpretation in code: this module only loads and indexes; the
resolver in :mod:`atlas.installations` applies the card.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ._data import packaged_text
from .mode_rules import RULES as MODE_RULES
from .placement import (
    GRANULARITIES,
    GRANULARITY_NONE,
    GRANULARITY_PER_GAME_FILE,
    ROLES,
    ROOT_CONTENT_DIRECTORY,
    ROOT_KINDS,
    ROOT_SAVEFILE_DIRECTORY,
    SUBDIR_TEMPLATE_HOLES,
    TEMPLATE_ROM_STEM,
    TEMPLATE_SAVE_ID,
)

# Packaged-data schema versions. The loaders are strict: unknown schema or
# malformed entries raise instead of coercing — a broken build must fail
# loudly, never resolve wrongly (REVIEW M3, M10).
ODDITIES_SCHEMA = 1
AUDIT_SCHEMA = 3

_KNOWN_VERDICTS = {"card", "standard", "standard-dir", "multi-option", "suspect", "unaudited"}
# The mode a card without a governing option selects. Named here because the
# loader validates against the same spelling the resolver looks up — two
# literals would let a card pass the load and select nothing.
MODE_ALWAYS = "always"
# The roots a mode may anchor at and the granularities it may select are the
# placement's own vocabularies — imported, not respelled here, for the same
# reason the file-name templates are: a card is data, and a value that only
# looks right would be stated as fact.
_KNOWN_MODE_ROOTS = set(ROOT_KINDS)
_KNOWN_GRANULARITIES = set(GRANULARITIES)
_KNOWN_ROLES = set(ROLES)
# A declared file name is a template in the placement's own hole grammar. Only
# these tokens exist: one the resolver fills, one the caller does. A token
# outside the set would travel into a stated filename and be read as literal
# text, so it fails the load instead.
_KNOWN_FILE_TEMPLATES = (TEMPLATE_ROM_STEM, TEMPLATE_SAVE_ID)
# A subdir segment is either a fixed name or exactly one of the placement's
# subdir templates — the same one-grammar rule as file names, plus a whole-
# segment requirement: ``_base_of`` undoes a subdir by counting segments, and
# that arithmetic stays exact only while one template fills to exactly one.
_KNOWN_SUBDIR_TEMPLATES = frozenset(SUBDIR_TEMPLATE_HOLES)
# How a libretro core's ``.so`` is spelled. Derived from the card key rather
# than restated in the card: the key IS that basename, so a second spelling
# could only ever be a way for the two to disagree.
SO_SUFFIX = "_libretro.so"
# What protects one recorded name. Exactly one per entry:
#
# - ``literal`` — the byte string the auditor read in the shipped binary. The
#   name was established from it, so a build that renames it fails the check
#   instead of leaving the card quietly describing a vocabulary that is gone.
# - ``unprotected`` — no literal spells this name (the core assembles it at run
#   time), with the reason. The reason states what does stand behind the name —
#   live observation, or source the byte check cannot reach — and saying so is
#   the point: an unchecked name that looks checked is worse than one marked as
#   what it is.
# - ``arrangement`` — the name is not the core's at all; the arrangement builds
#   this path. Anchoring it to the core binary would check the wrong artefact.
ANCHOR_KINDS = ("literal", "unprotected", "arrangement")


# This check exists verbatim three times, one per packaged-data loader
# (:func:`atlas.evidence._expect_str`, :func:`atlas.systems._expect_str`). The
# triplication is the deliberate cost of keeping the loaders independent of each
# other: each reads its one file and shares no machinery with the other two, so
# a defect in one table can never fail the load of another — and a fidelity
# finding about what counts as a string belongs in all three.
def _expect_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _expect_opt_str(value: object, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{where}: expected a string or null, got {value!r}")
    return value


def _expect_opt_bool(value: object, where: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{where}: expected a boolean or null, got {value!r}")
    return value


def _expect_str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(value)


def _expect_subdir(value: object, where: str) -> str | None:
    """A card's subdir — fixed segments, or exactly one known template per segment.

    The whole-segment rule is load-bearing twice over: ``_base_of`` undoes a
    subdir by counting segments, which stays exact only while one template
    fills to exactly one segment; and an affixed token (``pre<rom_stem>``)
    would be a spelling no read core writes. Any other angle bracket is the
    same typo risk :func:`_expect_file_names` refuses in file names — a token
    that only looks right would be stated as a literal directory name.
    """
    text = _expect_opt_str(value, where)
    if text is None:
        return None
    for segment in text.split("/"):
        if segment in _KNOWN_SUBDIR_TEMPLATES:
            continue
        if "<" in segment or ">" in segment:
            raise ValueError(
                f"{where}: segment {segment!r} — a template must be the whole segment, one of "
                f"{sorted(_KNOWN_SUBDIR_TEMPLATES)}"
            )
    return text


def _expect_file_names(value: object, where: str) -> tuple[str, ...]:
    """A card's file names — templates only in the vocabulary the resolver knows.

    The check is subtractive, not a token scan: the known templates are removed
    and *any* remaining angle bracket fails the load. A scan for well-formed
    ``<…>`` would pass a name whose bracket never closes (``<rom_stem.A1.bin``)
    or nests (``<<rom_stem>>``), and such a name is stated verbatim — the very
    "a typo cannot become a stated filename" guarantee this exists for. A real
    file name with a literal angle bracket would be refused too; none is known,
    and refusing one is the safe direction.
    """
    names = _expect_str_list(value, where)
    if not names:
        raise ValueError(
            f"{where}: an empty list declares nothing — omit the field (or use null) to state that "
            "the file set is not established, which is a different answer than a set with no files"
        )
    for name in names:
        if not name:
            raise ValueError(f"{where}: an empty string is not a file name")
        remainder = name
        for token in _KNOWN_FILE_TEMPLATES:
            remainder = remainder.replace(token, "")
        if "<" in remainder or ">" in remainder:
            raise ValueError(
                f"{where}: file name {name!r} carries an unknown template — only "
                f"{list(_KNOWN_FILE_TEMPLATES)} are filled or carried as holes, and everything else "
                "is stated verbatim as part of the name"
            )
    return names


@dataclass(frozen=True, slots=True)
class SaveGroup:
    """One directory's worth of a mode's save, with what it is and whom it belongs to.

    A mode's save is not always one list of files that share a meaning. MAME
    puts a machine's battery memory under ``nvram/``, its dip switches under
    ``cfg/`` beside an emulator-wide ``default.cfg``, and disk write-differences
    under ``diff/``; FinalBurn Neo writes a per-game ``.fs`` and a shared
    ``shared.memcard`` into one directory. Each group therefore carries its own
    ``subdir``, its own ``granularity`` and a ``role`` saying what kind of data
    it holds — two fields rather than one, because "whose is it" and "what is
    it" are different questions and MAME's two ``.cfg`` files answer them
    differently while sharing a directory.

    The evidence fields are per group for the same reason: a card can have
    read one part of a save to the source and another only far enough to name
    it, and a scope that covers the whole mode would be wrong about one of them.

    ``unnamed`` is the third state a group's file list can be in, and it is not
    the same as the other two. A group with it states a directory this
    configuration writes save data into whose **names do not follow from
    anything atlas reads** — MAME's differencing images for CHD hard disks take
    the disk image's own name out of the machine's ROM table inside the binary.
    Such a group reaches the answer as a :class:`~atlas.placement.FileGroup`
    with ``files=None``, so one walk over ``groups`` covers every place the
    save lives, and the ``file-names-unestablished`` caveat rides beside it
    with this text as the citation — the sentence a person reads and the
    reason behind it. Silence would be worse than either: a backup that skips
    the directory loses the player's progress on every machine with a hard
    disk.

    ``files`` is the declared set, or ``None`` when the card marks it
    unverified — the resolver then refuses to state filenames.
    A name may be a template: ``<rom_stem>`` the resolver fills from the
    content path, ``<save_id>`` it carries through as a hole for the caller.
    ``observe`` optionally widens the *observation* candidates beyond the
    declared defaults (e.g. Flycast's slot-2 VMUs, which exist only when a
    controller port's slot 2 is configured as a VMU). ``complete`` asserts
    that the group's candidate universe is closed — no other file can belong
    to it; a card may claim it only with source-verified provenance.

    ``root`` anchors the group at a *different* root than its mode — Flycast
    keeps the console flash and the unmoved shared cards under the system
    directory's ``dc`` while a per-game mode's own answer lives under the
    save root (issue #97). ``None`` — every group's ordinary state — means
    the mode's root; the loader refuses a restated one, since it could only
    ever disagree. A cross-root group reaches the caller through
    ``file_set.groups`` where the set is declared, and always through the
    ``file-set-spans-roots`` caveat, whose data names the resolved directory
    and the files — the caveat is what survives an observed answer, exactly
    the way ``file-names-unestablished`` carries MAME's unnamed tree.
    """

    subdir: str | None
    files: tuple[str, ...] | None
    granularity: str
    role: str
    observe: tuple[str, ...] | None = None
    complete: bool = False
    files_without_save_id: tuple[str, ...] | None = None
    files_established_for: str | None = None
    files_citation: str | None = None
    unnamed: str | None = None
    root: str | None = None


@dataclass(frozen=True, slots=True)
class SaveMode:
    """One value of the governing option and the behaviour it selects.

    ``groups`` is what the save consists of, one entry per directory-and-meaning
    (see :class:`SaveGroup`); the first is the mode's own state, the one a
    save-syncing client would take if it read nothing else. The fields a mode
    carried before the save could have several parts are still here as the
    first group's, so one spelling stays one spelling — they are read off
    ``groups[0]`` rather than stored twice.

    ``files`` is the declared file set for this mode, or ``None`` when the
    card marks it unverified — the resolver then refuses to state filenames.
    A name may be a template: ``<rom_stem>`` the resolver fills from the
    content path, ``<save_id>`` it carries through as a hole for the caller.
    ``observe`` optionally widens the *observation* candidates beyond the
    declared defaults (e.g. Flycast's slot-2 VMUs, which exist only when a
    controller port's slot 2 is configured as a VMU). ``complete`` asserts
    that the mode's candidate universe is closed — no other file can belong
    to the save; a card may claim it only with source-verified provenance.

    Two fields state what a single file list cannot:

    - ``files_without_save_id`` is the same set as the emulator names it when
      the content carries no platform-native id — Flycast falls back to the
      ROM's own name for arcade content and for a disc whose header states no
      id (``oslib.cpp:44`` vs ``:62``). The set is genuinely conditional on a
      fact atlas does not read, so the resolver states the id-keyed set and
      hands the alternative to the caller in a caveat instead of picking one.
    - ``files_established_for`` names the class of content the list itself was
      established for, and ``files_citation`` cites that. Not every difference
      between content classes is a spelling: Flycast connects four VMUs on a
      Dreamcast and two on a Naomi board, so for arcade content two of the
      four declared names can never exist. The scope travels into the same
      caveat, machine-readably, so the list is never read as established for
      content it was not.
    A group may anchor at a different root than its mode (``SaveGroup.root``)
    — the spanning-save answer that retired the old ``also_under`` field: the
    mode states every part with its files instead of naming a second root it
    cannot list (issue #97).

    ``writes_discarded`` is the third body a mode can have, beside ``groups``
    and ``inside_content``: the configuration keeps no save at all — the
    writes are discarded (hatari with write protection on throws the modified
    image away at eject). Like ``inside_content`` it is required prose that
    replaces the groups; unlike it, the declared emptiness it produces is the
    whole truth, so its granularity is ``"none"`` rather than the content
    file's own per-game grouping.
    """

    root: str
    groups: tuple[SaveGroup, ...]
    inside_content: str | None = None
    writes_discarded: str | None = None

    def __post_init__(self) -> None:
        if self.inside_content is not None and self.writes_discarded is not None:
            raise ValueError(
                "SaveMode: 'inside_content' and 'writes_discarded' contradict each other — one "
                "says the content file keeps the save, the other that nothing keeps it"
            )
        if self.stated is not None:
            self._check_stated_form()
            return
        if not self.groups:
            raise ValueError("SaveMode: a mode states at least one group")
        named = [group for group in self.groups if group.unnamed is None]
        # A mode whose every group is unnamed is a real statement, not an
        # empty one: it names the directory the save lives in and says why no
        # file name follows from anything atlas reads (ScummVM's slot files
        # are named per engine from the launcher target). Its declared set is
        # the empty set of *statable* names, never a claim of completeness.
        here = (
            [group for group in named if group.root is None and group.subdir == named[0].subdir]
            if named
            else []
        )
        if here and len({group.files is None for group in here}) != 1:
            # The directory's answer is the groups in it taken together, so one
            # unverified part would silently shorten a list stated as the whole.
            raise ValueError(
                "SaveMode: groups sharing a directory either all declare files or none do"
            )
        if sum(1 for group in here if group.files_without_save_id or group.files_established_for) > 1:
            raise ValueError(
                "SaveMode: two groups in one directory both scope their file list — the mode "
                "cannot say which scope its answer carries"
            )

    def _check_stated_form(self) -> None:
        """The groups-less forms: no separate save file exists — the loaded
        content file takes the writes, or nothing does. There is nothing to
        group — the statement replaces the groups, and the reason is required
        prose because an empty one would reach the caller as silence."""
        statement = self.stated or ""
        field = "inside_content" if self.inside_content is not None else "writes_discarded"
        if self.groups:
            raise ValueError(
                f"SaveMode: a mode stating '{field}' declares no groups — there is no "
                "separate file to group"
            )
        if not statement.strip():
            raise ValueError(f"SaveMode: '{field}' states a reason, not an empty string")

    @property
    def primary(self) -> SaveGroup:
        """The group the mode's own answer is about — the card states it first.

        Where every group is unnamed there is no named first group, and the
        first unnamed one carries the mode's directory and granularity just
        the same — it is a group like any other, only its file names are not
        derivable.
        """
        return self.named[0] if self.named else self.groups[0]

    # The groups-less forms answer the group-derived questions themselves:
    # with the save inside the content the medium is the content file, so the
    # declared set of *separate* files is empty and closed and the unit is per
    # game by the medium's own nature; with the writes discarded nothing keeps
    # any save at all, which is an empty closed set with no unit to state.
    # Either way there is neither a subdir to join nor a name to watch for.

    @property
    def stated(self) -> str | None:
        """The groups-less statement this mode carries, whichever form it is."""
        return self.inside_content if self.inside_content is not None else self.writes_discarded

    @property
    def named(self) -> tuple[SaveGroup, ...]:
        """The groups that state files. The rest name a directory and say why not."""
        return tuple(group for group in self.groups if group.unnamed is None)

    @property
    def unnamed(self) -> tuple[SaveGroup, ...]:
        """Directories this mode writes into whose names atlas cannot derive."""
        return tuple(group for group in self.groups if group.unnamed is not None)

    @property
    def here(self) -> tuple[SaveGroup, ...]:
        """Every group that lands in the mode's own directory.

        A group is a directory *and* a meaning, so one directory can hold
        several: Flycast's shared mode keeps four memory cards and the console's
        own flash side by side under ``dc/``. The fields below answer for that
        directory — which is what ``dir`` and ``file_set.files`` have always
        been about — so splitting one list into two by role does not move a
        single name out of an answer.
        """
        return tuple(
            group
            for group in self.named
            if group.root is None and group.subdir == self.primary.subdir
        )

    @property
    def subdir(self) -> str | None:
        if self.stated is not None:
            return None
        return self.primary.subdir

    @property
    def files(self) -> tuple[str, ...] | None:
        if self.stated is not None:
            return ()
        if not self.named:
            # Every group is unnamed: the set of statable names is empty, and
            # the groups (files=None) plus their caveat carry what that means
            # — files exist here, their names do not follow from any read.
            return ()
        if self.primary.files is None:
            return None
        return tuple(name for group in self.here for name in group.files or ())

    @property
    def granularity(self) -> str:
        if self.inside_content is not None:
            return GRANULARITY_PER_GAME_FILE
        if self.writes_discarded is not None:
            return GRANULARITY_NONE
        return self.primary.granularity

    @property
    def granularities(self) -> tuple[str, ...]:
        """Every distinct grouping among the mode's groups, the mode's own first.

        One entry for most modes; the honest plural for a mixed one, whose
        secondary groups group differently than its answer's headline. The
        unnamed groups count — a directory whose names are not derivable
        still has a grouping — and the groups-less forms answer with their
        single derived value.
        """
        if self.stated is not None:
            return (self.granularity,)
        return tuple(dict.fromkeys([self.granularity, *(g.granularity for g in self.groups)]))

    @property
    def observe(self) -> tuple[str, ...] | None:
        if self.stated is not None:
            return None
        if all(group.observe is None for group in self.here):
            return None
        return tuple(
            name
            for group in self.here
            for name in (group.observe if group.observe is not None else group.files or ())
        )

    @property
    def complete(self) -> bool:
        if self.stated is not None:
            # No separate save file can belong to this mode — that is the
            # statement itself, and the source read behind the card is what
            # licenses the claim.
            return True
        if not self.named:
            # The names exist and are not derivable — the one thing this mode
            # can never claim is that its (empty) statable set is the whole.
            return False
        return all(group.complete for group in self.here)

    @property
    def files_without_save_id(self) -> tuple[str, ...] | None:
        return next((g.files_without_save_id for g in self.here if g.files_without_save_id), None)

    @property
    def files_established_for(self) -> str | None:
        return next((g.files_established_for for g in self.here if g.files_established_for), None)

    @property
    def files_citation(self) -> str | None:
        return next((g.files_citation for g in self.here if g.files_citation), None)


@dataclass(frozen=True, slots=True)
class RetiredOption:
    """One spelling this card's generation retired: the key and the evidence.

    A retired key is an options-file entry the shipped core no longer reads —
    an older generation wrote it, the rename or split left it behind, and the
    value someone set there silently stopped applying (issue #79). The
    citation carries the proof, which is a *negative* binary fact: the key is
    absent from the shipped ``.so`` while its replacement is a whole literal.
    That is also why these keys are deliberately outside
    :func:`recorded_vocabulary`: an anchor demands a literal the binary must
    carry, and a retired key's whole point is that it must not.
    """

    key: str
    citation: str


@dataclass(frozen=True, slots=True)
class CoreCard:
    """A core's save rule card: identifiers, what selects a mode, modes, provenance.

    Three ways a mode gets selected, one per card: a governing *option*
    (``option_key``, its live value is the mode key), a governing *rule*
    (``rule_options`` is not ``None`` — a per-core function in
    :mod:`atlas.mode_rules`, keyed by the card key, reads the named options
    and whatever else it declared and returns a freely named mode), or
    nothing (the one ``always`` mode). The card stays what it always was —
    what *can* exist — and the rule is what decides what holds here: several
    interacting options are a product no single option's value can name, so
    the format grows code plus a card referencing it, not a DSL (issue #163).
    """

    key: str
    library_names: tuple[str, ...]
    option_key: str | None
    option_default: str | None
    modes: Mapping[str, SaveMode]
    provenance: str
    rule_options: tuple[str, ...] | None = None
    retired_options: tuple[RetiredOption, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "modes", MappingProxyType(dict(self.modes)))

    @property
    def so_name(self) -> str:
        """The ``.so`` basename this card describes — the key plus the suffix."""
        return f"{self.key}{SO_SUFFIX}"

    def matches(self, *, so_basename: str | None, library_name: str | None) -> bool:
        if so_basename is not None and so_basename == self.so_name:
            return True
        return library_name is not None and library_name in self.library_names


def _stated_mode(mode: Any, *, root: str, field: str, reason: str, where: str) -> SaveMode:
    """The groups-less forms: the content file takes the writes, or nothing does.

    Both replace the groups with required prose, so one loader carries them
    and the field name says which form it is building. The inside-content
    statement is about the loaded content file and anchors at its tree;
    the discarded statement may also anchor at the frontend's save root —
    where the save *would* have gone — because a core can keep nothing
    without the content being involved at all (SwanStation with both
    memory-card slots set to none).
    """
    allowed = (
        (ROOT_CONTENT_DIRECTORY,)
        if field == "inside_content"
        else (ROOT_CONTENT_DIRECTORY, ROOT_SAVEFILE_DIRECTORY)
    )
    if root not in allowed:
        raise ValueError(
            f"{where}: a mode stating '{field}' anchors where the writes would have landed — "
            f"root must be one of {sorted(allowed)}, got {root!r}"
        )
    if mode.get("groups") is not None:
        raise ValueError(
            f"{where}: '{field}' replaces 'groups' — there is no separate file to group"
        )
    if field == "inside_content":
        return SaveMode(root=root, groups=(), inside_content=reason)
    return SaveMode(root=root, groups=(), writes_discarded=reason)


def _save_mode(mode: Any, where: str) -> SaveMode:
    """One entry of a card's ``modes`` block — validated, never coerced."""
    if "also_under" in mode:
        # The field retired with issue #97: a spanning mode states each part
        # as a group anchored at its own root, files included. A card still
        # spelling it would silently lose the claim it thinks it makes.
        raise ValueError(
            f"{where}: 'also_under' is retired — state the part under the second root as a "
            "group with its own 'root' and its files"
        )
    root = _expect_str(mode.get("root"), f"{where}: root")
    if root not in _KNOWN_MODE_ROOTS:
        raise ValueError(f"{where}: root must be one of {sorted(_KNOWN_MODE_ROOTS)}, got {root!r}")
    inside_content = _expect_opt_str(mode.get("inside_content"), f"{where}: inside_content")
    writes_discarded = _expect_opt_str(mode.get("writes_discarded"), f"{where}: writes_discarded")
    if inside_content is not None and writes_discarded is not None:
        raise ValueError(
            f"{where}: 'inside_content' and 'writes_discarded' contradict each other — one says "
            "the content file keeps the save, the other that nothing keeps it"
        )
    if inside_content is not None:
        return _stated_mode(mode, root=root, field="inside_content", reason=inside_content, where=where)
    if writes_discarded is not None:
        return _stated_mode(
            mode, root=root, field="writes_discarded", reason=writes_discarded, where=where
        )
    raw_groups = mode.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError(f"{where}: 'groups' must be a non-empty list — a mode has at least one part")
    groups = tuple(
        _save_group(group, f"{where}: groups[{index}]") for index, group in enumerate(raw_groups)
    )
    for index, group in enumerate(groups):
        _check_group_root(group, index=index, mode_root=root, where=where)
    if any(
        (group.root or root) != ROOT_SAVEFILE_DIRECTORY and segment in _KNOWN_SUBDIR_TEMPLATES
        for group in groups
        for segment in (group.subdir or "").split("/")
    ):
        # The two templated behaviours read so far both key a directory the
        # frontend's *save* root hands the core. A template under another root
        # would state a content-keyed system or content directory no reading
        # has established — refuse it until a core shows that behaviour.
        raise ValueError(
            f"{where}: a subdir template is established only under {ROOT_SAVEFILE_DIRECTORY!r} — "
            "no read core keys another root's subdirectory on the content"
        )
    return SaveMode(root=root, groups=groups)


# The cross-root shapes one reading has established (issue #97): a mode whose
# answer lives under the frontend's save root can keep parts behind under the
# system directory (Flycast's console flash and unmoved shared cards) or the
# content's tree. Everything else is refused until a core shows the behaviour
# — the same lets-see-a-customer rule the subdir templates follow.
_KNOWN_GROUP_ROOTS = frozenset({"system_directory", "content_directory"})


def _check_group_root(group: SaveGroup, *, index: int, mode_root: str, where: str) -> None:
    """What a cross-root group may be — narrow on purpose, like every first shape.

    The refused fields are not second-class: each one's machinery answers for
    the mode's own directory (observation candidates, the per-directory scope
    rules, the unnamed tree's caveat arithmetic), and a cross-root group is
    deliberately outside all of it until a core needs otherwise. Its files
    are required for the same reason the old ``also_under`` never carried
    any: a second root with an unstatable list is exactly the claim that
    field made, and it retired because Flycast's list turned out statable.
    """
    if group.root is None:
        return
    at = f"{where}: groups[{index}]"
    if index == 0:
        raise ValueError(
            f"{at}: the first group is the mode's own answer — it cannot anchor at another root"
        )
    if group.root == mode_root:
        raise ValueError(
            f"{at}: 'root' restates the mode's own ({mode_root!r}) — omit it; a restated one "
            "could only ever disagree"
        )
    if mode_root != ROOT_SAVEFILE_DIRECTORY or group.root not in _KNOWN_GROUP_ROOTS:
        raise ValueError(
            f"{at}: a cross-root group is established only from a {ROOT_SAVEFILE_DIRECTORY!r} "
            f"mode into {sorted(_KNOWN_GROUP_ROOTS)} — no read core shows {mode_root!r} -> "
            f"{group.root!r}"
        )
    if group.files is None:
        raise ValueError(
            f"{at}: a cross-root group states its files — a second root with an unstatable "
            "list was the retired 'also_under' claim, and no read core needs it"
        )
    if group.unnamed is not None or group.observe is not None:
        raise ValueError(
            f"{at}: 'unnamed' and 'observe' answer for the mode's own directory — neither is "
            "established on a cross-root group"
        )
    if group.files_without_save_id is not None or group.files_established_for is not None:
        raise ValueError(
            f"{at}: the file-list scopes answer for the mode's own directory — none is "
            "established on a cross-root group"
        )


def _id_less_alternative(alternative: Any, files: Any, where: str) -> tuple[str, ...] | None:
    """The same set spelled for content that carries no platform-native id."""
    if alternative is None:
        return None
    if files is None or not any(TEMPLATE_SAVE_ID in name for name in files):
        raise ValueError(
            f"{where}: 'files_without_save_id' is the set for content that carries no id, so "
            f"'files' must declare the {TEMPLATE_SAVE_ID} case it is the alternative to"
        )
    names = _expect_file_names(alternative, f"{where}: files_without_save_id")
    if any(TEMPLATE_SAVE_ID in name for name in names):
        raise ValueError(
            f"{where}: 'files_without_save_id' describes content without an id — it cannot "
            f"name one with {TEMPLATE_SAVE_ID}"
        )
    return names


def _file_list_scope(mode: Any, files: Any, where: str) -> tuple[str | None, str | None]:
    """The content class a file list was established for, and the source for it.

    Both are answer content rather than flags: an empty one would reach the
    caller as an empty scope or an empty citation, which says nothing at all.
    """
    raw_scope = mode.get("files_established_for")
    established_for = (
        _expect_str(raw_scope, f"{where}: files_established_for") if raw_scope is not None else None
    )
    raw_citation = mode.get("files_citation")
    citation = _expect_str(raw_citation, f"{where}: files_citation") if raw_citation is not None else None
    if established_for is not None and files is None:
        raise ValueError(
            f"{where}: 'files_established_for' scopes a declared set — a mode that states no 'files' "
            "has nothing to scope"
        )
    if citation is not None and established_for is None:
        raise ValueError(
            f"{where}: 'files_citation' cites the scope in 'files_established_for', which this mode "
            "does not state"
        )
    return established_for, citation


def _save_group(mode: Any, where: str) -> SaveGroup:
    """One group of a mode — its directory, its files, and what they are."""
    if not isinstance(mode, dict):
        raise ValueError(f"{where}: a group must be an object")
    granularity = _expect_str(mode.get("granularity"), f"{where}: granularity")
    if granularity not in _KNOWN_GRANULARITIES:
        # It reaches the caller as the contractual Granularity.value, so a
        # misspelling here would be stated as this machine's actual grouping.
        raise ValueError(
            f"{where}: granularity must be one of {sorted(_KNOWN_GRANULARITIES)}, got {granularity!r}"
        )
    role = _expect_str(mode.get("role"), f"{where}: role")
    if role not in _KNOWN_ROLES:
        # It reaches the caller as the word a client filters a save sync on, so
        # a misspelling here would drop real save data or sync a settings file.
        raise ValueError(f"{where}: role must be one of {sorted(_KNOWN_ROLES)}, got {role!r}")
    files = mode.get("files")
    observe = mode.get("observe")
    complete = mode.get("complete", False)
    if not isinstance(complete, bool):
        # bool("false") is True in Python — never coerce this claim.
        raise ValueError(f"{where}: 'complete' must be a JSON boolean")
    alternative_names = _id_less_alternative(mode.get("files_without_save_id"), files, where)
    established_for, citation = _file_list_scope(mode, files, where)
    raw_unnamed = mode.get("unnamed")
    unnamed = _expect_str(raw_unnamed, f"{where}: unnamed") if raw_unnamed is not None else None
    if unnamed is not None and files is not None:
        # The field says the names cannot be derived at all. A list beside it
        # would be the card contradicting its own reason for existing.
        raise ValueError(
            f"{where}: a group with 'unnamed' states no 'files' — the field exists because the "
            "names do not follow from anything atlas reads"
        )
    group_root = _expect_opt_str(mode.get("root"), f"{where}: root")
    if group_root is not None and group_root not in _KNOWN_MODE_ROOTS:
        raise ValueError(
            f"{where}: root must be one of {sorted(_KNOWN_MODE_ROOTS)}, got {group_root!r}"
        )
    return SaveGroup(
        root=group_root,
        subdir=_expect_subdir(mode.get("subdir"), f"{where}: subdir"),
        files=_expect_file_names(files, f"{where}: files") if files is not None else None,
        granularity=granularity,
        role=role,
        observe=_expect_file_names(observe, f"{where}: observe") if observe is not None else None,
        complete=complete,
        files_without_save_id=alternative_names,
        files_established_for=established_for,
        files_citation=citation,
        unnamed=unnamed,
    )


def recorded_vocabulary(
    *,
    option_key: str | None,
    modes: Mapping[str, SaveMode],
    rule_options: tuple[str, ...] = (),
) -> frozenset[str]:
    """Every word a card states as this core's own: option keys, subdirs, file names.

    These are the names a caller reads back as fact, so they are the names the
    anchor tripwire covers. A subdir contributes one item per path segment,
    because that is the granularity the binary spells them at (``opera`` and
    ``per_game`` are two literals, not one). A rule card's option keys are
    recorded words exactly like a governing option's key is.

    Mode *keys* are deliberately absent. For an option-governed card they are
    the option's own values and the deployed core registers them — a
    measurement beats an anchor, and ``tests/test_oddities.py`` makes it. For
    a rule card they are atlas's own vocabulary, chosen here the way caveat
    codes are — nothing in a binary spells them, so there is nothing to pin.
    """
    words: set[str] = set()
    if option_key is not None:
        words.add(option_key)
    words.update(rule_options)
    for mode in modes.values():
        for group in mode.groups:
            if group.subdir is not None:
                words.update(segment for segment in group.subdir.split("/") if segment)
            for names in (group.files, group.observe, group.files_without_save_id):
                words.update(names or ())
    return frozenset(words)


def _expect_anchors(value: object, *, where: str, vocabulary: frozenset[str]) -> None:
    """Validate a card's ``anchors`` block — shape here, bytes in the tests.

    What the loader can decide without a machine it decides: an entry names
    exactly one of :data:`ANCHOR_KINDS`, carries a non-empty string, and covers
    a name this card actually records. Whether a ``literal`` really is a whole
    NUL-delimited literal in the deployed ``.so`` needs the binary, so it is a
    test; and whether every recorded name has an entry is a claim about the
    *shipped* cards, which is a test too — a synthetic card built inside a test
    for some other purpose would otherwise have to carry anchors to load at all.

    The block is validated and then dropped. Anchors are audit machinery, not
    an answer: nothing that reaches a caller holds them, so nothing can leak
    them (``provenance.status`` is kept out of :class:`CoreCard` the same way).
    """
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object of recorded name -> anchor, got {value!r}")
    for name, anchor in value.items():
        at = f"{where}[{name!r}]"
        if name not in vocabulary:
            raise ValueError(
                f"{at}: anchors a name this card does not record — an anchor for nothing outlives "
                "the name it was written for and silently protects the next typo instead"
            )
        if not isinstance(anchor, dict) or len(anchor) != 1:
            raise ValueError(
                f"{at}: expected an object naming exactly one of {list(ANCHOR_KINDS)}, got {anchor!r} "
                "— a name is protected one way, and two ways at once say neither"
            )
        ((kind, detail),) = anchor.items()
        if kind not in ANCHOR_KINDS:
            raise ValueError(f"{at}: unknown anchor kind {kind!r}, expected one of {list(ANCHOR_KINDS)}")
        # An empty literal matches every binary and an empty reason states no
        # reason — both are the opt-out this block exists to make impossible.
        _expect_str(detail, f"{at}.{kind}")


def _expect_selectable_modes(
    where: str,
    *,
    option_key: str | None,
    rule_options: tuple[str, ...] | None,
    modes: Mapping[str, SaveMode],
) -> None:
    """A card without a governing option or rule states exactly the ``always`` mode.

    Nothing selects between modes when neither an option nor a rule governs
    the card, so the resolver takes ``always`` and only ``always``. A card
    that names its one mode anything else, or names several, therefore
    describes behaviour that can never be applied: the answer comes back with
    no rule card behind it and no caveat either, because from the resolver's
    side nothing went wrong. The card is shipped with the code, so that is a
    build mistake, not a state of the machine — it fails the load. A rule
    card's mode names are the rule's to select, so they are free — the rule
    is code shipped beside the card, and returning a name the card does not
    state fails at apply time as the build mistake it is.
    """
    if option_key is not None or rule_options is not None or set(modes) == {MODE_ALWAYS}:
        return
    raise ValueError(
        f"{where}: a card with no governing_option.key and no governing_rule selects nothing, so "
        f"it must declare exactly the {MODE_ALWAYS!r} mode — got {sorted(modes) or 'no modes at all'}"
    )


def _retired_options(
    value: object, where: str, *, option_key: str | None, rule_options: tuple[str, ...] | None
) -> tuple[RetiredOption, ...]:
    """A card's ``retired_options`` block — the spellings its generation left behind.

    Every entry needs its citation: retirement is a claim about the shipped
    binary (the key is absent while its replacement is a literal), and a
    claim without its evidence is the guessing this format exists to refuse.
    A retired key colliding with a current one is a contradiction — one key
    cannot be both read and not read by the same generation. And a card that
    reads no options at all (the ``always`` shape, or a rule declaring none)
    never opens an options file, so retired knowledge on it would never be
    checked — stated as a load error rather than shipping a dead promise.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ValueError(f"{where}: retired_options must be a non-empty list, got {value!r}")
    current = {option_key, *(rule_options or ())} - {None}
    if not current:
        raise ValueError(
            f"{where}: retired_options on a card that reads no options — no read would ever "
            "check them, so the statement could never fire"
        )
    retired: list[RetiredOption] = []
    for i, entry in enumerate(value):
        at = f"{where}: retired_options[{i}]"
        if not isinstance(entry, dict) or set(entry) != {"key", "citation"}:
            raise ValueError(f"{at}: an entry names exactly 'key' and 'citation', got {entry!r}")
        key = _expect_str(entry["key"], f"{at}: key")
        if key in current:
            raise ValueError(
                f"{at}: {key!r} is a key this card's generation reads — retired and current "
                "at once is a contradiction"
            )
        if any(r.key == key for r in retired):
            raise ValueError(f"{at}: {key!r} is recorded twice")
        retired.append(RetiredOption(key=key, citation=_expect_str(entry["citation"], f"{at}: citation")))
    return tuple(retired)


def _governing_rule(
    value: Any, where: str, *, card_key: str, option_key: str | None
) -> tuple[str, ...] | None:
    """A card's ``governing_rule`` block: the options its selection rule reads.

    The list may be empty — ScummVM's rule reads a file of the emulator's own
    and no core option at all — but the block itself must name a rule that
    exists: the card is data shipped beside the code, so a marker with no
    function behind it is a build mistake and fails the load. The mirror
    claim (a rule with no card) is a test, because the loader validating one
    card cannot see which rules the others claimed.
    """
    if value is None:
        return None
    if option_key is not None:
        raise ValueError(
            f"{where}: 'governing_option' and 'governing_rule' are two answers to who selects "
            "the mode — a card states one of them"
        )
    if not isinstance(value, dict) or set(value) != {"options"}:
        raise ValueError(
            f"{where}: governing_rule must be an object naming exactly 'options', got {value!r}"
        )
    options = _expect_str_list(value["options"], f"{where}: governing_rule.options")
    if card_key not in MODE_RULES:
        raise ValueError(
            f"{where}: states a governing_rule and no selection rule is registered under "
            f"{card_key!r} in atlas.mode_rules — the marker would select nothing"
        )
    return options


def load_oddities(text: str | None = None) -> tuple[CoreCard, ...]:
    """Load the packaged rule cards (or *text* when supplied, for tests).

    Reading packaged data is not the machine seam — it is the library reading
    its own bundled world knowledge, which is exactly what the cards are.
    """
    if text is None:
        text = packaged_text("core_oddities.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != ODDITIES_SCHEMA:
        raise ValueError(
            f"core_oddities: unsupported schema {raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {ODDITIES_SCHEMA})"
        )
    cards: list[CoreCard] = []
    for key, entry in raw.get("cores", {}).items():
        where = f"card {key!r}"
        identifiers = entry.get("identifiers", {})
        saves = entry.get("saves", {})
        governing = saves.get("governing_option") or {}
        modes: dict[str, SaveMode] = {
            value: _save_mode(mode, f"{where} mode {value!r}")
            for value, mode in saves.get("modes", {}).items()
        }
        provenance = entry.get("provenance", {})
        option_key = _expect_opt_str(governing.get("key"), f"{where}: governing_option.key")
        rule_options = _governing_rule(
            saves.get("governing_rule"), where, card_key=key, option_key=option_key
        )
        retired_options = _retired_options(
            saves.get("retired_options"), where, option_key=option_key, rule_options=rule_options
        )
        _expect_selectable_modes(where, option_key=option_key, rule_options=rule_options, modes=modes)
        if "so" in identifiers:
            raise ValueError(
                f"{where}: identifiers.so is derived from the card key ({key + SO_SUFFIX!r}) and no "
                "longer read — a restated one could only ever disagree with it"
            )
        anchors = saves.get("anchors")
        if anchors is not None:
            _expect_anchors(
                anchors,
                where=f"{where}: saves.anchors",
                vocabulary=recorded_vocabulary(
                    option_key=option_key, modes=modes, rule_options=rule_options or ()
                ),
            )
        cards.append(
            CoreCard(
                key=key,
                library_names=_expect_str_list(
                    identifiers.get("library_name", []), f"{where}: identifiers.library_name"
                ),
                option_key=option_key,
                option_default=_expect_opt_str(governing.get("default"), f"{where}: governing_option.default"),
                modes=modes,
                provenance=provenance.get("source", "unstated"),
                rule_options=rule_options,
                retired_options=retired_options,
            )
        )
    return tuple(cards)


_PACKAGED: tuple[CoreCard, ...] | None = None


def lookup_card(*, so_basename: str | None, library_name: str | None) -> CoreCard | None:
    """Find the packaged rule card matching a core, by ``.so`` name or ``library_name``."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_oddities()
    for card in _PACKAGED:
        if card.matches(so_basename=so_basename, library_name=library_name):
            return card
    return None


@dataclass(frozen=True, slots=True)
class VerifiedOn:
    """What one arrangement's verification pinned: arrangement + core versions."""

    version: str | None
    core_library_version: str | None
    date: str | None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One core's audit verdict, capability summary, and verification record.

    ``save_options`` names the core options the audit found governing the save
    file set — world knowledge with the same provenance as ``note``, not a live
    read. It belongs to the ``multi-option`` verdict and only to it: that
    verdict *means* "the granularity depends on several interacting options the
    card schema cannot express", so an entry that cannot name them has not
    earned it, and any other verdict naming them would be stating a dependency
    it just denied.
    """

    key: str
    verdict: str
    per_game_capable: bool | None
    note: str
    verified: Mapping[str, VerifiedOn | None]
    save_options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "verified", MappingProxyType(dict(self.verified)))


def _verified_on(rec: Any, where: str) -> VerifiedOn | None:
    """One arrangement's verification record — ``None`` stays *never verified*.

    A record that is present must pin the arrangement ``version``. That is the
    field the drift check hangs on: with it null, no machine can ever disagree
    with the record, so the entry would read as verified everywhere and forever
    while pinning nothing at all — the one shape that is worse than *never
    verified*, because it claims the opposite. The core's version may stay null;
    plenty of cores report none, and the arrangement version still bounds what
    was checked.
    """
    if rec is None:
        return None
    return VerifiedOn(
        version=_expect_str(rec.get("version"), f"{where}.version"),
        core_library_version=_expect_opt_str(rec.get("core_library_version"), f"{where}.core_library_version"),
        date=_expect_opt_str(rec.get("date"), f"{where}.date"),
    )


def _audit_entry(key: str, entry: Any) -> AuditEntry:
    """One core's audit entry — verdict, capability, and what it was verified on."""
    where = f"audit {key!r}"
    verdict = _expect_str(entry.get("verdict"), f"{where}: verdict")
    if verdict not in _KNOWN_VERDICTS:
        raise ValueError(f"{where}: verdict must be one of {sorted(_KNOWN_VERDICTS)}, got {verdict!r}")
    if "per_game_capable" not in entry:
        raise ValueError(f"{where}: missing required field 'per_game_capable'")
    per_game_capable = _expect_opt_bool(entry["per_game_capable"], f"{where}: per_game_capable")
    note = _expect_str(entry.get("note"), f"{where}: note")
    save_options = _expect_str_list(entry.get("save_options", []), f"{where}: save_options")
    if verdict == "multi-option" and not save_options:
        raise ValueError(
            f"{where}: a 'multi-option' verdict must list the governing options in 'save_options' — "
            "the verdict states the granularity depends on them"
        )
    if verdict != "multi-option" and save_options:
        raise ValueError(f"{where}: 'save_options' belongs to a 'multi-option' verdict, got {verdict!r}")
    verified: dict[str, VerifiedOn | None] = {
        arrangement: _verified_on(rec, f"{where}: verified[{arrangement!r}]")
        for arrangement, rec in entry.get("verified", {}).items()
    }
    return AuditEntry(
        key=key,
        verdict=verdict,
        per_game_capable=per_game_capable,
        note=note,
        verified=verified,
        save_options=save_options,
    )


def load_audit(text: str | None = None) -> dict[str, AuditEntry]:
    """Load the packaged verification matrix (``data/core_audit.json``)."""
    if text is None:
        text = packaged_text("core_audit.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != AUDIT_SCHEMA:
        raise ValueError(
            f"core_audit: unsupported schema {raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {AUDIT_SCHEMA})"
        )
    return {key: _audit_entry(key, entry) for key, entry in raw.get("cores", {}).items()}


_AUDIT: dict[str, AuditEntry] | None = None


def lookup_audit(key: str) -> AuditEntry | None:
    """Find the packaged audit entry for a card key."""
    global _AUDIT
    if _AUDIT is None:
        _AUDIT = load_audit()
    return _AUDIT.get(key)
