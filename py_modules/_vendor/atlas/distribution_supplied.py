"""Distribution-supplied firmware — which files a distribution places into the firmware root itself.

A consumer's "delete this BIOS" action removed ``dolphin-emu/Sys/codehandler.bin``
— a file no firmware library carries, because RetroDECK ships it and its own
component prepare copies it in. From the outside it was indistinguishable from a
dump the user dropped there: declared required by ``dolphin_libretro.info``,
carrying no ``System.dat`` identity, and therefore ``checked`` ``unknown`` with
``satisfied`` ``True``.

The table here is the world-knowledge half of stating that, and it is
deliberately the smallest half. It says only **which trees and files one
component's preparation step copies** — for RetroDECK, the RetroArch
component's, which is where the firmware root's own trees come from — read line
by line out of the script that does the copying, with a ``file:line`` citation
per entry. Other components write into the same root by other means, and each
one is its own entry in this table or its own open point; the list is not a
census of everything a distribution puts there. It says nothing at all about any
machine. Whether the file at a destination really is that copy is decided by
:mod:`atlas.firmware` on the machine, by hashing the shipped file and the placed
file and comparing them — so the statement rests on an equality that was read,
never on the table's say-so.

That split is why this table needs no version gate where
:mod:`atlas.content_tree_wiring` needs one. That table records a *promise* (a
symlink the installer said it would make), and a promise can only be measured
against the version that made it. This one records *where to look for the
shipped file*, and the answer is then read off the live tree, so a table that
has fallen behind a release under-reports: a newly copied tree nobody has
recorded yet is simply not stated, and the equality behind a statement it does
make was still measured. The one thing a stale entry can get wrong is the
**repair** it implies — if a release stops copying a tree while going on
shipping it, the bytes still match and "the component prepare puts this back"
no longer holds. That is what the version pin is for reading, and it is a
reason to re-read the script at a release, not to gate the answer.

A distribution whose card is absent, a destination no entry covers, and a placed
file whose bytes differ from the shipped one are all one and the same answer —
nothing is stated.

A distribution is keyed by the **arrangement kind** atlas already answers under
(``retrodeck``), so the word in the contract and the word a handle reports are
one word.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ._data import packaged_text

SUPPLIED_SCHEMA = 1

# What one line of a preparation step copies: ``tree`` is a whole directory, so
# every file below the destination has a counterpart below the source, and
# ``file`` is a single file, matched exactly. The kind describes **what the copy
# places**, never which flag placed it — RetroDECK spells both with ``cp -rf``
# and reserves ``cp -f`` for one line of the seventeen, so a rule read off the
# flag would call two files trees. The card states the kind it read, because
# deriving one from whether the source *looks* like a file name would be a guess
# about the shipped tree, and the extension-free directory names in it
# (``qemu``, ``fbneo``) are exactly where such a guess would break.
SUPPLIED_KINDS = ("file", "tree")

SUPPLIED_FILE = "file"
SUPPLIED_TREE = "tree"


@dataclass(frozen=True, slots=True)
class SuppliedEntry:
    """One copy the preparation step makes: what it takes, where it puts it, and the line that says so.

    ``source`` is relative to the distribution's own extras root and
    ``destination`` relative to the firmware root, and they are *not* required
    to agree: RetroDECK's ``MSX/Databases`` lands as ``Databases`` and its
    ``Amiga/capsimg.so`` as ``capsimg.so``. The renaming is recorded here
    because a resolver that assumed the two spellings equal would look for the
    shipped file in a directory the distribution does not have.
    """

    kind: str
    source: str
    destination: str
    purpose: str
    citation: str

    def source_of(self, relative: str) -> str | None:
        """The extras-root-relative source that would have produced *relative*, or ``None``.

        *relative* is a path below the firmware root. A ``file`` entry answers
        for its destination exactly; a ``tree`` entry answers for anything
        *below* its destination and never for the destination itself, which is
        the directory rather than a file in it.
        """
        if self.kind == SUPPLIED_FILE:
            return self.source if relative == self.destination else None
        prefix = f"{self.destination}/"
        if relative.startswith(prefix):
            return f"{self.source}/{relative[len(prefix):]}"
        return None


@dataclass(frozen=True, slots=True)
class DistributionSupplied:
    """One distribution's copy list, with everything a statement about it must carry.

    ``version`` pins the distribution release the citations were read at and
    ``card_version`` the revision of this table — the first says which shipped
    script was read, the second travels into the answer so a consumer knows
    which reading stamped a statement, the way an identity's ``table_version``
    does.

    ``source_root`` is spelled the way the **distribution** spells it, which
    inside a Flatpak means the sandbox's (``/app/...``), because that is the
    path its own script names. Translating it to a host path is the
    arrangement's job and happens a layer above this module.
    """

    distribution: str
    card_version: str
    reviewed: str
    version: str
    source_root: str
    entries: tuple[SuppliedEntry, ...]

    def source_of(self, relative: str) -> str | None:
        """The extras-root-relative source for a path below the firmware root, or ``None``.

        ``None`` is the ordinary answer: most of what sits in a firmware root
        is the user's, and this table covers only what the distribution puts
        there itself.
        """
        for entry in self.entries:
            source = entry.source_of(relative)
            if source is not None:
                return source
        return None


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _expect_relpath(value: Any, where: str) -> str:
    text = _expect_str(value, where)
    if text.startswith("/") or text.endswith("/") or ".." in text.split("/"):
        raise ValueError(f"{where}: expected a clean relative path, got {text!r}")
    return text


_ENTRY_KEYS = {"kind", "source", "destination", "purpose", "citation"}


def _entry(raw: Any, where: str) -> SuppliedEntry:
    if not isinstance(raw, dict) or set(raw) != _ENTRY_KEYS:
        raise ValueError(f"{where}: an entry names exactly {sorted(_ENTRY_KEYS)}, got {raw!r}")
    kind = _expect_str(raw["kind"], f"{where}: kind")
    if kind not in SUPPLIED_KINDS:
        raise ValueError(f"{where}: kind must be one of {SUPPLIED_KINDS}, got {kind!r}")
    return SuppliedEntry(
        kind=kind,
        source=_expect_relpath(raw["source"], f"{where}: source"),
        destination=_expect_relpath(raw["destination"], f"{where}: destination"),
        purpose=_expect_str(raw["purpose"], f"{where}: purpose"),
        citation=_expect_str(raw["citation"], f"{where}: citation"),
    )


def _refuse_overlapping_destinations(entries: tuple[SuppliedEntry, ...], where: str) -> None:
    """No destination may be reached by two entries — a matched path has one source.

    Two forms of collision, one rule: the same destination twice, and a tree
    whose destination contains another entry's. Either would let one placed
    file resolve to two shipped files, and picking between them would be a
    ranking nobody established.
    """
    destinations = [entry.destination for entry in entries]
    if len(set(destinations)) != len(destinations):
        raise ValueError(f"{where}: two entries claim the same destination")
    for entry in entries:
        if entry.kind != SUPPLIED_TREE:
            continue
        for other in destinations:
            if other != entry.destination and other.startswith(f"{entry.destination}/"):
                raise ValueError(
                    f"{where}: the tree {entry.destination!r} contains the destination {other!r}, "
                    "so a file below it would have two sources"
                )


_DISTRIBUTION_KEYS = {
    "version",
    "source_root",
    "source_root_citation",
    "destination_root",
    "entries",
}


def _distribution(name: str, raw: Any, *, card_version: str, reviewed: str) -> DistributionSupplied:
    where = f"distribution-supplied card {name!r}"
    if not isinstance(raw, dict) or set(raw) != _DISTRIBUTION_KEYS:
        raise ValueError(f"{where}: expected exactly {sorted(_DISTRIBUTION_KEYS)}, got {raw!r}")
    raw_entries = raw["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(f"{where}: entries must be a non-empty list, got {raw_entries!r}")
    entries = tuple(_entry(entry, f"{where}: entries[{i}]") for i, entry in enumerate(raw_entries))
    _refuse_overlapping_destinations(entries, where)
    source_root = _expect_str(raw["source_root"], f"{where}: source_root")
    if not source_root.startswith("/") or source_root.endswith("/"):
        raise ValueError(
            f"{where}: source_root must be the absolute path the distribution itself spells, "
            f"got {source_root!r}"
        )
    _expect_str(raw["source_root_citation"], f"{where}: source_root_citation")
    _expect_str(raw["destination_root"], f"{where}: destination_root")
    return DistributionSupplied(
        distribution=name,
        card_version=card_version,
        reviewed=reviewed,
        version=_expect_str(raw["version"], f"{where}: version"),
        source_root=source_root,
        entries=entries,
    )


def load_distribution_supplied(text: str | None = None) -> dict[str, DistributionSupplied]:
    """Load the packaged copy lists (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("distribution_supplied.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != SUPPLIED_SCHEMA:
        raise ValueError(
            f"distribution_supplied: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {SUPPLIED_SCHEMA})"
        )
    card_version = _expect_str(raw.get("version"), "distribution_supplied: version")
    reviewed = _expect_str(raw.get("reviewed"), "distribution_supplied: reviewed")
    distributions = raw.get("distributions", {})
    if not isinstance(distributions, dict):
        raise ValueError(
            f"distribution_supplied: distributions must be an object, got {distributions!r}"
        )
    return {
        name: _distribution(name, entry, card_version=card_version, reviewed=reviewed)
        for name, entry in distributions.items()
    }


_PACKAGED: dict[str, DistributionSupplied] | None = None


def lookup_distribution_supplied(distribution: str | None) -> DistributionSupplied | None:
    """The packaged copy list for one distribution, or ``None`` — no fuzzy matching."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_distribution_supplied()
    if distribution is None:
        return None
    return _PACKAGED.get(distribution)
