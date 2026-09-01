"""Which arrangements atlas has seen alive — the evidence behind every answer.

Reading a config the way the emulator reads it is one thing; having watched a
real machine of that arrangement do it is another. Atlas's resolver rules are
source-verified against pinned upstream for every arrangement; RetroDECK and
EmuDeck have each been observed end to end on a live installation
(``docs/research/``, and the per-core column in ``docs/research/coverage-matrix.md``),
the bare RetroArch arrangements have not. An answer from an arrangement nobody
has observed is derived, not confirmed — and derived is worth saying out loud,
machine-readably, on the answer itself.

Two rules decide the shape here, both from CLAUDE.md's boundary rule:

- **The claim is about atlas, never about the machine.** The caveat says a live
  observation of the *arrangement* is missing. It does not say the configs were
  guessed (they are read the same way everywhere), and it does not say this
  installation is broken — that is what :class:`~atlas.installations.Health`
  answers, which is why the evidence note deliberately stays out of it.
- **The status is data, not code.** It lives in ``data/arrangement_evidence.json``
  — marked, versioned, source-cited — so the day an arrangement is verified on a
  reference machine, one file changes and the caveat retires by itself. No
  resolver mentions any arrangement's evidence state.

An arrangement absent from the file is unverified: a missing record is not
evidence, and the safe direction is to say so rather than to fall silent. A
handle kind that never gained an entry is a build mistake all the same, and the
test suite refuses it there instead of here.

The record's pinned version is not provenance alone, it is a **tripwire**: a
verified arrangement was verified against one version of itself, and the
machine says which one it runs. When the two differ, every answer says so
(``docs/re-verification.md`` is what closing it takes). Without that comparison
an update to the arrangement would age all of atlas's world knowledge — parser
grammar, path layout, shipped-build behaviour — while the answers stayed as
clean as the day they were confirmed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ._data import packaged_text
from .placement import Caveat

# Packaged-data schema version. The loader is strict for the same reason the
# rule-card loaders are: a malformed build fails loudly instead of resolving
# with knowledge nobody can place.
ARRANGEMENT_EVIDENCE_SCHEMA = 1

# The answer-level caveat every unverified arrangement attaches. Sits next to
# ``unverified-version`` (a rule card's pinned versions differ from this
# machine's) and reads the same way: a statement about what atlas has
# established, not about what the machine is doing.
CAVEAT_ARRANGEMENT_UNVERIFIED = "arrangement-unverified"

# The other half of the same axis: the arrangement *was* observed, and this
# machine no longer runs the version it was observed on. Per rule card,
# ``unverified-version`` says this about one core's pinned behaviour; this code
# says it about the arrangement as a whole — the parser grammar, the path
# layout, the shipped build. One comparison guards all of it against aging in
# silence.
CAVEAT_ARRANGEMENT_VERSION_DRIFTED = "arrangement-version-drifted"


# This check exists verbatim three times, one per packaged-data loader
# (:func:`atlas.oddities._expect_str`, :func:`atlas.systems._expect_str`). The
# triplication is the deliberate cost of keeping the loaders independent of each
# other: each reads its one file and shares no machinery with the other two, so
# a defect in one table can never fail the load of another — and a fidelity
# finding about what counts as a string belongs in all three.
def _expect_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class LiveVerification:
    """The installation an arrangement's knowledge was confirmed against."""

    version: str
    date: str | None
    reference: str


@dataclass(frozen=True, slots=True)
class ArrangementEvidence:
    """What atlas has established about one arrangement, beyond reading its configs.

    ``verified`` is ``None`` for an arrangement no live installation has
    confirmed — the state that produces the caveat. ``note`` carries the
    evidence level and what it rests on, in the register of the research
    documents; ``label`` is how the arrangement is named in prose.
    """

    kind: str
    label: str
    note: str
    verified: LiveVerification | None = None


def _live_verification(record: Any, where: str) -> LiveVerification | None:
    """One arrangement's verification record — ``None`` stays *never observed*.

    A present record must pin the ``version`` it was observed on and cite the
    installation, for the reason the core audit pins its own: a record that
    names nothing would read as verified everywhere and forever while
    establishing nothing, which is worse than never verified because it claims
    the opposite.
    """
    if record is None:
        return None
    if not isinstance(record, dict):
        raise ValueError(f"{where}: expected an object or null, got {record!r}")
    date = record.get("date")
    if date is not None and not isinstance(date, str):
        raise ValueError(f"{where}.date: expected a string or null, got {date!r}")
    return LiveVerification(
        version=_expect_str(record.get("version"), f"{where}.version"),
        date=date,
        reference=_expect_str(record.get("reference"), f"{where}.reference"),
    )


