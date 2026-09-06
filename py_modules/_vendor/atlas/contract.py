"""The canonical serialization of atlas results — the portable contract.

One JSON-shaped form per result type, used by the conformance vectors
(``vectors/``, schema 2) and available to consumers who want answers as plain
data. The rule for what serializes:

- **Structured fields are contractual**: directories, root kinds, holes,
  file-set state/files/completeness, granularity values and option identity,
  caveat codes and caveat data, installation identity, and health findings —
  which are caveats and serialize as caveats, code and data alike. Vectors
  assert them with exact equality; ports must reproduce them.
- **Prose is not**: ``sources``, caveat ``message``, ``option_provenance``,
  ``FileSet.provenance`` and ``EmulatorSpec.provenance`` are human-readable
  explanations and may change freely — they are deliberately absent here. Two
  words, two scopes: ``sources`` is what a whole answer was read from,
  ``*provenance`` is where one field's own value came from.
  ``FirmwareRequirement.system_source`` is neither and keeps its name: it is a
  closed vocabulary naming *which rule* assigned the system (an override, the
  core's ``systemname``, a slug, or nothing), so it is data a client branches
  on and it serializes.
- **A distribution's ``label`` is a third thing**: it serializes, and a port
  must reproduce it, because it is packaged and versioned world knowledge
  rather than a sentence someone wrote (:mod:`atlas.distribution_labels`) — and
  it is still the one serialized field a consumer must never branch on. It is
  how a project spells its own name, beside the identifier that stays the key.

A port that reproduces these dicts for every vector reads the machine the way
the reference does.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence, TypeVar

from .distribution_labels import distribution_label
from .every_installation import InstallationAnswer
from .firmware import (
    FirmwareAlternatives,
    FirmwareAnswer,
    FirmwareIdentification,
    FirmwareIdentity,
    FirmwareRequirement,
    SuppliedBy,
)
from .installations import (
    CatalogueAnswer,
    EmulatorEntry,
    Health,
    Installation,
    LaunchabilityAnswer,
    PlatformSystemsAnswer,
    RomPlacement,
    SystemPlatformsAnswer,
    SystemsAnswer,
)
from .platforms import PlatformIdentities
from .placement import (
    Caveat,
    ModPlacement,
    SavefilePlacement,
    SavestateAbsence,
    SavestatePlacement,
    ScreenshotPlacement,
    SoftPatchAnswer,
    TexturePlacement,
    Unresolved,
)

AnswerT = TypeVar("AnswerT")


def data_contract(data: Mapping[str, "str | Sequence[str] | Mapping[str, str]"]) -> dict[str, Any]:
    """The one rule for serializing a ``data`` block, caveat or refusal alike.

    Three shapes, one each: a string stays a string, a sequence of strings
    becomes a JSON array in the emitter's own order, and a mapping — a tally,
    at the two keys the guide documents — becomes a plain object. Written once
    because both callers below and the generated contract reference must agree
    literally — a second copy is a second answer waiting to happen.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            out[key] = value
        elif isinstance(value, Mapping):
            out[key] = dict(value)
        else:
            out[key] = list(value)
    return out


def _caveats_contract(caveats: Sequence[Caveat]) -> list[dict[str, Any]]:
    """One ``{code, data}`` per caveat, in order — the one serialization of a caveat."""
    return [{"code": c.code, "data": data_contract(c.data)} for c in caveats]


def _placement_core(placement: SavefilePlacement | SavestatePlacement) -> dict[str, Any]:
    """The fields both placement questions answer, in one shape.

    Shared rather than written twice, because the two answers really are the
    same answer about different data: one upstream function resolves both
    (``runloop.c:8752-8979``). What is *not* shared is exactly the field that
    only one of them has — see :func:`savestate_placement_contract`.
    """
    return {
        "dir": placement.dir,
        "root_kind": placement.root_kind,
        "needs": list(placement.needs),
        "fallback_dir": placement.fallback_dir,
        "physical_dir": placement.physical_dir,
        "file_set": {
            "state": placement.file_set.state,
            "files": list(placement.file_set.files),
            "complete": placement.file_set.complete,
            "groups": [
                {
                    "dir": group.dir,
                    "files": None if group.files is None else list(group.files),
                    "granularity": group.granularity,
                    "role": group.role,
                }
                for group in placement.file_set.groups
            ],
        },
        "caveats": _caveats_contract(placement.caveats),
    }


