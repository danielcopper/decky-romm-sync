"""Firmware — which emulator wants which file, where it goes, and what lies there.

Firmware knowledge splits exactly along atlas's boundary rule, and this module
is where the split is made visible:

- **Which files a core wants is on the machine.** RetroArch ships a ``.info``
  file next to every core, and it declares that core's firmware
  (``firmwareN_path`` / ``firmwareN_desc`` / ``firmwareN_opt``) and its
  ``systemname``. So the declarations are *read*, live, from the installation's
  own ``libretro_info_path`` — never from a shipped table that drifts against
  the cores actually installed.
- **What a correct file's bytes are is written nowhere on the machine.** The
  ``md5``/``sha1``/``size`` triple comes from libretro-database's
  ``System.dat``, and stays a packaged, versioned, source-cited lookup
  (``data/firmware_hashes.json``). "No hash known" is a normal state, not an
  edge case: ``System.dat`` covers only part of the firmware universe.

The model is **emulator-centric**, because a firmware requirement is a property
of an emulator and of nothing else::

    System            snes, dreamcast, gb
      └─ Emulator     mgba, sameboy, flycast   ← decides BOTH the demand AND the place
           ├─ need         required | optional
           ├─ path         absolute destination + expected file name
           └─ identity     expected bytes — judges what is actually lying there

The hash is not a level above the name: the *emulator* decides the name and the
path, the *hash* decides whether what sits there is the right thing. Two cores
on one machine routinely want the same bytes under different names — gambatte
asks for ``gb_bios.bin``, SameBoy for ``dmg_boot.bin``, and the packaged table
carries both under one md5. A byte-identical file under the *other* name does
**not** satisfy the requirement (SameBoy opens ``dmg_boot.bin`` and nothing
else), so ``missing`` stays ``missing`` — what the shared identity buys is a
better instruction: copy what you already have instead of downloading it.

Two axes, kept apart on purpose (:class:`FirmwareRequirement`):

- ``need`` — ``required`` or ``optional``, straight from ``firmwareN_opt``.
  It says what an emulator asks for, never what is on disk.
- ``present`` / ``checked`` — what the machine answers. ``checked`` keeps
  **four** values: ``verified`` and ``mismatch`` are results, ``unchecked``
  means the identity is known but verification was not asked for, and
  ``unknown`` means it cannot be established at all. "We did not look" and "we
  looked and cannot tell" must never collapse into one value.

The invariant that ties the two together: ``checked is None`` exactly when
``present is not True`` — nothing at the destination, or a look that did not
happen, leaves nothing to check.

*Unknown* is deliberately not a ``need``. Having no declaration to check
against is a property of the answer, not of a file, so it is a
:class:`~atlas.placement.Caveat` attached to an answer whose requirement list
is **empty**. A caller that ignores caveats then gets an empty list rather than
a satisfied one: empty is honest, "nothing missing" would be a lie. Which
caveat says *which kind* of empty this is, and there are three
(:func:`_empty_answer_caveat`): :data:`CAVEAT_NO_FIRMWARE_DECLARATION` —
nothing declares it and that was read; :data:`CAVEAT_NO_FIRMWARE_REQUIREMENT` —
declarations exist and none became a requirement;
:data:`CAVEAT_FIRMWARE_DECLARATION_UNKNOWN` — what is declared could not be
established at all.
"""

from __future__ import annotations

import json
import os
import tomllib
import re
from dataclasses import dataclass
from glob import escape as _glob_escape
from typing import Any, Literal, Mapping, Protocol, cast

from ._data import packaged_text
from .core_info import (
    UNREAD_EMPTY,
    UNREAD_NO_SLOT,
    UNREAD_UNCOUNTED,
    FirmwareSlot,
    enumerate_firmware,
    parse_core_info,
    unread_reason,
)
from .esde import KIND_LIBRETRO
from .machine import (
    DIGEST_MD5,
    DIGEST_SHA1,
    KIND_DIRECTORY,
    KIND_FILE,
    KIND_INACCESSIBLE,
    KIND_MISSING,
    READ_MISSING,
    READ_OK,
    SYMLINK_HOPS,
    Machine,
    PathKind,
    ReadStatus,
)
from . import duckstation, emulator_settings, melonds, qt_ini
from .oddities import SaveMode, load_oddities
from .standalone_firmware import (
    StandaloneFirmwareCard,
    StandaloneFirmwareConfigFile,
    StandaloneFirmwareSearch,
    lookup_standalone_firmware_card,
)
from .placement import (
    CAVEAT_CORE_MODE_UNESTABLISHED,
    CAVEAT_SANDBOX_PATH_UNTRANSLATED,
    HOLE_CWD,
    ROOT_SYSTEM_DIRECTORY,
    TEMPLATE_CWD,
    UNRESOLVED_CORE_NOT_INSTALLED,
    UNRESOLVED_EMULATOR_CONFIG_UNREADABLE,
    UNRESOLVED_STANDALONE,
    Caveat,
)
from .retroarch_cfg import cfg_uint


class SandboxTranslation(Protocol):
    """How an emulator's own absolute config value reads from this host.

    A Flatpak emulator writes its configuration from inside its sandbox, so a
    ``/var/config/PCSX2/bios`` in it is a path only the app can open — the
    host reads the same directory under ``~/.var/app/<id>/config``. The save
    route has always translated; the firmware route read such values as host
    paths and reported the BIOS missing.

    Declared here as the one method this route needs, and satisfied by the
    installation handles' own sandbox: the translation is arrangement
    knowledge, which lives a layer above this module and must not be
    reimplemented in it.
    """

    def translate(self, path: str) -> str | None:
        """*path* as this host reads it, or ``None`` where no host path exists."""
        ...


FirmwareNeed = Literal["required", "optional"]

NEED_REQUIRED: FirmwareNeed = "required"
NEED_OPTIONAL: FirmwareNeed = "optional"

FIRMWARE_NEEDS = ("required", "optional")

FirmwareChecked = Literal["verified", "mismatch", "unchecked", "unknown"]

CHECKED_VERIFIED: FirmwareChecked = "verified"
CHECKED_MISMATCH: FirmwareChecked = "mismatch"
CHECKED_UNCHECKED: FirmwareChecked = "unchecked"
CHECKED_UNKNOWN: FirmwareChecked = "unknown"

FIRMWARE_CHECKED = ("verified", "mismatch", "unchecked", "unknown")

# Caveat codes — stable identifiers, like the placement ones.
# The three below are the answer-level vocabulary for an empty requirement
# list, and they are three because an empty answer has three reasons: nothing
# is declared, something is declared that never became a requirement, or atlas
# could not establish what is declared at all. One code for all three would let
# a read that did not happen read as "nothing needed" (:func:`_empty_answer_caveat`).
CAVEAT_NO_FIRMWARE_DECLARATION = "no-firmware-declaration"
CAVEAT_NO_FIRMWARE_REQUIREMENT = "no-firmware-requirement"
CAVEAT_FIRMWARE_DECLARATION_UNKNOWN = "firmware-declaration-unknown"
# The entry's declaration is atlas's packaged card, not a machine read — the
# provenance statement every ``packaged`` entry carries, so the distinction
# between "the emulator declares" and "atlas's card states" is never lost in
# a requirement list that looks like any other.
CAVEAT_FIRMWARE_PACKAGED_DECLARATION = "firmware-packaged-declaration"
# The emulator boots on a built-in BIOS/firmware replacement while its
# external-BIOS switch is off — melonDS's compiled default — so no external
# file is probed and an empty requirement list is the true answer. Stated so
# a checker never reads "nothing required" as "nothing configurable": the
# data names the switch whose flip makes the external set required.
CAVEAT_FIRMWARE_BUILTIN_REPLACEMENT = "firmware-builtin-replacement"
# One fact, one code on both routes, the same sharing as the two below: the
# save route refuses an emulator whose governing configuration exists and
# cannot be read with this word, and the firmware route states the same read
# failure as a caveat on an ``unreadable`` declaration.
CAVEAT_EMULATOR_CONFIG_UNREADABLE = UNRESOLVED_EMULATOR_CONFIG_UNREADABLE
CAVEAT_INFO_PATH_UNRESOLVED = "info-path-unresolved"
CAVEAT_CORE_DIR_UNRESOLVED = "core-dir-unresolved"
CAVEAT_FIRMWARE_ROOT_MISSING = "firmware-root-missing"
# The same two-route sharing as the standalone code below, in the same
# direction: the save routes answer a core this installation does not have with
# the typed Unresolved outcome, this route with a caveat, and both spell it the
# same word.
CAVEAT_CORE_NOT_INSTALLED = UNRESOLVED_CORE_NOT_INSTALLED
# One fact, one code on both routes: the placement route answers a standalone
# emulator with the typed Unresolved outcome, the firmware route with this
# caveat, and a client that learned the word on one route reads the other.
CAVEAT_STANDALONE_UNSUPPORTED = UNRESOLVED_STANDALONE
CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE = "emulator-catalogue-unavailable"
CAVEAT_FIRMWARE_UNREADABLE = "firmware-unreadable"
CAVEAT_FIRMWARE_CONTENT_UNIDENTIFIED = "firmware-content-unidentified"
CAVEAT_SYSTEM_UNKNOWN = "system-unknown"
# The marked-word code: a requirement's ``system`` is one of atlas's own
# spellings (:data:`SYSTEMS_WITHOUT_CATALOGUE_ID`) because no ES-DE build
# declares an id for that machine. The word is exported anyway — refusing to
# answer would hide real firmware — and this code is what keeps it from being
# read as a catalogue id. Same form as ``_unknown``: a token no catalogue
# declares, plus a structured caveat saying why.
CAVEAT_SYSTEM_NOT_IN_CATALOGUE = "system-not-in-catalogue"
CAVEAT_SYSTEM_ASSIGNMENT_DERIVED = "system-assignment-derived"
CAVEAT_CORE_WITHOUT_SYSTEMNAME = "core-without-systemname"
CAVEAT_SYSTEM_ASSIGNMENT_MAY_HIDE_CORES = "system-assignment-may-hide-cores"
CAVEAT_CORE_INFO_UNREADABLE = "core-info-unreadable"
CAVEAT_EMULATOR_CATALOGUE_UNREADABLE = "emulator-catalogue-unreadable"
# Four ways to answer from no full catalogue, and they are four different
# claims: the arrangement has none (unavailable), it has one that could not be
# read (unreadable), atlas has not established where this arrangement keeps
# one (unestablished), and part of its catalogue sits where atlas does not
# open — EmuDeck's ES-DE bundles its es_systems.xml inside the AppImage
# (ES-DE INSTALL.md v3.4.1:1470), so only the on-disk layers were read and
# the enumeration is incomplete (sealed). Only unavailable and unreadable say
# anything about the machine; the other two are about atlas, and a client
# must not read either as an absence. Sealed is also the one of the four that
# may accompany real entries: what the readable layers declare is stated, and
# the caveat says the frontend may declare more.
CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED = "emulator-catalogue-unestablished"
CAVEAT_EMULATOR_CATALOGUE_SEALED = "emulator-catalogue-sealed"
# The fifth family member is not a degradation at all: the custom
# es_systems.xml can declare itself the whole catalogue with a document-level
# <loadExclusive/> (ES-DE INSTALL.md v3.4.1:1466; honored in
# SystemData::loadConfig, SystemData.cpp:858-895), and then the bundled layer
# is not loaded — by the frontend and by atlas alike. The enumeration such an
# answer carries is *complete*; the code states why it is as small as it is.
CAVEAT_EMULATOR_CATALOGUE_EXCLUSIVE = "emulator-catalogue-exclusive"
CAVEAT_FIRMWARE_PATH_OBSTRUCTED = "firmware-path-obstructed"
CAVEAT_FIRMWARE_PATH_INACCESSIBLE = "firmware-path-inaccessible"
CAVEAT_FIRMWARE_SCAN_INCOMPLETE = "firmware-scan-incomplete"
CAVEAT_CORE_ENUMERATION_INCOMPLETE = "core-enumeration-incomplete"
CAVEAT_FIRMWARE_PATH_ESCAPES_ROOT = "firmware-path-escapes-root"
CAVEAT_FIRMWARE_PATH_UNRESOLVABLE = "firmware-path-unresolvable"
CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE = "firmware-path-names-no-file"
# The configured value is relative and the emulator resolves it against the
# working directory of its own process (xemu opens every ``[sys.files]`` value
# verbatim with plain fopen/access, system/vl.c:2527-2535, :2918 with
# osdep.h:645-653 at v0.8.135) — a property of the launch, not of the machine,
# so no destination exists for atlas to observe. The placement families state
# the same situation as a ``working_directory`` root with the ``<cwd>`` hole;
# a firmware requirement's ``path`` is contractually the absolute observed
# destination, so here the file stays out of the requirement list and this
# caveat carries the anchor as data instead: the key, the declared value, and
# the ``<cwd>``-templated path the launcher's working directory completes.
# Distinct from ``firmware-root-unusable`` (a relative *root* refusing every
# declaration resolved against it) and from ``firmware-path-names-no-file``
# (a value naming no file at all): this value names its file perfectly well —
# where it lands is what only the launch decides.
CAVEAT_FIRMWARE_PATH_LAUNCH_DEPENDENT = "firmware-path-launch-dependent"
# Distinct from CAVEAT_FIRMWARE_ROOT_MISSING, which says the root is not there:
# this one says the configured value cannot name a place at all, so no
# declaration can be resolved against it however well the file exists.
CAVEAT_FIRMWARE_ROOT_UNUSABLE = "firmware-root-unusable"
CAVEAT_FIRMWARE_DECLARATION_UNREAD = "firmware-declaration-unread"
CAVEAT_FIRMWARE_CONTENT_CONTRADICTORY = "firmware-content-contradictory"
# The third member of the identification family: the table does not know this
# content (unidentified), the fields describe no single file (contradictory),
# or — this one — the request never named content at all.
CAVEAT_FIRMWARE_CONTENT_UNSTATED = "firmware-content-unstated"

# The search family: an emulator that names no file, only a directory to look
# in. All three are statements about what a *content* read did or did not
# establish, which is the only thing such an answer can rest on.
# The bytes at this path are a row of the emulator's own recognition table, so
# this is the image it would boot — the positive counterpart of
# CAVEAT_FIRMWARE_CONTENT_UNIDENTIFIED, and stated rather than left implicit
# because a caller cannot re-derive it without the table.
CAVEAT_FIRMWARE_IMAGE_IDENTIFIED = "firmware-image-identified"
# Two or more images rank exactly alike and the emulator keeps whichever one
# the directory hands it last — an order no read reproduces, so which of them
# boots is not established.
CAVEAT_FIRMWARE_IMAGE_AMBIGUOUS = "firmware-image-ambiguous"
# Files of an accepted size are there and nothing was hashed, so whether any
# of them is a BIOS is unanswered — the degradation of asking a content
# question without asking for a content check.
CAVEAT_FIRMWARE_SEARCH_UNVERIFIED = "firmware-search-unverified"

# The two ``.info`` files libretro ships as templates rather than as cores:
# both declare firmware0_path = "filename.ext" with opt = "true/false". The
# offline generator dropped them implicitly (no core ever matched); a live
# reader has to say so.
TEMPLATE_INFO_STEMS = ("00_example_libretro", "puzzlescript_libretro")


@dataclass(frozen=True, slots=True)
class FirmwareHash:
    """One firmware file's identity, as libretro-database's ``System.dat`` states it.

    ``name`` is the key ``System.dat`` uses, verbatim: usually a bare file name
    (``scph5501.bin``) but sometimes a relative path (``dc/dc_boot.bin``) — the
    upstream data mixes both, so the table does too rather than normalizing
    away information it does not own.
    """

    name: str
    md5: str
    sha1: str
    size: int


@dataclass(frozen=True, slots=True)
class FirmwareIdentity:
    """What one firmware content *is*: its bytes, and every name it goes by.

    ``known_as`` is the alias set — every key in the packaged table carrying
    exactly these bytes, the queried name included. 18 of the table's 369
    distinct contents are known under more than one name (``dmg_boot.bin`` ≡
    ``gb_bios.bin``, ``dc/boot.bin`` ≡ ``dc/dc_boot.bin``, …), which is what
    makes "you already have these bytes, under another name" a statable answer.
    """

    md5: str
    sha1: str
    size: int
    known_as: tuple[str, ...] = ()


class FirmwareHashes:
    """Read-only view over the packaged hash table — by name and by content."""

    def __init__(self, files: dict[str, FirmwareHash], meta: dict[str, Any]) -> None:
        self._files = files
        self._meta = meta
        contents: dict[tuple[str, str, int], list[str]] = {}
        for name, entry in files.items():
            contents.setdefault(_content_key(entry.md5, entry.sha1, entry.size), []).append(name)
        self._contents = {key: tuple(sorted(names)) for key, names in contents.items()}

    @property
    def meta(self) -> dict[str, Any]:
        """The table's ``_meta`` block (upstream source, version, generation date)."""
        return dict(self._meta)

    def names(self) -> tuple[str, ...]:
        """Every name the table has an identity for, sorted."""
        return tuple(sorted(self._files))

    def get(self, name: str) -> FirmwareHash | None:
        """The identity stored under *name* exactly, or ``None``."""
        return self._files.get(name)

    def _identity(self, entry: FirmwareHash) -> FirmwareIdentity:
        return FirmwareIdentity(
            md5=entry.md5,
            sha1=entry.sha1,
            size=entry.size,
            known_as=self._contents[_content_key(entry.md5, entry.sha1, entry.size)],
        )

    def for_path(self, path: str) -> FirmwareIdentity | None:
        """The identity expected at a declared path — matched by path, then base name.

        Upstream keys 91 of its entries by a relative path and the rest by a
        bare file name, with no base name shared between the two forms, so
        trying both is unambiguous. ``None`` is a normal, expected answer:
        ``System.dat`` covers only part of the firmware universe.
        """
        entry = self._files.get(path) or self._files.get(os.path.basename(path))
        return None if entry is None else self._identity(entry)

    def for_content(
        self, *, md5: str | None = None, sha1: str | None = None, size: int | None = None
    ) -> FirmwareIdentity | None:
        """The identity of *content*, matched by every field the caller supplies.

        Matching is by bytes, not by name, so it answers the download flow:
        whatever the file is called where it came from, its content says what it
        is and :attr:`FirmwareIdentity.known_as` says what to call it here.
        Every supplied field must agree — a caller that knows both digests gets
        no match when they disagree, rather than a coin-flip.
        """
        if md5 is None and sha1 is None:
            raise ValueError("for_content: at least one of md5/sha1 is needed — size alone is not an identity")
        for (entry_md5, entry_sha1, entry_size), names in self._contents.items():
            if md5 is not None and entry_md5 != md5.lower():
                continue
            if sha1 is not None and entry_sha1 != sha1.lower():
                continue
            if size is not None and entry_size != size:
                continue
            return self._identity(self._files[names[0]])
        return None

    def contradicts_itself(
        self, *, md5: str | None = None, sha1: str | None = None, size: int | None = None
    ) -> bool:
        """Did :meth:`for_content` miss because the *fields* disagree, not the content?

        A caller passing an md5 and a sha1 from two different files gets no
        match — and reporting that as "unknown content" points them at the
        table when the problem is the request.

        The question is asked of the **digests**, because they alone identify:
        when the table knows a supplied md5 or sha1 and the full request still
        matches nothing, the entry that digest names carries a different value
        for something else the caller supplied. That is the caller's request
        disagreeing with itself, whether the field that disagrees is the other
        digest or the size — a size the table pairs with no entry at all is
        exactly the case, and asking "does any entry have this size" instead
        answered ``False`` there and blamed the table for an md5 it knows
        perfectly.

        Unknown digests are not a contradiction: content the table does not
        cover is a normal answer, and a size that happens to match some entry
        says nothing about it.
        """
        if sum(value is not None for value in (md5, sha1, size)) < 2:
            return False
        if md5 is not None and self.for_content(md5=md5) is not None:
            return True
        return sha1 is not None and self.for_content(sha1=sha1) is not None


def _content_key(md5: str, sha1: str, size: int) -> tuple[str, str, int]:
    return (md5.lower(), sha1.lower(), size)


def _hash_from_raw(name: str, raw: dict[str, Any]) -> FirmwareHash:
    md5, sha1, size = raw.get("md5"), raw.get("sha1"), raw.get("size")
    if not isinstance(md5, str) or not isinstance(sha1, str) or not isinstance(size, int):
        raise ValueError(f"{name}: an entry must carry string 'md5'/'sha1' and integer 'size'")
    return FirmwareHash(name=name, md5=md5, sha1=sha1, size=size)


def load_hashes(text: str | None = None) -> FirmwareHashes:
    """Load the packaged hash table (or *text* when supplied, for tests).

    With no argument the bundled ``data/firmware_hashes.json`` is read from the
    installed package. This is the one firmware read that does **not** go
    through the machine seam, and deliberately so: it is the library reading
    its own bundled world knowledge. Everything about *which* files are wanted
    comes from the machine instead (:func:`read_core_declarations`).
    """
    if text is None:
        text = packaged_text("firmware_hashes.json")
    data = json.loads(text)
    raw_files: dict[str, dict[str, Any]] = data.get("files", {})
    files = {name: _hash_from_raw(name, raw) for name, raw in raw_files.items()}
    return FirmwareHashes(files, data.get("_meta", {}))


# Libretro ``systemname`` strings mapped into atlas's one system vocabulary —
# ES-DE's ids, the same names every other question takes (``atlas.systems``,
# pinned to RetroDECK 0.10.9b's linux ``es_systems.xml``). World knowledge: the
# strings are read live from the machine, but what they *mean* is written
# nowhere on it, so the table is versioned and source-cited like the packaged
# data files it stands beside. Multiple variants map to one id because
# libretro's naming changed over the years.
#
# Two guards hold the table to its target: every value is an ES-DE id or a
# declared entry of :data:`SYSTEMS_WITHOUT_CATALOGUE_ID`, and the evidence
# joins recorded for the re-pointed entries are re-run against the deployed
# ``es_systems.xml`` (tests/test_firmware.py). Citations below name that file
# by line and the shipped ``.info`` files under the Flatpak's
# ``retroarch/rd_extras/cores/``.
#
# One thing this table never claims is a launch: a core's ``.info`` firmware
# list is display knowledge — at the RetroArch pin (``a79435a``, see
# docs/research/retrodeck-save-placement.md) it is evaluated only by display
# surfaces (``menu_displaylist.c:880`` and ``ui_qt.cpp:1238``, of which the
# Qt-less binary RetroDECK ships reaches only the first), and
# ``task_content.c``/``runloop.c`` reference none of it — so filing a file
# under an id says which system's firmware it is, never that some launch
# checks for it.
SYSTEMNAME_MAP_VERSION = "2"  # "1" was the unversioned libretro-slug generation
SYSTEMNAME_MAP_REVIEWED = "2026-08-09"

