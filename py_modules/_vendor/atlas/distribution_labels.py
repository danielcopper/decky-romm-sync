"""How a distribution spells its own name — the presentation half of an identifier.

The contract's keys are identifiers: ``retrodeck``, ``emudeck``,
``bare_retroarch_flatpak``, ``bare_retroarch_native``. They are stable, greppable
and machine-shaped, and nothing here changes that. What they are not is
readable: the first consumer rendered one verbatim ("provided by retrodeck"),
and the alternative on its side would have been title-casing a string it did not
write — which produces ``Retrodeck`` for a project that writes ``RetroDECK``,
and would have to guess again at every new identifier. The spelling is a fact
about the project, atlas holds it, so atlas states it.

No machine states a spelling as a **name**. The strings themselves do occur:
EmuDeck's marker lives under a directory called ``EmuDeck``
(``EMUDECK_SETTINGS_SUFFIX``), the bare Flatpak's application id is
``org.libretro.RetroArch``, its configured paths repeat that id, and RetroDECK's
marker names ``RetroDECK`` as the repository it updates from. Each of those is a
path segment, an application id or a repository name, and a resolver reading a
display name out of them would answer ``retroarch`` for a native install — whose
config directory is spelled lowercase — and would have nothing at all to tell
the two RetroArch installations apart. So the spelling is world knowledge under
CLAUDE.md's boundary rule: packaged, versioned, and cited to the project's own
text (``data/distribution_labels.json``), never derived from the identifier or
from a path at runtime.

**A label is presentation and never a key.** It is for rendering to a person,
and nothing branches on one: a consumer branches on ``kind`` and
``supplied_by.distribution``, and so does every route in atlas. Two comparisons
exist and both are gates rather than logic — the loader's check that the cited
spelling survives inside the label, and the vector gate's check that a corpus
states the packaged spelling rather than one of its own. That is also why a
label carries no version into an answer the way a copy list's ``card_version``
does: no decision rests on which revision of this table was read, so stamping
one onto every answer would state a provenance nobody acts on. The file's
``version`` and ``reviewed`` say when the spellings were last read against their
sources, which is what a re-reading needs.

Not to be confused with the ``label`` in ``data/arrangement_evidence.json``,
which is a sentence fragment written for caveat prose ("an EmuDeck
arrangement", "the bare RetroArch Flatpak") and reads as one inside a message.
This table holds the project's own display name, which a client renders on its
own. The two agree for ``retrodeck`` by coincidence and for nothing else.

**A kind with no entry is a table error.** :func:`distribution_label` raises
rather than returning the identifier or a title-cased guess — falling back would
put exactly the string this module exists to replace into a consumer's UI, and
would do it silently. The gap cannot ship: a test derives the kind set from the
handle classes and fails when the table does not cover it, and a second one does
the same for every distribution the copy lists name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ._data import packaged_text

# Packaged-data schema version. The loader is strict the way its neighbours are:
# a malformed build fails loudly instead of answering out of a table nobody can
# place.
DISTRIBUTION_LABELS_SCHEMA = 1


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class DistributionLabel:
    """One identifier's presentation name, and where its spelling was read.

    ``spelling`` is the name as the cited source writes it and ``label`` is what
    atlas presents. They are the same string wherever one name is enough. They
    differ where two kinds are one program in two packagings — the bare
    RetroArch pair — and there the label adds a qualifier of atlas's own, which
    the loader allows only *around* the cited spelling, never instead of it:
    ``RetroArch (Flatpak)`` may say more than the README does, but it may not
    say ``Retroarch``.
    """

    kind: str
    label: str
    spelling: str
    citation: str


_ENTRY_KEYS = {"label", "spelling", "citation"}


def _entry(kind: str, raw: Any) -> DistributionLabel:
    where = f"distribution label {kind!r}"
    if not isinstance(raw, dict) or set(raw) != _ENTRY_KEYS:
        raise ValueError(f"{where}: an entry names exactly {sorted(_ENTRY_KEYS)}, got {raw!r}")
    label = _expect_str(raw["label"], f"{where}: label")
    spelling = _expect_str(raw["spelling"], f"{where}: spelling")
    if spelling not in label:
        raise ValueError(
            f"{where}: the label {label!r} does not contain the cited spelling {spelling!r} — "
            "a label may add a qualifier around the name its source spells, never re-spell it"
        )
    return DistributionLabel(
        kind=kind,
        label=label,
        spelling=spelling,
        citation=_expect_str(raw["citation"], f"{where}: citation"),
    )


def load_distribution_labels(text: str | None = None) -> dict[str, DistributionLabel]:
    """Load the packaged spellings (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("distribution_labels.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != DISTRIBUTION_LABELS_SCHEMA:
        raise ValueError(
            f"distribution_labels: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {DISTRIBUTION_LABELS_SCHEMA})"
        )
    distributions = raw.get("distributions", {})
    if not isinstance(distributions, dict) or not distributions:
        raise ValueError(
            f"distribution_labels: distributions must be a non-empty object, got {distributions!r}"
        )
    return {kind: _entry(kind, entry) for kind, entry in distributions.items()}


_LABELS: dict[str, DistributionLabel] | None = None


def distribution_label(kind: str) -> str:
    """The name *kind* writes for itself — for rendering, never for branching.

    Raises :class:`ValueError` for an identifier the table does not cover. That
    is the point: the caller asked for a name to show a person, and the two ways
    to answer without one — hand back the identifier, or title-case it — both
    put a string atlas never established in front of a user, and do it without
    a sound. A gap here is a build mistake, and it fails like one.
    """
    global _LABELS
    if _LABELS is None:
        _LABELS = load_distribution_labels()
    record = _LABELS.get(kind)
    if record is None:
        raise ValueError(
            f"distribution_labels: no packaged spelling for {kind!r} "
            f"(the table names {sorted(_LABELS)}) — every identifier atlas reports needs one"
        )
    return record.label