def savefile_placement_contract(placement: SavefilePlacement) -> dict[str, Any]:
    """The stable, JSON-shaped form of a :class:`~atlas.placement.SavefilePlacement`.

    The granularity block is plural on purpose: ``readings`` is one entry per
    switch that went into selecting the mode (its provenance prose stays out,
    like all provenance), and each ``alternatives`` entry names another mode
    with the full option combination that selects it — a client renders
    "is the wanted option active, and in which file does it change" from the
    readings, for one switch or several alike.
    """
    granularity = placement.granularity
    return {
        **_placement_core(placement),
        "granularity": None
        if granularity is None
        else {
            "value": granularity.value,
            "mode": granularity.mode,
            "readings": [
                {"key": r.key, "value": r.value, "options_file": r.options_file}
                for r in granularity.readings
            ],
            "alternatives": [
                {"mode": a.mode, "options": dict(a.options), "values": list(a.values)}
                for a in granularity.alternatives
            ],
        },
    }


def savestate_placement_contract(placement: SavestatePlacement) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.placement.SavestatePlacement`.

    :func:`savefile_placement_contract` without ``granularity``, and the omission is the
    contract rather than an oversight: granularity is a rule card's word about
    how a *core* groups the data it writes, and no core writes a savestate —
    RetroArch serializes it and the libretro API never tells the core where it
    goes. A ``"granularity": null`` here would be a field no answer could ever
    fill, which a client would rightly read as "not established yet".

    ``root_kind`` speaks its own closed vocabulary
    (:data:`~atlas.placement.STATE_ROOT_KINDS`) for the same reason: a savestate
    is never anchored at ``savefile_directory`` and never at a core's system
    directory. ``emulator_directory`` joined it with the standalone savestate
    cards (#225) — the same word, and the same fact, as on a standalone entry's
    save answer.
    """
    return _placement_core(placement)


def screenshot_placement_contract(placement: ScreenshotPlacement) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.placement.ScreenshotPlacement`.

    The savefile shape minus ``file_set``, ``fallback_dir`` and
    ``granularity`` — each omission the contract, for the reasons the
    placement's own docstring gives: no closed set of dated names exists to
    state, the directory is created at the moment of the shot rather than
    fallen back from, and nothing groups screenshots but the directory
    itself. ``root_kind`` speaks the question's own two-word vocabulary.
    """
    return {
        "dir": placement.dir,
        "root_kind": placement.root_kind,
        "needs": list(placement.needs),
        "physical_dir": placement.physical_dir,
        "caveats": _caveats_contract(placement.caveats),
    }


def screenshot_answer_contract(outcome: ScreenshotPlacement | Unresolved) -> dict[str, Any]:
    """A screenshot question's answer, placement or refusal — the family pattern."""
    if isinstance(outcome, Unresolved):
        return unresolved_contract(outcome)
    return screenshot_placement_contract(outcome)