SYSTEMNAME_TO_SLUG: Mapping[str, str] = {
    # PlayStation
    "Sony - PlayStation": "psx",
    "PlayStation": "psx",
    "Sony PlayStation 2": "ps2",
    "PSP": "psp",
    # Sega. es_systems.xml:616 "dreamcast" (Sega Dreamcast) launches
    # flycast_libretro (:620); flycast_gles2 and retrodream carry the same
    # systemname without a catalogue launch under it.
    "Sega - Dreamcast": "dreamcast",
    "Sega Dreamcast": "dreamcast",
    "Sega - Saturn": "saturn",
    "Saturn": "saturn",
    "Sega - Mega-CD - Sega CD": "segacd",
    # es_systems.xml:1102 "mastersystem" (Sega Master System) launches
    # gearsystem_libretro and smsplus_libretro; emux_sms carries "Sega Master
    # System" too, without a catalogue launch under it. Ruled over "mark3"
    # (:1085), the same hardware's JP id — one id for the hardware, the J
    # BIOS dump included.
    "Sega - Master System - Mark III": "mastersystem",
    "Sega Master System": "mastersystem",
    "Sega 8-bit": "mastersystem",
    "Sega 8-bit (MS/GG/SG-1000)": "mastersystem",
    # es_systems.xml:810 "gamegear" (Sega Game Gear); its command :814
    # launches genesis_plus_gx — the core whose bios.gg the override files
    # here — alongside gearsystem (:816) and smsplus (:817).
    "Sega - Game Gear": "gamegear",
    "Sega - Mega Drive - Genesis": "genesis",
    "Sega Genesis": "genesis",
    "Sega 8/16-bit (Various)": "genesis",
    "Sega 8/16-bit + 32X (Various)": "genesis",
    # Nintendo
    "Nintendo - Game Boy": "gb",
    "Nintendo - Game Boy Color": "gbc",
    "Game Boy/Game Boy Color": "gb",
    "Nintendo - Game Boy Advance": "gba",
    "Game Boy Advance": "gba",
    "Game Boy/Game Boy Color/Game Boy Advance": "gba",
    "Nintendo - Nintendo DS": "nds",
    "Nintendo DS": "nds",
    "Nintendo - Famicom Disk System": "fds",
    "Nintendo - Nintendo Entertainment System": "nes",
    "Nintendo Entertainment System": "nes",
    "Nintendo 64": "n64",
    "Super Nintendo Entertainment System": "snes",
    "Super Nintendo Entertainment System / Game Boy / Game Boy Color": "snes",
    "GameCube / Wii": "gc",
    # Atari. es_systems.xml:301 "atarilynx" (Atari Lynx) launches all three
    # shipped carriers of "Lynx" (handy, holani, mednafen_lynx).
    "Atari - Lynx": "atarilynx",
    "Lynx": "atarilynx",
    "Atari - 5200": "atari5200",
    "Atari 5200": "atari5200",
    "Atari - 7800": "atari7800",
    "Atari 7800": "atari7800",
    "Atari - 8-bit": "atari800",
    "Atari 8-bit Family": "atari800",
    "Atari - ST": "atarist",
    "Atari ST/STE/TT/Falcon": "atarist",
    # NEC. Every carrier of these systemnames that declares firmware declares
    # exactly the CD system cards (syscard*.pce, gexpress.pce — the
    # mednafen_pce, mednafen_pce_fast and mednafen_supergrafx .info files;
    # geargrafx carries "PC Engine/SuperGrafx" and declares none), so the
    # ruled id is what the files are for: es_systems.xml:1600 "pcenginecd"
    # (NEC PC Engine CD), launching mednafen_pce and mednafen_pce_fast.
    # supergrafx launches mednafen_supergrafx (:2071), mednafen_pce (:2072)
    # and geargrafx (:2073); tg16 launches mednafen_pce (:2142),
    # mednafen_pce_fast (:2143) and mednafen_supergrafx (:2144).
    "NEC - PC Engine - TurboGrafx 16": "pcenginecd",
    "PC Engine/PCE-CD": "pcenginecd",
    "PC Engine SuperGrafx": "pcenginecd",
    "PC Engine/SuperGrafx": "pcenginecd",
    "PC Engine/SuperGrafx/CD": "pcenginecd",
    "PC-FX": "pcfx",
    "PC-98": "pc98",
    # SNK
    "SNK - Neo Geo": "neogeo",
    "Neo Geo": "neogeo",
    "SNK Neo Geo CD": "neogeocd",
    # Commodore
    "Commodore - Amiga": "amiga",
    "Amiga": "amiga",
    "C64": "c64",
    "C64 SuperCPU": "c64",
    "C64DTV": "c64",
    # No ES-DE build declares a c128 id; the catalogue files the C128 under
    # c64 itself — es_systems.xml:354 "c64" launches vice_x128 (:362) — and
    # vice_x128's own database says "Commodore - 64".
    "C128": "c64",
    # NOT the Commodore: ep128emu_core's systemname, and its firmware descs
    # say what it is ("exos21.rom (Enterprise 128 Expandible OS 2.1)", …).
    # No catalogue id exists — SYSTEMS_WITHOUT_CATALOGUE_ID entry "ep128".
    "128": "ep128",
    # Other
    "Coleco - ColecoVision": "colecovision",
    "ColecoVision": "colecovision",
    "ColecoVision/CreatiVision/My Vision": "colecovision",
    "Microsoft - MSX": "msx",
    "MSX": "msx",
    "MSX/SVI/ColecoVision/SG-1000": "msx",
    "Amstrad - CPC": "amstradcpc",
    "The 3DO Company - 3DO": "3do",
    "3DO": "3do",
    "Magnavox - Odyssey2": "odyssey2",
    "Magnavox Odyssey2 / Philips Videopac+": "odyssey2",
    # es_systems.xml:368 "cdimono1" (Philips CD-i) launches both shipped
    # carriers, same_cdi (:372) and cdi2015 (:373) — whose required file is
    # literally cdimono1.zip.
    "CD-i": "cdimono1",
    "CDi": "cdimono1",
    "Intellivision": "intellivision",
    "DOS": "dos",
    "PC-8000 / PC-8800 series": "pc88",
    "Sharp X1": "x1",
    "Sharp X68000": "x68000",
    "Pokemon Mini": "pokemini",
    # [D] — the one semantic join: es_systems.xml:1032 "macintosh" (Apple
    # Macintosh) launches standalone MAME macse/macplus, not minivmac, but
    # both sides are the 68k Macintosh line (minivmac_libretro.info requires
    # MacII.ROM). Only plausible id; no launching-core witness.
    "Mac68k": "macintosh",
    # Elektronika BK-0010/0011M — no catalogue id exists;
    # SYSTEMS_WITHOUT_CATALOGUE_ID entry "bk" (bk_libretro.info).
    "BK-0010/BK-0011(M)": "bk",
    # TI-83 calculators (numero_libretro.info: ti83.rom/ti83plus.rom/
    # ti83se.rom) — no catalogue id exists; ES-DE's "ti99" is the TI-99/4A
    # home computer, a different machine. SYSTEMS_WITHOUT_CATALOGUE_ID "ti83".
    "TI83": "ti83",
    "Super Cassette Vision": "scv",
    "FreeChaF": "channelf",
    "Vircon32": "vircon32",
    # es_systems.xml:1526 "palm" (Palm OS) launches mu_libretro (:1530), the
    # sole shipped carrier.
    "Palm OS": "palm",
    "CP System I/II": "cps",
    # Multi-system / game engines. es_systems.xml:155 "arcade" launches the
    # carriers of "Arcade (various)" (fbneo, the mame variants, dice) — the
    # umbrella id the catalogue itself files these cores under; per-board
    # refinements are per-file override work.
    "Arcade (various)": "arcade",
    # "Game engine" is scummvm_libretro's systemname and nothing else's;
    # es_systems.xml:1848 "scummvm" launches it (:1852).
    "Game engine": "scummvm",
    # es_systems.xml:1653 "ports" launches ecwolf_libretro (:1659) — the
    # catalogue's own filing, bucket though it is.
    "Wolfenstein 3D Game Engine": "ports",
    # "RPG Maker XP/VX/VX Ace Game Engine" is deliberately unmapped: no
    # shipped .info carries it, so it files mechanically like any other
    # systemname this table does not know.
    "J2ME": "j2me",
    "Java ME": "j2me",
    "ZX Spectrum (various)": "zxspectrum",
}

# Systems that are real on the machine and absent from the catalogue: the
# pinned ES-DE build declares no id for them (guard-tested against the
# deployed es_systems.xml — nothing there spells them, commented blocks
# included). atlas answers with its own spelling rather than hiding real
# firmware, and every answer that uses one carries
# :data:`CAVEAT_SYSTEM_NOT_IN_CATALOGUE` so the word cannot be read as a
# catalogue id. Keys are the spellings; values name the machine for the
# caveat's prose. Same output form as ``_unknown``: a token no catalogue
# declares, plus a structured caveat saying why — only the fact differs
# (here the catalogue lacks the word; there the core states no systemname).
SYSTEMS_WITHOUT_CATALOGUE_ID: Mapping[str, str] = {
    # bk_libretro.info: systemname "BK-0010/BK-0011(M)", eight bk/*.ROM dumps.
    "bk": "Elektronika BK-0010/0011M",
    # numero_libretro.info: ti83.rom / ti83plus.rom / ti83se.rom.
    "ti83": "Texas Instruments TI-83 calculators",
    # ep128emu_core_libretro.info: systemname "128", firmware descs
    # "Enterprise 128 Expandible OS", "Enterprise 64 BASIC", … — the spelling
    # follows the emulator family's own name (ep128emu).
    "ep128": "Enterprise 64/128",
}

# Per-file system overrides for multi-system cores. A core covering several
# systems carries one ``systemname``, but its firmware belongs to different
# systems — mGBA declares the Game Boy boot ROMs under "Game Boy/Game Boy
# Color/Game Boy Advance".
#
# Evidence level [D], derived — **not** [V]. Provenance: each entry is atlas's
# reading of which machine a dump belongs to, cross-read against the platform
# keys in RomM's ``backend/models/fixtures/known_bios_files.json``. There is no
# upstream source that states this per file: libretro's ``.info`` carries one
# ``systemname`` for the whole core and nothing per firmware entry, and
# ``System.dat`` keys identities by name without a system. The disagreements are
# real and this table takes a side: the Super Game Boy dumps (``SGB1.sfc``,
# ``sgb_bios.bin``, …) are filed under ``snes`` here because the cartridge runs
# in an SNES, while RomM files the same bytes under ``super-gb``.
#
# Deliberately incomplete, and not to be completed by hand — the set of boot-ROM
# variants grows with every core release (SkyEmu alone adds ``dmg_rom.bin``,
# ``dmg0_rom.bin``, ``cgb0_boot.bin``, ``cgb_agb_boot.bin``). A declaration this
# table does not cover falls back to the core's ``systemname``, and where that
# fallback can be wrong the answer says so — see
# :func:`system_assignment_caveats`. Visible beats silent.
#
# Entries are added only where the file's machine is not in question, and each
# addition shrinks how often that caveat has to fire. Versioned like the
# packaged data files it stands beside.
FIRMWARE_SYSTEM_OVERRIDE_VERSION = "3"
FIRMWARE_SYSTEM_OVERRIDE_REVIEWED = "2026-08-09"

FIRMWARE_SYSTEM_OVERRIDE: Mapping[str, str] = {
    # Game Boy family (mGBA, VBA-M, Mesen-S, Gambatte, SameBoy, …)
    "gb_bios.bin": "gb",
    "dmg_boot.bin": "gb",
    "gbc_bios.bin": "gbc",
    "cgb_boot.bin": "gbc",
    "sgb_bios.bin": "snes",
    "sgb_boot.bin": "snes",
    "sgb2_boot.bin": "snes",
    "SGB1.sfc": "snes",
    "SGB2.sfc": "snes",
    # Sega CD (Genesis Plus GX, PicoDrive)
    "bios_CD_E.bin": "segacd",
    "bios_CD_U.bin": "segacd",
    "bios_CD_J.bin": "segacd",
    # Master System (Genesis Plus GX). One id for the hardware, the J dump
    # included — ruled mastersystem, not mark3.
    "bios_E.sms": "mastersystem",
    "bios_U.sms": "mastersystem",
    "bios_J.sms": "mastersystem",
    # Game Gear (Genesis Plus GX): "bios.gg (GameGear BIOS)" by its own desc.
    "bios.gg": "gamegear",
    # ColecoVision BIOS declared by smsplus under systemname "Sega 8-bit"
    # (smsplus_libretro.info firmware1: "BIOS.col (Colecovision BIOS)") —
    # without this rule it files under mastersystem.
    "BIOS.col": "colecovision",
    # Flycast's arcade boards. The .info files all of these under systemname
    # "Sega Dreamcast", but each desc names its board (flycast_libretro.info
    # firmware1-7), and the catalogue launches Flycast under the boards' own
    # ids (es_systems.xml:1349 naomi, :1360 naomi2, :333 atomiswave).
    "naomi.zip": "naomi",  # "dc/naomi.zip (Naomi Bios from MAME)"
    "naomi2.zip": "naomi2",  # "dc/naomi2.zip (Naomi2 Bios from MAME)"
    "hod2bios.zip": "naomi",  # "(Naomi The House of the Dead 2 Bios from MAME)"
    "f355dlx.zip": "naomi",  # "(Naomi Ferrari F355 Challenge deluxe Bios from MAME)"
    "f355bios.zip": "naomi",  # "(Naomi Ferrari F355 Challenge twin/deluxe Bios from MAME)"
    "airlbios.zip": "naomi",  # "(Naomi Airline Pilots deluxe Bios from MAME)"
    "awbios.zip": "atomiswave",  # "dc/awbios.zip (Atomiswave BIOS from MAME)"
    # SNES enhancement chips, declared by every bsnes variant and by Snes9x.
    # These are cartridge coprocessor ROMs — they exist only inside SNES
    # cartridges, so the machine is not in question. Ten installed bsnes
    # variants used to carry a derived-assignment caveat for these alone.
    "dsp1.data.rom": "snes",
    "dsp1.program.rom": "snes",
    "dsp1b.data.rom": "snes",
    "dsp1b.program.rom": "snes",
    "dsp2.data.rom": "snes",
    "dsp2.program.rom": "snes",
    "dsp3.data.rom": "snes",
    "dsp3.program.rom": "snes",
    "dsp4.data.rom": "snes",
    "dsp4.program.rom": "snes",
    "cx4.data.rom": "snes",
    "st010.data.rom": "snes",
    "st010.program.rom": "snes",
    "st011.data.rom": "snes",
    "st011.program.rom": "snes",
    "st018.data.rom": "snes",
    "st018.program.rom": "snes",
    # The Satellaview BIOS, an SNES add-on — same vocabulary choice as the
    # Super Game Boy dumps above, as are bsnes's names for the SGB dumps.
    "BS-X.bin": "snes",
    "sgb.boot.rom": "snes",
    "sgb1.boot.rom": "snes",
    "sgb2.boot.rom": "snes",
    "sgb1.program.rom": "snes",
    "sgb2.program.rom": "snes",
    # Famicom Disk System, declared by the NES cores under systemname
    # "Nintendo Entertainment System" — a genuinely wrong assignment without
    # this rule, and the same class of error as 5200.rom below.
    "disksys.rom": "fds",
    # Nintendo DS / DSi, declared by melonDS, DeSmuME and NooDS. The names are
    # DS-specific; the generic "firmware.bin" several of them also declare is
    # deliberately not listed, because a bare name that generic cannot be
    # claimed for one system.
    "bios7.bin": "nds",
    "bios9.bin": "nds",
    "dsi_bios7.bin": "nds",
    "dsi_bios9.bin": "nds",
    "dsi_firmware.bin": "nds",
    "dsi_nand.bin": "nds",
    # Atari 5200 (atari800, whose systemname is "Atari 8-bit Family"). Without
    # this the 5200 BIOS is filed under atari800 and a query for atari5200
    # cannot reach it at all.
    "5200.rom": "atari5200",
}

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# A ``systemname`` that names several machines at once. libretro writes these
# as a slash list, with or without spaces: "Game Boy/Game Boy Color",
# "GameCube / Wii", "Atari ST/STE/TT/Falcon" — the slash carries the whole
# signal, so it is looked for literally; the spacing around it says nothing.
_SEVERAL_SYSTEMS_SEPARATOR = "/"

SystemSource = Literal["override", "systemname", "slug", "none", "card"]

SOURCE_OVERRIDE: SystemSource = "override"
SOURCE_SYSTEMNAME: SystemSource = "systemname"
SOURCE_SLUG: SystemSource = "slug"
SOURCE_NONE: SystemSource = "none"
# The system came from a standalone firmware card's own list — the card
# declares which catalogue systems it answers for, exactly as the standalone
# save cards do, so there is no per-file derivation to weigh.
SOURCE_CARD: SystemSource = "card"


def system_decision(file_name: str, systemname: str) -> tuple[str, SystemSource]:
    """The system a declared file belongs to, *and how it was arrived at*.

    Four ways, in order: the per-file override (the only one that knows which
    machine a dump belongs to), the ``systemname`` map — both speaking ES-DE
    ids, or a declared own spelling where no id exists — then a mechanical
    slug of an unmapped ``systemname`` (so every declaration stays reachable
    by some word), and — when the ``.info`` states no ``systemname`` at all —
    ``_unknown``.

    The *source* is the point. Everything but ``override`` assigns a file by
    what its whole core is called, which is only sound while the core covers one
    system. :func:`system_assignment_caveats` turns that into a stated caveat
    instead of a silent guess.
    """
    override = FIRMWARE_SYSTEM_OVERRIDE.get(file_name)
    if override is not None:
        return override, SOURCE_OVERRIDE
    known = SYSTEMNAME_TO_SLUG.get(systemname)
    if known is not None:
        return known, SOURCE_SYSTEMNAME
    slug = _NON_SLUG.sub("-", systemname.lower()).strip("-")
    return (slug, SOURCE_SLUG) if slug else ("_unknown", SOURCE_NONE)


def system_for(file_name: str, systemname: str) -> str:
    """The system a declared file belongs to, in atlas's vocabulary."""
    return system_decision(file_name, systemname)[0]


@dataclass(frozen=True, slots=True)
class FirmwareDeclaration:
    """One firmware path, as one installed core's ``.info`` file declares it.

    ``path`` is verbatim from ``firmwareN_path`` — by convention relative to the
    emulator's system directory and possibly carrying subdirectories
    (``dc/dc_boot.bin``); an absolute one is composed with that directory just
    the same (:func:`_join_under`).
    ``need`` is ``firmwareN_opt`` inverted: RetroArch only ever writes that
    flag when it reads as one of its four booleans, over a slot that starts out
    ``false``, so an absent *and* an unreadable flag both mean required
    (:func:`atlas.core_info.enumerate_firmware`).
    """

    path: str
    file_name: str
    description: str
    need: FirmwareNeed
    system: str
    system_source: SystemSource


@dataclass(frozen=True, slots=True)
class CoreDeclarations:
    """One installed core's ``.info`` file, as far as firmware is concerned.

    Every installed core gets one of these — including the ones that declare
    nothing. That is the whole point: "this core is here and wants no firmware"
    is an answer, and it is a different answer from "atlas does not know this
    core", which has no :class:`CoreDeclarations` at all.

    ``database`` is the ``.info`` field of that name, split on ``|``: the
    libretro-database names the core covers. It is read purely as a *signal* —
    it is deliberately not used to assign a system, because it is a different
    vocabulary (``Sinclair - ZX 81`` where ``systemname`` says ``ZX81``), and on
    a real installation 88 of 117 single-entry database names are unknown to the
    ``systemname`` map; leaning on it would mean maintaining a second table of
    the same size.

    ``info_status`` is the read status of the ``.info`` itself. A core whose
    ``.so`` is on disk but whose ``.info`` is missing, unreadable, or not UTF-8
    is still *here* — atlas simply does not know what it wants, which is a state
    of its own and never a silent deletion from the inventory.

    ``firmware_count`` is that field as the ``.info`` spells it and ``unread``
    the ``firmware…path`` keys its enumeration left out, however they are
    spelled — together the reason ``firmware`` can be shorter than the file
    looks (:func:`atlas.core_info.enumerate_firmware`).
    ``unread_stating_a_path`` is the part of ``unread`` that put a value behind
    the key, carried because the values themselves do not survive the
    enumeration and only a declaration with one could have been about some
    particular file.
    """

    core_so: str
    stem: str
    systemname: str
    system: str
    firmware: tuple[FirmwareDeclaration, ...]
    database: tuple[str, ...] = ()
    info_status: ReadStatus = READ_OK
    firmware_count: str = ""
    unread: tuple[str, ...] = ()
    unread_stating_a_path: tuple[str, ...] = ()
    # The ``corename`` field of the same ``.info`` — RetroArch's own display
    # name for the core ("Gambatte"), the closest thing a derived catalogue
    # entry has to a label. Empty where the file states none or could not be
    # read; a caller labels with the ``.so`` name then, never a guess.
    corename: str = ""

    @property
    def serves_several_systems(self) -> bool:
        """Does the machine say this core covers more than one system?

        Three independent readings, any of which counts, because each one is
        evidence and missing it means staying silent about a wrong assignment:

        - ``database`` names several systems (mGBA, Genesis Plus GX);
        - ``systemname`` itself names several (``ColecoVision/CreatiVision/My
          Vision``, ``GameCube / Wii``) — the same separator
          :data:`SYSTEMNAME_TO_SLUG` is already keyed on;
        - the two disagree where both are mappable: one source contradicting
          the other is exactly a reason not to trust either blindly. No
          shipped core trips this today — vice_x128 was the case (systemname
          ``C128``, database ``Commodore - 64``) until map version 2 filed the
          C128 under ``c64`` the way the catalogue itself does, turning the
          disagreement into agreement — but a reading that exists is kept:
          the next info set can disagree again.

        Over-firing says "this could be derived" when it is not; staying silent
        says nothing when it is. The first is recoverable, so the reading is
        deliberately generous. A core with a ``systemname`` and no ``database``
        has only one source and is taken at its word — stated here because it is
        an assumption, not a reading.
        """
        if len(self.database) > 1:
            return True
        if _SEVERAL_SYSTEMS_SEPARATOR in self.systemname:
            return True
        if len(self.database) == 1:
            from_database = SYSTEMNAME_TO_SLUG.get(self.database[0])
            from_systemname = SYSTEMNAME_TO_SLUG.get(self.systemname)
            if from_database is not None and from_systemname is not None:
                return from_database != from_systemname
        return False


