"""atlas's own system vocabulary — the ids every question about a system takes.

Those ids are **ES-DE's system names** — ``gb``, ``n64``, ``dreamcast`` —
because that is what a frontend catalogue on the machine declares, and a
resolver that reads the machine should speak the machine's names. The ids are
pinned to the ``es_systems.xml`` of a stated build (``data/system_ids.json``),
which is what makes membership checkable rather than an opinion: a name that
file does not declare is not an id, however plausible it looks.

What this module deliberately does **not** do is translate. Translation lives
in :mod:`atlas.platforms` and is split exactly along the boundary rule: which
public identities a *platform* has is cited world knowledge (the crosswalk
table), and which platform a *system* belongs to is read off the machine —
every catalogue system carries a ``<platform>`` tag, and the resolvers read it
live. What this file adds to that split is the snapshot column: the stated
build's platform tags per system (``platforms``), the fallback for a system
whose tags cannot be read live — a sealed catalogue, a system this
installation does not declare — always marked as vocabulary knowledge when
used, never passed off as a read.

:func:`known_systems` hands over the target set, :func:`from_esde_system` says
whether one name is in it. The failure they prevent is the expensive one: an
identifier no catalogue declares reaches a question, the question answers "no
emulator for that system", and a vocabulary mistake has been read as a fact
about the machine.
"""

from __future__ import annotations

import json

from ._data import packaged_text

# Packaged-data schema version, strict for the reason the whole loader is: a
# malformed build fails loudly instead of answering out of a list nobody can
# place. Schema 2 added the per-system platform tags of the same stated build.
SYSTEM_IDS_SCHEMA = 2


# This check exists verbatim three times, one per packaged-data loader
# (:func:`atlas.evidence._expect_str`, :func:`atlas.oddities._expect_str`). The
# triplication is the deliberate cost of keeping the loaders independent of each
# other: each reads its one file and shares no machinery with the other two, so
# a defect in one table can never fail the load of another — and a fidelity
# finding about what counts as a string belongs in all three.
def _expect_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _load_document(text: str | None) -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    """The whole packaged document, validated fail-closed: ids and platform tags."""
    if text is None:
        text = packaged_text("system_ids.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != SYSTEM_IDS_SCHEMA:
        raise ValueError(
            f"system_ids: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {SYSTEM_IDS_SCHEMA})"
        )
    ids = raw.get("systems")
    if not isinstance(ids, list) or not ids:
        raise ValueError("system_ids: 'systems' must be a non-empty list of canonical ids")
    systems = frozenset(_expect_str(name, "systems entry") for name in ids)
    if len(systems) != len(ids):
        raise ValueError("system_ids: 'systems' carries a duplicate id")
    tags = raw.get("platforms")
    if not isinstance(tags, dict) or frozenset(tags) != systems:
        raise ValueError("system_ids: 'platforms' must map exactly the ids in 'systems'")
    platforms: dict[str, tuple[str, ...]] = {}
    for system, tokens in tags.items():
        if not isinstance(tokens, list):
            raise ValueError(f"system_ids: platforms[{system!r}] must be a list of tags")
        platforms[system] = tuple(
            _expect_str(token, f"platforms[{system!r}] entry") for token in tokens
        )
    return systems, platforms


def load_system_ids(text: str | None = None) -> frozenset[str]:
    """Load the packaged id set (or *text* when supplied, for tests).

    Fail-closed, because every refusal here is a name a question could
    otherwise be asked about: an unreadable schema, an empty list, a non-string
    entry, a repeated id and a platform column that does not cover exactly the
    id set each fail the load rather than shipping a vocabulary whose own
    account of itself does not add up.
    """
    return _load_document(text)[0]


_SYSTEM_IDS: frozenset[str] | None = None
_PLATFORM_TAGS: dict[str, tuple[str, ...]] | None = None


def _document() -> tuple[frozenset[str], dict[str, tuple[str, ...]]]:
    global _SYSTEM_IDS, _PLATFORM_TAGS
    if _SYSTEM_IDS is None or _PLATFORM_TAGS is None:
        _SYSTEM_IDS, _PLATFORM_TAGS = _load_document(None)
    return _SYSTEM_IDS, _PLATFORM_TAGS


def _ids() -> frozenset[str]:
    return _document()[0]


def vocabulary_platform_tags(system: str) -> tuple[str, ...] | None:
    """The stated build's ``<platform>`` tags for *system*, or ``None`` off-vocabulary.

    The snapshot column, not a read: what the stated build's catalogue tags
    this system with. A resolver reaches for it only when the live tag cannot
    be read — a sealed catalogue, a system this installation does not declare —
    and marks the answer as vocabulary knowledge when it does.
    """
    return _document()[1].get(system)


def known_systems() -> tuple[str, ...]:
    """Every canonical system id, sorted — the vocabulary the questions take.

    The target set for a caller who maintains a mapping out of some other
    vocabulary: check yours against this and the day a name stops being an id
    is the day your own tests say so, instead of the day a question comes back
    empty for a system that is right there on the machine.
    """
    return tuple(sorted(_ids()))


def from_esde_system(name: str) -> str | None:
    """*name* if it is a canonical system id, else ``None``.

    ES-DE's names *are* the canonical ids, so this translates nothing — it
    answers "is this an id atlas can be asked about", which is the question a
    caller holding a name from anywhere else actually has. It is also the only
    honest way to check membership from outside: the id set is packaged data
    that moves with atlas releases, never a literal a client should carry.

    >>> from_esde_system("dreamcast")
    'dreamcast'
    """
    return name if name in _ids() else None