def texture_placement_contract(placement: TexturePlacement) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.placement.TexturePlacement`.

    It shares ``dir``, ``needs``, ``physical_dir`` and ``caveats`` with the save
    placements and carries neither ``root_kind``, ``fallback_dir`` nor
    ``file_set`` — three omissions that are each the contract rather than an
    oversight. Nothing asked which root the tree hangs off; the sorted-directory
    fallback is RetroArch's own path math for files *it* writes, and a core's
    user directory is not one of them; and the files below a texture root are a
    pack the user installed, so observing them would report a caller's own
    downloads back as an answer about the emulator.

    The two fields that *are* its own carry ``null`` for opposite reasons, which
    is why both serialize rather than being omitted when unset: ``enabled`` is
    ``null`` where nothing established whether replacement is on — never to be
    read as off — and ``keying`` is ``null`` where no cited evidence states how
    the tree is divided per game, never as a claim that it is undivided.
    """
    return {
        "dir": placement.dir,
        "needs": list(placement.needs),
        "physical_dir": placement.physical_dir,
        "enabled": placement.enabled,
        "keying": placement.keying,
        "caveats": _caveats_contract(placement.caveats),
    }


def mod_placement_contract(placement: ModPlacement) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.placement.ModPlacement`.

    The texture placement's fields, with the directory trio moved inside
    ``trees``: this family's answer is plural because a mod is not one kind of
    thing, and an emulator may read three of them from three directories under
    one switch. Ten of the eleven rows atlas ships serialize a one-element list,
    and a client that only ever reads ``trees[0]`` is right about those ten and
    visibly wrong about the eleventh — which is the point of the shape.

    ``role`` is ``null`` on a single-tree answer (nothing to tell apart) and
    names the emulator's own word for the tree where there are several. Both
    ``keying`` and ``enabled`` carry ``null`` for the reasons they do on a
    texture answer: no cited evidence states how the tree is divided, and
    nothing established whether the feature is on — never to be read as off.
    """
    return {
        "trees": [
            {
                "role": tree.role,
                "dir": tree.dir,
                "physical_dir": tree.physical_dir,
                "keying": tree.keying,
            }
            for tree in placement.trees
        ],
        "needs": list(placement.needs),
        "enabled": placement.enabled,
        "caveats": _caveats_contract(placement.caveats),
    }


def soft_patch_contract(answer: SoftPatchAnswer) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.placement.SoftPatchAnswer`.

    Every field is structured and every one serializes, including the two that
    carry ``null``: ``applies`` is ``null`` where nothing established whether
    this core's content is loaded into memory — never to be read as *no* — and
    a candidate's ``attempted`` is ``null`` where nobody has read this build's
    compile-time flags, which is likewise not *this format is unsupported*.

    ``continuations`` are listed in full rather than described by a rule,
    because they are the answer: nine file names per format, in the order
    RetroArch applies them. A client that wants only the first patch reads
    ``path``; one managing a chain reads the list it would otherwise have to
    compose from upstream arithmetic. The order of ``candidates`` is contractual
    too — it is the attempt order, and a port that emits the four in another
    order is answering a different question.
    """
    return {
        "candidates": [
            {
                "format": candidate.format,
                "path": candidate.path,
                "continuations": list(candidate.continuations),
                "attempted": candidate.attempted,
            }
            for candidate in answer.candidates
        ],
        "applies": answer.applies,
        "caveats": _caveats_contract(answer.caveats),
    }


def unresolved_contract(unresolved: Unresolved) -> dict[str, Any]:
    """The stable form of an :class:`~atlas.placement.Unresolved` outcome.

    Its ``data`` serializes by the same rule a caveat's does
    (:func:`data_contract`), because it is the same vocabulary: an aggregate
    refusal's ``paths`` is the JSON list it is in the written contract.
    """
    return {"unresolved": {"code": unresolved.code, "data": data_contract(unresolved.data)}}


def _findings_contract(health: Health) -> list[dict[str, Any]]:
    """The findings alone — one ``{code, data}`` per issue, in order.

    A finding is a caveat and serializes as one, like every other caveat in
    this module. Bare codes were the old shape and lost exactly what a client
    acts on: *which* marker is invalid, *which* root is missing, what the
    failing read answered.
    """
    return _caveats_contract(health.issues)


def health_contract(health: Health) -> dict[str, Any]:
    """The stable form of a health answer — the summary, and the findings.

    Every answer in this grammar serializes as an object, and health is an
    answer like the rest: ``ok`` is its summary field, the one a client renders
    (the same role ``requirements_met`` plays on a firmware answer), and
    ``issues`` carries the findings. ``ok`` is derived from the findings and
    stays derived on the value object — serializing it is stating the summary,
    not storing a second copy of the fact.

    :func:`installation_contract` embeds the findings *without* this wrapper:
    there, health is a field of an installation's identity rather than an
    answer in its own right, and the object it sits in already carries the
    summary in the only form that matters there — an empty list.
    """
    return {"ok": health.ok, "issues": _findings_contract(health)}


def installation_contract(installation: Installation) -> dict[str, Any]:
    """The stable identity/health form of an installation handle.

    ``label`` is the one field here nothing may branch on: it is how the
    distribution spells its own name (``RetroDECK`` for ``retrodeck``), packaged
    and cited in :mod:`atlas.distribution_labels`, and it exists so a client
    renders a name rather than title-casing an identifier into one the project
    does not use. ``kind`` stays the key — the label may be re-spelled the day a
    project re-spells itself, and a consumer that matched on it would break
    while a consumer that renders it simply reads the new name.
    """
    return {
        "kind": installation.kind,
        "label": distribution_label(installation.kind),
        "kinds": list(installation.kinds),
        "root": installation.root(),
        "health": _findings_contract(installation.health()),
    }