@dataclass(frozen=True, slots=True)
class _Info:
    """One ``.info`` as far as firmware is concerned — the parse, before the machine."""

    systemname: str
    database: tuple[str, ...]
    firmware: tuple[FirmwareDeclaration, ...]
    firmware_count: str
    unread: tuple[str, ...]
    unread_stating_a_path: tuple[str, ...]
    corename: str = ""


# What a core whose ``.info`` could not be read declares: nothing, and not
# because it wants nothing — ``info_status`` carries that difference.
_UNREADABLE_INFO = _Info("", (), (), "", (), ())


def _declaration_of(slot: FirmwareSlot, systemname: str) -> FirmwareDeclaration:
    """One enumerated slot as atlas files it — by the file its path names."""
    file_name = os.path.basename(slot.path)
    system, source = system_decision(file_name, systemname)
    return FirmwareDeclaration(
        path=slot.path,
        file_name=file_name,
        description=slot.description,
        need=NEED_OPTIONAL if slot.optional else NEED_REQUIRED,
        system=system,
        system_source=source,
    )


def _declarations_in(text: str) -> _Info:
    """Read one ``.info``: its ``systemname``, its ``database``, and its firmware."""
    fields = parse_core_info(text)
    systemname = fields.get("systemname", "")
    enumeration = enumerate_firmware(fields)
    return _Info(
        systemname=systemname,
        database=tuple(entry for entry in fields.get("database", "").split("|") if entry),
        firmware=tuple(_declaration_of(slot, systemname) for slot in enumeration.slots),
        firmware_count=enumeration.count,
        unread=enumeration.unread,
        unread_stating_a_path=enumeration.unread_stating_a_path,
        corename=fields.get("corename", ""),
    )


@dataclass(frozen=True, slots=True)
class CoreEnumeration:
    """The installed cores, and the directory that names them if it could not be read.

    Two fields because an empty ``cores`` is ambiguous on its own: an
    installation that ships none, and one whose core directory could not be
    listed, are opposite facts about the machine and only the first licenses
    saying anything about it. ``unreadable`` is empty exactly when the
    enumeration is trustworthy — the same shape as
    :class:`~atlas.machine.GlobResult`, which is where it comes from.
    """

    cores: tuple[CoreDeclarations, ...] = ()
    unreadable: tuple[str, ...] = ()

    @property
    def listed(self) -> bool:
        """Did the enumeration actually happen?"""
        return not self.unreadable


def read_core_declarations(
    machine: Machine, info_dir: str, *, core_dir: str | None = None
) -> CoreEnumeration:
    """Read what every *installed* core declares, live, from its ``.info`` file.

    When *core_dir* is given the **cores** are the enumeration: every ``.so``
    there is an installed core, and its ``.info`` is what atlas reads *about*
    it. That way a core whose ``.info`` is missing, unreadable, or not UTF-8
    still appears — carrying its read status instead of vanishing from the
    inventory. The other direction matters too: an ``.info`` set routinely
    covers more cores than an installation ships (RetroDECK: 292 ``.info``
    against 211 ``.so``), and firmware demanded by a core that cannot run is
    exactly the noise a shipped table produced.

    With *core_dir* ``None`` there is no core enumeration to be had, so the
    ``.info`` files are the enumeration and nothing is filtered — the caller
    states that gap as a caveat rather than having it silently narrow the
    answer.

    libretro's two template ``.info`` files are dropped by name: they declare
    the literal placeholder ``filename.ext``.

    The answer carries whether the directory that *names* the cores could be
    read, because an empty list means two opposite things: this installation
    ships no cores, or the place they would be named could not be listed. Only
    the first licenses a statement about the machine, and the caller cannot
    tell them apart from the list alone — which is what the old "empty means
    nobody looked" reading got wrong in the other direction, treating a
    genuinely empty directory as a failure.
    """
    directory = info_dir if core_dir is None else core_dir
    suffix = ".info" if core_dir is None else ".so"
    listing = machine.glob(os.path.join(_glob_escape(directory), f"*{suffix}"))
    stems = [os.path.basename(path)[: -len(suffix)] for path in listing.matches]
    cores: list[CoreDeclarations] = []
    for stem in stems:
        if stem in TEMPLATE_INFO_STEMS:
            continue
        result = machine.read_text(os.path.join(info_dir, f"{stem}.info"))
        info = _UNREADABLE_INFO if result.text is None else _declarations_in(result.text)
        cores.append(
            CoreDeclarations(
                core_so=f"{stem}.so",
                stem=stem,
                systemname=info.systemname,
                system=system_for("", info.systemname),
                firmware=info.firmware,
                database=info.database,
                info_status=result.status,
                firmware_count=info.firmware_count,
                unread=info.unread,
                unread_stating_a_path=info.unread_stating_a_path,
                corename=info.corename,
            )
        )
    return CoreEnumeration(tuple(sorted(cores, key=lambda c: c.core_so)), listing.unreadable)


def system_assignment_caveats(core: CoreDeclarations) -> tuple[Caveat, ...]:
    """State it when a core's firmware was filed by what the *core* is called.

    Only the per-file override knows which machine a dump belongs to. Every
    other route assigns a file by its core's ``systemname``, which is sound
    exactly while the core covers one system — and the ``.info`` says when it
    does not: ``database`` names every system the core serves.

    Two distinct cases, never one bucket:

    - **No ``systemname`` at all.** SkyEmu ships none, only a ``database``
      naming three systems, so eight of its ten declarations land on
      ``_unknown``. That is not a fallback that might be wrong, it is no
      assignment at all, and it gets its own code.
    - **Fallback on a multi-system core.** The core covers several systems by
      any of the readings in :attr:`CoreDeclarations.serves_several_systems`,
      and at least one file was filed by its single ``systemname``. mGBA's
      ``gba_bios.bin`` goes this way.

    A core whose declarations are all override-assigned states nothing — there
    is nothing uncertain to state. Neither does a core that declares no
    firmware, nor one whose only derived declaration names no file at all
    (``dc/``): that is refused before it is a requirement, and naming the empty
    string here would put a gap in the middle of a list of files.
    """
    caveats: list[Caveat] = []
    derived = tuple(
        d.file_name for d in core.firmware if d.system_source != SOURCE_OVERRIDE and d.file_name
    )
    if derived:
        files = ", ".join(sorted(set(derived)))
        if not core.systemname:
            covers = (
                f"its database field names {len(core.database)} systems ({'|'.join(core.database)})"
                if core.database
                else "and its .info names no database either, so nothing on the machine says what it covers"
            )
            caveats.append(
                Caveat(
                    CAVEAT_CORE_WITHOUT_SYSTEMNAME,
                    f"{core.core_so} states no systemname in its .info, so nothing says which of its systems "
                    f"these files belong to and they are filed as _unknown: {files} — {covers}",
                    {
                        "core_so": core.core_so,
                        "files": files,
                        "database": "|".join(core.database),
                        "table_version": FIRMWARE_SYSTEM_OVERRIDE_VERSION,
                    },
                )
            )
        elif core.serves_several_systems:
            caveats.append(
                Caveat(
                    CAVEAT_SYSTEM_ASSIGNMENT_DERIVED,
                    f"these files' system is inherited from {core.core_so}'s own systemname "
                    f"({core.systemname!r}); the core covers more than one system, and no per-file "
                    f"source is established yet, so the filing may be wrong: {files}",
                    {
                        "core_so": core.core_so,
                        "systemname": core.systemname,
                        "files": files,
                        "database": "|".join(core.database),
                        "table_version": FIRMWARE_SYSTEM_OVERRIDE_VERSION,
                    },
                )
            )
    return tuple(caveats)


def catalogue_vocabulary_caveats(core: CoreDeclarations) -> tuple[Caveat, ...]:
    """State it when a core's firmware is filed under a word no catalogue declares.

    A handful of systems exist on the machines atlas supports and in no ES-DE
    build (:data:`SYSTEMS_WITHOUT_CATALOGUE_ID`). Refusing to answer would
    hide real firmware, so the answer keeps atlas's own spelling — and this
    caveat marks every use of one, so a consumer validating identifiers
    against ``known_systems()`` reads a marked word rather than a membership
    mistake. One caveat per spelling used, naming the files filed under it.
    """
    caveats: list[Caveat] = []
    filed: dict[str, list[str]] = {}
    for declaration in core.firmware:
        if declaration.system in SYSTEMS_WITHOUT_CATALOGUE_ID and declaration.file_name:
            filed.setdefault(declaration.system, []).append(declaration.file_name)
    for system in sorted(filed):
        files = ", ".join(sorted(set(filed[system])))
        caveats.append(
            Caveat(
                CAVEAT_SYSTEM_NOT_IN_CATALOGUE,
                f"no ES-DE system id exists for this system "
                f"({SYSTEMS_WITHOUT_CATALOGUE_ID[system]}); {system!r} is atlas's own spelling — "
                f"{core.core_so} files under it: {files}",
                {
                    "core_so": core.core_so,
                    "system": system,
                    "files": files,
                    # Deliberately not "table_version": the assignment caveats
                    # cite the override table under that key, and this one
                    # cites the systemname map — one key per governing table,
                    # so a consumer reading the number knows what it versions.
                    "map_version": SYSTEMNAME_MAP_VERSION,
                },
            )
        )
    return tuple(caveats)


@dataclass(frozen=True, slots=True)
class FirmwareRequirement:
    """One (core, declared file) pair — the atom of the whole model.

    ``path`` is the **absolute, resolved** destination — where the file really
    lands once the kernel has followed every symlink on the way. It is stated
    whether or not a file is there, because "where does this go" is the question
    a download flow asks, and resolving it means two declarations of the same
    file are one place rather than two (LRPS2's ``pcsx2/bios`` *is* the firmware
    root on RetroDECK, so a placing client would otherwise write two copies).
    ``declared`` keeps the string the core spelled, which is the name it will
    open — nothing is lost by resolving.

    ``need`` says what the core asks for; ``found`` and ``checked`` say what the
    machine holds. ``found`` is the path kind read at the destination and keeps
    all four apart — a directory in the way is not a missing file, and a path
    that could not be looked at is neither. ``checked`` is ``None`` exactly when
    there is nothing at the destination to check, and otherwise keeps its four
    values apart: ``unchecked`` (identity known, verification not asked for) is
    not ``unknown`` (it could not be established), and neither is a verdict.

    ``regions`` is ``None`` on an ordinary requirement — every launch needs
    this file — and names the console regions whose launch this file serves on
    an option inside a :class:`FirmwareAlternatives` group, which is the only
    place a region-scoped requirement may stand (:class:`CoreFirmware`
    enforces it). DuckStation is what the field exists for: exactly one of its
    per-region BIOS keys is read per launch, selected by the console region
    the running disc sets (``GetBIOSImage``, bios.cpp:321-338 and
    system.cpp:1613-1648, :2510 at 64655818e), so two named images were never
    two files one launch needs.
    """

    # ``None`` exactly for a card-declared requirement: a standalone emulator
    # has no ``.so``, and inventing one would collide with the real namespace.
    core_so: str | None
    system: str
    system_source: SystemSource
    need: FirmwareNeed
    file_name: str
    path: str
    declared: str
    description: str
    identity: FirmwareIdentity | None
    found: PathKind
    checked: FirmwareChecked | None
    regions: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.need not in FIRMWARE_NEEDS:
            raise ValueError(f"FirmwareRequirement: need must be one of {FIRMWARE_NEEDS}, got {self.need!r}")
        if self.regions is not None and (
            not self.regions or any(not region for region in self.regions)
        ):
            raise ValueError(
                f"FirmwareRequirement: regions must be None or name at least one region, got {self.regions!r}"
            )
        if self.found not in (KIND_FILE, KIND_DIRECTORY, KIND_MISSING, KIND_INACCESSIBLE):
            raise ValueError(f"FirmwareRequirement: found must be a path kind, got {self.found!r}")
        if self.found in (KIND_FILE, KIND_DIRECTORY):
            if self.checked not in FIRMWARE_CHECKED:
                raise ValueError(
                    f"FirmwareRequirement: something is there, so checked must be one of {FIRMWARE_CHECKED}, "
                    f"got {self.checked!r}"
                )
        elif self.checked is not None:
            raise ValueError("FirmwareRequirement: nothing is there to check, so checked must be None")

    @property
    def present(self) -> bool | None:
        """Is anything at the destination? ``None`` when atlas could not look.

        Derived from :attr:`found`, and deliberately three-valued: "could not
        look" is not "not there".
        """
        if self.found == KIND_INACCESSIBLE:
            return None
        return self.found in (KIND_FILE, KIND_DIRECTORY)

    @property
    def satisfied(self) -> bool | None:
        """Is the right file where this core will look for it?

        ``True`` only when a file is there and atlas *established* that it is
        the right one. ``False`` when nothing is there, or when the bytes are
        known to be wrong — a present file can absolutely fail this, which is
        the whole reason the identity table exists. ``None`` for everything atlas
        did not establish:

        - the path could not be looked at;
        - a directory sits there (something is present, nothing is confirmed);
        - the identity is known and could not be read (unreadable bytes);
        - the identity is known and verification was **not asked for**.

        That last one is the load-bearing case. Without hashing, "a file with
        the right name is there" is all atlas knows, and calling it satisfied
        would be an all-clear it did not earn. It is affordable to say so: a
        verified single-core answer costs 0.03 s and the whole tree 0.8 s on the
        reference machine, so a caller who wants the green light can ask for it.

        A file whose identity the table does not cover stays ``True``: nothing
        further can ever be established about it, so withholding the answer
        would withhold it forever.
        """
        if self.found == KIND_INACCESSIBLE:
            return None
        if self.found == KIND_MISSING:
            return False
        if self.found == KIND_DIRECTORY:
            return None
        if self.checked == CHECKED_MISMATCH:
            return False
        if self.checked == CHECKED_UNCHECKED:
            return None
        if self.checked == CHECKED_UNKNOWN and self.identity is not None:
            return None
        return True


@dataclass(frozen=True, slots=True)
class FirmwareAlternatives:
    """Requirements of which one launch needs exactly one — the console region decides.

    A conjunction is what a requirement list means, and DuckStation's
    per-region BIOS keys are not one: ``GetBIOSImage`` reads exactly one of
    them per launch, switched on the console region (bios.cpp:321-338 at
    64655818e), which under the shipped Auto setting is the running disc's own
    region (system.cpp:1613-1648, :2510) — a run-time fact no configuration
    records. So the alternatives are stated as one entry: each option carries
    the :attr:`~FirmwareRequirement.regions` whose launch it serves, the
    region sets are disjoint (two options one region could pick between would
    be a claim about ranking nobody established), and a launch needs the
    option whose regions contain its console region. A region no option lists
    has nothing stated for it — the entry's caveats say why.

    A single option is a normal group, not a degenerate one: a named NTSC-U
    image beside a search that found nothing is still only what a US console
    boots, and flattening it into an unconditional requirement would claim a
    PAL launch needs it.
    """

    options: tuple[FirmwareRequirement, ...]

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("FirmwareAlternatives: an empty group states nothing — options must be non-empty")
        claimed: set[str] = set()
        for option in self.options:
            if option.regions is None:
                raise ValueError(
                    "FirmwareAlternatives: every option must state the regions whose launch it serves"
                )
            if len(set(option.regions)) != len(option.regions):
                raise ValueError(
                    "FirmwareAlternatives: an option repeats a region within its own tuple — "
                    "one region, one statement"
                )
            overlap = claimed.intersection(option.regions)
            if overlap:
                raise ValueError(
                    f"FirmwareAlternatives: regions must be disjoint across options — {sorted(overlap)} "
                    "would leave the pick unstated"
                )
            claimed.update(option.regions)

    @property
    def satisfied(self) -> bool | None:
        """The three-valued lift over the selector atlas cannot read.

        ``True`` when every stated option is satisfied. ``False`` when every
        stated option demonstrably fails. ``None`` otherwise: whether THIS
        launch is served depends on a run-time fact, and a mixed group has no
        honest single verdict. Regions no option lists are outside this
        verdict and the entry's caveats speak for them — a group of one
        satisfied NTSC-U image says nothing about the two regions with no
        image stated.
        """
        verdicts = {option.satisfied for option in self.options}
        if verdicts == {True}:
            return True
        if verdicts == {False}:
            return False
        return None


@dataclass(frozen=True, slots=True)
class RefusedDeclaration:
    """A declaration atlas would not follow — it leaves the root, or will not resolve.

    It is not a requirement — there is no destination to state — but it is a
    firmware file this core asked for, so it has to stay visible on the core.
    Otherwise ``unmet`` and ``undetermined`` together would look like the whole
    story while a required file had been quietly dropped.

    ``reason`` is the caveat code that says *why*: leaving the root and being
    unresolvable are different facts about the machine.
    """

    declared: str
    need: FirmwareNeed
    reason: str


CoreDeclarationState = Literal["read", "unreadable", "absent", "unsupported", "packaged"]

DECLARATION_READ: CoreDeclarationState = "read"
DECLARATION_UNREADABLE: CoreDeclarationState = "unreadable"
DECLARATION_ABSENT: CoreDeclarationState = "absent"
# Not a state of the machine but of atlas's coverage: the emulator is here and
# atlas has no source for what it wants. Spelled the way the placement route
# spells the same fact, because it *is* the same fact asked twice.
DECLARATION_UNSUPPORTED: CoreDeclarationState = "unsupported"
# The declaration is atlas's packaged card, not a file read off this machine:
# a standalone emulator ships no ``.info``, so what it expects is established
# from its source at the shipped release and packaged with citations
# (:mod:`atlas.standalone_firmware`). Only the declaration is packaged — the
# destinations are resolved against this arrangement's own trees the way the
# emulator resolves them, and what sits there is read live, exactly like the
# ``.info`` route. The entry's caveat states the provenance, so a client
# never mistakes a card for a machine read.
DECLARATION_PACKAGED: CoreDeclarationState = "packaged"

CORE_DECLARATION_STATES = ("read", "unreadable", "absent", "unsupported", "packaged")


@dataclass(frozen=True, slots=True)
class CoreFirmware:
    """What one emulator wants, resolved against the live firmware root.

    ``declaration`` is the load-bearing field, and it has four values because
    an empty ``requirements`` list has four meanings:

    - ``read`` — atlas read this core's own ``.info``, so an empty list is the
      answer "this core needs no firmware".
    - ``unreadable`` — the core is here, its declaration is not (missing,
      unreadable, or not UTF-8). What it wants is unknown.
    - ``absent`` — no such core here at all. Exactly that, and nothing else:
      a claim about what is installed on this machine.
    - ``unsupported`` — the emulator is here and atlas has no source for what
      it wants, which today means a standalone emulator: it ships no ``.info``,
      and its own rules are outside the resolver's coverage. A claim about
      atlas, not about the machine, and the same one the placement route makes
      with :data:`~atlas.placement.UNRESOLVED_STANDALONE`.

    Only the first makes an empty list mean *complete*; the other three make it
    mean *unknown*, and each carries a caveat saying which. ``unsupported`` and
    the evidence caveats are different axes and never substitute for each
    other: ``arrangement-unverified`` says a reading was never confirmed live,
    while this says there was no reading to confirm.

    ``requirements`` is a conjunction: every entry is needed. An entry may be
    a :class:`FirmwareAlternatives` group, and then what is needed is exactly
    one of its options — the console region decides which. A region-scoped
    requirement stands only inside a group; a plain entry carrying
    ``regions`` would smuggle a condition into the conjunction, so it is
    refused here.
    """

    core_so: str | None
    label: str | None
    declaration: CoreDeclarationState
    requirements: tuple[FirmwareRequirement | FirmwareAlternatives, ...]
    caveats: tuple[Caveat, ...]
    refused: tuple[RefusedDeclaration, ...] = ()
    # The ``.info`` path keys RetroArch's own enumeration never takes (a
    # spelling it does not compose, a slot past the count, an empty value it
    # discards). A field rather than a caveat read back out of the answer:
    # whether this core declares something nobody asks for is a fact about the
    # core, and the identification route needs it as data, not as the presence
    # of a message. It stays out of the contract — the caveat that states it
    # already carries the keys, and one fact serialized twice is one fact that
    # can disagree with itself.
    unread: tuple[str, ...] = ()
    # The part of ``unread`` that put a value behind the key. Both are needed
    # because they answer different questions: ``unread`` is what the caveat
    # states (a line the file spends and the emulator ignores, whatever it
    # holds), this one is what the identification route may count (only a
    # declaration that stated a path could have been about some particular
    # file). Also out of the contract, and for the same reason.
    unread_stating_a_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.declaration not in CORE_DECLARATION_STATES:
            raise ValueError(
                f"CoreFirmware: declaration must be one of {CORE_DECLARATION_STATES}, got {self.declaration!r}"
            )
        if self.declaration not in (DECLARATION_READ, DECLARATION_PACKAGED) and self.requirements:
            raise ValueError(
                "CoreFirmware: requirements can only come from a declaration that was read or packaged"
            )
        if self.declaration != DECLARATION_READ and not self.caveats:
            raise ValueError(
                "CoreFirmware: a declaration that was not read off the machine must state why "
                "(or, packaged, its provenance) — an unexplained list lies"
            )
        if self.refused and not self.caveats:
            raise ValueError("CoreFirmware: a refused declaration must state why, or it vanishes")
        for entry in self.requirements:
            if isinstance(entry, FirmwareRequirement) and entry.regions is not None:
                raise ValueError(
                    "CoreFirmware: a region-scoped requirement stands only inside a FirmwareAlternatives "
                    "group — a plain entry carrying regions would smuggle a condition into the conjunction"
                )

    @property
    def unmet(self) -> tuple[FirmwareRequirement, ...]:
        """Required files that are demonstrably not usable — absent or wrong.

        An alternatives group contributes its options only when the whole
        group fails: one region's image being absent while another region's is
        served is not "the launch lacks this file", it is a fact the region
        decides, and that is :attr:`undetermined`'s side of the line.
        """
        out: list[FirmwareRequirement] = []
        for entry in self.requirements:
            if isinstance(entry, FirmwareAlternatives):
                if entry.satisfied is False:
                    out.extend(o for o in entry.options if o.need == NEED_REQUIRED)
            elif entry.need == NEED_REQUIRED and entry.satisfied is False:
                out.append(entry)
        return tuple(out)

    @property
    def undetermined(self) -> tuple[FirmwareRequirement, ...]:
        """Required files atlas could not judge — unverified, unreadable, or unlookable.

        From an alternatives group that has no single verdict, the options not
        established usable: which of them the launch needs is the run-time
        fact the group exists to state, so none of the not-satisfied ones may
        be presented as settled.
        """
        out: list[FirmwareRequirement] = []
        for entry in self.requirements:
            if isinstance(entry, FirmwareAlternatives):
                if entry.satisfied is None:
                    out.extend(
                        o for o in entry.options if o.need == NEED_REQUIRED and o.satisfied is not True
                    )
            elif entry.need == NEED_REQUIRED and entry.satisfied is None:
                out.append(entry)
        return tuple(out)

    @property
    def requirements_met(self) -> bool | None:
        """Are all *required* files in place and right? ``None`` when atlas cannot say.

        The tri-state is the point, and it is the one number a client renders:
        ``None`` when the declaration could not be read, when a required file
        could not be judged — including one that was simply never verified — or
        when a required declaration was refused for leaving the firmware root.
        ``True`` is never reached out of ignorance, and never with a required
        file whose bytes are known to be wrong.

        Note what follows for ``verify=False``: a core whose required files have
        known identities answers ``None``, not ``True``. Presence alone is not
        the question this field asks.

        An alternatives group folds in through its own three-valued
        :attr:`FirmwareAlternatives.satisfied`: ``False`` blocks (no region
        boots), a mixed group leaves ``None`` (whether THIS launch is served
        is the run-time fact atlas cannot read), and only an all-satisfied
        group lets ``True`` through.
        """
        if self.declaration != DECLARATION_READ:
            return None
        group_verdicts = [
            entry.satisfied for entry in self.requirements if isinstance(entry, FirmwareAlternatives)
        ]
        if self.unmet or any(verdict is False for verdict in group_verdicts):
            return False
        if self.undetermined or any(verdict is None for verdict in group_verdicts):
            return None
        return None if any(r.need == NEED_REQUIRED for r in self.refused) else True


