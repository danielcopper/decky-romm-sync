"""Save-memory records — which files RetroArch writes for a core, and why only those.

Two facts meet in a savefile's name, and they are different kinds of knowledge:

- **The name is the frontend's, and it is the same for every core.** RetroArch
  builds it from the content's own stem and appends one of exactly two
  extensions — ``.srm`` for ``RETRO_MEMORY_SAVE_RAM`` (``runloop.c:8720-8723``)
  and ``.rtc`` for ``RETRO_MEMORY_RTC``, derived from the first — registering
  both before any core is asked (``save.c:710-724`` at RetroArch a79435a,
  reached for all non-subsystem content at ``runloop.c:4461``). The other three ``RETRO_MEMORY_*`` ids never
  reach a file: they are read for cheats, achievements and netplay. That
  mapping is code here rather than data because nothing on a machine states it
  and no core can change it — the same standing as the patch-format order in
  :mod:`atlas.placement`.
- **Which of the two a core fills is the core's, and it is not readable here.**
  RetroArch writes a file only where the core answers with a pointer *and* a
  non-zero size (``save.c:480``), and a core answers that only once content is
  loaded. atlas never runs a game, so the claim comes from the core's own
  source at the revision the machine ships, and it is scoped to that revision
  the way every other version-bound claim in this package is.

**Records are keyed by core and system**, which is where this table differs
from every other one here. A core is not one behaviour: mGBA answers a Game
Boy cartridge's clock and a Game Boy Advance cartridge's not at all
(``libretro.c:2357-2370`` at mgba c758314), so a record spelled ``mgba`` alone
would have to be wrong about one of the two. Where the system is not known the
answer is not narrowed by guessing at it — an unkeyed lookup states nothing.

**Only memory handed to the frontend is in scope, which is what the name
means.** A file the core writes itself — Flycast's ``.bin`` VMUs, Beetle
Saturn's ``.bcr``/``.bkr``/``.smpc`` — never passes through these ids and can
never be recorded here; that is :mod:`atlas.oddities`'s half of the split, and
:func:`_memory_types` enforces the boundary by refusing every word but the two.

**A record is an upper bound, and the distinction is load-bearing.** Whether
*this* cartridge carries a battery or a clock is a fact about the game — mGBA
reads it out of the ROM header, gambatte off byte ``0x147`` — and no table can
hold it. What a record states is which files can occur at all: the candidate
set a caller has to look for, which is exactly the question a save-syncing
client asks and exactly what ``file_set`` was shaped to carry.

Facts in data, interpretation in code: this module only loads and indexes; the
resolver in :mod:`atlas.installations` decides when a record applies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ._data import packaged_text
from .placement import TEMPLATE_ROM_STEM
from .systems import known_systems

# Packaged-data schema version. The loader is strict for the same reason every
# other packaged-data loader here is: a malformed build fails loudly instead of
# resolving with knowledge nobody can place.
SAVE_MEMORY_SCHEMA = 1

# How a record's ``.so`` is spelled — the record key plus this suffix, exactly
# as :data:`atlas.oddities.SO_SUFFIX` and :data:`atlas.textures.SO_SUFFIX` spell
# it for the cards.
SO_SUFFIX = "_libretro.so"

# The two libretro memory ids that reach a file, and the extension RetroArch
# gives each. Ordered as RetroArch registers them (``save.c:715`` then
# ``:719``), so a stated file set carries the frontend's own order rather than
# a sort this module invented.
MEMORY_SAVE_RAM = "save_ram"
MEMORY_RTC = "rtc"
MEMORY_TYPES = (MEMORY_SAVE_RAM, MEMORY_RTC)
MEMORY_TYPE_EXTENSIONS: Mapping[str, str] = MappingProxyType(
    {MEMORY_SAVE_RAM: ".srm", MEMORY_RTC: ".rtc"}
)

# The template every standard name is built on is the placement's own
# ``<rom_stem>`` hole, imported rather than respelled — the rule the rule-card
# loader follows for the same token, and for a sharper reason here: the
# substitution is performed by the resolver against *placement's* copy, so a
# second spelling would not fail, it would leave the literal token in a name a
# ``declared`` answer states as the save's filename.


# This check exists verbatim in every packaged-data loader
# (:func:`atlas.oddities._expect_str`, :func:`atlas.textures._expect_str`,
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


def _memory_types(value: object, where: str) -> tuple[str, ...]:
    """The memory ids a core fills for this system — known, distinct, in frontend order.

    **An empty list is a claim, not a gap**, and it is the commonest one: most
    cores fill no id at all, so RetroArch writes them no save file. Recording
    that is the difference between *atlas established there is none* and *atlas
    has not looked* — the first is an answer, the second is silence, and an
    empty list is how the first is spelled. A system the record leaves out
    keeps the second meaning.

    What an empty list does **not** claim is that the content has no save. A
    core that ignores the interface may still write its own files, which is a
    rule card's question; the answer says so rather than reading "no ids" as
    "no save".

    The order is normalized to RetroArch's own so two records stating one fact
    cannot produce two different file sets.
    """
    listed = _expect_str_list(value, where)
    unknown = [name for name in listed if name not in MEMORY_TYPE_EXTENSIONS]
    if unknown:
        raise ValueError(
            f"{where}: {unknown!r} are not memory ids that reach a file "
            f"(known: {sorted(MEMORY_TYPE_EXTENSIONS)})"
        )
    if len(set(listed)) != len(listed):
        raise ValueError(f"{where}: a memory id is listed twice, got {listed!r}")
    return tuple(name for name in MEMORY_TYPES if name in set(listed))


@dataclass(frozen=True, slots=True)
class SystemMemory:
    """What one core fills for one system, pinned to the build it was read at.

    ``verified_core`` is the core's own ``library_version`` — for these cores
    that string names the very commit the binary was built from
    (``"0.11-dev c758314"``), which is what makes the citation checkable rather
    than merely plausible. It lives here and not in ``core_audit.json`` for the
    reason :class:`atlas.textures.AbsentSwitch` keeps its own: that record's
    version moves whenever a live round re-verifies a core's *placement*, and a
    bump for an unrelated reason would silently re-validate a claim nobody
    re-read the source for.
    """

    memory_types: tuple[str, ...]
    verified_core: str
    citation: str

    @property
    def file_templates(self) -> tuple[str, ...]:
        """The names RetroArch would write, as templates over the content's stem.

        Empty where the core fills no id — an established emptiness, which the
        answer states as a declared set of no files rather than as an unknown.
        """
        return tuple(
            f"{TEMPLATE_ROM_STEM}{MEMORY_TYPE_EXTENSIONS[name]}" for name in self.memory_types
        )

    @property
    def frontend_writes_nothing(self) -> bool:
        """Does RetroArch write no save file at all for this core and system?

        Named for what it is rather than left as ``not memory_types``: the
        resolver branches on it to state a caveat, and the emptiness carries a
        meaning a bare length check hides.
        """
        return not self.memory_types


@dataclass(frozen=True, slots=True)
class SaveMemoryRecord:
    """One core's save-memory record: which files it can produce, per system."""

    key: str
    library_names: tuple[str, ...]
    systems: Mapping[str, SystemMemory]
    provenance: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "systems", MappingProxyType(dict(self.systems)))

    @property
    def so_name(self) -> str:
        """The ``.so`` basename this record describes — the key plus the suffix."""
        return f"{self.key}{SO_SUFFIX}"

    def matches(self, *, so_basename: str | None, library_name: str | None) -> bool:
        if so_basename is not None and so_basename == self.so_name:
            return True
        return library_name is not None and library_name in self.library_names

    def for_system(self, system: str | None) -> SystemMemory | None:
        """This record's entry for *system*, or ``None`` where it states none.

        For an unnamed system this falls to :meth:`unanimous`, which answers
        only where every system the record covers writes the same files.
        """
        if system is None:
            return self.unanimous()
        return self.systems.get(system)

    def unanimous(self) -> SystemMemory | None:
        """The one answer this record gives for every system it covers, if it gives one.

        A record is keyed by core *and* system because one core is not one
        behaviour — but most cores are: 70 of the 72 shipped records state the
        same memory ids for every system they cover. The two that do not are
        mGBA, the record the key was designed around, and VBA-M, whose memory
        function switches on the loaded image type first. Where they agree, the
        answer holds whichever of those systems the content turns out to be,
        and stating it is a reading of the record rather than a guess about the
        content.

        Where they disagree this is ``None``, and the caller who wants an
        answer names the system. That is not a formality: mGBA answers a Game
        Boy cartridge's clock and a Game Boy Advance cartridge's not at all, so
        collapsing the two would be wrong for one of them.

        The claim is scoped to the systems the record covers. The resolver says
        so with ``file-set-across-systems``, because a core run for a system
        the record never names has established nothing here.
        """
        distinct = {entry.memory_types for entry in self.systems.values()}
        if len(distinct) != 1:
            return None
        return next(iter(self.systems.values()), None)