def _identity_contract(identity: FirmwareIdentity | None) -> dict[str, Any] | None:
    """One packaged identity: the bytes it pins, and what kind of thing they are.

    ``kind`` is in because it decides what a difference from those bytes means
    — for a ``file`` a ``mismatch``, for an ``archive`` a ``not-comparable``
    that judges nothing — and a consumer cannot derive it from the three fields
    above it. ``archive_reason`` and ``table_version`` stay out: *which* drift
    moved the bytes, and which version of the curated list called it a drift,
    are explanations rather than the thing a consumer branches on, and both
    travel on the caveat that rides with the value, where the explanations live.
    """
    if identity is None:
        return None
    return {
        "md5": identity.md5,
        "sha1": identity.sha1,
        "size": identity.size,
        "kind": identity.kind,
    }


def _supplied_by_contract(supplied: SuppliedBy | None) -> dict[str, Any] | None:
    """Whose file is at the destination — the distribution's own copy, or nothing stated.

    Three of the four fields are what a consumer acts on: ``distribution`` says
    who would put it back, ``source`` is the shipped file that was hashed (so
    the claim can be re-checked), and ``card_version`` says which revision of
    the packaged copy list named the pair — the same provenance an identity's
    table version carries, and the field that lets a vendored older atlas be
    recognised as one. The fourth is ``label``, the name that distribution
    writes for itself, and it is the one a consumer only renders: "provided by
    RetroDECK" is the sentence this field exists for, and ``distribution``
    remains the word to branch on.

    ``null`` is the ordinary value and it claims nothing: not the distribution's
    copy, no copy list for this distribution, and a destination no entry covers
    all read the same way — atlas did not establish this file's provenance.
    """
    if supplied is None:
        return None
    return {
        "distribution": supplied.distribution,
        "label": supplied.label,
        "source": supplied.source,
        "card_version": supplied.card_version,
    }


def _requirement_contract(requirement: FirmwareRequirement) -> dict[str, Any]:
    return {
        "core_so": requirement.core_so,
        "system": requirement.system,
        "system_source": requirement.system_source,
        "need": requirement.need,
        "file_name": requirement.file_name,
        "path": requirement.path,
        "declared": requirement.declared,
        "declared_kind": requirement.declared_kind,
        "identity": _identity_contract(requirement.identity),
        "found": requirement.found,
        "present": requirement.present,
        "checked": requirement.checked,
        "satisfied": requirement.satisfied,
        "supplied_by": _supplied_by_contract(requirement.supplied_by),
    }


def _requirement_entry_contract(
    entry: FirmwareRequirement | FirmwareAlternatives,
) -> dict[str, Any]:
    """One entry of a core's requirement list, in whichever of its two shapes.

    The list is a conjunction, and an entry that is *one of several files —
    the console region decides* must not be mistakable for two files both
    needed, so the group is its own single-key shape (the discriminator style
    ``unresolved`` and ``no_savestates`` use): each option is a full
    requirement plus ``regions``, the console regions whose launch it serves.
    A plain requirement stays exactly the fields it always was — ``regions``
    appears nowhere on it, because an unconditional entry has no scope to
    state.
    """
    if isinstance(entry, FirmwareAlternatives):
        return {
            "alternatives": [
                {
                    **_requirement_contract(option),
                    "regions": list(option.regions or ()),
                }
                for option in entry.options
            ]
        }
    return _requirement_contract(entry)