@dataclass(frozen=True, slots=True)
class UnclaimedFile:
    """A file in the firmware tree that no installed core asks for.

    ``identity`` is matched by **content**, not by name — the name is exactly
    what is not to be trusted about an unclaimed file. It is therefore only
    available when the caller asked for verification; without it atlas states
    the file and says nothing about what it is.
    """

    path: str
    identity: FirmwareIdentity | None

    @property
    def known_as(self) -> tuple[str, ...]:
        """The canonical names this content is known under, when recognised."""
        return () if self.identity is None else self.identity.known_as


@dataclass(frozen=True, slots=True)
class FirmwareAnswer:
    """One firmware answer: per emulator what it wants, plus what nobody wants.

    ``root`` is the resolved system directory — the directory RetroArch hands
    its cores — or ``None`` when the configs do not state one; then there is
    nothing to resolve against and ``cores`` is empty with a caveat saying so.
    That caveat is an invariant, not a habit: an answer with no root and no
    caveat is the one shape this whole module exists to prevent — an empty
    list that says nothing, which a caller can only read as "nothing needed".
    ``hash_checked`` records whether identity verification ran at all, so a
    list of present files can never be mistaken for a list of verified ones.
    """

    root: str | None
    cores: tuple[CoreFirmware, ...]
    unclaimed: tuple[UnclaimedFile, ...]
    hash_checked: bool
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]

    def __post_init__(self) -> None:
        if self.root is None and not self.caveats:
            raise ValueError(
                "FirmwareAnswer: an answer with no root must state why there is none — without it the "
                "empty answer reads as 'this machine needs no firmware'"
            )

    @property
    def requirements(self) -> tuple[FirmwareRequirement, ...]:
        """Every requirement in the answer, flattened and sorted by destination."""
        return _requirements_of(self.cores)


@dataclass(frozen=True, slots=True)
class FirmwareIdentification:
    """What a piece of content is, and every place on this machine that wants it.

    ``identity`` is ``None`` when the packaged table does not recognise the
    content — a normal answer, and one that must not be read as "this file is
    junk": the table covers only what ``System.dat`` covers.
    """

    identity: FirmwareIdentity | None
    requirements: tuple[FirmwareRequirement, ...]
    sources: tuple[str, ...]
    caveats: tuple[Caveat, ...]

    @property
    def known_as(self) -> tuple[str, ...]:
        """The canonical names this content is known under, when recognised."""
        return () if self.identity is None else self.identity.known_as


@dataclass(frozen=True, slots=True)
class FirmwareContext:
    """One live read of everything a firmware answer is derived from.

    Assembled once per query by the installation handle, then shared by every
    entry point below, so a single answer can never mix two revisions of the
    configs it was derived from.

    ``cores_read`` says whether the core enumeration happened at all. An empty
    ``cores`` means two different things — this installation ships no cores, or
    atlas never got to look — and only the first licenses a statement about what
    the machine has.

    ``arrangement_version`` is the version the arrangement states about itself
    in that same read, or ``None`` where it states none. It rides here rather
    than being fetched where it is used, so the evidence an answer states and
    the configs it was derived from come from one snapshot — and so a handle
    that assembles a context cannot leave it out by forgetting.
    """

    root: str | None
    cores: tuple[CoreDeclarations, ...]
    hashes: FirmwareHashes
    cores_read: bool = True
    sources: tuple[str, ...] = ()
    caveats: tuple[Caveat, ...] = ()
    arrangement_version: str | None = None
    # The XDG bases this arrangement's standalone emulators resolve their own
    # trees against — where a packaged firmware card's destinations land. Both
    # ``None`` on an arrangement that has not established them; a card then
    # stays unanswered rather than guessed at.
    standalone_data_home: str | None = None
    standalone_config_home: str | None = None
    # The flatpak app id those bases belong to, where the emulator runs as one
    # — it decides how the emulator spells its own directory (#246).
    standalone_flatpak: str | None = None
    # How an absolute path in a standalone emulator's own configuration reads
    # from this host (:class:`SandboxTranslation`). ``None`` on an arrangement
    # that resolves no standalone card, where nothing would ask.
    standalone_sandbox: SandboxTranslation | None = None
    # Whether those bases are a flatpak's pinned XDG variables. It settles the
    # root of an emulator that picks one by whether XDG_CONFIG_HOME is set —
    # inside a sandbox it always is, so there is nothing left to probe.
    standalone_xdg_pinned: bool = False


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """One emulator a frontend catalogue declares for a system.

    ``standalone_token`` is the emulator identity the launch command states
    for a standalone entry — the ``%EMULATOR_…%`` token, or on EmuDeck the
    launcher route's resolved word — set by the handle that read the
    catalogue, because what a command identifies is arrangement knowledge.
    ``None`` where the command identifies nothing atlas can act on.

    The two homes are the per-entry override of the context's standalone
    bases: ``None`` means the arrangement's own pair governs, and a value
    means this entry's launch picks a binary whose trees hang elsewhere —
    EmuDeck's flatpak variant reads ``~/.var/app/<id>``, not the host's XDG
    tree — set by the same handle for the same reason the token is.
    """

    label: str
    kind: str
    core_so: str | None
    standalone_token: str | None = None
    standalone_data_home: str | None = None
    standalone_config_home: str | None = None
    standalone_flatpak: str | None = None


@dataclass(frozen=True, slots=True)
class Catalogue:
    """A frontend's emulator enumeration for one system — and whether it was read.

    The distinction is the whole point: an enumeration that came back empty says
    the frontend knows no emulator for that system, while one that could not be
    read says nothing at all. Collapsing them turns a read failure into a claim
    about the machine.

    ``hole`` is the third state, between those two: part of the catalogue could
    not be consulted at all — EmuDeck's bundled ``es_systems.xml`` is sealed
    inside the AppImage — and the caveat here is the caller's statement of that
    fact, stated on every answer this catalogue informs. The entries are still
    real (the readable part declares them), but an **empty** enumeration under
    a hole says nothing about the machine: the declaration may sit in the part
    nobody could read, so the answer is a look that failed, never "no emulator
    covers this system". ``None`` means the enumeration is whole and ``read``
    alone decides.
    """

    entries: tuple[CatalogueEntry, ...]
    read: bool = True
    hole: Caveat | None = None


_SAVE_ARTIFACTS: frozenset[str] | None = None


def _mode_save_files(mode: SaveMode) -> set[str]:
    """The concrete files one save mode claims, relative to the firmware root.

    A file name still carrying a template hole names no concrete file, so it
    claims nothing here.
    """
    return {
        f"{mode.subdir}/{name}" if mode.subdir else name
        for name in (*(mode.files or ()), *(mode.observe or ()))
        if "<" not in name
    }


def save_artifact_paths() -> frozenset[str]:
    """Paths under the firmware root that the save rule cards claim as *save* data.

    Some cores write saves into the system directory — Flycast keeps its VMUs
    in ``dc/``, LRPS2 its memory cards under ``pcsx2/memcards``. Those files sit
    in the firmware tree but reporting them as firmware-anything is a category
    error, and atlas already knows better: the cards in
    ``data/core_oddities.json`` name the directory and the files. Every mode
    rooted in the system directory contributes, whatever the core's option is
    set to right now — a leftover VMU from the other setting is still a save.
    """
    global _SAVE_ARTIFACTS
    if _SAVE_ARTIFACTS is None:
        paths: set[str] = set()
        for card in load_oddities():
            for mode in card.modes.values():
                if mode.root == ROOT_SYSTEM_DIRECTORY:
                    paths.update(_mode_save_files(mode))
        _SAVE_ARTIFACTS = frozenset(paths)
    return _SAVE_ARTIFACTS


def _observe(
    machine: Machine, path: str, identity: FirmwareIdentity | None, *, verify: bool
) -> tuple[PathKind, FirmwareChecked | None, Caveat | None]:
    """What the machine says about one destination: what is there, and how sure we are.

    All four path kinds are distinct answers, because the caller acts on each
    differently. A directory sitting at the destination is not "missing" in any
    useful sense — nothing can be placed there — and an inaccessible path is not
    an absent file, it is a look that did not happen.
    """
    kind = machine.path_kind(path)
    if kind == KIND_INACCESSIBLE:
        return (
            kind,
            None,
            Caveat(
                CAVEAT_FIRMWARE_PATH_INACCESSIBLE,
                f"{path} cannot be looked at (permissions or an I/O failure), so whether anything is there "
                "is unknown — this is not an absent file",
                {"path": path},
            ),
        )
    if kind == KIND_DIRECTORY:
        # Not necessarily wrong: LRPS2 declares "pcsx2/bios" and means the
        # folder, and RetroDECK links it to the firmware root. Atlas states what
        # is there and establishes nothing — telling the caller to clear the
        # path would break exactly that arrangement.
        return (
            kind,
            CHECKED_UNKNOWN,
            Caveat(
                CAVEAT_FIRMWARE_PATH_OBSTRUCTED,
                f"a directory is at {path}, where this core's declaration names a file — some cores declare "
                "a folder instead (LRPS2 does), so this may be correct; either way nothing about it can be "
                "established, and it is not a missing file",
                {"path": path},
            ),
        )
    if kind != KIND_FILE:
        return kind, None, None
    if identity is None:
        # Nothing to check against — and that is not the same as "not checked".
        return KIND_FILE, CHECKED_UNKNOWN, None
    if not verify:
        return KIND_FILE, CHECKED_UNCHECKED, None
    # Size is a free pre-filter: a wrong size settles the question without
    # reading a byte of the file.
    size = machine.file_size(path)
    if size is not None and size != identity.size:
        return KIND_FILE, CHECKED_MISMATCH, None
    digest = machine.file_digest(path, DIGEST_MD5)
    if digest is None:
        return (
            KIND_FILE,
            CHECKED_UNKNOWN,
            Caveat(
                CAVEAT_FIRMWARE_UNREADABLE,
                f"{path} is there but its bytes cannot be read, so its identity stays unestablished — "
                "this is a read failure, not a verdict on the file",
                {"path": path},
            ),
        )
    matches = digest.lower() == identity.md5.lower()
    return KIND_FILE, CHECKED_VERIFIED if matches else CHECKED_MISMATCH, None


def resolve_links(machine: Machine, path: str) -> str | None:
    """Follow symlinks segment by segment, the way the kernel would.

    ``None`` when the chain does not settle within
    :data:`~atlas.machine.SYMLINK_HOPS` hops — a loop, or a chain longer than
    the kernel would follow, both of which are ``ELOOP``. Goes through the
    seam's ``readlink`` and uses the seam's hop limit, so a fixture machine
    resolves exactly like the real one; a limit that differed anywhere would
    make vectors built on it prove nothing.

    ``..`` is applied to the *resolved* path, not to the spelling. That is the
    whole point: the kernel resolves a component and then walks up from where
    it landed, so ``link/..`` leaves the link's target directory, not the
    directory the link sits in.

    One of three kernel walks in atlas, next to ``FixtureMachine._resolve``
    (which additionally refuses to step through a non-directory, because it
    answers for paths rather than resolving them) and
    ``atlas.installations._resolve_symlink_chain`` (which also collects the
    links traversed). A fidelity finding about symlinks, ``..`` or the hop
    limit belongs in all three; the limit itself is shared, never copied.
    """
    parts = [p for p in path.split("/") if p and p != "."]
    resolved = "/"
    hops = SYMLINK_HOPS
    while parts:
        segment = parts.pop(0)
        if segment == "..":
            resolved = os.path.dirname(resolved) or "/"
            continue
        candidate = os.path.join(resolved, segment)
        target = machine.readlink(candidate)
        if target is None:
            resolved = candidate
            continue
        hops -= 1
        if hops < 0:
            return None
        if os.path.isabs(target):
            resolved = "/"
        # A relative target is relative to the directory holding the link,
        # which is exactly where `resolved` already stands.
        parts = [p for p in target.split("/") if p and p != "."] + parts
    return resolved


def _stays_under(root: str, path: str) -> bool:
    """Is *path* the firmware *root* itself, or something inside it?

    Both arguments are **resolved** paths — the check is the last step of a
    resolution, never a substitute for one. The root counts as inside on
    purpose: RetroDECK links ``bios/pcsx2/bios`` back to the firmware root so
    LRPS2 finds its folder, and that declaration resolves to the root exactly.

    Every place atlas decides to read, hash, or report something is bounded by
    this one predicate, so the bound cannot drift between the declaration side
    and the scan side.
    """
    prefix = root if root.endswith("/") else f"{root}/"
    return path == root or path.startswith(prefix)


@dataclass(frozen=True, slots=True)
class Destination:
    """Where a declared path actually lands — or why atlas would not follow it.

    Exactly one of ``path`` and ``refusal`` is set. ``refusal`` is a caveat
    code, and there are four of them on purpose: "this leaves the firmware
    root", "this could not be resolved at all", "this names no file" and
    "there is no root to resolve against" are different facts, and a consumer
    branching on a code it can trust must not be handed the wrong one. The
    last one is also of a different *scope*: the first three are about the
    declaration in hand, while an unusable root refuses every declaration of
    every core alike.
    """

    path: str | None = None
    refusal: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.refusal is None):
            raise ValueError(
                "Destination: exactly one of path and refusal is set — a landing place without a refusal, "
                f"or a refusal without one, got path={self.path!r} refusal={self.refusal!r}"
            )


def _join_under(root: str, declared: str) -> str:
    """``fill_pathname_join`` — the composition RetroArch performs.

    Directory, exactly one separator, then the declared path **verbatim**
    (``file_path.c:983-993`` with ``fill_pathname_slash``, ``:395-410``). There
    is no special case for an absolute declaration, which is the whole point:
    ``/etc/passwd`` composes to ``<root>//etc/passwd`` and names a file inside
    the system directory, not the one it reads like. That composed name is what
    ``path_is_valid`` is asked about (``core_info.c:2381-2383``) and what the
    core is handed, so it is what atlas answers.
    """
    if not root:
        return declared
    return f"{root}{declared}" if root.endswith("/") else f"{root}/{declared}"


def destination_under(machine: Machine, root: str, declared: str) -> Destination:
    """Where a declared path lands under *root* — resolved, in the kernel's order.

    ``firmwareN_path`` is a relative path by contract, and it is read from a
    config file a user (or anything writing that file) can edit, so every read
    atlas then does — presence, size, digest, and the directories the unclaimed
    scan walks — is bounded by the root. An absolute declaration is not the way
    out of that bound it looks like: RetroArch composes it with the system
    directory like any other (:func:`_join_under`), and so does atlas. What
    still leaves the root is a climb (``../``), and that is refused.

    Everything else is decided on **resolved** paths, and nothing is normalized
    first. Collapsing ``..`` lexically before resolving would eat the component
    in front of it even when that component is a symlink, and the kernel does
    the opposite: it resolves the component and applies ``..`` to where it
    landed. With ``pcsx2/bios`` linked to the firmware root,
    ``pcsx2/bios/../x.bin`` is ``<root>/../x.bin`` to the kernel — outside — and
    ``<root>/pcsx2/x.bin`` to a lexical reading. The kernel opens the file, so
    the kernel's reading is the one that counts.

    The answer is the resolved path. Two declarations that land on the same
    file then look like one place, which is what keeps a placing client from
    writing two copies: LRPS2's ``pcsx2/bios`` *is* the firmware root here.

    A declaration whose last component is ``.``, ``..`` or empty is refused
    before any of that: those name a directory step, not a file. They resolve
    to a perfectly legal directory — for ``sub/..`` the firmware root itself,
    for ``dc/`` the subdirectory — and answering that as a destination would
    state a requirement for a file the core never named, on a path nothing can
    ever be placed at.

    A *root* that is not an absolute path is refused first of all, because it
    is not a bound: the resolver builds every path from ``/``, so a relative
    root resolves to one that contains everything below it and the containment
    check then passes on anything, including a declaration that names a file
    nowhere near the firmware tree.

    A cfg reaches this: ``system_directory = "system"`` is not blank and not
    ``default``, so it survives ``expand_home`` as written, and the sandbox
    translation only rewrites absolute spellings — the relative value arrives
    here unchanged. RetroArch resolves such a root against the working
    directory of the running process, which is not a fact on disk, so atlas
    has no destination to state and says so instead of promoting the value to
    an absolute path it invented. That the root itself is unusable is stated
    separately, by ``firmware-root-missing``.
    """
    if not os.path.isabs(root):
        return Destination(refusal=CAVEAT_FIRMWARE_ROOT_UNUSABLE)
    if os.path.basename(declared) in ("", ".", ".."):
        return Destination(refusal=CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE)
    resolved_root = resolve_links(machine, root)
    resolved = resolve_links(machine, _join_under(root, declared))
    if resolved_root is None or resolved is None:
        return Destination(refusal=CAVEAT_FIRMWARE_PATH_UNRESOLVABLE)
    if _stays_under(resolved_root, resolved):
        return Destination(path=resolved)
    return Destination(refusal=CAVEAT_FIRMWARE_PATH_ESCAPES_ROOT)


def _why_refused(refusal: str, root: str) -> str:
    """The prose behind one refusal code — one sentence fragment per fact."""
    if refusal == CAVEAT_FIRMWARE_PATH_ESCAPES_ROOT:
        return f"does not stay under the firmware root {root} once symlinks are resolved"
    if refusal == CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE:
        return "ends in a directory step ('.', '..', or nothing at all) and so names no file"
    return "cannot be resolved at all — a symlink loop, or a chain longer than the kernel follows"


def _refusal_caveat(core: CoreDeclarations, declaration: FirmwareDeclaration, refusal: str, root: str) -> Caveat:
    """The caveat behind one refused declaration — with the right subject.

    Three of the four refusals are about the declaration, so the core and the
    path it spelled lead the sentence. An unusable root is not: it holds for
    every declaration of every core alike, and saying "this core declares X,
    which cannot be resolved" would blame a file that is perfectly well
    formed. The data is the same either way — a consumer branches on the code.
    """
    if refusal == CAVEAT_FIRMWARE_ROOT_UNUSABLE:
        message = (
            f"the firmware root {root!r} is not an absolute path, so nothing resolves against it and no "
            f"destination exists to answer — {core.core_so}'s {declaration.need} declaration "
            f"{declaration.path!r} is refused for that reason, not for anything about the file"
        )
    else:
        message = (
            f"{core.core_so} declares {declaration.path!r}, which {_why_refused(refusal, root)} — atlas "
            f"will not read or place a file it cannot vouch for, so this {declaration.need} file is "
            "refused rather than answered"
        )
    return Caveat(
        refusal,
        message,
        {
            "core_so": core.core_so,
            "declared": declaration.path,
            "need": declaration.need,
            "root": root,
        },
    )


def _requirements_for(
    machine: Machine,
    context: FirmwareContext,
    core: CoreDeclarations,
    *,
    verify: bool,
) -> tuple[tuple[FirmwareRequirement, ...], tuple[RefusedDeclaration, ...], tuple[Caveat, ...], list[Caveat]]:
    """Resolve one core's declarations: what it wants, what was refused, and why.

    Returns ``(requirements, refused, core_caveats, answer_caveats)``. The
    refusal caveat belongs to the **core** — it is a fact about that core's
    declaration, and if it only travelled on the answer, a caller reading one
    emulator's entry would see a requirement list that silently lost a file.
    """
    root = context.root
    assert root is not None  # callers resolve the empty-root answer before getting here
    requirements: list[FirmwareRequirement] = []
    refused: list[RefusedDeclaration] = []
    core_caveats: list[Caveat] = []
    answer_caveats: list[Caveat] = []
    for declaration in core.firmware:
        destination = destination_under(machine, root, declaration.path)
        if destination.path is None:
            refusal = destination.refusal or CAVEAT_FIRMWARE_PATH_ESCAPES_ROOT
            refused.append(
                RefusedDeclaration(declared=declaration.path, need=declaration.need, reason=refusal)
            )
            core_caveats.append(_refusal_caveat(core, declaration, refusal, root))
            continue
        path = destination.path
        identity = context.hashes.for_path(declaration.path)
        found, checked, caveat = _observe(machine, path, identity, verify=verify)
        if caveat is not None:
            answer_caveats.append(caveat)
        requirements.append(
            FirmwareRequirement(
                core_so=core.core_so,
                system=declaration.system,
                system_source=declaration.system_source,
                need=declaration.need,
                file_name=declaration.file_name,
                path=path,
                declared=declaration.path,
                description=declaration.description,
                identity=identity,
                found=found,
                checked=checked,
            )
        )
    return (
        tuple(sorted(requirements, key=lambda r: r.path)),
        tuple(refused),
        tuple(core_caveats),
        answer_caveats,
    )


