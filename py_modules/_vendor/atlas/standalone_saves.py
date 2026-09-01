"""Standalone save cards — which emulators atlas can answer the save question for.

A standalone emulator is handed nothing by a frontend: its save tree is its
own, shaped by its own configuration and its own compiled-in defaults. The
card here is the thin, versioned half of that knowledge — which emulator,
which configuration file governs it, which catalogue systems it answers for,
and the citations behind both — while the reading itself is code beside it in
:mod:`atlas.installations`, exactly the split the libretro rule cards make
(:mod:`atlas.mode_rules`): a card states what *can* be, the code reads what
*is* on this machine, and neither guesses.

The cards are keyed by the ``%EMULATOR_…%`` token the ES-DE catalogue names in
a launch command, because for a standalone entry that token is the only
identifier there is — the same key the standalone texture cards use
(:mod:`atlas.textures`). A card whose token has no resolver function
registered is a marker selecting nothing, and fails the load the way a rule
card without a rule does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._data import packaged_text

SAVES_SCHEMA = 2


@dataclass(frozen=True, slots=True)
class StandaloneSaveCard:
    """One standalone emulator's save knowledge: config file, systems, citations.

    ``settings`` names the configuration file the resolver reads the way the
    emulator does — by **name**, with its address stated once in
    ``atlas/data/emulator_settings.json``, because the save, texture, mod and
    firmware answers of one emulator open the same file and each carrying its
    own copy is how two of them came to disagree about it. It is ``None`` for
    an emulator whose save tree is fixed by the build rather than by any file
    (PPSSPP's Linux memstick is a compiled-in XDG join): naming a file the
    resolver never reads would state a governing config that does not govern.
    ``systems`` is the closed list of catalogue systems this card answers
    for: an emulator can serve several with different trees (Dolphin keeps
    GameCube cards and a Wii NAND), and a system outside the list is a
    question the card does not answer, stated rather than stretched.

    There is deliberately no ``flatpak`` field, since #288. Which app id an
    arrangement installs an emulator as is a property of the **installation**
    and of no single question, so it is stated once in
    ``atlas/data/emulator_settings.json``, beside the directory spellings that
    are keyed by that very id. It sat here until then, which is why an
    emulator with no save card could reach no trees of its own at all: MAME's
    savestate card is complete and its save card does not exist, and the
    savestate answer refused a launch it could have read.

    ``citations`` are the emulator's own source references the **resolver**
    speaks — the line ranges that go into an answer's caveats and readings —
    keyed by the slot the code asks for. They live on the card because a
    resolver can be shared by two emulators that are not the same source:
    PrimeHack is a Dolphin fork with Dolphin's save shape, read by Dolphin's
    resolver, and every file it inherits sits at different lines than in the
    Dolphin release beside it. A shared reading with one hard-coded citation
    would state one build's evidence for the other's answer.

    A citation belongs to the **build** rather than to the emulator, which is
    why the reserved ``installations`` key states one set per flatpak app id,
    exactly as the directory name does: the PrimeHack revision RetroDECK
    builds and the one Flathub ships are three years apart, and all seven of
    the lines a save answer names differ between them.
    """

    token: str
    settings: str | None
    systems: tuple[str, ...]
    provenance: str
    citations: Mapping[str, str] = field(default_factory=dict)
    citation_installations: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def cite(self, slot: str, *, flatpak: str | None) -> str:
        """The card's citation for one slot, in the build this launch runs.

        *flatpak* has no default for the same reason ``user_directory``'s has
        none: a reading that forgot it would name the arrangement's own
        build's lines for an answer about somebody else's, and look exactly
        like a verified one. Raises for a slot the card does not state — the
        card and the code shipped out of step.
        """
        stated = self.citation_installations.get(flatpak or "", self.citations)
        citation = stated.get(slot)
        if citation is None:
            raise ValueError(
                f"standalone save card {self.token!r} states no {slot!r} citation for "
                f"{flatpak or 'the arrangement own build'} — the resolver reading it names "
                "that source in its answer, and the card and the code shipped out of step"
            )
        return citation


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _card(token: str, entry: Any) -> StandaloneSaveCard:
    """One emulator's card — validated, never coerced."""
    where = f"standalone save card {token!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    saves = entry.get("saves")
    if not isinstance(saves, dict):
        raise ValueError(f"{where}: expected a 'saves' object, got {saves!r}")
    settings = saves.get("settings")
    if settings is not None:
        settings = _expect_str(settings, f"{where}: saves.settings")
    systems = saves.get("systems")
    if not isinstance(systems, list) or not systems:
        raise ValueError(f"{where}: saves.systems must be a non-empty list, got {systems!r}")
    if "flatpak" in entry:
        raise ValueError(
            f"{where}: which app id an arrangement installs this emulator as is stated once in "
            "atlas/data/emulator_settings.json — a copy here could only ever drift from it, and "
            "an emulator without a save card could not state it at all (#288)"
        )
    provenance = entry.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError(f"{where}: expected a 'provenance' object, got {provenance!r}")
    stated_citations = saves.get("citations", {})
    if not isinstance(stated_citations, dict):
        raise ValueError(f"{where}: expected a 'saves.citations' object, got {stated_citations!r}")
    # Copied rather than popped: the caller's parsed document is theirs, and a
    # loader that empties a key out of it makes a second load of the same
    # object see a card without its overrides.
    installations = stated_citations.get("installations", {})
    citations = {k: v for k, v in stated_citations.items() if k != "installations"}
    if not isinstance(installations, dict):
        raise ValueError(
            f"{where}: expected a 'saves.citations.installations' object, got {installations!r}"
        )
    for app_id, stated in installations.items():
        _expect_str(app_id, f"{where}: saves.citations.installations key")
        if not isinstance(stated, dict) or set(stated) != set(citations):
            raise ValueError(
                f"{where}: saves.citations.installations[{app_id!r}] must state the same slots "
                f"as the default set {sorted(citations)} — a partial override reads as one "
                "build's evidence and answers with another's"
            )
    return StandaloneSaveCard(
        token=token,
        settings=settings,
        systems=tuple(_expect_str(s, f"{where}: saves.systems[]") for s in systems),
        provenance=_expect_str(provenance.get("source"), f"{where}: provenance.source"),
        citations={
            _expect_str(slot, f"{where}: saves.citations key"): _expect_str(
                citation, f"{where}: saves.citations[{slot!r}]"
            )
            for slot, citation in citations.items()
        },
        citation_installations={
            app_id: {
                slot: _expect_str(
                    citation, f"{where}: saves.citations.installations[{app_id!r}][{slot!r}]"
                )
                for slot, citation in stated.items()
            }
            for app_id, stated in installations.items()
        },
    )


def load_standalone_saves(text: str | None = None) -> tuple[StandaloneSaveCard, ...]:
    """Load the packaged standalone save cards (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("standalone_saves.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != SAVES_SCHEMA:
        raise ValueError(
            f"standalone_saves: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {SAVES_SCHEMA})"
        )
    return tuple(_card(token, entry) for token, entry in raw.get("emulators", {}).items())


_PACKAGED: tuple[StandaloneSaveCard, ...] | None = None


def lookup_standalone_save_card(token: str | None) -> StandaloneSaveCard | None:
    """The packaged card for one emulator token, or ``None`` — no fuzzy matching."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_standalone_saves()
    if token is None:
        return None
    return next((card for card in _PACKAGED if card.token == token), None)