def firmware_contract(answer: FirmwareAnswer) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.firmware.FirmwareAnswer`.

    ``description`` is prose and stays out. Three derived fields are in on
    purpose, because a consumer deriving them itself is exactly how the answer
    gets read wrongly: ``declaration`` separates "this core needs nothing" from
    "atlas knows nothing about this core", ``satisfied`` says whether one file
    is actually usable (a *present* file with the wrong bytes is not), and
    ``requirements_met`` is the single number a client renders — never ``true``
    out of ignorance, never ``true`` with a required file known to be wrong.
    Two limits of it belong next to each other, because a consumer that renders
    only this field cannot see either:

    - With ``hash_checked`` false it is ``null`` wherever a required file's
      identity is known and was not verified. Presence is not the question the
      field asks; a caller who wants a green light passes ``verify``.
    - It can be ``true`` over a required file the packaged table does not cover
      at all (``checked`` ``unknown`` with ``identity`` ``null``). Nothing
      further can ever be established about such a file, so withholding the
      answer would withhold it forever — but "in place under the right name" is
      all that was checked. On the reference machine that is three requirements
      across two cores (blueMSX's databases and machine ROMs, Dolphin's
      ``codehandler.bin``).

    ``supplied_by`` is none of those three: it is read rather than derived, and
    it answers a different question from all of them — whose file is at the
    destination. A file the distribution places itself is restored by its own
    component prepare, and some of them (the Dolphin ``Sys`` tree RetroDECK
    copies is the case it exists for) no firmware library carries at all — so a
    client offering to delete or replace firmware must read it before acting.
    ``found`` is the path kind actually read, which ``present`` alone cannot
    carry (a directory at the destination is not a missing file), and
    ``refused`` names declarations atlas would not follow, with the reason,
    so a dropped file can never make a core look complete. ``path`` is the
    **resolved** destination and ``declared`` the string the core spelled: two
    declarations that land on one file are one place, and the name the core
    opens is still stated. ``declared_kind`` is the third of that group and
    says what the core does with the name: opens it as a file, or lists it as a
    folder. Like ``declared`` it is about the declaration rather than what was
    found, so it stands over an empty destination — which is where it earns its
    keep, because ``found: "missing"`` on a folder declaration is a folder to
    create rather than a file to fetch.

    A core's ``requirements`` list is a conjunction, and one entry may be an
    ``alternatives`` group instead of a requirement — see
    :func:`_requirement_entry_contract`: a launch needs the option whose
    ``regions`` contain its console region, which is the running disc's own
    under DuckStation's shipped Auto setting. A region no option lists has
    nothing stated for it, and the entry's caveats say why.
    """
    return {
        "root": answer.root,
        "hash_checked": answer.hash_checked,
        "cores": [
            {
                "core_so": core.core_so,
                "label": core.label,
                "declaration": core.declaration,
                "requirements_met": core.requirements_met,
                "requirements": [_requirement_entry_contract(r) for r in core.requirements],
                "refused": [{"declared": r.declared, "need": r.need, "reason": r.reason} for r in core.refused],
                "caveats": _caveats_contract(core.caveats),
            }
            for core in answer.cores
        ],
        "unclaimed": [
            {
                "path": f.path,
                "identity": _identity_contract(f.identity),
                "known_as": list(f.known_as),
            }
            for f in answer.unclaimed
        ],
        "caveats": _caveats_contract(answer.caveats),
    }


def identification_contract(identification: FirmwareIdentification) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.firmware.FirmwareIdentification`."""
    return {
        "identity": _identity_contract(identification.identity),
        "known_as": list(identification.known_as),
        "requirements": [_requirement_contract(r) for r in identification.requirements],
        "caveats": _caveats_contract(identification.caveats),
    }


def emulator_contract(entry: EmulatorEntry) -> dict[str, Any]:
    """The stable form of one catalogue entry.

    ``system`` is here because an entry is only an answer *for* a system: the
    catalogue question carries it in the call, but an entry that travels — into
    a client's own list, into a serialized answer read later — otherwise names
    the emulator without naming what it launches.

    ``declared_index`` is the entry's 0-based place in the launch list ES-DE
    builds from the declaring layer's ``<command>`` elements, and ``selection``
    says why the list order moved. The list travels in *effective* order, so
    without the pair a reader cannot tell an entry promoted out of the middle
    from the declared first one a user also selected — and a client whose own
    default is the frontend's default looks for ``declared_index == 0``, never
    ``entries[0]``. That lookup can legitimately find nothing: the values are
    distinct and ascending *in declared order* — never guaranteed in
    serialized order, which a promotion can reshuffle — and they may skip
    one, because a command
    ES-DE keeps with empty text holds a position that carries no entry here. It
    is ``null`` on a derived entry (``emulator-list-derived``), which no layer
    declared and which therefore has no declared position.

    ``caveats`` serialize ``{code, data}`` like every other caveat in this
    module. Bare codes were this serializer's own dialect and lost what the
    data says (which game's override was not checked), which is exactly the
    thing a client acts on.
    """
    return {
        "system": entry.system,
        "label": entry.label,
        "kind": entry.kind,
        "core_so": entry.core_so,
        "declared_index": entry.declared_index,
        "selection": entry.selection,
        "caveats": _caveats_contract(entry.caveats),
    }


def catalogue_contract(answer: CatalogueAnswer) -> dict[str, Any]:
    """The stable form of a catalogue answer — entries, and why there are none.

    Both caveat lists carry ``{code, data}``: at answer level, which
    arrangement could not answer and whether that is a fact about the machine
    or about atlas; on an entry, what its own degradation is about. One shape
    for a caveat, wherever it sits.
    """
    return {
        "entries": [emulator_contract(e) for e in answer.entries],
        "caveats": _caveats_contract(answer.caveats),
    }


def launchable_contract(answer: LaunchabilityAnswer) -> dict[str, Any]:
    """The stable form of a launchability answer — the verdict, and everything a 'no' needs.

    ``entry`` travels with the two verdicts that have one — ``launchable``,
    and ``entry-not-accepted``, where the entry *is* the finding — and is
    ``null`` on the rest: an emulator for a file nothing launches would
    answer a different question. ``alternatives`` is ``entry-not-accepted``'s
    remedy alone: the declared entries established to take the file.
    ``accepted`` stays the declared tokens verbatim, the same non-vocabulary
    the ROM placement's ``extensions`` field carries.
    """
    return {
        "verdict": answer.verdict,
        "extension": answer.extension,
        "accepted": list(answer.accepted),
        "entry": emulator_contract(answer.entry) if answer.entry is not None else None,
        "alternatives": list(answer.alternatives),
        "caveats": _caveats_contract(answer.caveats),
    }


def systems_contract(answer: SystemsAnswer) -> dict[str, Any]:
    """The stable form of a systems answer — what the catalogue declares, or why nothing."""
    return {
        "systems": list(answer.systems),
        "caveats": _caveats_contract(answer.caveats),
    }


def platform_systems_contract(answer: PlatformSystemsAnswer) -> dict[str, Any]:
    """The stable form of a forward platform answer — resolved platforms, matches, statuses.

    ``platforms`` empty means the id resolved to nothing and the
    ``platform-unmapped`` caveat says so; an empty ``matches`` under resolved
    platforms is the different statement that nothing on this machine answers
    to a real platform. Every match carries its status and where its tags came
    from — the two fields a consumer branches on.
    """
    return {
        "vocabulary": answer.vocabulary,
        "value": answer.value,
        "platforms": list(answer.platforms),
        "matches": [
            {
                "system": m.system,
                "status": m.status,
                "platforms": list(m.platforms),
                "tags_source": m.tags_source,
            }
            for m in answer.matches
        ],
        "caveats": _caveats_contract(answer.caveats),
    }


def _platform_identities_contract(identities: PlatformIdentities) -> dict[str, Any]:
    return {
        "platform": identities.platform,
        "igdb": [{"id": i.id, "slug": i.slug, "name": i.name} for i in identities.igdb],
        "libretro": list(identities.libretro),
        "screenscraper": identities.screenscraper,
        "thegamesdb": identities.thegamesdb,
    }


def system_platforms_contract(answer: SystemPlatformsAnswer) -> dict[str, Any]:
    """The stable form of a reverse platform answer — the tags and their identities.

    ``identities`` carries one entry per tag the crosswalk knows; the tags it
    does not know are stated by the ``platform-unknown`` / ``platform-scraping-ignored``
    caveats, so tags, identities and caveats always add up. ``status`` and
    ``tags_source`` qualify the whole answer: whether the system is here, and
    whether its tags were read off the machine or taken from the vocabulary
    snapshot.
    """
    return {
        "system": answer.system,
        "status": answer.status,
        "tags_source": answer.tags_source,
        "platforms": list(answer.platforms),
        "identities": [_platform_identities_contract(i) for i in answer.identities],
        "caveats": _caveats_contract(answer.caveats),
    }


def rom_placement_contract(placement: RomPlacement) -> dict[str, Any]:
    """The stable form of a ROM placement — the directory, the extensions, and why not.

    ``dir`` serializes as ``null`` where atlas resolved none, which is the
    field's honest value and not an omission: the caveats say which of the ways
    it was, and a client that treats null as "look in the default place" would
    be inventing the answer this refuses to give. ``physical_dir`` is ``null``
    in the ordinary case — no symlinks were traversed — and carries the backing
    directory only where they were, the same way a save placement's does.
    """
    return {
        "dir": placement.dir,
        "physical_dir": placement.physical_dir,
        "extensions": list(placement.extensions),
        "caveats": _caveats_contract(placement.caveats),
    }


def savefile_answer_contract(outcome: SavefilePlacement | Unresolved) -> dict[str, Any]:
    """A savefile question's answer in whichever of its two shapes it took.

    The route answers with a placement, or refuses with an
    :class:`~atlas.placement.Unresolved` for a core this installation does not
    have. Both are contractual, and one serializer for the pair is what lets a
    caller write the answer down without first deciding which it got — the
    aggregate route above all, where one installation can answer while its
    neighbour refuses the very same question.
    """
    if isinstance(outcome, Unresolved):
        return unresolved_contract(outcome)
    return savefile_placement_contract(outcome)


def savestate_absence_contract(absence: SavestateAbsence) -> dict[str, Any]:
    """The stable form of a :class:`~atlas.placement.SavestateAbsence`.

    Its own top-level key rather than a placement with holes, because the
    shapes must not be mistakable: ``no_savestates`` states the emulator has
    no such feature — an answer, not a refusal — and the citation rides
    contractually because a client repeating the claim repeats its source.
    ``sources`` stays out, like every provenance prose.
    """
    return {
        "no_savestates": {
            "emulator": absence.emulator,
            "citation": absence.citation,
            "caveats": _caveats_contract(absence.caveats),
        }
    }


def savestate_answer_contract(
    outcome: SavestatePlacement | SavestateAbsence | Unresolved,
) -> dict[str, Any]:
    """A savestate question's answer — placement, stated no, or refusal.

    One shape more than the savefile twin (#284): an emulator with no
    savestate feature answers the question with a cited no, which is neither
    a directory nor a refusal, so the serializer branches three ways.
    """
    if isinstance(outcome, Unresolved):
        return unresolved_contract(outcome)
    if isinstance(outcome, SavestateAbsence):
        return savestate_absence_contract(outcome)
    return savestate_placement_contract(outcome)


def texture_answer_contract(outcome: TexturePlacement | Unresolved) -> dict[str, Any]:
    """A texture-pack question's answer in whichever of its two shapes it took.

    This route refuses more often than the save routes do, and the pair
    serializer is what keeps that from being the caller's problem: a core that
    is not installed, a standalone entry, and a core whose texture wiring atlas
    has not established all come back as the typed refusal, while everything
    else is a placement.
    """
    if isinstance(outcome, Unresolved):
        return unresolved_contract(outcome)
    return texture_placement_contract(outcome)


def mod_answer_contract(outcome: ModPlacement | Unresolved) -> dict[str, Any]:
    """A mod question's answer in whichever of its two shapes it took.

    Refuses on the same three conditions the texture question does — a core
    that is not installed, a standalone entry atlas cannot place, and an
    emulator whose mod wiring is not established — so one serializer for the
    pair keeps that from being the caller's problem.
    """
    if isinstance(outcome, Unresolved):
        return unresolved_contract(outcome)
    return mod_placement_contract(outcome)


def soft_patch_answer_contract(outcome: SoftPatchAnswer | Unresolved) -> dict[str, Any]:
    """A soft-patching question's answer, candidates or refusal.

    One refusal reaches here and only one: a core the machine established is
    not installed. The candidate files themselves would still be true — they are
    the content's own — but the question was asked about a core this
    installation does not have, and the family answers that with one code
    wherever it is asked.
    """
    if isinstance(outcome, Unresolved):
        return unresolved_contract(outcome)
    return soft_patch_contract(outcome)


def installation_answers_contract(
    answers: Sequence[InstallationAnswer[AnswerT]],
    serialize: Callable[[AnswerT], dict[str, Any]],
) -> list[dict[str, Any]]:
    """The stable form of an aggregate answer — every installation's answer, labelled.

    Composed rather than defined: the label is :func:`installation_contract`,
    the payload is whatever serializer the question already has, passed in by
    the caller who asked it (``savefile_placement_contract`` for ``savefile_location``,
    ``catalogue_contract`` for ``emulators_for``, …). The aggregate adds no
    fields of its own, so it may not add serialization of its own either — an
    aggregate answer that differed from the handle-route answer in any way
    would be a resolver rule hiding in the fan-out.

    The list is ordered, and the order is contractual: it is detection order,
    the same order :func:`~atlas.detect.detect` states. An empty list is the
    empty machine, and stays a truthful answer rather than an error.
    """
    return [
        {
            "installation": installation_contract(answered.installation),
            "answer": serialize(answered.answer),
        }
        for answered in answers
    ]