def _by_destination(requirement: FirmwareRequirement) -> str:
    """Sort key for requirement lists: the resolved destination, the family's one order."""
    return requirement.path


def _requirements_of(cores: tuple[CoreFirmware, ...]) -> tuple[FirmwareRequirement, ...]:
    """Every requirement across *cores*, flattened and sorted by destination.

    One ordering for every answer that hands requirements back, so an
    identification and an inventory list the same files in the same order.
    An alternatives group flattens to its options — each still carries its
    ``regions``, so the flat view loses no scope, only the grouping.
    """
    flat = (
        option
        for core in cores
        for entry in core.requirements
        for option in (entry.options if isinstance(entry, FirmwareAlternatives) else (entry,))
    )
    return tuple(sorted(flat, key=lambda r: (r.path, r.core_so)))


def _empty_answer(context: FirmwareContext, extra: tuple[Caveat, ...] = ()) -> FirmwareAnswer:
    return FirmwareAnswer(
        root=None,
        cores=(),
        unclaimed=(),
        hash_checked=False,
        sources=context.sources,
        caveats=(*context.caveats, *extra),
    )


def _no_declaration(subject: str, data: dict[str, str]) -> Caveat:
    """Nothing declares it, and that was established — the empty list is the answer."""
    return Caveat(
        CAVEAT_NO_FIRMWARE_DECLARATION,
        f"no installed core declares {subject} — every emulator in this answer was read and none names "
        "one, so there is nothing here to check against; this is an absence that was established, not a gap",
        data,
    )


def _no_requirement(subject: str, data: dict[str, str]) -> Caveat:
    """Declared, and still not a requirement — refused, or never enumerated by its core."""
    return Caveat(
        CAVEAT_NO_FIRMWARE_REQUIREMENT,
        f"nothing that declares {subject} produced a requirement: every declaration was either refused "
        "(it names no place inside the firmware root) or sits outside the enumeration its own core "
        "performs — the emulators below say which, one file at a time; an empty list means unresolved, "
        "not complete",
        data,
    )


def _declaration_unknown(subject: str, data: dict[str, str]) -> Caveat:
    """A read that did not happen — the one thing that may never become a claim."""
    return Caveat(
        CAVEAT_FIRMWARE_DECLARATION_UNKNOWN,
        f"whether anything here declares {subject} could not be established — the enumeration this "
        "answer would derive it from did not happen, or nothing in it could be read; the empty list "
        "below is a look that failed, never 'nothing needed'",
        data,
    )


def _declared_without_requiring(cores: tuple[CoreFirmware, ...]) -> bool:
    """Was firmware declared here that never became a requirement?

    Both halves are fields: a declaration atlas would not follow (``refused``)
    and one the emulator never asks for (``unread``). Reading them back out of
    the caveat list would make a message the load-bearing part of an answer.
    """
    return any(core.refused or core.unread for core in cores)


def _empty_answer_caveat(
    cores: tuple[CoreFirmware, ...],
    *,
    enumerated: bool,
    declared: bool,
    subject: str,
    data: dict[str, str],
) -> Caveat:
    """Which kind of empty this answer is — one code per kind, never one for three.

    An answer with no requirement is empty for one of three reasons, and they
    are different instructions to a client: nothing is declared (read, and the
    absence is the answer), something is declared that never became a
    requirement (the machine says what it wants and atlas would not follow it),
    or what is declared could not be established at all (a read that did not
    happen). A single code covering all three reads as the mildest of them,
    which is how a failed enumeration becomes "this machine needs nothing" —
    so each kind carries its own code and a consumer branches on that alone.

    *enumerated* is whether the enumeration this answer rests on happened;
    *declared* whether the subject was declared without becoming a requirement.
    Order matters: a subject where something could not be read may not be
    reported as "declared but unresolved", because that claims atlas saw the
    declarations.

    "Nothing is declared" needs **every** emulator in the answer to have been
    read, not merely one: an absence is a claim about all of them, and one
    readable core alongside five unreadable ones establishes nothing about
    what those five want. The reference machine reads 206 of its 211 cores,
    so the weaker reading would answer "nothing needed" over five unknowns on
    every query it touches.
    """
    if not enumerated or not all(core.declaration == DECLARATION_READ for core in cores):
        return _declaration_unknown(subject, data)
    if declared:
        return _no_requirement(subject, data)
    return _no_declaration(subject, data)


def _why_uncounted(raw_count: str) -> str:
    """Why an enumeration reached no further — what its ``firmware_count`` said."""
    bound = cfg_uint(raw_count)
    if not raw_count:
        return "its .info states no firmware_count, and without one it enumerates no firmware at all"
    if bound is None:
        return (
            f"its firmware_count is {raw_count!r}, which RetroArch does not read as a number, "
            "so it enumerates no firmware at all"
        )
    if bound == 0:
        return "its firmware_count is 0, so it enumerates no firmware"
    return f"its firmware_count is {bound}, so it reads firmware0_ up to firmware{bound - 1}_ and no further"


# The other two reasons a declaration goes unread. Both are properties of the
# key alone, so unlike the uncounted one they need nothing from the file.
_WHY_UNREAD = {
    UNREAD_NO_SLOT: "no key it composes is spelled that way",
    UNREAD_EMPTY: "an empty value, which the read that finds it discards",
}
# Told in the order upstream fails in, so a file wrong in several ways reads
# from the earliest failure to the latest rather than in dictionary order.
_UNREAD_ORDER = (UNREAD_NO_SLOT, UNREAD_UNCOUNTED, UNREAD_EMPTY)


def _why_unread(unread: tuple[str, ...], raw_count: str) -> str:
    """Why each declaration went unread, grouped so every reason present is stated.

    A file can be wrong in more than one way at once, and one reason standing
    for three would name a cause the reader cannot find in their file. So the
    keys are grouped by :func:`atlas.core_info.unread_reason` and each group
    states its own; a group only names its keys when there is another group to
    tell it apart from, because with one reason the caveat has already listed
    them all.
    """
    grouped: dict[str, list[str]] = {}
    for key in unread:
        grouped.setdefault(unread_reason(key, raw_count), []).append(key)
    clauses: list[str] = []
    for reason in _UNREAD_ORDER:
        keys = grouped.get(reason)
        if not keys:
            continue
        why = _why_uncounted(raw_count) if reason == UNREAD_UNCOUNTED else _WHY_UNREAD[reason]
        clauses.append(f"{', '.join(keys)} ({why})" if len(grouped) > 1 else why)
    return "; ".join(clauses)


def _unread_declaration_caveats(core: CoreDeclarations) -> tuple[Caveat, ...]:
    """State the firmware a core's ``.info`` declares outside its own enumeration.

    RetroArch reads firmware through the ``firmware_count`` slots it composes
    keys for and takes nothing else
    (:func:`atlas.core_info.enumerate_firmware`), so a path declared under a
    spelling it never composes, at a slot the count does not reach, or with an
    empty value it discards, is a line the file states and the emulator acts on
    nowhere. atlas answers what the emulator reads — and says so here, because
    the answer on its own looks exactly like a core that simply wants less than
    its file lists.
    """
    if not core.unread:
        return ()
    keys = ", ".join(core.unread)
    return (
        Caveat(
            CAVEAT_FIRMWARE_DECLARATION_UNREAD,
            f"{core.core_so} declares {keys}, which RetroArch does not take: "
            f"{_why_unread(core.unread, core.firmware_count)} "
            "— what they state is not part of this core's requirements",
            {
                "core_so": core.core_so,
                "declared": keys,
                "firmware_count": core.firmware_count,
            },
        ),
    )


def _core_caveats(core: CoreDeclarations, refusals: tuple[Caveat, ...]) -> tuple[Caveat, ...]:
    """Everything one resolved core states — the same set on every route.

    The two routes that resolve a read declaration (the inventory/system-by-
    systemname one and the catalogue one) must state the same facts about the
    same core, or the answer a consumer gets depends on which question it
    asked.
    """
    return (
        *refusals,
        *_unread_declaration_caveats(core),
        *system_assignment_caveats(core),
        *catalogue_vocabulary_caveats(core),
    )