def load_arrangement_evidence(text: str | None = None) -> dict[str, ArrangementEvidence]:
    """Load the packaged evidence records (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("arrangement_evidence.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != ARRANGEMENT_EVIDENCE_SCHEMA:
        raise ValueError(
            f"arrangement_evidence: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {ARRANGEMENT_EVIDENCE_SCHEMA})"
        )
    records: dict[str, ArrangementEvidence] = {}
    for kind, entry in raw.get("arrangements", {}).items():
        where = f"arrangement {kind!r}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected an object, got {entry!r}")
        records[kind] = ArrangementEvidence(
            kind=kind,
            label=_expect_str(entry.get("label"), f"{where}: label"),
            note=_expect_str(entry.get("note"), f"{where}: note"),
            verified=_live_verification(entry.get("verified"), f"{where}: verified"),
        )
    return records


_EVIDENCE: dict[str, ArrangementEvidence] | None = None


def lookup_arrangement(kind: str) -> ArrangementEvidence | None:
    """The packaged evidence record for an installation kind, if there is one."""
    global _EVIDENCE
    if _EVIDENCE is None:
        _EVIDENCE = load_arrangement_evidence()
    return _EVIDENCE.get(kind)


def arrangement_caveats(kind: str, *, observed_version: str | None = None) -> tuple[Caveat, ...]:
    """What every answer from *kind* must state about its own evidence.

    Two states produce a caveat, and they are the halves of one question — has
    anyone watched this arrangement work, and was it *this* arrangement?

    - **Never observed.** One caveat, on the answer itself, so a client
      branching on codes learns it without knowing which arrangements exist.
      The message states what is missing and what is not: the missing part is a
      live observation of the arrangement; the part that is *not* missing is
      the config reading, which is source-verified against pinned upstream
      wherever it runs. A client that renders this as "atlas guessed" would
      report something nobody claimed.
    - **Observed, on another version.** The record pins the version its
      knowledge was confirmed against, and this machine states a different one.
      What that establishes is that the confirmation is old, not that the
      answer is wrong — nothing here re-decides a placement, and the message
      says so while naming the re-verification that is pending.

    *observed_version* is the version the machine states about itself, or
    ``None`` when it states none. The comparison runs only when both sides
    speak, so an arrangement whose machine names no version stays silent — and
    that silence means *no drift established*, not *no drift*. The alternative
    would put a permanent caveat on every answer of an installation that may
    well be current, which claims a comparison nobody made. Where missing live
    versions do decide something — a rule card pinned to one — the per-card
    ``unverified-version`` caveat states that at the point it matters.

    An arrangement verified against the version this machine runs says nothing
    at all, exactly as an installation with no health issues says nothing.
    """
    record = lookup_arrangement(kind)
    if record is None or record.verified is None:
        label = record.label if record is not None else f"the {kind!r} arrangement"
        return (
            Caveat(
                CAVEAT_ARRANGEMENT_UNVERIFIED,
                f"no live installation of {label} has been observed by atlas — how its configs are read is "
                "source-verified against pinned upstream, but nothing has confirmed the wiring end to end on "
                "a running machine, so this answer is derived rather than verified "
                "(docs/how-to-use.md, 'What atlas has actually seen')",
                {"kind": kind},
            ),
        )
    # An empty string names no version any more than ``None`` does, and a
    # comparison against it would report drift to nothing.
    if not observed_version or observed_version == record.verified.version:
        return ()
    return (
        Caveat(
            CAVEAT_ARRANGEMENT_VERSION_DRIFTED,
            f"atlas's knowledge of {record.label} was verified against {record.verified.version}, and this "
            f"machine states {observed_version} — the reading procedures are source-verified either way, but "
            "nothing has confirmed them against the version running here, so re-verification is pending "
            "(docs/re-verification.md)",
            {
                "kind": kind,
                "verified": record.verified.version,
                "observed": observed_version,
            },
        ),
    )