def _system_memory(value: object, where: str) -> SystemMemory:
    if not isinstance(value, dict) or set(value) != {"memory_types", "verified_core", "citation"}:
        raise ValueError(
            f"{where}: expected {{'memory_types': …, 'verified_core': …, 'citation': …}}, got {value!r}"
        )
    return SystemMemory(
        memory_types=_memory_types(value.get("memory_types"), f"{where}.memory_types"),
        verified_core=_expect_str(value.get("verified_core"), f"{where}.verified_core"),
        citation=_expect_str(value.get("citation"), f"{where}.citation"),
    )


def _record(key: str, entry: Any) -> SaveMemoryRecord:
    """One core's record — validated, never coerced."""
    where = f"save-memory record {key!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    identifiers = entry.get("identifiers", {})
    if "so" in identifiers:
        raise ValueError(
            f"{where}: identifiers.so is derived from the record key ({key + SO_SUFFIX!r}) and not "
            "read — a restated one could only ever disagree with it"
        )
    systems = entry.get("systems")
    if not isinstance(systems, dict) or not systems:
        raise ValueError(f"{where}: expected a non-empty 'systems' object, got {systems!r}")
    vocabulary = set(known_systems())
    unknown = sorted(name for name in systems if name not in vocabulary)
    if unknown:
        raise ValueError(
            f"{where}: {unknown!r} are not atlas system ids — a record is keyed by the same "
            "vocabulary every other question about a system takes"
        )
    return SaveMemoryRecord(
        key=key,
        library_names=_expect_str_list(
            identifiers.get("library_name", []), f"{where}: identifiers.library_name"
        ),
        systems={
            name: _system_memory(value, f"{where}: systems[{name!r}]")
            for name, value in systems.items()
        },
        provenance=_expect_str(
            entry.get("provenance", {}).get("source"), f"{where}: provenance.source"
        ),
    )


def load_save_memory(text: str | None = None) -> tuple[SaveMemoryRecord, ...]:
    """Load the packaged records (or *text* when supplied, for tests).

    Reading packaged data is not the machine seam — it is the library reading
    its own bundled world knowledge, which is exactly what these records are.
    """
    raw = json.loads(text if text is not None else packaged_text("save_memory.json"))
    if not isinstance(raw, dict) or raw.get("schema") != SAVE_MEMORY_SCHEMA:
        raise ValueError(
            f"save_memory: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {SAVE_MEMORY_SCHEMA})"
        )
    return tuple(_record(key, entry) for key, entry in raw.get("cores", {}).items())


_PACKAGED: tuple[SaveMemoryRecord, ...] | None = None


def lookup_save_memory(
    *, so_basename: str | None, library_name: str | None
) -> SaveMemoryRecord | None:
    """Find the packaged record matching a core, by ``.so`` name or ``library_name``."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_save_memory()
    for record in _PACKAGED:
        if record.matches(so_basename=so_basename, library_name=library_name):
            return record
    return None