def _read_core(
    machine: Machine,
    context: FirmwareContext,
    core: CoreDeclarations,
    label: str | None,
    *,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """One core whose ``.info`` was read: the answer for it, and what it observed.

    Both routes that reach a read declaration — the per-core/system one and the
    catalogue one — build it here, because they differ only in where the label
    comes from. A field carried at one site and forgotten at the other is
    invisible: the answer still type-checks, the caveat that mentions the fact
    still appears on the core, and the suite stays green. ``unread`` was
    exactly that. It reached the catalogue route empty, so
    ``_declared_without_requiring`` saw nothing declared and an empty
    ``firmware_for_system`` answer stopped saying which kind of empty it was.
    """
    requirements, refused, core_caveats, observed = _requirements_for(
        machine, context, core, verify=verify
    )
    return (
        CoreFirmware(
            core_so=core.core_so,
            label=label,
            declaration=DECLARATION_READ,
            requirements=requirements,
            caveats=_core_caveats(core, core_caveats),
            refused=refused,
            unread=core.unread,
            unread_stating_a_path=core.unread_stating_a_path,
        ),
        observed,
    )


def _resolve_cores(
    machine: Machine,
    context: FirmwareContext,
    cores: tuple[CoreDeclarations, ...],
    *,
    verify: bool,
    labels: Mapping[str, str] | None = None,
) -> tuple[tuple[CoreFirmware, ...], list[Caveat]]:
    resolved: list[CoreFirmware] = []
    answer_caveats: list[Caveat] = []
    for core in cores:
        label = None if labels is None else labels.get(core.core_so)
        if core.info_status != READ_OK:
            resolved.append(_undeclarable_core(core, label))
            continue
        answer, observed = _read_core(machine, context, core, label, verify=verify)
        answer_caveats.extend(observed)
        resolved.append(answer)
    return tuple(resolved), answer_caveats


def _cores_a_derived_assignment_may_hide(
    all_cores: tuple[CoreDeclarations, ...], selected: tuple[CoreDeclarations, ...], system: str
) -> tuple[CoreDeclarations, ...]:
    """Installed cores this system query could not reach *because* of a derived slug.

    Without a frontend catalogue the selection is keyed on the cores' own
    ``systemname``. A core whose firmware was filed by a system that was derived
    rather than ruled can therefore sit under the wrong slug — and then it is not
    selected, so the caveat that would have said so never gets attached either.

    A candidate has to be one: the core must have a derived assignment **and**
    its own ``database`` must name the queried system. That is on-machine
    evidence that this core covers it, so atari800 shows up for ``atari5200``
    (its database names ``Atari - 5200`` while its systemname says ``Atari
    8-bit Family``) while thirty unrelated cores do not. Listing every derived
    core instead would put atari800 and blueMSX under a PlayStation query and
    train the reader to skip the line.
    """
    chosen = {core.core_so for core in selected}
    return tuple(
        core
        for core in all_cores
        if core.core_so not in chosen
        and any(SYSTEMNAME_TO_SLUG.get(name) == system for name in core.database)
        and system_assignment_caveats(core)
    )


def _undeclarable_core(core: CoreDeclarations, label: str | None) -> CoreFirmware:
    """A core that is here, whose ``.info`` is not — present, and unexplained."""
    return CoreFirmware(
        core_so=core.core_so,
        label=label,
        declaration=DECLARATION_UNREADABLE,
        requirements=(),
        caveats=(
            Caveat(
                CAVEAT_CORE_INFO_UNREADABLE,
                f"{core.core_so} is installed, but its .info could not be read ({core.info_status}) — what "
                "this core wants is unknown, so the empty list below is not 'needs nothing'",
                {"core_so": core.core_so, "status": core.info_status},
            ),
        ),
    )


def firmware_for_core(
    machine: Machine, context: FirmwareContext, *, core_so: str, verify: bool = False
) -> FirmwareAnswer:
    """Does *core_so* need firmware, and where does each file go?

    *core_so* is the core's ``.so`` name (``"mgba_libretro.so"``), its bare
    stem, or a full path — all three name the same core. An installed core that
    declares nothing answers ``declaration="read"`` with an empty requirement
    list: that is the honest "no, it needs nothing". A core whose ``.info``
    could not be read answers ``"unreadable"``, and one this installation does
    not ship answers ``"absent"`` — in both, the empty list means unknown.
    """
    stem = os.path.basename(core_so)
    if stem.endswith(".so"):
        stem = stem[: -len(".so")]
    if context.root is None:
        return _empty_answer(context)
    match = next((c for c in context.cores if c.stem == stem), None)
    if match is None:
        # The core-level twin of an unknown system: the caller named something
        # this installation does not have. Saying "no firmware declared" here
        # would read as "needs nothing" for a core that may declare plenty —
        # and when the cores were never enumerated, atlas cannot even claim
        # absence, so it says only that nothing could be read.
        reason = (
            Caveat(
                CAVEAT_CORE_NOT_INSTALLED,
                f"{stem}.so is not installed here (it is not among the cores atlas enumerated) — there is no "
                "declaration for it, so the empty list means unknown, not 'needs nothing'",
                {"core_so": f"{stem}.so"},
            )
            if context.cores_read
            else _declaration_unknown(f"firmware for {stem}.so", {"core_so": f"{stem}.so"})
        )
        return FirmwareAnswer(
            root=context.root,
            cores=(
                CoreFirmware(
                    core_so=f"{stem}.so",
                    label=None,
                    declaration=DECLARATION_ABSENT,
                    requirements=(),
                    caveats=(reason,),
                ),
            ),
            unclaimed=(),
            hash_checked=verify,
            sources=context.sources,
            caveats=(*context.caveats, reason),
        )
    cores, caveats = _resolve_cores(machine, context, (match,), verify=verify)
    return FirmwareAnswer(
        root=context.root,
        cores=cores,
        unclaimed=(),
        hash_checked=verify,
        sources=context.sources,
        caveats=(*context.caveats, *caveats),
    )


def derived_core_selection(
    cores: tuple[CoreDeclarations, ...], system: str
) -> tuple[tuple[CoreDeclarations, ...], Caveat | None]:
    """The installed cores filed under *system* by their own declarations.

    The selection every catalogue-less answer shares — the firmware route and
    the catalogue route (issue #133) alike, one function so the two can never
    derive different lists for one system: a core answers for *system* when
    the map files the core itself there, or any one of its declared files
    (the per-file overrides put Flycast under ``naomi``). The second element
    is the may-hide statement, ``None`` when nothing is at risk: a list keyed
    on the cores' own ``systemname`` can miss a core whose assignment was
    derived rather than ruled, and saying which beats an under-inclusive
    list that reads as complete.
    """
    selected = tuple(
        core
        for core in cores
        if core.system == system or any(d.system == system for d in core.firmware)
    )
    hidden = _cores_a_derived_assignment_may_hide(cores, selected, system)
    if not hidden:
        return selected, None
    names = ", ".join(sorted(c.core_so for c in hidden))
    return selected, Caveat(
        CAVEAT_SYSTEM_ASSIGNMENT_MAY_HIDE_CORES,
        f"this list is keyed on the cores' own systemname, and {len(hidden)} installed core(s) "
        f"name {system!r} in their database while filing firmware by a system that was derived "
        f"rather than ruled — an emulator missing from this list is one of these: {names}",
        {
            "count": str(len(hidden)),
            "cores": names,
            "system": system,
            "table_version": FIRMWARE_SYSTEM_OVERRIDE_VERSION,
        },
    )


# A catalogue-shaped answer whose entries come from the installed cores' own
# declarations rather than from any catalogue (issue #133). One code on every
# such answer, whatever the arrangement's catalogue status beside it says
# (unavailable on a bare RetroArch, sealed on EmuDeck): the list is real, the
# order claims no default, and no entry carries a launch command.
CAVEAT_EMULATOR_LIST_DERIVED = "emulator-list-derived"


def derived_enumeration_lead(system: str) -> Caveat:
    """The lead caveat framing a list derived from the cores of a catalogue-less arrangement."""
    return Caveat(
        CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
        "this installation ships no emulator catalogue, so the emulators for this system are derived "
        "from the installed cores' own systemname — the identifier is atlas's one system vocabulary "
        "either way; what a catalogue would have said about the emulator list is unknown",
        {"system": system},
    )


def _derived_enumeration(
    machine: Machine, context: FirmwareContext, system: str, *, verify: bool
) -> tuple[list[CoreFirmware], list[Caveat]]:
    """The emulators filed under *system* by the installed cores' own declarations.

    :func:`derived_core_selection`, resolved for the firmware answer.
    Observation caveats and the may-hide statement ride along. What does
    *not* ride along is the lead caveat framing the list — *why* the
    enumeration is derived differs between the two callers (an arrangement
    with no catalogue at all, and a word no catalogue can declare), and each
    states its own reason.
    """
    selected, hidden = derived_core_selection(context.cores, system)
    cores, observation_caveats = _resolve_cores(machine, context, selected, verify=verify)
    caveats = list(observation_caveats)
    if hidden is not None:
        caveats.append(hidden)
    return list(cores), caveats


def _cores_by_systemname(
    machine: Machine, context: FirmwareContext, system: str, *, verify: bool
) -> tuple[list[CoreFirmware], list[Caveat]]:
    """The emulators for *system* where no frontend catalogue exists.

    The installed cores' own ``systemname`` is then the only enumeration there
    is. The identifier is the same vocabulary as everywhere else — ES-DE ids,
    which the systemname map speaks since its version 2 — but *which emulators
    carry it* was derived from the cores rather than read from a frontend, and
    the caveat states that: an enumeration a catalogue would disagree with is
    a real possibility, an identifier mismatch is not.
    """
    cores, caveats = _derived_enumeration(machine, context, system, verify=verify)
    return cores, [derived_enumeration_lead(system), *caveats]


def _uncatalogued_word_caveat(system: str) -> Caveat:
    """The answer-level mark on a question asked in one of atlas's own spellings.

    Not :data:`CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE`, however similar the
    consequence: that code claims the *machine* ships no catalogue, which is
    false on a catalogued arrangement. The true fact is about the word — no
    build declares it, so no catalogue anywhere could enumerate or deny its
    emulators — and it holds identically on every arrangement, which is what
    makes the own spellings one vocabulary rather than a per-route dialect.
    """
    return Caveat(
        CAVEAT_SYSTEM_NOT_IN_CATALOGUE,
        f"no ES-DE system id exists for this system ({SYSTEMS_WITHOUT_CATALOGUE_ID[system]}); "
        f"{system!r} is atlas's own spelling — no frontend catalogue can enumerate its emulators, "
        "so this list is derived from the installed cores' own systemname on every arrangement",
        {"system": system, "map_version": SYSTEMNAME_MAP_VERSION},
    )


def _packaged_provenance_caveat(entry: CatalogueEntry, card: StandaloneFirmwareCard) -> Caveat:
    """The provenance statement every ``packaged`` entry carries."""
    return Caveat(
        CAVEAT_FIRMWARE_PACKAGED_DECLARATION,
        f"{entry.label}'s expectations are atlas's packaged card, established from the "
        f"emulator's source at the shipped release, not a declaration read off this machine "
        f"— {card.provenance}",
        {"label": entry.label, "token": card.token},
    )


def _packaged_standalone_core(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    system: str,
    *,
    data_home: str,
    config_home: str,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """A carded standalone entry: packaged declaration, live destinations.

    The card names the XDG base and the emulator-relative path the emulator
    itself probes; the caller supplied the bases this entry's binary reads —
    the arrangement's own, or the entry's per-launch override — so the
    destination is the join the emulator performs, resolved through symlinks
    the way every requirement path is, and what sits there is read live
    (:func:`_observe`, identity-less: no packaged identity can exist for a
    user-supplied file, so a present file answers ``unknown`` and stays
    honestly unverifiable).
    """
    requirements: list[FirmwareRequirement] = []
    answer_caveats: list[Caveat] = []
    for declared in card.files:
        base = data_home if declared.base == "data" else config_home
        composed = os.path.join(base, declared.subdir, declared.name)
        path = resolve_links(machine, composed) or composed
        found, checked, caveat = _observe(machine, path, None, verify=verify)
        if caveat is not None:
            answer_caveats.append(caveat)
        requirements.append(
            FirmwareRequirement(
                core_so=None,
                system=system,
                system_source=SOURCE_CARD,
                # The loader validated the card's need against this same
                # closed pair; the cast bridges the module boundary the
                # import direction imposes (standalone_firmware cannot
                # import the Literal from here without a cycle).
                need=cast(FirmwareNeed, declared.need),
                file_name=declared.name,
                path=path,
                declared=os.path.join(declared.subdir, declared.name),
                description=declared.purpose,
                identity=None,
                found=found,
                checked=checked,
            )
        )
    return (
        CoreFirmware(
            core_so=None,
            label=entry.label,
            declaration=DECLARATION_PACKAGED,
            requirements=tuple(sorted(requirements, key=_by_destination)),
            caveats=(_packaged_provenance_caveat(entry, card),),
        ),
        answer_caveats,
    )


def _melonds_config_unreadable_core(entry: CatalogueEntry, path: str) -> CoreFirmware:
    """The governing config exists and could not be read — present, unexplained."""
    return CoreFirmware(
        core_so=None,
        label=entry.label,
        declaration=DECLARATION_UNREADABLE,
        requirements=(),
        caveats=(
            Caveat(
                CAVEAT_EMULATOR_CONFIG_UNREADABLE,
                f"melonDS's configuration ({path}) exists and could not be read — which "
                "files this launch would probe is unknown, so the empty list below is not "
                "'needs nothing'",
                {"label": entry.label, "config": path},
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class _ConfigProbe:
    """One probed configuration key: the requirement it names, or why it names none."""

    requirement: FirmwareRequirement | None = None
    entry_caveats: tuple[Caveat, ...] = ()
    answer_caveats: tuple[Caveat, ...] = ()


def _expect_card_keys(card: StandaloneFirmwareCard, read: tuple[str, ...]) -> None:
    """The keys a card declares and the keys its resolver reads are one set.

    The guard was one-way: a key the resolver reads and the card omits raises,
    so nothing stopped a card from declaring a key no answer is derived from.
    Such an entry is decorative — it carries a purpose and a citation that
    describe a setting atlas never looks at, and it reads like coverage.
    Crossing both ways is what keeps the card and the reading one statement.

    Only for the resolvers whose key set is fixed. melonDS's is not: its card
    declares every key ``verifySetup`` may probe, and which of them one answer
    reads is decided by two switches read live, so a subset there is correct.
    """
    declared = {entry.key for entry in card.config_files}
    if declared != set(read):
        raise ValueError(
            f"standalone firmware card {card.token!r} declares {sorted(declared)} and this "
            f"resolver reads {sorted(read)} — the card and the code shipped out of step"
        )


def _sandbox_host_path(
    sandbox: SandboxTranslation | None,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    key: str,
    value: str,
) -> tuple[str | None, Caveat | None]:
    """An absolute configuration value as this host reads it, or why it cannot.

    Only absolute values go through the sandbox map: a relative one is
    composed against a root this route already resolved on the host side. A
    value the map cannot land anywhere is not a missing file — it is a path
    only the emulator's own sandbox can open, and saying "missing" about it
    would report a fault that is not there.
    """
    if sandbox is None or not os.path.isabs(value):
        return value, None
    host = sandbox.translate(value)
    if host is not None:
        return host, None
    return None, Caveat(
        CAVEAT_SANDBOX_PATH_UNTRANSLATED,
        f"{entry.label}'s {key} is {value!r}, a path only the emulator's sandbox can read — "
        "where it lands on this host is not established, so nothing below says whether the "
        "file is there",
        {"label": entry.label, "token": card.token, "key": key, "path": value},
    )


def _melonds_probe(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    declared: StandaloneFirmwareConfigFile,
    config: melonds.MelonConfig,
    system: str,
    *,
    config_home: str,
    flatpak: str | None,
    sandbox: SandboxTranslation | None,
    verify: bool,
) -> _ConfigProbe:
    """One key read the way the emulator reads it, then observed live.

    The value is resolved the way ``OpenLocalFile`` resolves it — absolute as
    spelled, anything else below the melonDS config directory
    (Platform.cpp:157-178) — and a value that names no file at all (unset, or
    ending in a directory step) is refused with the vocabulary the core route
    refuses such a declaration with, rather than answered with a directory
    dressed as a file. An absolute one is the emulator's own spelling, so it
    goes through the sandbox map before it becomes a host read.
    """
    value = melonds.get_string(config, declared.key)
    if os.path.basename(value) in ("", ".", ".."):
        return _ConfigProbe(
            entry_caveats=(
                Caveat(
                    CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE,
                    f"{entry.label}'s {declared.key} is {value!r}, which ends in a directory "
                    "step ('.', '..', or nothing at all) and so names no file — this launch "
                    "probes it and its own check fails, so no destination is stated for a "
                    "file the launch does require",
                    {
                        "label": entry.label,
                        "token": card.token,
                        "key": declared.key,
                        "declared": value,
                        "need": NEED_REQUIRED,
                    },
                ),
            )
        )
    host, untranslated = _sandbox_host_path(sandbox, entry, card, declared.key, value)
    if host is None:
        assert untranslated is not None
        return _ConfigProbe(entry_caveats=(untranslated,))
    composed = melonds.local_file_path(config_home, host, flatpak)
    path = resolve_links(machine, composed) or composed
    found, checked, observed = _observe(machine, path, None, verify=verify)
    return _ConfigProbe(
        requirement=FirmwareRequirement(
            core_so=None,
            system=system,
            system_source=SOURCE_CARD,
            need=NEED_REQUIRED,
            # The name the configuration spelled, not the one symlinks end at
            # — the same rule the card-declared route follows, so a resolved
            # destination never renames what the emulator asked for.
            file_name=os.path.basename(value),
            path=path,
            declared=value,
            description=declared.purpose,
            identity=None,
            found=found,
            checked=checked,
        ),
        answer_caveats=() if observed is None else (observed,),
    )


def _melonds_standalone_core(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    system: str,
    *,
    data_home: str,
    config_home: str,
    flatpak: str | None,
    sandbox: SandboxTranslation | None,
    xdg_pinned: bool,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """melonDS's expectations: ``verifySetup`` performed as reads.

    Which files the launch probes is decided by two configuration switches
    read live — ``Emu.ExternalBIOSEnable`` and ``Emu.ConsoleType``
    (verifySetup, EmuInstance.cpp:633-667 at 1.1) — and every probed path is
    a configuration value. With the external-BIOS switch off — the compiled
    default — DS games boot on the built-in replacement, and the empty
    requirement list is the true answer, stated with the switch named.
    """
    del data_home  # melonDS keeps its settings under the config home
    del xdg_pinned  # and states that one base, so no launch has a root to pick
    read = melonds.read_config(machine, config_home, flatpak)
    if read.unreadable is not None:
        return _melonds_config_unreadable_core(entry, read.unreadable), []
    config = read.config
    assert config is not None  # a read is either a document or an unreadable path
    extbios = melonds.get_bool(config, "Emu.ExternalBIOSEnable")
    console = melonds.console_type(config)
    by_key = {declared.key: declared for declared in card.config_files}
    requirements: list[FirmwareRequirement] = []
    answer_caveats: list[Caveat] = []
    caveats: list[Caveat] = [_packaged_provenance_caveat(entry, card)]
    for key in melonds.probed_firmware_keys(extbios, console):
        declared = by_key.get(key)
        if declared is None:
            raise ValueError(
                f"standalone firmware card {card.token!r} names no entry for {key!r} — the "
                "card and the code shipped out of step"
            )
        probe = _melonds_probe(
            machine,
            entry,
            card,
            declared,
            config,
            system,
            config_home=config_home,
            flatpak=flatpak,
            sandbox=sandbox,
            verify=verify,
        )
        if probe.requirement is not None:
            requirements.append(probe.requirement)
        caveats.extend(probe.entry_caveats)
        answer_caveats.extend(probe.answer_caveats)
    if not extbios:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_BUILTIN_REPLACEMENT,
                f"{entry.label} boots DS games on its built-in BIOS/firmware replacement — "
                "Emu.ExternalBIOSEnable is off (the compiled default), so no external DS "
                "BIOS or firmware is probed, and Wi-Fi settings persist in wfcsettings.bin "
                "in the config directory instead (EmuInstance.cpp:673-686); switching it on "
                "makes the DS.* paths required. In DSi mode the DSi BIOS pair and NAND stay "
                "required regardless",
                {
                    "label": entry.label,
                    "token": card.token,
                    "switch": "Emu.ExternalBIOSEnable",
                    # The value as read, so a client can compare it against
                    # the configuration it would edit: 0 is DS, 1 is DSi.
                    "console_type": str(console),
                },
            )
        )
    return (
        CoreFirmware(
            core_so=None,
            label=entry.label,
            declaration=DECLARATION_PACKAGED,
            requirements=tuple(sorted(requirements, key=_by_destination)),
            caveats=tuple(caveats),
        ),
        answer_caveats,
    )


# The resolvers for ``config_files`` cards, keyed by token the way the save
# resolvers are — the card states the keys and the claims, the code here
# reads the emulator's own gating, and a card without its function is the
# card and the code shipped out of step.
# PCSX2 v2.6.3 — the expectation takes two settings, not one: a directory to
# look in and a name to look for. ``FullpathToBios`` combines them and returns
# nothing at all while the name is empty (Pcsx2Config.cpp:2057-2062), which is
# the state a fresh install is in.
_PCSX2_DATA_ROOT = "PCSX2"
_PCSX2_BIOS_DIR_KEY = ("Folders", "Bios")
_PCSX2_BIOS_DIR_DEFAULT = "bios"
_PCSX2_BIOS_NAME_KEY = ("Filenames", "BIOS")


def _pcsx2_standalone_core(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    system: str,
    *,
    data_home: str,
    config_home: str,
    flatpak: str | None,
    sandbox: SandboxTranslation | None,
    xdg_pinned: bool,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """PCSX2's expectation: the image named inside the directory named.

    Two settings of one file. ``[Folders] Bios`` gives the directory — the
    same ``LoadPathFromSettings`` shape the memory-card and texture
    directories use, so a relative value resolves against the DataRoot — and
    ``[Filenames] BIOS`` gives the image inside it. An empty name is the
    shipped state rather than a fault: RetroDECK points the directory at its
    BIOS root and leaves the choice to the user. The answer says so, because
    "no BIOS chosen" is why a game would not boot.

    **No per-game statement here, and the reason is an ORDERING rather than a
    layering rule** (#303). Both DuckStation firmware siblings above state their
    per-game layer; PCSX2's does not, and the difference is easy to get wrong.
    ``[Filenames] BIOS`` *is* a layered key — ``FilenameOptions::LoadSave``
    (Pcsx2Config.cpp:1667-1672) is reached from ``VMManager::LoadCoreSettings``
    on ``Host::GetSettingsInterface``, which returns the layered interface
    itself (VMManager.cpp:598-607, :645-648; Host.cpp:173-176). But its only
    consumer is ``LoadBIOS`` (ps2/BiosTools.cpp:317-333, reading
    ``EmuConfig.FullpathToBios()`` at :321), and ``LoadBIOS`` is called from
    exactly one place, **before** the layer for that game exists:

        VMManager.cpp:1398   if (!LoadBIOS())          <- reads the base value
        VMManager.cpp:1427   UpdateDiscDetails(true);  <- installs the layer at :1108

    Nothing re-reads the BIOS afterwards (the only other reader of
    ``FullpathToBios`` compares it to decide a UI relayout,
    ImGui/FullscreenUI.cpp:1029-1034), and no previous game's layer survives to
    the next boot: ``Shutdown`` clears the disc details and calls
    ``UpdateGameSettingsLayer`` with a zero CRC, which installs ``nullptr``
    (:1650), then re-reads settings at :1698 — "clear out any
    potentially-incorrect settings from the last game".

    So a code path being real is not the same as it being reachable, and this
    silence rests on where a call sits rather than on which layer a key comes
    from. **If upstream ever moves the BIOS load after the layer is installed,
    this answer must start speaking** — the same statement its DuckStation
    sibling makes, over ``[Filenames] BIOS``, closing on ``[Folders] Bios``,
    which no per-game file can move (``EmuFolders::LoadConfig``,
    Pcsx2Config.cpp:2280-2316, handed the base layer at both call sites,
    VMManager.cpp:552 and :835). Re-read the ordering before assuming this
    comment still holds at a newer pin.
    """
    del xdg_pinned  # PCSX2 states one base, so no launch has a root to pick
    # Which home the file sits under is the settings table's to say, so both
    # go to the lookup and neither is assumed here.
    ini_path = emulator_settings.settings_file(card.token, "PCSX2.ini").only(
        config_home=config_home, data_home=data_home, flatpak=flatpak
    )
    result = machine.read_text(ini_path)
    if result.status not in (READ_OK, READ_MISSING):
        return (
            CoreFirmware(
                core_so=None,
                label=entry.label,
                declaration=DECLARATION_UNREADABLE,
                requirements=(),
                caveats=(
                    Caveat(
                        CAVEAT_EMULATOR_CONFIG_UNREADABLE,
                        f"PCSX2's configuration ({ini_path}) exists and could not be read — "
                        "which BIOS image this launch expects is unknown, so the empty list "
                        "below is not 'needs nothing'",
                        {"label": entry.label, "config": ini_path},
                    ),
                ),
            ),
            [],
        )
    values = qt_ini.values(result.text or "") if result.status == READ_OK else {}
    data_root = os.path.join(config_home, _PCSX2_DATA_ROOT)
    # Both keys the way the emulator matches them (#295): CSimpleIniA is
    # ASCII case-insensitive (:func:`atlas.qt_ini.simpleini_value`).
    stated_dir = qt_ini.simpleini_value(values, *_PCSX2_BIOS_DIR_KEY)[0] or ""
    _expect_card_keys(
        card, ("/".join(_PCSX2_BIOS_DIR_KEY), "/".join(_PCSX2_BIOS_NAME_KEY))
    )
    by_key = {declared.key: declared for declared in card.config_files}
    declared = by_key["/".join(_PCSX2_BIOS_NAME_KEY)]
    caveats: list[Caveat] = [_packaged_provenance_caveat(entry, card)]
    if not stated_dir:
        bios_dir = os.path.join(data_root, _PCSX2_BIOS_DIR_DEFAULT)
    elif not os.path.isabs(stated_dir):
        bios_dir = os.path.join(data_root, stated_dir)
    else:
        host, untranslated = _sandbox_host_path(
            sandbox, entry, card, "Folders/Bios", stated_dir
        )
        if host is None:
            assert untranslated is not None
            return (
                CoreFirmware(
                    core_so=None,
                    label=entry.label,
                    declaration=DECLARATION_PACKAGED,
                    requirements=(),
                    caveats=(*caveats, untranslated),
                ),
                [],
            )
        bios_dir = host
    name = qt_ini.simpleini_value(values, *_PCSX2_BIOS_NAME_KEY)[0] or ""
    if not name:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE,
                f"{entry.label} has no BIOS image chosen — [Filenames] BIOS is empty, and "
                "FullpathToBios then composes no path at all (Pcsx2Config.cpp:2057-2062), so "
                "no game boots until one is picked. It would be picked from "
                f"{bios_dir}, which is where a PlayStation 2 BIOS belongs on this machine",
                {
                    "label": entry.label,
                    "token": card.token,
                    "key": "Filenames/BIOS",
                    "declared": "",
                    "need": NEED_REQUIRED,
                    "dir": bios_dir,
                },
            )
        )
        return (
            CoreFirmware(
                core_so=None,
                label=entry.label,
                declaration=DECLARATION_PACKAGED,
                requirements=(),
                caveats=tuple(caveats),
            ),
            [],
        )
    # [Filenames] BIOS is a file NAME, and FullpathToBios composes it exactly
    # the way FullpathToMcd composes a memory card's:
    # Path::Combine(EmuFolders::Bios, BaseFilenames.Bios)
    # (Pcsx2Config.cpp:2057-2062 at v2.6.3). os.path.join is wrong for it —
    # Python lets an absolute second argument replace the first, while
    # Path::Combine swallows that value's leading separator and joins it BELOW
    # the BIOS directory (:func:`atlas.qt_ini.path_combine`). The same
    # defect #312 fixed on the memory-card side, reached by a quieter route:
    # there atlas tested for an absolute value and acted on it, here the join
    # itself did the replacing with nothing in the code to read.
    #
    # The directory above went through the sandbox and the name does not: after
    # the combine it carries no root of its own, so there is nothing to
    # translate.
    composed = qt_ini.path_combine(bios_dir, name)
    path = resolve_links(machine, composed) or composed
    found, checked, observed = _observe(machine, path, None, verify=verify)
    requirement = FirmwareRequirement(
        core_so=None,
        system=system,
        system_source=SOURCE_CARD,
        need=NEED_REQUIRED,
        # The basename of what the emulator OPENS, not of the raw value: the
        # two agree for every ordinary name and part company on a degenerate
        # one, and the composed path is the authority on both halves.
        file_name=os.path.basename(composed),
        path=path,
        declared=name,
        description=declared.purpose,
        identity=None,
        found=found,
        checked=checked,
    )
    return (
        CoreFirmware(
            core_so=None,
            label=entry.label,
            declaration=DECLARATION_PACKAGED,
            requirements=(requirement,),
            caveats=tuple(caveats),
        ),
        [] if observed is None else [observed],
    )


# xemu v0.8.135 — four files sit in ``[sys.files]`` and only three are
# firmware. The boot ROM, the flash image and the hard disk each refuse the
# start when missing, in the emulator's own words; the EEPROM is generated
# where none exists and belongs to the save answer, which already states it.
_XEMU_FILE_KEYS = (
    "sys.files/bootrom_path",
    "sys.files/flashrom_path",
    "sys.files/hdd_path",
)


def xemu_file_value(document: Mapping[str, Any], key: str) -> str | None:
    """One ``[sys.files]`` value as written, or ``None`` where it names nothing.

    *key* may be the bare setting (``hdd_path``) or a card's own address for it
    (``sys.files/hdd_path``); the last segment is the name inside the table.

    Public in this module because the save route reads the same four settings
    out of the same file and had its own copy of these four lines — two
    readings of one table that could drift apart while both looked right.
    ``installations`` imports ``firmware``, so the shared one lives here.
    """
    files = document.get("sys", {})
    files = files.get("files", {}) if isinstance(files, Mapping) else {}
    value = files.get(key.rsplit("/", 1)[-1]) if isinstance(files, Mapping) else None
    return value if isinstance(value, str) and value else None


def _xemu_unreadable_core(entry: CatalogueEntry, path: str, why: str) -> CoreFirmware:
    return CoreFirmware(
        core_so=None,
        label=entry.label,
        declaration=DECLARATION_UNREADABLE,
        requirements=(),
        caveats=(
            Caveat(
                CAVEAT_EMULATOR_CONFIG_UNREADABLE,
                f"xemu's configuration ({path}) {why} — which files this launch expects is "
                "unknown, so the empty list below is not 'needs nothing'",
                {"label": entry.label, "config": path},
            ),
        ),
    )


def _xemu_standalone_core(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    system: str,
    *,
    data_home: str,
    config_home: str,
    flatpak: str | None,
    sandbox: SandboxTranslation | None,
    xdg_pinned: bool,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """xemu's expectations: the three files in ``[sys.files]`` that refuse the start.

    Each is a plain path the configuration states outright — no defaults, no
    composition — so the reading is short and the interesting part is which of
    the four keys belong here at all. The EEPROM does not: xemu generates one
    where none exists, and it is the console's own settings, which the save
    answer already states. The hard disk does, and is claimed by both answers
    on purpose: a console does not start without one, and every save lives
    inside it.
    """
    del xdg_pinned  # xemu states one base, so no launch has a root to pick
    # Which home the file sits under is the settings table's to say — xemu's
    # is the data one, and this resolver no longer has to know that.
    toml_path = emulator_settings.settings_file(card.token, "xemu.toml").only(
        config_home=config_home, data_home=data_home, flatpak=flatpak
    )
    result = machine.read_text(toml_path)
    if result.status not in (READ_OK, READ_MISSING):
        return _xemu_unreadable_core(entry, toml_path, "exists and could not be read"), []
    try:
        document: Mapping[str, Any] = (
            tomllib.loads(result.text or "") if result.status == READ_OK else {}
        )
    except tomllib.TOMLDecodeError:
        return _xemu_unreadable_core(entry, toml_path, "is not parseable TOML"), []
    _expect_card_keys(card, _XEMU_FILE_KEYS)
    by_key = {declared.key: declared for declared in card.config_files}
    requirements: list[FirmwareRequirement] = []
    caveats: list[Caveat] = [_packaged_provenance_caveat(entry, card)]
    answer_caveats: list[Caveat] = []
    for key in _XEMU_FILE_KEYS:
        declared = by_key[key]
        value = xemu_file_value(document, key) or ""
        if not value:
            caveats.append(
                Caveat(
                    CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE,
                    f"{entry.label}'s {key} names no file — the setting is empty, and the "
                    "console refuses to start without this one",
                    {
                        "label": entry.label,
                        "token": card.token,
                        "key": key,
                        "declared": "",
                        "need": NEED_REQUIRED,
                    },
                )
            )
            continue
        if not os.path.isabs(value):
            # xemu opens the value relative to its own process's working
            # directory (verbatim into the QEMU options, system/vl.c:2983-3095;
            # probed with plain fopen/access, vl.c:2527-2535 and :2918 with
            # osdep.h:645-653; no launch step chdirs, ui/xemu.c:1278-1379, all
            # at v0.8.135) — a fact of the launch no read of this machine can
            # establish, so no destination is stated and the caveat carries the
            # anchor as data instead of a requirement carrying an invented one.
            caveats.append(
                Caveat(
                    CAVEAT_FIRMWARE_PATH_LAUNCH_DEPENDENT,
                    f"{entry.label}'s {key} is the relative value {value!r}, which xemu "
                    "opens relative to the working directory of the launching process "
                    "(the configured string is passed verbatim into the QEMU options, "
                    "system/vl.c:2983-3095, and opened with plain fopen/access, "
                    "vl.c:2527-2535 and :2918 with osdep.h:645-653, at v0.8.135) — a "
                    "property of the launch, not of the machine, so no destination is "
                    f"stated for a file the launch does require; fill '{HOLE_CWD}' with "
                    "the launcher's working directory to complete the path",
                    {
                        "label": entry.label,
                        "token": card.token,
                        "key": key,
                        "declared": value,
                        "need": NEED_REQUIRED,
                        "path": os.path.join(TEMPLATE_CWD, value),
                    },
                )
            )
            continue
        host, untranslated = _sandbox_host_path(sandbox, entry, card, key, value)
        if host is None:
            assert untranslated is not None
            caveats.append(untranslated)
            continue
        path = resolve_links(machine, host) or host
        found, checked, observed = _observe(machine, path, None, verify=verify)
        if observed is not None:
            answer_caveats.append(observed)
        requirements.append(
            FirmwareRequirement(
                core_so=None,
                system=system,
                system_source=SOURCE_CARD,
                need=NEED_REQUIRED,
                file_name=os.path.basename(value),
                path=path,
                declared=value,
                description=declared.purpose,
                identity=None,
                found=found,
                checked=checked,
            )
        )
    return (
        CoreFirmware(
            core_so=None,
            label=entry.label,
            declaration=DECLARATION_PACKAGED,
            requirements=tuple(sorted(requirements, key=_by_destination)),
            caveats=tuple(caveats),
        ),
        answer_caveats,
    )


# DuckStation (the frozen fork build, 2024-09-19) — the emulator that names no
# file. A directory is searched, every file of an accepted size is kept, and
# what it *is* is decided by hashing it against a table compiled into the
# binary. So the answer is a question about content, and without a content
# check there is nothing to answer with: the honest degradation is to name the
# directory and say the identification was not asked for.
_DUCKSTATION_ANY_REGION = "any"


def _duckstation_unreadable_core(entry: CatalogueEntry, path: str) -> CoreFirmware:
    """The settings file exists and could not be read — present, unexplained."""
    return CoreFirmware(
        core_so=None,
        label=entry.label,
        declaration=DECLARATION_UNREADABLE,
        requirements=(),
        caveats=(
            Caveat(
                CAVEAT_EMULATOR_CONFIG_UNREADABLE,
                f"DuckStation's configuration ({path}) exists and could not be read — where "
                "this launch searches for a BIOS image is unknown, so the empty list below "
                "is not 'needs nothing'",
                {"label": entry.label, "config": path},
            ),
        ),
    )


def _duckstation_named_image(
    machine: Machine,
    entry: CatalogueEntry,
    *,
    system: str,
    region: str,
    key: str,
    name: str,
    bios_dir: str,
    purpose: str,
    verify: bool,
) -> tuple[FirmwareRequirement, Caveat | None]:
    """One region key that names an image: a file name, composed the way the emulator composes it.

    The value is joined onto the search directory rather than read as a path
    of its own (``Path::Combine(EmuFolders::Bios, bios_name)``, bios.cpp:350),
    which is why a name is what belongs in the setting. DuckStation's combine
    is PCSX2's to the token outside an unported ``_WIN32`` arm
    (file_system.cpp:859-874 at stenzek/duckstation@64655818e), so the compose
    goes through the one ported :func:`atlas.qt_ini.path_combine` — an
    absolute value joins *below* the directory, where ``os.path.join`` would
    let it replace the directory and name a file this emulator never opens
    (#320). The requirement is scoped to its one region, because only that
    region's launch reads this key at all (``GetBIOSImage`` switches on the
    console region and reads exactly one of the three, bios.cpp:321-338) — it
    always ends up an option of the entry's alternatives group, never an
    unconditional requirement.
    """
    composed = qt_ini.path_combine(bios_dir, name)
    path = resolve_links(machine, composed) or composed
    found, checked, observed = _observe(machine, path, None, verify=verify)
    requirement = FirmwareRequirement(
        core_so=None,
        system=system,
        system_source=SOURCE_CARD,
        need=NEED_REQUIRED,
        # The basename of what the emulator OPENS, not of the raw value — the
        # same distinction the PCSX2 site above draws: the two agree for every
        # ordinary name and part company on a degenerate one.
        file_name=os.path.basename(composed),
        path=path,
        declared=name,
        description=f"{purpose} — the image this launch opens for a {region} console ({key})",
        identity=None,
        found=found,
        checked=checked,
        regions=(region,),
    )
    del entry
    return requirement, observed


def _duckstation_sized_files(
    machine: Machine, table: duckstation.BiosTable, bios_dir: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Every file in the directory the search would keep, and where the walk stopped short.

    Two globs, because upstream's ``FindFiles`` is asked for hidden names too
    (``FILESYSTEM_FIND_HIDDEN_FILES``) and a wildcard here never matches a
    leading dot. The size test is the emulator's own first filter, and it is
    free: a wrong size settles the file without reading a byte of it.
    """
    matches: list[str] = []
    unreadable: list[str] = []
    for pattern in ("*", ".*"):
        result = machine.glob(os.path.join(bios_dir, pattern))
        matches.extend(result.matches)
        unreadable.extend(result.unreadable)
    kept = [path for path in sorted(set(matches)) if table.accepts_size(machine.file_size(path))]
    return tuple(kept), tuple(sorted(set(unreadable)))


def _duckstation_candidates(
    machine: Machine, table: duckstation.BiosTable, paths: tuple[str, ...]
) -> tuple[duckstation.BiosCandidate, ...]:
    """The kept files with their identities — the content read the emulator performs.

    A digest the seam could not produce is carried as such. Reading it as "the
    table does not know these bytes" would be a verdict on content nobody saw,
    which is the distinction ``_observe`` makes one screen up for the declared
    route.
    """
    candidates = []
    for path in paths:
        digest = machine.file_digest(path, DIGEST_MD5)
        candidates.append(
            duckstation.BiosCandidate(
                path=path,
                image=None if digest is None else table.identify(digest),
                unreadable=digest is None,
            )
        )
    return tuple(candidates)


def _duckstation_region_caveats(
    candidates: tuple[duckstation.BiosCandidate, ...],
    *,
    bios_dir: str,
    card: StandaloneFirmwareCard,
) -> list[Caveat]:
    """What a single pick does not say about a directory holding several regions.

    The pick is made for a console of *any* region, because the region is the
    running disc's and no configuration records it — the production route has
    never had another value to pass. Where the directory holds known images of
    more than one region, naming one of them and stopping reads as a claim
    about what boots: ``IsValidBIOSForRegion`` is tried before ``priority``
    (bios.cpp:387-395), so a disc of the other region is served by its own
    image. One run-time fact atlas cannot read decides between them, which is
    the same shape — and the same code — as the DataRoot the environment picks.
    """
    regions = sorted({c.image.region for c in candidates if c.image is not None})
    if len(regions) < 2:
        return []
    return [
        Caveat(
            CAVEAT_CORE_MODE_UNESTABLISHED,
            f"{bios_dir} holds images of more than one region ({', '.join(regions)}), and "
            "which one boots is decided by the running disc — a fact no configuration "
            "records. The image named above is the one the ranking puts first for a console "
            "of any region; a disc of another region is served by its own",
            {
                "core": card.token,
                "reason": "the console's region is the running disc's",
                "dir": bios_dir,
                "regions": ", ".join(regions),
            },
        )
    ]


def _duckstation_search_caveats(
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    *,
    bios_dir: str,
    pick: duckstation.BiosPick,
    candidates: tuple[duckstation.BiosCandidate, ...],
    table: duckstation.BiosTable,
) -> list[Caveat]:
    """What the pick is, and the ways it is less than a decision."""
    caveats: list[Caveat] = [
        Caveat(
            CAVEAT_FIRMWARE_UNREADABLE,
            f"{candidate.path} is of a size this emulator accepts and its bytes cannot be "
            "read, so what it is stays unestablished — a read failure, not a verdict on the "
            "file"
            + (
                ", and it is the one the ranking reached first, so no image is named below"
                if candidate.path == pick.chosen.path
                else "; the emulator hashes it and may well boot it instead"
            ),
            {"path": candidate.path, "dir": bios_dir, "token": card.token},
        )
        for candidate in sorted(candidates, key=lambda c: c.path)
        if candidate.unreadable
    ]
    # An unreadable pick is fully stated above, and nothing about its identity
    # follows: the whole of what was established is that a read failed.
    image = pick.chosen.image
    if pick.chosen.unreadable:
        return caveats
    if image is None:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_CONTENT_UNIDENTIFIED,
                f"{pick.chosen.path} is of a size this emulator accepts and its bytes are in "
                "no row of DuckStation's own table, so what console it is stays unknown — the "
                "emulator boots such an image anyway and says 'Using an unknown BIOS'. The one "
                "image it recognises without a hash is the OpenBIOS replacement, by an "
                f"eight-byte signature at offset {table.openbios.get('offset')}, which no read "
                "through atlas's seam reaches",
                {"path": pick.chosen.path, "dir": bios_dir, "token": card.token},
            )
        )
    else:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_IMAGE_IDENTIFIED,
                f"{pick.chosen.path} is {image.name} — DuckStation's own table knows these "
                f"bytes, and it is the image a console of any region boots from this "
                f"directory; region {image.region}",
                {
                    "path": pick.chosen.path,
                    "image": image.name,
                    "region": image.region,
                    "token": card.token,
                    "table": str(table.meta.get("revision", "")),
                },
            )
        )
    caveats.extend(_duckstation_region_caveats(candidates, bios_dir=bios_dir, card=card))
    if not pick.decided:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_IMAGE_AMBIGUOUS,
                f"{entry.label} has {len(pick.tied)} images in {bios_dir} that rank exactly "
                "alike, and the emulator keeps the last one the directory hands it — an order "
                "no read reproduces, so which of them boots is not established here",
                {
                    "label": entry.label,
                    "dir": bios_dir,
                    "tied": str(len(pick.tied)),
                    "chosen": pick.chosen.path,
                },
            )
        )
    return caveats


# What a per-game file does to the firmware answer, and — the half worth as
# much — what it cannot do. The image keys and the search directory sit in the
# same ``[BIOS]`` section and are read through different doors: the three names
# through the layered interface, the directory through the base layer alone.
_DUCKSTATION_BIOS_LAYER_GOVERNS = (
    "A per-game value there decides which image inside the search directory a launch loads for "
    "its console region: the name is combined with that directory and loaded outright, where "
    "an empty value leaves the region to the content search (bios.cpp:321-351). The search "
    "directory itself is fixed: [BIOS] SearchDirectory is read from the base settings alone "
    "(EmuFolders::LoadConfig, settings.cpp:1964-1981), and no per-game file moves it."
)
# The door those three come through, and it is not the memory-card keys' door:
# Host::GetStringSettingValue reads the layered interface, unlike the
# Host::GetBase*SettingValue family beside it.
_DUCKSTATION_BIOS_LAYER_READ = (
    "BIOS::GetBIOSImage, bios.cpp:321-338, on Host::GetStringSettingValue, host.cpp:124-128"
)


def _duckstation_game_settings_caveats(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    *,
    read: duckstation.SettingsRead,
    search: StandaloneFirmwareSearch,
    sandbox: SandboxTranslation | None,
) -> list[Caveat]:
    """The per-game layer stated beside the firmware answer, in its own keys.

    The save route makes the same statement about ``[MemoryCards]`` and both
    read it out of :mod:`atlas.duckstation`, so one entry's answers cannot
    drift into two tellings of one fact. The keys are the card's own — the
    three region keys it already names, section-qualified — rather than a list
    written twice, so a card that grew a fourth region would be stated without
    touching this.
    """
    if not duckstation.applies_game_settings(read.values):
        return []
    keys = tuple(f"[{key.replace('/', '] ', 1)}" for _, key in search.region_keys)
    raw = (
        qt_ini.simpleini_value(
            read.values, duckstation.GAME_SETTINGS_SECTION, duckstation.GAME_SETTINGS_KEY
        )[0]
        or ""
    )
    # Only an absolute configured value goes through the sandbox map, the way
    # the save route does it and the way the BIOS directory two calls below
    # does: a relative one was already joined onto a DataRoot this route
    # resolved on the host side, so there is nothing there to translate.
    if os.path.isabs(raw):
        host, untranslated = _sandbox_host_path(
            sandbox,
            entry,
            card,
            f"{duckstation.GAME_SETTINGS_SECTION}/{duckstation.GAME_SETTINGS_KEY}",
            raw,
        )
        if host is None:
            assert untranslated is not None
            # No host spelling means the listing cannot be made from here,
            # which is the unread state and never the silent one — silence
            # would say no game overrides this answer, which is exactly what
            # was not established.
            return [
                untranslated,
                duckstation.per_game_unread_caveat(
                    token=card.token,
                    directory=raw,
                    keys=keys,
                    governs=_DUCKSTATION_BIOS_LAYER_GOVERNS,
                    read_through=_DUCKSTATION_BIOS_LAYER_READ,
                    sandbox_value=raw,
                ),
            ]
        directory = host
    else:
        directory = duckstation.load_path(
            read.values,
            read.root,
            duckstation.GAME_SETTINGS_SECTION,
            duckstation.GAME_SETTINGS_KEY,
            duckstation.GAME_SETTINGS_DEFAULT,
        )
    return duckstation.per_game_caveats(
        machine,
        token=card.token,
        directory=directory,
        keys=keys,
        governs=_DUCKSTATION_BIOS_LAYER_GOVERNS,
        read_through=_DUCKSTATION_BIOS_LAYER_READ,
    )


def _duckstation_standalone_core(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    system: str,
    *,
    data_home: str,
    config_home: str,
    flatpak: str | None,
    sandbox: SandboxTranslation | None,
    xdg_pinned: bool,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """DuckStation's expectation: a directory, and whatever in it is a BIOS.

    Three region keys may name an image, and one launch reads exactly one of
    them — the console region decides, which under the shipped Auto setting
    is the running disc's own (bios.cpp:321-338, system.cpp:1613-1648 and
    :2510). So the moment any key names an image, everything this entry
    states is region-scoped, and it all rides in one
    :class:`FirmwareAlternatives` group: each named image as the option of
    its region, the search's find as the option of the regions left to it.
    Where every key is empty — the state both arrangements ship — the search
    serves whatever region a disc sets, and its answer stays the one
    unconditional requirement it always was. The search is a content question
    either way: without a hash check there is a directory and a count, and
    saying more than that would be a guess dressed as an answer.
    """
    search = card.search
    if search is None:
        raise ValueError(
            f"standalone firmware card {card.token!r} states no search and this "
            "resolver performs one — the card and the code shipped out of step"
        )
    read = duckstation.read_settings(
        machine,
        config_home=config_home,
        data_home=data_home,
        flatpak=flatpak,
        xdg_pinned=xdg_pinned,
    )
    if read.unreadable is not None:
        return _duckstation_unreadable_core(entry, read.unreadable), []
    caveats: list[Caveat] = [_packaged_provenance_caveat(entry, card)]
    if read.ambiguous:
        caveats.append(
            duckstation.dataroot_caveat(card.token, "the search directory below")
        )
    caveats.extend(
        _duckstation_game_settings_caveats(
            machine, entry, card, read=read, search=search, sandbox=sandbox
        )
    )
    section, name = search.directory_key.split("/", 1)
    composed = duckstation.load_path(
        read.values, read.root, section, name, search.directory_default
    )
    # The composed path, not the stated value: a relative value was already
    # joined onto the DataRoot, which is a host path the map leaves alone, so
    # the only spelling that translates here is an absolute one the emulator
    # wrote itself.
    host, untranslated = _sandbox_host_path(
        sandbox, entry, card, search.directory_key, composed
    )
    if host is None:
        assert untranslated is not None
        return (
            CoreFirmware(
                core_so=None,
                label=entry.label,
                declaration=DECLARATION_PACKAGED,
                requirements=(),
                caveats=(*caveats, untranslated),
            ),
            [],
        )
    bios_dir = resolve_links(machine, host) or host
    named: list[FirmwareRequirement] = []
    answer_caveats: list[Caveat] = []
    searched: list[str] = []
    for region, key in search.region_keys:
        key_section, key_name = key.split("/", 1)
        # The key the way the emulator matches it (#295): CSimpleIniA is
        # ASCII case-insensitive (:func:`atlas.qt_ini.simpleini_value`).
        value = qt_ini.simpleini_value(read.values, key_section, key_name)[0] or ""
        if not value:
            searched.append(region)
            continue
        requirement, observed = _duckstation_named_image(
            machine,
            entry,
            system=system,
            region=region,
            key=key,
            name=value,
            bios_dir=bios_dir,
            purpose=search.purpose,
            verify=verify,
        )
        named.append(requirement)
        if observed is not None:
            answer_caveats.append(observed)
    found: list[FirmwareRequirement] = []
    if searched:
        found, search_caveats, observed = _duckstation_search(
            machine,
            entry,
            card,
            system=system,
            search=search,
            bios_dir=bios_dir,
            regions=tuple(searched),
            # Scoped exactly when a named key took a region away from the
            # search: then the search speaks for the regions left to it, and
            # its find is one option among the alternatives rather than what
            # every launch needs.
            scoped=bool(named),
            verify=verify,
        )
        caveats.extend(search_caveats)
        answer_caveats.extend(observed)
    requirements: tuple[FirmwareRequirement | FirmwareAlternatives, ...]
    if named:
        options = tuple(sorted((*named, *found), key=_by_destination))
        requirements = (FirmwareAlternatives(options=options),)
    else:
        requirements = tuple(sorted(found, key=_by_destination))
    return (
        CoreFirmware(
            core_so=None,
            label=entry.label,
            declaration=DECLARATION_PACKAGED,
            requirements=requirements,
            caveats=tuple(caveats),
        ),
        answer_caveats,
    )


def _duckstation_search(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    *,
    system: str,
    search: StandaloneFirmwareSearch,
    bios_dir: str,
    regions: tuple[str, ...],
    scoped: bool,
    verify: bool,
) -> tuple[list[FirmwareRequirement], list[Caveat], list[Caveat]]:
    """The search itself: what the directory holds, and what that establishes.

    The regions left to it share one answer, because the pick differs between
    them only in preference: an image of another region still boots, with a
    warning (bios.cpp:353-359), so what a launch needs is *an* image rather
    than one per region. With *scoped* the find carries those regions — a
    named key took another region away, so the search's answer is that
    group's leftover option, not what every launch needs — and unscoped it
    stays the unconditional requirement of the everything-searched state.
    """
    table = duckstation.bios_table()
    caveats: list[Caveat] = []
    kept, unreadable = _duckstation_sized_files(machine, table, bios_dir)
    if unreadable:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_SCAN_INCOMPLETE,
                f"{', '.join(unreadable)} could not be listed, so the search below saw only "
                "part of what this launch would see",
                {"dir": bios_dir, "unreadable": ", ".join(unreadable)},
            )
        )
    if not kept:
        if unreadable:
            # The listing failed, so "this directory holds nothing of the right
            # size" is a statement about contents nobody saw. The incomplete
            # scan above is the whole of what was established here.
            return [], caveats, []
        state = (
            "holds no file of a size this emulator accepts"
            if machine.path_kind(bios_dir) == KIND_DIRECTORY
            else "is not a directory this launch can search"
        )
        # Only the keys whose regions actually fell to this search: with a
        # named key beside them, "all keys are empty" would be a false claim
        # about a configuration the answer just read.
        empty_keys = ", ".join(key for region, key in search.region_keys if region in regions)
        # The sentence scopes itself only when a named key really took a
        # region away; in the shipped all-keys-empty state the search speaks
        # for every launch, and enumerating the regions would dress the plain
        # case as a conditional one. The regions ride in the data either way.
        scope = f" for a {', '.join(regions)} console" if scoped else ""
        these = "for these regions " if scoped else ""
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_PATH_NAMES_NO_FILE,
                f"{entry.label} has no BIOS image to boot{scope}: "
                f"{bios_dir} {state}, and nothing in the configuration names one "
                f"{these}either ({empty_keys} are empty). A PlayStation starts nothing until "
                "an image is there",
                {
                    "label": entry.label,
                    "token": card.token,
                    "key": search.directory_key,
                    "declared": "",
                    "need": NEED_REQUIRED,
                    "dir": bios_dir,
                    "regions": ", ".join(regions),
                },
            )
        )
        return [], caveats, []
    if not verify:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_SEARCH_UNVERIFIED,
                f"{bios_dir} holds {len(kept)} files of a size this emulator accepts, and "
                "which of them is a BIOS is a question about their bytes — DuckStation hashes "
                "them against its own table, and this answer was asked for without a content "
                "check, so nothing here says one is there",
                {
                    "label": entry.label,
                    "dir": bios_dir,
                    "candidates": str(len(kept)),
                    "need": NEED_REQUIRED,
                    # Which launches the unanswered search speaks for — all of
                    # them in the shipped all-keys-empty state, and only the
                    # leftover regions once a key names another region's image.
                    "regions": ", ".join(regions),
                },
            )
        )
        return [], caveats, []
    candidates = _duckstation_candidates(machine, table, kept)
    pick = table.pick(candidates, _DUCKSTATION_ANY_REGION)
    assert pick is not None  # kept is non-empty
    caveats.extend(
        _duckstation_search_caveats(
            entry, card, bios_dir=bios_dir, pick=pick, candidates=candidates, table=table
        )
    )
    if pick.chosen.unreadable:
        # Nothing was established about the file that would boot, so there is
        # no requirement to state — the same shape this route takes when no
        # content check was asked for at all. Stating one would carry
        # ``satisfied: true`` about bytes nobody read.
        return [], caveats, []
    found, checked, observed = _observe(machine, pick.chosen.path, None, verify=False)
    requirement = FirmwareRequirement(
        core_so=None,
        system=system,
        system_source=SOURCE_CARD,
        need=NEED_REQUIRED,
        file_name=os.path.basename(pick.chosen.path),
        path=pick.chosen.path,
        declared=os.path.basename(pick.chosen.path),
        description=f"{search.purpose} — found by the search, not named by any setting",
        identity=None,
        found=found,
        checked=checked,
        regions=regions if scoped else None,
    )
    return [requirement], caveats, [] if observed is None else [observed]


_STANDALONE_CONFIG_RESOLVERS = {
    "PCSX2": _pcsx2_standalone_core,
    "XEMU": _xemu_standalone_core,
    "MELONDS": _melonds_standalone_core,
    "DUCKSTATION": _duckstation_standalone_core,
}


def _carded_standalone_core(
    machine: Machine,
    entry: CatalogueEntry,
    card: StandaloneFirmwareCard,
    system: str,
    *,
    data_home: str,
    config_home: str,
    flatpak: str | None,
    sandbox: SandboxTranslation | None,
    xdg_pinned: bool,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """A carded standalone entry, routed by the shape its card states.

    A ``config_files`` card's probe set is the emulator's own live decision
    and a ``search`` card's is a directory read, so both need a resolver
    registered beside them; a ``files`` card names its paths outright and the
    one packaged route serves it.
    """
    if not card.config_files and card.search is None:
        return _packaged_standalone_core(
            machine,
            entry,
            card,
            system,
            data_home=data_home,
            config_home=config_home,
            verify=verify,
        )
    resolver = _STANDALONE_CONFIG_RESOLVERS.get(card.token)
    if resolver is None:
        shape = "config_files" if card.config_files else "search"
        raise ValueError(
            f"standalone firmware card {card.token!r} states {shape} but has no "
            "resolver registered — the card and the code shipped out of step"
        )
    # Both bases go to every resolver: which one an emulator keeps its
    # settings under is the emulator's business, not the route's — xemu's live
    # under the data home while melonDS's and PCSX2's live under the config
    # one.
    return resolver(
        machine,
        entry,
        card,
        system,
        data_home=data_home,
        config_home=config_home,
        flatpak=flatpak,
        sandbox=sandbox,
        xdg_pinned=xdg_pinned,
        verify=verify,
    )


def _standalone_entry_core(
    machine: Machine,
    context: FirmwareContext,
    entry: CatalogueEntry,
    system: str,
    *,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """A standalone entry: its card's answer, or the honest refusal.

    The bases are the entry's own where its launch establishes them — on
    EmuDeck the picked variant decides which trees the emulator reads — and
    otherwise the arrangement's pair.
    """
    card = lookup_standalone_firmware_card(entry.standalone_token)
    data_home = entry.standalone_data_home or context.standalone_data_home
    config_home = entry.standalone_config_home or context.standalone_config_home
    flatpak = entry.standalone_flatpak or context.standalone_flatpak
    if (
        card is not None
        and system in card.systems
        and data_home is not None
        and config_home is not None
    ):
        return _carded_standalone_core(
            machine,
            entry,
            card,
            system,
            data_home=data_home,
            config_home=config_home,
            flatpak=flatpak,
            sandbox=context.standalone_sandbox,
            xdg_pinned=context.standalone_xdg_pinned,
            verify=verify,
        )
    return (
        CoreFirmware(
            core_so=entry.core_so,
            label=entry.label,
            declaration=DECLARATION_UNSUPPORTED,
            requirements=(),
            caveats=(
                Caveat(
                    CAVEAT_STANDALONE_UNSUPPORTED,
                    f"{entry.label} is a standalone emulator — its firmware rules are outside "
                    "the resolver's current coverage (ROADMAP.md), so the empty list means "
                    "unknown; the emulator is here, atlas's source for it is not",
                    {"label": entry.label},
                ),
            ),
        ),
        [],
    )


def _catalogue_entry_core(
    machine: Machine,
    context: FirmwareContext,
    entry: CatalogueEntry,
    by_stem: Mapping[str, CoreDeclarations],
    system: str,
    *,
    verify: bool,
) -> tuple[CoreFirmware, list[Caveat]]:
    """One catalogue entry resolved, plus the observations it produced.

    Five states, kept apart: a carded standalone emulator (packaged
    declaration, live destinations), a standalone one outside atlas's
    coverage, a core the catalogue names that is not installed (not here), an
    installed core whose ``.info`` could not be read, and one that was read.
    """
    if entry.kind != KIND_LIBRETRO or entry.core_so is None:
        return _standalone_entry_core(machine, context, entry, system, verify=verify)
    core = by_stem.get(entry.core_so[: -len(".so")] if entry.core_so.endswith(".so") else entry.core_so)
    if core is None:
        # Same guard as the per-core route: absence is a claim, and it
        # needs the core enumeration to have happened.
        reason = (
            Caveat(
                CAVEAT_CORE_NOT_INSTALLED,
                f"the catalogue declares {entry.label} on {entry.core_so}, but that core is "
                "not installed here — atlas has no declaration for it, so the empty list "
                "means unknown, not 'needs nothing'",
                {"core_so": entry.core_so, "label": entry.label},
            )
            if context.cores_read
            else _declaration_unknown(
                f"firmware for {entry.core_so}", {"core_so": entry.core_so, "label": entry.label}
            )
        )
        return (
            CoreFirmware(
                core_so=entry.core_so,
                label=entry.label,
                declaration=DECLARATION_ABSENT,
                requirements=(),
                caveats=(reason,),
            ),
            [],
        )
    if core.info_status != READ_OK:
        return _undeclarable_core(core, entry.label), []
    return _read_core(machine, context, core, entry.label, verify=verify)


def _empty_system_statement(
    cores: tuple[CoreFirmware, ...], *, enumerated: bool, system: str
) -> Caveat | None:
    """The answer-level statement a system answer with no requirement carries — if any.

    An identifier that resolved no emulator at all, on an enumeration that did
    happen, is unknown here (:data:`CAVEAT_SYSTEM_UNKNOWN`) — a different
    answer from "nobody declares firmware for it", and a different thing for a
    client to do. A consumer working in RomM slugs that forgets to translate
    ("dc" for ES-DE's "dreamcast") lands exactly here, and must not read it as
    "nothing needed". A published own spelling is exempt: the word IS
    vocabulary, so nothing filing under it is a machine fact, not an
    identifier mistake — it answers empty through the empty-kind route below,
    the same shape an id whose emulators declare nothing answers with.

    Otherwise the empty-kind vocabulary decides (:func:`_empty_answer_caveat`),
    with one suppression: a system whose emulators were read and declare
    nothing says so per emulator — ``declaration="read"`` with an empty list
    is the answer, exactly as the per-core route gives it, and an answer-level
    line would add nothing while reading as a degradation. What the entries
    cannot say is the other two: that firmware was declared here and never
    became a requirement, or that nothing could be read at all. And only
    entries that exist can say anything: an own spelling nothing files under
    arrives with no cores at all (its ``system-unknown`` deliberately
    suppressed above), so the established absence is stated here or nowhere.
    """
    if not cores and enumerated and system not in SYSTEMS_WITHOUT_CATALOGUE_ID:
        return Caveat(
            CAVEAT_SYSTEM_UNKNOWN,
            f"no emulator on this machine covers the system {system!r} — nothing was resolved, so this "
            "empty answer says the identifier is unknown here, not that nothing is needed; check the "
            "vocabulary before reading it as complete",
            {"system": system},
        )
    if any(core.requirements for core in cores):
        return None
    empty = _empty_answer_caveat(
        cores,
        enumerated=enumerated,
        declared=_declared_without_requiring(cores),
        subject=f"firmware for system {system!r}",
        data={"system": system},
    )
    if empty.code != CAVEAT_NO_FIRMWARE_DECLARATION or not cores:
        return empty
    return None


def firmware_for_system(
    machine: Machine,
    context: FirmwareContext,
    *,
    system: str,
    catalogue: Catalogue | None = None,
    verify: bool = False,
) -> FirmwareAnswer:
    """Which emulators can run *system*, and what each of them wants.

    *system* is one vocabulary on every route: ES-DE's system names, the same
    ids every other question takes (``atlas.systems``), plus the published own
    spellings (:data:`SYSTEMS_WITHOUT_CATALOGUE_ID`) for the systems no ES-DE
    build declares. With a frontend *catalogue* (ES-DE, on the installs that
    ship it) an id's emulator list is the frontend's — including entries whose
    core is not installed and standalone emulators, both stated as such rather
    than dropped. Without one the list is derived from the installed cores'
    own ``systemname`` through the packaged map;
    :data:`CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE` states that the
    *enumeration* is derived — the identifier means the same thing either way.
    A catalogue with a ``hole`` answers between the two: its entries are the
    frontend's and the hole is stated on the answer, while an id its readable
    part does not declare answers empty as a look that failed
    (:data:`CAVEAT_FIRMWARE_DECLARATION_UNKNOWN`), never as
    :data:`CAVEAT_SYSTEM_UNKNOWN` — the declaration may sit in the part of the
    catalogue nobody could consult.

    An own spelling is answered identically on **every** arrangement: no
    catalogue can enumerate or deny a word no build declares, so the cores'
    own declarations are the only enumeration there is, catalogued or not,
    and the answer carries :data:`CAVEAT_SYSTEM_NOT_IN_CATALOGUE` — at answer
    level for the word asked about, and on each core for the files filed
    under one. An own spelling nothing files under answers **empty, marked
    and established**: the word is vocabulary, so the emptiness is a machine
    fact — the mark plus :data:`CAVEAT_NO_FIRMWARE_DECLARATION`, the same
    established absence an id's declaration-less emulators state per entry,
    said at answer level here because there is no entry to say it. And
    :data:`CAVEAT_SYSTEM_UNKNOWN` fires only for words in neither vocabulary.
    (``_unknown`` is deliberately not such a word: it marks cores that state
    no systemname at all, a fact about those cores rather than about a
    system, and it keeps its route-dependent behavior.)

    Each emulator answers with its **whole** declaration set, every requirement
    carrying its own ``system``: a multi-system core (mGBA declares Game Boy
    boot ROMs) wants what it wants regardless of which of its systems was
    asked about, and silently dropping entries would turn "needs firmware" into
    a wrong answer.
    """
    if context.root is None:
        return _empty_answer(context)

    resolved: list[CoreFirmware] = []
    caveats: list[Caveat] = []
    # Whether the enumeration happened at all decides whether this answer may
    # say anything about the machine when it comes back empty. A holed
    # catalogue that names entries still enumerated — the readable part is
    # authoritative for what it declares — but one that names none performed
    # no enumeration for this system: the declaration may sit in the part
    # nobody could consult, which is exactly what the hole states.
    enumerated = (
        context.cores_read
        if catalogue is None
        else catalogue.read and (catalogue.hole is None or bool(catalogue.entries))
    )

    if system in SYSTEMS_WITHOUT_CATALOGUE_ID:
        # A word no build declares is answered from the cores on every
        # arrangement — a catalogue can neither enumerate nor deny it, so
        # consulting one would only ever manufacture "unknown identifier"
        # out of a word this module itself publishes. The cores are the
        # enumeration here even when a catalogue exists, so whether *they*
        # were read is what an empty answer hangs on.
        enumerated = context.cores_read
        resolved, caveats = _derived_enumeration(machine, context, system, verify=verify)
        caveats.insert(0, _uncatalogued_word_caveat(system))
    elif catalogue is None:
        resolved, caveats = _cores_by_systemname(machine, context, system, verify=verify)
    elif not catalogue.read:
        caveats.append(
            Caveat(
                CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
                "the frontend's emulator catalogue could not be read, so which emulators run this system is "
                "unknown — this answer is empty because atlas could not look, not because nothing is there",
                {"system": system},
            )
        )
    else:
        if catalogue.hole is not None:
            # The same position the unreadable statement rides: the part of
            # the catalogue nobody could consult is stated on every answer
            # this catalogue informs, entries or none. Like that statement,
            # the catalogue-status statement is the first caveat its branch
            # appends — an invariant the handles rely on when they ride
            # further statements adjacent to it.
            caveats.append(catalogue.hole)
        by_stem = {core.stem: core for core in context.cores}
        for entry in catalogue.entries:
            core, observed = _catalogue_entry_core(
                machine, context, entry, by_stem, system, verify=verify
            )
            resolved.append(core)
            caveats.extend(observed)

    statement = _empty_system_statement(tuple(resolved), enumerated=enumerated, system=system)
    caveats.extend(() if statement is None else (statement,))

    return FirmwareAnswer(
        root=context.root,
        cores=tuple(resolved),
        unclaimed=(),
        hash_checked=verify,
        sources=context.sources,
        caveats=(*context.caveats, *caveats),
    )


def _unclaimed_identity(
    machine: Machine, context: FirmwareContext, path: str, *, verify: bool
) -> tuple[FirmwareIdentity | None, Caveat | None]:
    """What an unclaimed file *is*, by content — nothing at all without ``verify``.

    The name is exactly what is not to be trusted about a file nobody declared,
    so without hashing atlas states the file and says nothing about it.
    """
    if not verify:
        return None, None
    digest = machine.file_digest(path, DIGEST_MD5)
    sha1 = machine.file_digest(path, DIGEST_SHA1)
    if digest is None or sha1 is None:
        return None, Caveat(
            CAVEAT_FIRMWARE_UNREADABLE,
            f"{path} is there but its bytes cannot be read, so what it is stays unknown",
            {"path": path},
        )
    return context.hashes.for_content(md5=digest, sha1=sha1), None


def _unclaimed_in(
    machine: Machine,
    context: FirmwareContext,
    directory: str,
    claimed: set[str],
    artifacts: set[str],
    *,
    verify: bool,
) -> tuple[list[UnclaimedFile], list[Caveat]]:
    """The files in one directory that no installed core asks for.

    An entry that cannot be looked at is stated, not dropped: whether it is a
    file nobody declared is then unknown, and an unknown is the one thing this
    list may not silently contain — it would arrive as an
    :class:`UnclaimedFile` whose path is real and whose identity was invented.

    Only for entries that survive the exclusions, though. Whether a path is
    *this scan's* subject is settled before anything is said about it: a
    declared destination that cannot be looked at is already stated by
    :func:`_observe`, and saying it again here would put the same fact in the
    answer twice; a save the rule cards claim is never this scan's business at
    all, and a caveat calling an unreadable memory card a possibly-undeclared
    firmware file is the category error the artifact exclusion exists to
    prevent.

    A directory the scan could not list at all is the same fact one level up,
    and it is stated the same way: once, naming the directory. Without it an
    unreadable firmware tree reports as a tree with nothing unclaimed in it,
    which is the reassuring answer and the wrong one.
    """
    found: list[UnclaimedFile] = []
    caveats: list[Caveat] = []
    listing = machine.glob(os.path.join(_glob_escape(directory), "*"))
    caveats.extend(
        Caveat(
            CAVEAT_FIRMWARE_SCAN_INCOMPLETE,
            f"{unreadable} lies in the firmware tree and could not be listed (permissions or an I/O "
            "failure) — whether anything undeclared sits in it is unknown, so the list below is what "
            "atlas could see and not the whole tree",
            {"path": unreadable},
        )
        for unreadable in listing.unreadable
    )
    for entry in listing.matches:
        kind = machine.path_kind(entry)
        if kind not in (KIND_FILE, KIND_INACCESSIBLE):
            continue
        resolved_entry = resolve_links(machine, entry) or entry
        if resolved_entry in claimed or resolved_entry in artifacts:
            continue
        if kind == KIND_INACCESSIBLE:
            caveats.append(
                Caveat(
                    CAVEAT_FIRMWARE_PATH_INACCESSIBLE,
                    f"{entry} lies in the firmware tree, is claimed by no core, and cannot be looked at "
                    "(permissions or an I/O failure) — so whether it is a file nobody declared is unknown; "
                    "it is stated here instead of appearing below as a file atlas never saw",
                    {"path": entry},
                )
            )
            continue
        identity, caveat = _unclaimed_identity(machine, context, entry, verify=verify)
        if caveat is not None:
            caveats.append(caveat)
        found.append(UnclaimedFile(path=resolved_entry, identity=identity))
    return found, caveats


def _unclaimed_files(
    machine: Machine,
    context: FirmwareContext,
    claimed: set[str],
    *,
    verify: bool,
) -> tuple[tuple[UnclaimedFile, ...], list[Caveat]]:
    """Files sitting where a declaration points, that no installed core asks for.

    The scan is bounded to the directories declarations actually reference —
    the system directory itself plus every subdirectory named in a
    ``firmwareN_path``. An unbounded walk would mark thousands of files (whole
    core data trees live under the same root) and drown the signal; this bound
    needs no exclusion list and grows on its own as cores declare new paths.
    Save data the rule cards claim is excluded outright
    (:func:`save_artifact_paths`).

    Every scanned directory is clamped to the root's subtree, and the root
    itself is the only one that may equal it. A claimed path is resolved and
    inside the root, but that is not enough: a folder declaration is allowed to
    land on the root exactly (LRPS2's ``pcsx2/bios``, which RetroDECK links
    back to it), and the *parent* of that landing is one level above the
    firmware tree. Without the clamp a stock RetroDECK scans it, and whatever
    sits there is reported — and with ``verify`` hashed — as unclaimed
    firmware.

    Names beginning with a dot never appear in this list, and that is a
    decision rather than an oversight: a wildcard in the seam's glob does not
    match a leading dot (:mod:`atlas.machine` states the rule normatively, and
    a real ``glob`` behaves the same), and what sits under a dot in a firmware
    tree is tooling residue — a file manager's ``.directory``, a sync tool's
    bookkeeping — not firmware anyone placed there. It narrows *this list*
    only: a declared path is resolved, never globbed, so a core that declares a
    dotted file still gets its requirement answered.
    """
    root = context.root
    assert root is not None
    resolved_root = resolve_links(machine, root) or root
    # Resolved, because that is what the entries below are compared against:
    # RetroDECK's dir_prep links whole firmware subdirectories elsewhere, and a
    # VMU reached through ``dc -> dreamcast`` is the same save file under both
    # spellings — matched on the unresolved name it would be reported as
    # firmware nobody asked for.
    artifacts = {
        resolve_links(machine, path) or path
        for path in (os.path.join(resolved_root, name) for name in save_artifact_paths())
    }
    directories = {resolved_root} | {
        parent
        for parent in (os.path.dirname(p) for p in claimed)
        if _stays_under(resolved_root, parent)
    }
    found: list[UnclaimedFile] = []
    caveats: list[Caveat] = []
    for directory in sorted(directories):
        in_directory, unreadable = _unclaimed_in(
            machine, context, directory, claimed, artifacts, verify=verify
        )
        found.extend(in_directory)
        caveats.extend(unreadable)
    return tuple(sorted(found, key=lambda f: f.path)), caveats


def firmware_inventory(machine: Machine, context: FirmwareContext, *, verify: bool = False) -> FirmwareAnswer:
    """Every installed core's firmware, plus what is lying around unclaimed.

    The aggregate view: how much is in place, how much of that is
    hash-correct, what is missing, and what sits in the firmware tree that
    nothing asks for. Unclaimed files are identified by **content**, so
    ``verify`` is what turns their identity on — a name proves nothing about a
    file nobody declared.
    """
    if context.root is None:
        return _empty_answer(context)
    cores, caveats = _resolve_cores(machine, context, context.cores, verify=verify)
    # Only declarations that stay under the root define the scan, so a config
    # pointing outside it can never widen where atlas reads or hashes.
    claimed: set[str] = set()
    for core in context.cores:
        for declaration in core.firmware:
            landing = destination_under(machine, context.root, declaration.path).path
            if landing is not None:
                claimed.add(landing)
    unclaimed, unclaimed_caveats = _unclaimed_files(machine, context, claimed, verify=verify)
    caveats.extend(unclaimed_caveats)
    if not claimed:
        caveats.append(
            _empty_answer_caveat(
                cores,
                enumerated=context.cores_read,
                declared=_declared_without_requiring(cores),
                subject="any firmware",
                data={},
            )
        )
    return FirmwareAnswer(
        root=context.root,
        cores=cores,
        unclaimed=unclaimed,
        hash_checked=verify,
        sources=context.sources,
        caveats=(*context.caveats, *caveats),
    )


def _stated_content(md5: str | None, sha1: str | None, size: int | None) -> dict[str, str]:
    """What the caller stated about the content — every field, so none is dropped.

    A caveat about a request that matched nothing has to carry the whole
    request: told only the digests, a caller whose *size* is the field the
    table disagrees with cannot see which of its values was rejected.
    """
    stated: dict[str, str] = {}
    if md5 is not None:
        stated["md5"] = md5
    if sha1 is not None:
        stated["sha1"] = sha1
    if size is not None:
        stated["size"] = str(size)
    return stated


def _declared_beyond_requirements(
    cores: tuple[CoreFirmware, ...], hashes: FirmwareHashes, identity: FirmwareIdentity
) -> bool:
    """Could a declaration this answer never resolved have been about *this* content?

    Two kinds of declaration never become a requirement and so can never be
    matched by content. A **refused** one still names the path it wanted, so it
    is answered precisely: the packaged table says what belongs there. An
    **unread** one is only known by the key it was declared under
    (``firmware3_path``) and not by the path it named
    (:class:`CoreDeclarations`), so it cannot be tied to an identity at all —
    and it is counted anyway, because the alternative is to answer that an
    absence was established while a core's ``.info`` may name exactly this
    file. Over-reporting "unresolved" costs a caller one look at the core
    entries; the other direction is the claim this module exists to refuse.

    That reasoning reaches exactly as far as its own premise: it rests on the
    declaration *maybe naming this file*, so a key whose value is empty — a
    line that names no file at all, and one atlas states for a different reason
    — is left out. Counting it would turn an established absence into an
    unresolved one over a possibility that cannot be true, and this route's
    two empty answers are different instructions to a caller.
    """
    for core in cores:
        if core.unread_stating_a_path:
            return True
        for refusal in core.refused:
            expected = hashes.for_path(refusal.declared)
            if expected is not None and expected.md5.lower() == identity.md5.lower():
                return True
    return False


def identify_firmware(
    machine: Machine,
    context: FirmwareContext,
    *,
    md5: str | None = None,
    sha1: str | None = None,
    size: int | None = None,
) -> FirmwareIdentification:
    """What is this content, and which requirements on this machine does it satisfy?

    The download flow, answered by bytes: a file arrives under whatever name
    its source gave it, and the question is where it goes, under what name, and
    whether it is even the right thing. Every requirement whose expected
    identity is this content comes back — across cores and across systems,
    because one identity is routinely wanted in several places under several
    names — each carrying its own absolute destination and expected file name.

    Matching a requirement is by identity, never by the name the caller's file
    happens to carry. A requirement whose file name the packaged table does not
    cover can never be matched: atlas will not claim that unknown bytes belong
    somewhere.

    A request that names no content at all — a bare ``size``, which two
    different files routinely share — is answered, not raised: it is a state of
    this domain like any other, and it comes back as an empty identification
    carrying :data:`CAVEAT_FIRMWARE_CONTENT_UNSTATED`.
    """
    caveats: list[Caveat] = list(context.caveats)
    stated = _stated_content(md5, sha1, size)
    if md5 is None and sha1 is None:
        caveats.append(
            Caveat(
                CAVEAT_FIRMWARE_CONTENT_UNSTATED,
                "this request names no content: a size alone is not an identity — files of one size are "
                "not one file — so there is nothing to look up and nothing this answer could be about",
                stated,
            )
        )
        return FirmwareIdentification(
            identity=None, requirements=(), sources=context.sources, caveats=tuple(caveats)
        )
    identity = context.hashes.for_content(md5=md5, sha1=sha1, size=size)
    if identity is None:
        if context.hashes.contradicts_itself(md5=md5, sha1=sha1, size=size):
            # The table knows these fields, just not together. Blaming the table
            # would send the caller looking in the wrong place.
            caveats.append(
                Caveat(
                    CAVEAT_FIRMWARE_CONTENT_CONTRADICTORY,
                    "the fields given describe no single file: the table knows a digest among them, and the "
                    "entry it names carries a different value for something else stated here — so the "
                    "request contradicts itself rather than the content being unknown",
                    stated,
                )
            )
        else:
            caveats.append(
                Caveat(
                    CAVEAT_FIRMWARE_CONTENT_UNIDENTIFIED,
                    "the packaged identity table does not recognise this content — that is a normal answer "
                    "(the table covers only what System.dat covers), so it says nothing about the file's worth",
                    stated,
                )
            )
        return FirmwareIdentification(
            identity=None, requirements=(), sources=context.sources, caveats=tuple(caveats)
        )
    if context.root is None:
        return FirmwareIdentification(
            identity=identity, requirements=(), sources=context.sources, caveats=tuple(caveats)
        )
    # What this answer is made of is the declarations plus what sits at each
    # destination — the unclaimed scan answers a different question entirely
    # (files nobody declared), and none of its result reaches here. Running it
    # would glob and stat every directory a declaration references for a lookup
    # the caller already has the bytes for.
    cores, _observations = _resolve_cores(machine, context, context.cores, verify=False)
    wanted = tuple(
        r
        for r in _requirements_of(cores)
        if r.identity is not None and r.identity.md5.lower() == identity.md5.lower()
    )
    if not wanted:
        caveats.append(
            _empty_answer_caveat(
                cores,
                enumerated=context.cores_read,
                declared=_declared_beyond_requirements(cores, context.hashes, identity),
                subject=f"a file with this identity (known as {', '.join(identity.known_as)})",
                data={"md5": identity.md5},
            )
        )
    # An identification hands back requirements without their emulator, so a
    # caveat about how one of them got its system has to travel with it or it is
    # lost. Only about *these* requirements, though: a core's caveat names the
    # files it is about, and attaching it because some other file of the same
    # core was derived puts warnings about files that are not in this answer.
    derived_in_answer = {r.core_so for r in wanted if r.system_source != SOURCE_OVERRIDE}
    for core in cores:
        if core.core_so in derived_in_answer:
            caveats.extend(core.caveats)
    return FirmwareIdentification(
        identity=identity, requirements=wanted, sources=context.sources, caveats=tuple(caveats)
    )
