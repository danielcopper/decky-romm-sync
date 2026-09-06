"""Per-core mode-selection rules for cards no single option can govern.

A rule card (``governing_rule`` in ``data/core_oddities.json``) keeps its modes
as data — what *can* exist — and the function here, keyed by the card key, is
what decides which of them holds on this machine: it reads the options the
card declared, and whatever else it stated it needs (a content class, a config
file of the emulator's own), and returns the mode's name. Several interacting
options are a product no single option's value can name, so the format grows
code plus a card referencing it, never a DSL (issue #163).

The boundary rule holds unchanged: everything a rule decides on is read off
the running machine, handed in by the resolver through :class:`RuleReading`.
A rule that cannot decide returns no mode and says why, as structured
caveats — the card then steps aside exactly as it does for a generation
nobody could confirm, and the standard answer below says what it can.

The resolver records which of the declared options a rule actually consulted,
and those readings — plus any the rule adds itself, like ScummVM's
``savepath`` — become the answer's :class:`~atlas.placement.Granularity`
readings: the caller sees every switch that went into the selection, its
live value, and where to change it.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Callable, Mapping

from .placement import (
    CAVEAT_CORE_GENERATION_MISMATCH,
    CAVEAT_CORE_MODE_UNESTABLISHED,
    CAVEAT_CORE_OPTION_VALUE_UNESTABLISHED,
    CAVEAT_SAVE_ROOT_REDIRECTED,
    CAVEAT_SAVE_ROOT_UNRESOLVABLE,
    REASON_CARD_INDEX_OUTSIDE_RECORDED_NAMES,
    REASON_CONTENT_CLASS_UNNAMED,
    REASON_CONTENT_CLASS_UNRECORDED,
    REASON_EMULATED_MODEL_UNRECORDED,
    REASON_INI_OUTRANKED_BY_CASCADE,
    REASON_INI_PRESENCE_UNESTABLISHED,
    REASON_INI_SEARCH_PATH_UNLISTABLE,
    REASON_SAVEPATH_CONFIG_UNREADABLE,
    REASON_SAVEPATH_UNTRANSLATABLE,
    Caveat,
    DataValue,
    OptionReading,
)

# How a rule saw a file it asked for. Three states rather than text-or-None,
# because two different absences must not collapse: a file that is not there
# is a machine fact a rule may decide on (a fresh ScummVM has no ini and the
# default applies), a file that is there and cannot be read leaves the rule
# unable to decide — and deciding anyway would be the guess this project
# refuses.
FILE_READ = "read"
FILE_ABSENT = "absent"
FILE_UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class FileLookup:
    """One file the resolver read for a rule: its text, its state, its path."""

    text: str | None
    status: str
    path: str | None


@dataclass(frozen=True, slots=True)
class RuleReading:
    """Everything the resolver hands a rule to decide with — all machine reads.

    ``option_values`` maps the card's declared rule options to their live
    values (``None`` where nothing on the machine states one and the core
    registered no default). The mapping records which keys the rule consults,
    so the answer's readings list is exactly the switches that mattered here —
    hatari reads one of its two write-protect options, never both, because
    which one governs is the content's class.

    ``content_extension`` is the loaded content's extension, lowered, without
    the dot — ``None`` when the question named no content — and
    ``content_stem`` its file stem, for a rule whose emulator derives names
    from it (MAME's per-driver ini). ``system_file`` reads a file from the
    directory RetroArch hands cores as the system directory, ``home_file``
    one relative to the home the emulator's own ``$HOME`` expands to (the
    sandbox environment's HOME — ``None``-homed machines answer unreadable,
    because the emulator's expansion is then not a path atlas can follow).
    ``system_entries`` and ``home_entries`` list the names in a directory
    under the same two roots — ``()`` when the directory is not there, which
    is a truthful negative, and ``None`` when it exists and could not be
    listed, or the root itself is out of reach. ``save_dirs`` is every
    spelling the frontend's save root can have reached the core under (the
    configured root, and the sorted directory RetroArch redirects to), for a
    rule that must compare a configured path against it, and ``is_directory``
    answers whether an emulator-spelled path is a directory on this machine —
    ``None`` where that could not be established (the path did not translate
    to a host view).
    """

    option_values: Mapping[str, str | None]
    content_extension: str | None
    content_stem: str | None
    system_file: Callable[[str], FileLookup]
    home_file: Callable[[str], FileLookup]
    system_entries: Callable[[str], "tuple[str, ...] | None"]
    home_entries: Callable[[str], "tuple[str, ...] | None"]
    save_dirs: tuple[str, ...]
    is_directory: Callable[[str], bool | None]


@dataclass(frozen=True, slots=True)
class ModeChoice:
    """What a rule decided: the mode, the reachable others, and any degradation.

    ``alternatives`` is one ``(mode, ((option, value), ...))`` per other mode a
    caller could switch to — the option combination that selects it, in the
    card's own vocabulary. The resolver turns them into the answer's
    :class:`~atlas.placement.ModeAlternative` by adding each mode's own
    granularity, which the rule deliberately does not know. ``readings`` is
    for switches that are not core options at all (ScummVM's ``savepath``
    lives in the emulator's own ini); the consulted core options are recorded
    by the resolver and need no restating here.
    """

    mode: str | None
    alternatives: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    caveats: tuple[Caveat, ...] = ()
    readings: tuple[OptionReading, ...] = ()


def _value_unestablished(core: str, option_key: str) -> Caveat:
    """Nothing on this machine, and nothing in the core, states this option."""
    return Caveat(
        CAVEAT_CORE_OPTION_VALUE_UNESTABLISHED,
        f"core {core!r} is recorded as placing its saves outside the standard layout under "
        f"option {option_key!r}, and which value governs it here was never established — no "
        "configuration on this machine states one and the installed core declared no default, so "
        "the recorded behaviour is not applied; the standard answer below may miss the real save "
        "stack",
        {"core": core, "option_key": option_key},
    )


def _unknown_value(core: str, option_key: str, value: str) -> Caveat:
    """A live value the recorded behaviour cannot interpret — the record lags."""
    return Caveat(
        CAVEAT_CORE_GENERATION_MISMATCH,
        f'core option {option_key} = "{value}" is a value the recorded save behaviour of core '
        f"{core!r} cannot interpret — the record lags this core's generation; the configured "
        "save behaviour is unknown until re-audited, and the standard answer below may miss the "
        "real save stack",
        {"core": core, "option_key": option_key, "value": value},
    )


def _mode_unestablished(core: str, reason: str, because: str, **facts: DataValue) -> Caveat:
    """The rule as a whole could not decide — the slug and its subject travel with the code.

    *reason* is the slug from :data:`CORE_MODE_UNESTABLISHED_REASONS` a client
    branches on, *because* the sentence that says the same thing to a person,
    and *facts* the subject the sentence names — the extension it saw, the path
    it could not translate, the ini files it is about. Three arguments rather
    than one, because the sentence used to be all three at once.
    """
    return Caveat(
        CAVEAT_CORE_MODE_UNESTABLISHED,
        f"core {core!r} is recorded as selecting between save behaviours by a rule, and the rule "
        f"could not decide here: {because} — the recorded behaviour is not applied; the standard "
        "answer below may miss the real save stack",
        {"core": core, "reason": reason, **facts},
    )


def _require_values(core: str, pairs: tuple[tuple[str, str | None], ...]) -> tuple[Caveat, ...]:
    """One caveat per switch nothing on the machine states — empty means all read."""
    return tuple(_value_unestablished(core, key) for key, value in pairs if value is None)


def _refuse_alien(
    core: str, triples: tuple[tuple[str, str | None, tuple[str, ...]], ...]
) -> tuple[Caveat, ...]:
    """One caveat per read value the recorded behaviour cannot interpret."""
    return tuple(
        _unknown_value(core, key, value or "")
        for key, value, known in triples
        if value not in known
    )


# ---------------------------------------------------------------------------
# mednafen_saturn — two independent sharing switches over one three-file set.
# shared_int swaps the stem of the internal pair (.bkr backup RAM and .smpc
# RTC/language, both written through MDFNMKF_SAV), shared_ext the cartridge's
# .bcr (MDFNMKF_CART) — libretro.cpp:1045-1073 at ccba526, options read at
# :257-276. The four combinations are the four modes, nothing else selects.
# ---------------------------------------------------------------------------

_SATURN_INT = "beetle_saturn_shared_int"
_SATURN_EXT = "beetle_saturn_shared_ext"
_SATURN_MODES: dict[tuple[str, str], str] = {
    ("disabled", "disabled"): "per-game",
    ("enabled", "disabled"): "internal-shared",
    ("disabled", "enabled"): "cartridge-shared",
    ("enabled", "enabled"): "both-shared",
}


def _mednafen_saturn(reading: RuleReading) -> ModeChoice:
    int_value = reading.option_values[_SATURN_INT]
    ext_value = reading.option_values[_SATURN_EXT]
    missing = _require_values(
        "mednafen_saturn", ((_SATURN_INT, int_value), (_SATURN_EXT, ext_value))
    )
    if missing:
        return ModeChoice(None, caveats=missing)
    toggles = ("enabled", "disabled")
    alien = _refuse_alien(
        "mednafen_saturn",
        ((_SATURN_INT, int_value, toggles), (_SATURN_EXT, ext_value, toggles)),
    )
    if alien:
        return ModeChoice(None, caveats=alien)
    mode = _SATURN_MODES[(int_value or "", ext_value or "")]
    return ModeChoice(
        mode,
        alternatives=tuple(
            (other, ((_SATURN_INT, combo[0]), (_SATURN_EXT, combo[1])))
            for combo, other in _SATURN_MODES.items()
            if other != mode
        ),
    )


# ---------------------------------------------------------------------------
# hatari — which write-protect option governs is the content's class. Floppy
# images are written back into themselves at eject (floppy.c:599-634 at
# 7008194); hard-disk content takes writes in place; either class's protect
# option set to 'on' discards the changes instead. The classes are the
# dispatch in retro_load_game (libretro.c:1597-1652): st/msa/stx/dim/ipf and
# an .m3u playlist of them load as floppies (a .zip reaches the same writer,
# floppy.c:626-627), ide/vhd/gem attach as hard disks.
# ---------------------------------------------------------------------------

_HATARI_FLOPPY = frozenset({"st", "msa", "stx", "dim", "ipf", "zip", "m3u"})
_HATARI_HARD_DISK = frozenset({"ide", "vhd", "gem"})
_HATARI_FLOPPY_KEY = "hatari_writeprotect_floppy"
_HATARI_HD_KEY = "hatari_writeprotect_hd"


def _hatari(reading: RuleReading) -> ModeChoice:
    extension = reading.content_extension
    if extension is None:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    "hatari",
                    REASON_CONTENT_CLASS_UNNAMED,
                    "which write-protect option governs depends on the content's class (a floppy "
                    "image is written back into itself, a hard-disk image takes writes in place), "
                    "and no content was named",
                ),
            ),
        )
    if extension in _HATARI_FLOPPY:
        option_key, prefix = _HATARI_FLOPPY_KEY, "floppy"
    elif extension in _HATARI_HARD_DISK:
        option_key, prefix = _HATARI_HD_KEY, "hard-disk"
    else:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    "hatari",
                    REASON_CONTENT_CLASS_UNRECORDED,
                    f"the content's extension {extension!r} is outside both recorded classes "
                    "(floppy: st/msa/stx/dim/ipf/zip/m3u; hard disk: ide/vhd/gem), so which "
                    "write-protect option governs was never established",
                    extension=extension,
                ),
            ),
        )
    value = reading.option_values[option_key]
    if value is None:
        return ModeChoice(None, caveats=(_value_unestablished("hatari", option_key),))
    if value in ("off", "auto"):
        mode = f"{prefix}-writeback"
        alternatives = ((f"{prefix}-discarded", ((option_key, "on"),)),)
    elif value == "on":
        mode = f"{prefix}-discarded"
        alternatives = ((f"{prefix}-writeback", ((option_key, "off"),)),)
    else:
        return ModeChoice(None, caveats=(_unknown_value("hatari", option_key, value),))
    return ModeChoice(mode, alternatives=alternatives)


# ---------------------------------------------------------------------------
# scummvm — the save directory is ScummVM's own 'savepath' setting, persisted
# in <system dir>/scummvm.ini (libretro-os-utils.cpp:64-69 at 686cdd1). Set
# and pointing at an existing directory it governs; unset, or set to something
# that is not a directory, the emulator removes the key and falls back to the
# frontend's save directory (checkPathSetting, :169-181, applied at :218-221).
# ---------------------------------------------------------------------------

_SCUMMVM_INI = "scummvm.ini"
_SCUMMVM_DEFAULT_MODE = "frontend-save-dir"


def _scummvm_ini_savepath(text: str) -> str | None:
    """The application domain's ``savepath``, parsed the way ConfMan spells it.

    Only the ``[scummvm]`` section is read: that is the application domain the
    backend writes the setting into. A per-target section may carry its own
    override, but which target the loaded content maps to is launcher
    configuration atlas does not read — the card's own prose says so.
    """
    in_scummvm = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_scummvm = line == "[scummvm]"
            continue
        if not in_scummvm or "=" not in line or line.startswith("#") or line.startswith(";"):
            continue
        key, _, value = line.partition("=")
        if key.strip() == "savepath" and value.strip():
            return value.strip()
    return None


def _scummvm(reading: RuleReading) -> ModeChoice:
    ini = reading.system_file(_SCUMMVM_INI)
    if ini.status == FILE_UNREADABLE:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    "scummvm",
                    REASON_SAVEPATH_CONFIG_UNREADABLE,
                    "the save directory is ScummVM's own 'savepath' setting in scummvm.ini, and "
                    "the ini could not be read — whether the saves were routed elsewhere is "
                    "unknowable here",
                ),
            ),
        )
    savepath = _scummvm_ini_savepath(ini.text) if ini.status == FILE_READ and ini.text else None
    if savepath is None:
        provenance = (
            "scummvm.ini states no savepath — the registered default is the frontend's save "
            "directory, flat (libretro-os-utils.cpp:212-216 at 686cdd1)"
            if ini.status == FILE_READ
            else "no scummvm.ini exists yet — the registered default is the frontend's save "
            "directory, flat (libretro-os-utils.cpp:212-216 at 686cdd1)"
        )
        return ModeChoice(
            _SCUMMVM_DEFAULT_MODE,
            readings=(OptionReading("savepath", None, provenance, ini.path),),
        )
    a_directory = reading.is_directory(savepath)
    if a_directory is None:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    "scummvm",
                    REASON_SAVEPATH_UNTRANSLATABLE,
                    f"scummvm.ini sets savepath to {savepath!r}, which no view of this machine "
                    "translates to a host path — whether it governs cannot be established",
                    path=savepath,
                ),
            ),
            readings=(
                OptionReading(
                    "savepath", savepath, f'scummvm.ini: savepath = "{savepath}"', ini.path
                ),
            ),
        )
    if not a_directory:
        return ModeChoice(
            _SCUMMVM_DEFAULT_MODE,
            readings=(
                OptionReading(
                    "savepath",
                    savepath,
                    f'scummvm.ini: savepath = "{savepath}" — set, but not a directory on this '
                    "machine, so the emulator removes the key at startup and falls back to the "
                    "frontend's save directory (checkPathSetting, libretro-os-utils.cpp:169-181 "
                    "at 686cdd1)",
                    ini.path,
                ),
            ),
        )
    if any(_same_path(savepath, save_dir) for save_dir in reading.save_dirs):
        return ModeChoice(
            _SCUMMVM_DEFAULT_MODE,
            readings=(
                OptionReading(
                    "savepath",
                    savepath,
                    f'scummvm.ini: savepath = "{savepath}" — the frontend\'s save directory, '
                    "spelled out (the backend re-writes its default into the ini)",
                    ini.path,
                ),
            ),
        )
    return ModeChoice(
        None,
        caveats=(
            Caveat(
                CAVEAT_SAVE_ROOT_REDIRECTED,
                f"ScummVM's own configuration routes its saves to {savepath!r} "
                f"(scummvm.ini: savepath), a directory outside every root kind this answer "
                "states — the standard answer below is where the frontend would look, not where "
                "this emulator writes; the slot files there are named per engine from the "
                "launcher target, which atlas does not read",
                {
                    "core": "scummvm",
                    "key": "savepath",
                    "path": savepath,
                    "options_file": ini.path or "",
                },
            ),
        ),
        readings=(
            OptionReading("savepath", savepath, f'scummvm.ini: savepath = "{savepath}"', ini.path),
        ),
    )


def _same_path(left: str, right: str) -> bool:
    """One directory spelled twice? Normalized string equality, nothing cleverer.

    Both spellings are the emulator's own view, so no host translation
    belongs here — and a false *inequality* only costs a redirect caveat
    about a path that happens to be the save directory, which is still a
    true statement about the configuration.
    """
    return posixpath.normpath(left.rstrip("/")) == posixpath.normpath(right.rstrip("/"))


# ---------------------------------------------------------------------------
# swanstation — each memory-card slot has its own type option, and the mode is
# the pair: slot 1 defaults to the libretro SRAM interface (the frontend's
# .srm), slot 2 to no card. The per-game types name the card after the disc's
# game code or its title (libretro_host_interface.cpp:404-415 at 4d309c0,
# selected in system.cpp:1450-1508). Which other modes an answer lists is the
# rule's judgment: twenty are reachable, so the alternatives are the one-edit
# neighbours — every slot, changed once — rather than the whole product.
# ---------------------------------------------------------------------------

_SWAN_CARD1 = "swanstation_MemoryCards_Card1Type"
_SWAN_CARD2 = "swanstation_MemoryCards_Card2Type"
_SWAN_PLAYLIST = "swanstation_MemoryCards_UsePlaylistTitle"
_SWAN_TYPES = {
    "Libretro": "libretro",
    "Shared": "shared",
    "PerGame": "per-code",
    "PerGameTitle": "per-title",
    "None": "none",
}
_SWAN_CARD1_VALUES = ("Libretro", "Shared", "PerGame", "PerGameTitle", "None")
_SWAN_CARD2_VALUES = ("None", "Shared", "PerGame", "PerGameTitle")


def _swanstation_mode(card1: str, card2: str) -> str:
    return f"card1-{_SWAN_TYPES[card1]}+card2-{_SWAN_TYPES[card2]}"


def _swanstation(reading: RuleReading) -> ModeChoice:
    card1 = reading.option_values[_SWAN_CARD1]
    card2 = reading.option_values[_SWAN_CARD2]
    missing = _require_values("swanstation", ((_SWAN_CARD1, card1), (_SWAN_CARD2, card2)))
    if missing:
        return ModeChoice(None, caveats=missing)
    alien = _refuse_alien(
        "swanstation",
        (
            (_SWAN_CARD1, card1, _SWAN_CARD1_VALUES),
            (_SWAN_CARD2, card2, _SWAN_CARD2_VALUES),
        ),
    )
    if alien:
        return ModeChoice(None, caveats=alien)
    if "PerGameTitle" in (card1, card2):
        # The playlist option only moves a title-named card's stem, so it is
        # read — and stated as a reading — exactly where a slot is title-named.
        _ = reading.option_values[_SWAN_PLAYLIST]
    alternatives = []
    for value in _SWAN_CARD1_VALUES:
        if value != card1:
            alternatives.append((_swanstation_mode(value, card2 or ""), ((_SWAN_CARD1, value),)))
    for value in _SWAN_CARD2_VALUES:
        if value != card2:
            alternatives.append((_swanstation_mode(card1 or "", value), ((_SWAN_CARD2, value),)))
    return ModeChoice(_swanstation_mode(card1 or "", card2 or ""), alternatives=tuple(alternatives))


# ---------------------------------------------------------------------------
# beetle psx / beetle psx hw — one implementation, two option prefixes. Slot 0
# is the frontend's .srm through the libretro SRAM interface, or the core's
# own <stem>.<idx>.mcr under the mednafen method; slot 1 adds a second .mcr;
# sharing swaps the stem (libretro.cpp:2145-2163, :2459-2485, :5240-5249 at
# d6383bf). The card-image index options supply the digit in the name
# (:2159-2164), so the recorded names hold for the registered defaults (left
# 0, right 1) and the rule steps aside for any other selection.
# ---------------------------------------------------------------------------


def _beetle_mode_name(method: str, memcard1: str, shared: str) -> str:
    """The mode a combination selects — sharing that moves no file folds away."""
    slot0 = "srm" if method == "libretro" else "mcr"
    second = memcard1 == "enabled"
    is_shared = shared == "enabled" and (slot0 == "mcr" or second)
    return f"{slot0}{'+second-card' if second else '-only'}{'-shared' if is_shared else ''}"


def _beetle_offset_indexes(
    reading: RuleReading, *, method: str, memcard1: str, left_key: str, right_key: str
) -> tuple[tuple[str, str | None], ...]:
    """The card-image indexes off their defaults, read only where the slot is in play."""
    indexes: list[tuple[str, str | None, str]] = []
    if method == "mednafen":
        indexes.append((left_key, reading.option_values[left_key], "0"))
    if memcard1 == "enabled":
        indexes.append((right_key, reading.option_values[right_key], "1"))
    return tuple((key, value) for key, value, default in indexes if value != default)


def _beetle_alternatives(
    keys: tuple[str, str, str], method: str, memcard1: str, shared: str
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """The one-edit neighbours — a flip whose mode folds back to the current one drops."""
    method_key, memcard1_key, shared_key = keys
    mode = _beetle_mode_name(method, memcard1, shared)
    flips = (
        ("mednafen" if method == "libretro" else "libretro", memcard1, shared),
        (method, "disabled" if memcard1 == "enabled" else "enabled", shared),
        (method, memcard1, "disabled" if shared == "enabled" else "enabled"),
    )
    return tuple(
        (
            _beetle_mode_name(*combo),
            ((method_key, combo[0]), (memcard1_key, combo[1]), (shared_key, combo[2])),
        )
        for combo in flips
        if _beetle_mode_name(*combo) != mode
    )


def _beetle_psx_rule(prefix: str, core: str) -> Callable[[RuleReading], ModeChoice]:
    method_key = f"{prefix}use_mednafen_memcard0_method"
    memcard1_key = f"{prefix}enable_memcard1"
    shared_key = f"{prefix}shared_memory_cards"
    left_key = f"{prefix}memcard_left_index"
    right_key = f"{prefix}memcard_right_index"
    toggles = ("enabled", "disabled")

    def rule(reading: RuleReading) -> ModeChoice:
        method = reading.option_values[method_key]
        memcard1 = reading.option_values[memcard1_key]
        shared = reading.option_values[shared_key]
        missing = _require_values(
            core, ((method_key, method), (memcard1_key, memcard1), (shared_key, shared))
        )
        if missing:
            return ModeChoice(None, caveats=missing)
        alien = _refuse_alien(
            core,
            (
                (method_key, method, ("libretro", "mednafen")),
                (memcard1_key, memcard1, toggles),
                (shared_key, shared, toggles),
            ),
        )
        if alien:
            return ModeChoice(None, caveats=alien)
        offset = _beetle_offset_indexes(
            reading,
            method=method or "",
            memcard1=memcard1 or "",
            left_key=left_key,
            right_key=right_key,
        )
        if offset:
            names = ", ".join(f'{key} = "{value}"' for key, value in offset)
            return ModeChoice(
                None,
                caveats=(
                    _mode_unestablished(
                        core,
                        REASON_CARD_INDEX_OUTSIDE_RECORDED_NAMES,
                        f"the card-image index options select other card files than the recorded "
                        f"names cover ({names} — the digit in <stem>.<idx>.mcr is the option's "
                        "value, libretro.cpp:2159-2164 at d6383bf)",
                        # An option nothing on this machine states carries the
                        # empty value this package spells an absent value with,
                        # rather than dropping the key and hiding which options
                        # the statement is about.
                        options={key: value or "" for key, value in offset},
                    ),
                ),
            )
        return ModeChoice(
            _beetle_mode_name(method or "", memcard1 or "", shared or ""),
            alternatives=_beetle_alternatives(
                (method_key, memcard1_key, shared_key), method or "", memcard1 or "", shared or ""
            ),
        )

    return rule


# ---------------------------------------------------------------------------
# genesis_plus_gx — frontend SRAM is real for the cartridge systems and
# unreachable for the CD ones, whose BRAM tree hangs on three interacting
# options (libretro.c:1385-1500 at 46a5521): the system BRAM is region-keyed
# scd_E/U/J.brm or per-game <stem>.brm, and the backup cart is one shared
# size-keyed file, a per-game one, or absent. Which world applies is the
# content's class, read off the extension the way the loader dispatches.
# ---------------------------------------------------------------------------

_GPGX_SYSTEM = "genesis_plus_gx_system_bram"
_GPGX_CART = "genesis_plus_gx_cart_bram"
_GPGX_SIZE = "genesis_plus_gx_cart_size"
_GPGX_CD = frozenset({"cue", "iso", "chd", "m3u"})
_GPGX_CARTRIDGE = frozenset({"md", "smd", "gen", "sms", "gg", "sg", "68k", "sgd", "mdx", "bms"})
_GPGX_SIZES = ("128k", "256k", "512k", "1meg", "2meg", "4meg")
_PER_BIOS = "per bios"
_PER_CART = "per cart"
_PER_GAME = "per game"


def _gpgx_cd_mode(system: str, cart: str, size: str) -> str:
    base = "cd-bios-bram" if system == _PER_BIOS else "cd-game-bram"
    if size == "disabled":
        return base
    suffix = f"+cart-{size}" if cart == _PER_CART else f"+cart-{size}-per-game"
    return base + suffix


def _gpgx_content_class(extension: str | None) -> ModeChoice | None:
    """The cartridge half of the dispatch — ``None`` means CD, read the options."""
    if extension is None:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    "genesis_plus_gx",
                    REASON_CONTENT_CLASS_UNNAMED,
                    "the save story splits on the content's class (a cartridge fills the "
                    "frontend's SRAM interface, a CD writes the core's own BRAM files), and no "
                    "content was named",
                ),
            ),
        )
    if extension in _GPGX_CARTRIDGE:
        return ModeChoice("cartridge")
    if extension == "bin":
        # A raw .bin is a cartridge dump in every ordinary library, and the
        # loader would still boot one carrying a CD header as a disc — the
        # mode's scoped file list says so instead of a second class guess.
        return ModeChoice("cartridge-raw-image")
    if extension not in _GPGX_CD:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    "genesis_plus_gx",
                    REASON_CONTENT_CLASS_UNRECORDED,
                    f"the content's extension {extension!r} is outside both recorded classes "
                    "(cartridge: md/smd/gen/sms/gg/sg/68k/sgd/mdx/bms and raw .bin; CD: "
                    "cue/iso/chd/m3u), so which save story applies was never established",
                    extension=extension,
                ),
            ),
        )
    return None


def _gpgx_cd_alternatives(
    system: str, cart: str, size: str
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """The one-edit neighbours of a CD mode — never the whole 26-combination product."""
    alternatives: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    other_system = _PER_GAME if system == _PER_BIOS else _PER_BIOS
    alternatives.append((_gpgx_cd_mode(other_system, cart, size), ((_GPGX_SYSTEM, other_system),)))
    if size != "disabled":
        other_cart = _PER_GAME if cart == _PER_CART else _PER_CART
        alternatives.append((_gpgx_cd_mode(system, other_cart, size), ((_GPGX_CART, other_cart),)))
    for other_size in ("disabled", *_GPGX_SIZES):
        if other_size != size:
            alternatives.append(
                (_gpgx_cd_mode(system, cart, other_size), ((_GPGX_SIZE, other_size),))
            )
    return tuple(alternatives)


def _genesis_plus_gx(reading: RuleReading) -> ModeChoice:
    cartridge_choice = _gpgx_content_class(reading.content_extension)
    if cartridge_choice is not None:
        return cartridge_choice
    system = reading.option_values[_GPGX_SYSTEM]
    cart = reading.option_values[_GPGX_CART]
    size = reading.option_values[_GPGX_SIZE]
    missing = _require_values(
        "genesis_plus_gx", ((_GPGX_SYSTEM, system), (_GPGX_CART, cart), (_GPGX_SIZE, size))
    )
    if missing:
        return ModeChoice(None, caveats=missing)
    alien = _refuse_alien(
        "genesis_plus_gx",
        (
            (_GPGX_SYSTEM, system, (_PER_BIOS, _PER_GAME)),
            (_GPGX_CART, cart, (_PER_CART, _PER_GAME)),
            (_GPGX_SIZE, size, ("disabled", *_GPGX_SIZES)),
        ),
    )
    if alien:
        return ModeChoice(None, caveats=alien)
    return ModeChoice(
        _gpgx_cd_mode(system or "", cart or "", size or ""),
        alternatives=_gpgx_cd_alternatives(system or "", cart or "", size or ""),
    )


# ---------------------------------------------------------------------------
# mame — whether the frontend's paths reach the emulator at all is one switch,
# and whether MAME's own ini is read is another. mame_mame_paths_enable off
# (the registered default) has the glue impose -nvram_directory,
# -cfg_directory and -diff_directory on the command line (retro_init.cpp:
# 549-577 at a90e86e1), which outranks every ini (OPTION_PRIORITY_CMDLINE =
# HIGH+1, emuopts.h:18; every ini priority sits below it, mameopts.h:27-39) —
# that is the card's one stated mode, whatever the second switch says. On,
# the glue sets nothing (:554-555) and MAME's own world governs. With
# mame_read_config off no ini is read at all (parse_one_ini, mameopts.cpp:
# 116-120) and the fork's compiled-in defaults apply — states/mame/nvram,
# states/mame/cfg, system/mame/diff (emuopts.cpp:59-64) — relative paths,
# resolved against the frontend process's working directory, which is process
# state no read of this machine can establish. With it on, inis are searched
# along $HOME/.mame then <system dir>/mame/ini (INI_PATH, retromain.cpp:
# 85-93, with the system-dir failsafe appended at :181-205; the OSD's
# duplicate 'inipath' row is ignored — add_entries override_existing=false,
# options.h:184/options.cpp:702-719 — and $HOME expands per element,
# options.cpp:531-569), first find per file name wins. Both elements are
# machine-readable, so the rule reads mame.ini and the driver's <stem>.ini
# the way the emulator does. The rest of the cascade (vertical.ini,
# <source>.ini, <parent>.ini, ...) applies by driver metadata inside the
# binary, which atlas cannot attribute — their presence is checked, never
# their meaning guessed.
# ---------------------------------------------------------------------------

_MAME_PATHS = "mame_mame_paths_enable"
_MAME_READ_CONFIG = "mame_read_config"
_MAME_FRONTEND_MODE = "frontend-paths"
_MAME_HOME_INIS = ".mame"
_MAME_SYSTEM_INIS = "mame/ini"
_MAME_MAIN_INI = "mame.ini"
# The one cascade member that is not a plain name in a search directory:
# parse_standard_inis composes "source" + PATH_SEPARATOR + <sourcefile>
# (mameopts.cpp:86 at libretro/mame a90e86e1).
_MAME_SOURCE_INIS = "source"
# The fork's compiled-in save trees (emuopts.cpp:59-64 at a90e86e1) — what
# governs when MAME's own paths are in force and no ini names a directory.
_MAME_DEFAULT_TREES = (
    ("nvram_directory", "states/mame/nvram"),
    ("cfg_directory", "states/mame/cfg"),
    ("diff_directory", "system/mame/diff"),
)
_MAME_TOGGLES = ("enabled", "disabled")


def _mame_ini_values(text: str) -> dict[str, str]:
    """The three directory keys from one MAME ini, read the way MAME writes them.

    A MAME ini is ``name value`` lines — whitespace-separated, the value
    optionally quoted — with ``#`` comments; not the ``key=value`` shape
    other emulators use.
    """
    wanted = {key for key, _ in _MAME_DEFAULT_TREES}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in wanted:
            values[parts[0]] = parts[1].strip().strip('"')
    return values


def _mame_ini_lookup(reading: RuleReading, name: str) -> FileLookup:
    """*name* along MAME's effective ini search path — the first find wins.

    An unreadable stop comes back as-is: the emulator may have found a file
    there, and deciding without knowing would be the guess this project
    refuses.
    """
    last = FileLookup(None, FILE_ABSENT, None)
    for fetch, base in (
        (reading.home_file, _MAME_HOME_INIS),
        (reading.system_file, _MAME_SYSTEM_INIS),
    ):
        found = fetch(posixpath.join(base, name))
        if found.status != FILE_ABSENT:
            return found
        last = found
    return last


def _mame_stray_inis(reading: RuleReading, stem: str | None) -> tuple[str, ...] | None:
    """Cascade members atlas cannot attribute — ``None`` when it cannot even look.

    One member sits a directory DOWN: ``parse_standard_inis`` composes
    ``source/<sourcefile>.ini`` with the prefix before searching for it
    (mameopts.cpp:86-87 at libretro/mame a90e86e1), and it parses at
    OPTION_PRIORITY_SOURCE_INI — above mame.ini's. A listing of the search
    directory alone names that tree as the plain directory ``source``, which
    ends in no ``.ini`` and would drop out of the check silently, so the
    directory is listed too.
    """
    attributable = {_MAME_MAIN_INI}
    if stem:
        attributable.add(f"{stem}.ini")
    strays: set[str] = set()
    for entries, base in (
        (reading.home_entries, _MAME_HOME_INIS),
        (reading.system_entries, _MAME_SYSTEM_INIS),
    ):
        listing = entries(base)
        if listing is None:
            return None
        strays.update(n for n in listing if n.endswith(".ini") and n not in attributable)
        below = entries(posixpath.join(base, _MAME_SOURCE_INIS))
        if below is None:
            return None
        strays.update(
            posixpath.join(_MAME_SOURCE_INIS, n) for n in below if n.endswith(".ini")
        )
    return tuple(sorted(strays))


def _mame_unresolvable(
    trees: tuple[tuple[str, str], ...], because: str, options_file: str | None
) -> Caveat:
    listed = ", ".join(f"{key} = {tree!r}" for key, tree in trees)
    data: dict[str, str] = {"core": "mame", **dict(trees)}
    if options_file:
        data["options_file"] = options_file
    return Caveat(
        CAVEAT_SAVE_ROOT_UNRESOLVABLE,
        f"MAME's own paths are in force (mame_mame_paths_enable is on) and {because}: "
        f"{listed} — resolved against the frontend process's working directory, which is "
        "process state no read of this machine can establish; the standard answer below is "
        "where the frontend would look, not where this emulator writes",
        data,
    )


def _mame_redirected(key: str, path: str, options_file: str | None) -> Caveat:
    return Caveat(
        CAVEAT_SAVE_ROOT_REDIRECTED,
        f"MAME's own configuration routes its {key} to {path!r}, read along its ini search "
        "path the way the emulator reads it — the standard answer below is where the frontend "
        "would look, not where this emulator writes",
        {"core": "mame", "key": key, "path": path, "options_file": options_file or ""},
    )


@dataclass(frozen=True, slots=True)
class _MameRefusal:
    """Why the ini branch cannot decide: the slug, the sentence, and the inis it is about.

    One shape for all four refusals rather than a tuple whose length says which
    one it is — ``members`` is the cascade file or files the statement names,
    empty where the refusal is about the search path itself and no ini was
    reached.
    """

    reason: str
    because: str
    members: tuple[str, ...] = ()

    @property
    def facts(self) -> dict[str, DataValue]:
        """``members`` where the refusal names inis, nothing where it names none."""
        return {"members": self.members} if self.members else {}


def _mame_cascade_refusal(
    main: FileLookup, driver: FileLookup, stem: str | None, strays: tuple[str, ...] | None
) -> _MameRefusal | None:
    """Why the ini branch cannot decide — ``None`` when it can."""
    if main.status == FILE_UNREADABLE:
        return _MameRefusal(
            REASON_INI_PRESENCE_UNESTABLISHED,
            "MAME's own paths and ini reading are both on, and whether a mame.ini exists "
            "along the emulator's search path could not be established — whether the save "
            "trees were routed elsewhere is unknowable here",
            (_MAME_MAIN_INI,),
        )
    if driver.status == FILE_UNREADABLE:
        return _MameRefusal(
            REASON_INI_PRESENCE_UNESTABLISHED,
            f"MAME's own paths and ini reading are both on, and whether a {stem}.ini exists "
            "along the emulator's search path could not be established — the driver's ini "
            "outranks mame.ini, so which values govern is unknowable here",
            (f"{stem}.ini",),
        )
    if strays is None:
        return _MameRefusal(
            REASON_INI_SEARCH_PATH_UNLISTABLE,
            "MAME reads an ini cascade, and one of its search directories could not be "
            "listed — whether a higher-priority ini overrides the values read is unknowable "
            "here",
        )
    if strays:
        outranks = (
            f"{strays[0]} on its search path outranks mame.ini"
            if len(strays) == 1
            else f"{', '.join(strays)} on its search path outrank mame.ini"
        )
        return _MameRefusal(
            REASON_INI_OUTRANKED_BY_CASCADE,
            "MAME reads an ini cascade whose members apply by driver metadata inside the "
            f"binary, which atlas cannot attribute — {outranks}, so which values govern was "
            "never established",
            strays,
        )
    return None


def _mame_ini_readings(
    main: FileLookup, driver: FileLookup
) -> tuple[dict[str, str], dict[str, "str | None"]]:
    """The directory keys both inis state, each with the file its value came from."""
    values: dict[str, str] = {}
    sources: dict[str, str | None] = {}
    for lookup in (main, driver):  # the driver's ini parses at higher priority
        if lookup.status == FILE_READ and lookup.text:
            for key, value in _mame_ini_values(lookup.text).items():
                values[key] = value
                sources[key] = lookup.path
    return values, sources


def _mame_tree_caveats(
    values: dict[str, str], sources: dict[str, "str | None"]
) -> tuple[Caveat, ...]:
    """What the effective values say per tree: a redirect each, or the unresolvable rest."""
    caveats: list[Caveat] = []
    unresolved: list[tuple[str, str]] = []
    unresolved_file: str | None = None
    for key, default in _MAME_DEFAULT_TREES:
        # A directory option is a MAME multipath; a write opens its first
        # element. A value carrying `$VAR` is expanded from the process's
        # environment (options.cpp:531-569) — process state, like the cwd.
        target = values.get(key, default).split(";")[0].strip()
        if target.startswith("/"):
            caveats.append(_mame_redirected(key, target, sources.get(key)))
        else:
            unresolved.append((key, target))
            if unresolved_file is None:
                unresolved_file = sources.get(key)
    if unresolved:
        because = (
            "the values it reads leave these trees relative"
            if any(key in values for key, _ in unresolved)
            else "no ini along its search path names these trees, so the compiled-in "
            "defaults apply"
        )
        caveats.append(_mame_unresolvable(tuple(unresolved), because, unresolved_file))
    return tuple(caveats)


def _mame_own_ini(reading: RuleReading) -> ModeChoice:
    """Both switches on: the inis govern, read the way the emulator searches them."""
    main = _mame_ini_lookup(reading, _MAME_MAIN_INI)
    stem = reading.content_stem
    driver = (
        _mame_ini_lookup(reading, f"{stem}.ini")
        if stem
        else FileLookup(None, FILE_ABSENT, None)
    )
    refusal = _mame_cascade_refusal(main, driver, stem, _mame_stray_inis(reading, stem))
    if refusal is not None:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished("mame", refusal.reason, refusal.because, **refusal.facts),
            ),
        )
    values, sources = _mame_ini_readings(main, driver)
    return ModeChoice(None, caveats=_mame_tree_caveats(values, sources))


def _mame(reading: RuleReading) -> ModeChoice:
    paths = reading.option_values[_MAME_PATHS]
    if paths is None:
        return ModeChoice(None, caveats=(_value_unestablished("mame", _MAME_PATHS),))
    if paths not in _MAME_TOGGLES:
        return ModeChoice(None, caveats=(_unknown_value("mame", _MAME_PATHS, paths),))
    if paths == "disabled":
        # The glue's command line outranks every ini, so the second switch
        # cannot matter here and is deliberately not consulted.
        return ModeChoice(_MAME_FRONTEND_MODE)
    read_config = reading.option_values[_MAME_READ_CONFIG]
    if read_config is None:
        return ModeChoice(None, caveats=(_value_unestablished("mame", _MAME_READ_CONFIG),))
    if read_config not in _MAME_TOGGLES:
        return ModeChoice(None, caveats=(_unknown_value("mame", _MAME_READ_CONFIG, read_config),))
    if read_config == "disabled":
        return ModeChoice(
            None,
            caveats=(
                _mame_unresolvable(
                    _MAME_DEFAULT_TREES,
                    "mame_read_config is off, so no ini is read and the compiled-in "
                    "defaults apply",
                    None,
                ),
            ),
        )
    return _mame_own_ini(reading)


# ---------------------------------------------------------------------------
# puae — an Amiga's save is the floppy's own write or the CD models' NVRAM, and
# which of the two applies is the content's class, read off the extension the
# way libretro-dc.c's dc_get_image_type dispatches it (:825-868 at 0043cf9):
# adf/adz/fdi/dms/raw/ipf load as floppies, cue/ccd/nrg/mds/iso/chd as CDs.
# retro_get_memory_size answers RETRO_MEMORY_SYSTEM_RAM alone
# (libretro-core.c:8881-8886), so nothing reaches the frontend's .srm.
#
# Floppies. UAE's fetch_path returns the frontend's save directory for every
# name under __LIBRETRO__ (misc.c:609-617), so the write file it composes —
# <save dir>/<stem>_save.adf, DISK_get_saveimagepath(-2) falling through to
# DISK_get_default_saveimagepath with saveimageoriginalpath fixed at 0
# (disk.c:1166-1210, misc.c:155; the "_save.adf" spelling at :1142-1149) —
# stands under the save root. Whether one is created at all is
# disk_setwriteprotect (:3229-3285), and its only callers are the two libretro
# redirect helpers (libretro-core.c:605, :642). With both switches off no write
# file exists while the emulation runs, and the image's own format decides:
# a plain image leaves drv->wrprot at 0 and drive_write_data writes the track
# back into the content file (disk.c:2937-2969, ADF_NORMAL), while a gzipped or
# DMS image is unpacked into memory so openwritefile sets it (:1266-1268) and an
# IPF or FDI image sets it at insert (:1494-1501, :1517-1520) — either way
# drive_writeprotected (:853-866) makes drive_write_data drop the write. The
# extension is what this rule reads and the header is what the emulator checks,
# so an .adf carrying a UAE--ADF header takes the read-only path too
# (:1324-1327, :1533-1534); the card's mode prose says so.
# 'puae_floppy_write_protection' writes floppy_write_protect=true
# (libretro-core.c:4097-4112), which is p->floppy_read_only: it makes
# drive_writeprotected true for every image (disk.c:865) and makes
# disk_setwriteprotect return before creating anything (:3248-3251), so it
# discards the writes whatever the redirect switch says.
# 'puae_floppy_write_redirect' (libretro-core.c:4128-4162) makes
# diskfile_iswriteprotect demand a write file for every image
# (disk.c:1300-1306); floppy_open_redirect creates it and reinserts
# (libretro-core.c:564-610), floppy_close_redirect gzips it to
# <stem>_save.adf.gz and removes the plain file (:612-658), and the original
# image is chmod'd read-only (disk.c:3276-3282).
#
# CD. retro_config_kickstart appends flash_file=<save dir>/<name>.nvr only where
# the model has an extended Kickstart (libretro-core.c:5577-5578, :5640-5661),
# which emu_config_string gives CDTV, CD32 and CD32FR alone (:5969-5978) — and
# it appends it for the *model*, so a floppy on a CD model gets one too. The
# name is the content's own stem, or the shared puae_libretro(_cdtv).nvr under
# 'puae_shared_nvram'. Which model runs a CD is 'puae_model' where it names one,
# and on 'auto' the CD default 'puae_model_cd' — always a CD model — unless a
# boot hard drive is configured, in which case the hard-disk model applies
# except where a marker in the content's own launch path names a CD one
# (:7000-7028). That path is not a value this answer reads, so the rule refuses
# it rather than picking one of the two.
#
# Two writable stores sit beside every mode here and none of them is stated, so
# no group claims a closed candidate universe. 'puae_use_boot_hd' on anything
# but 'disabled' mounts a Boot HD read-write for floppy and CD content alike
# (retro_config_boot_hd, :5390-5493, called at :6495 and :7039) —
# <save dir>/puae_libretro.hdf under the hdf sizes, <save dir>/BootHD/ under
# 'files'. And two .uae files under the save root are appended after every line
# this rule reasons about — puae_libretro_global.uae (:7330-7343) and
# <save dir>/<content stem>.uae (:7345-7362) — so either can override a mode
# outright: flash_file is parsed by cfgfile_string (cfgfile.c:6068), whose body
# at :3250-3257 copies into the destination on every match with no first-wins
# guard, so the last occurrence in a UAE config stands. The model preset
# puae_libretro_<model>.uae (:6009-6032) cannot: it is read into
# uae_custom_config (:6022), concatenated into the preset (:6231) and emitted
# with it (:6488 for floppies, :7031 for CDs) *before* retro_config_kickstart
# writes flash_file= (:6491, :7034), so the core's own line wins over it. What
# it does reach is the Kickstart, parsed straight out of it at :6024.
# ---------------------------------------------------------------------------

_PUAE_PROTECT = "puae_floppy_write_protection"
_PUAE_REDIRECT = "puae_floppy_write_redirect"
_PUAE_SHARED_NVRAM = "puae_shared_nvram"
_PUAE_MODEL = "puae_model"
_PUAE_BOOT_HD = "puae_use_boot_hd"
_PUAE_AUTO = "auto"
_PUAE_DISABLED = "disabled"
_PUAE_ENABLED = "enabled"
_PUAE_TOGGLES = (_PUAE_DISABLED, _PUAE_ENABLED)
# The classes this record covers, and the class token each extension carries.
# The two floppy sets differ in what an unswitched write does, which is the
# image format rather than the dispatch: dc_get_image_type puts all six in one
# class, and diskfile_iswriteprotect's header checks split them again.
_PUAE_CLASS_FLOPPY = "floppy"
_PUAE_CLASS_FLOPPY_READ_ONLY = "floppy-read-only"
_PUAE_CLASS_CD = "cd"
_PUAE_FLOPPY_IN_PLACE = frozenset({"adf", "raw"})
_PUAE_FLOPPY_READ_ONLY = frozenset({"adz", "dms", "fdi", "ipf"})
_PUAE_CD = frozenset({"cue", "ccd", "nrg", "mds", "iso", "chd"})
_PUAE_CD32 = "CD32"
_PUAE_CD_MODELS = frozenset({"CDTV", _PUAE_CD32, "CD32FR"})
# puae_use_boot_hd's registered value set. The core's handler has no else
# (libretro-core.c:4478-4488), so an unrecognized value leaves the flag as it
# was rather than meaning 'disabled' — the rule refuses it instead of reading
# a machine's setting as a value the core never saw.
_PUAE_BOOT_HD_VALUES = (
    _PUAE_DISABLED,
    "files",
    "hdf20",
    "hdf40",
    "hdf80",
    "hdf128",
    "hdf256",
    "hdf512",
)
_PUAE_MODELS = frozenset(
    {
        _PUAE_AUTO,
        "A500OG",
        "A500",
        "A500PLUS",
        "A600",
        "A1200OG",
        "A1200",
        "A2000OG",
        "A2000",
        "A4030",
        "A4040",
    }
) | _PUAE_CD_MODELS
_PUAE_FLOPPY_WRITEBACK = "floppy-writeback"
_PUAE_FLOPPY_FORMAT_DISCARDED = "floppy-format-discarded"
_PUAE_FLOPPY_DISCARDED = "floppy-discarded"
_PUAE_FLOPPY_REDIRECTED = "floppy-redirected"
_PUAE_CD_PER_GAME = "cd-per-game"
_PUAE_CD_SHARED = "cd-shared"
_PUAE_CD_NO_NVRAM = "cd-no-nvram"
# What a floppy class does with both switches off — the only place the two
# floppy classes differ.
_PUAE_UNSWITCHED = {
    _PUAE_CLASS_FLOPPY: _PUAE_FLOPPY_WRITEBACK,
    _PUAE_CLASS_FLOPPY_READ_ONLY: _PUAE_FLOPPY_FORMAT_DISCARDED,
}


def _puae_flipped(value: str) -> str:
    """The other value of a two-value switch."""
    return _PUAE_DISABLED if value == _PUAE_ENABLED else _PUAE_ENABLED


def _puae_model_unrecorded(core: str, because: str, model: str) -> Caveat:
    """The emulated model decides part of the save story and this one is outside it."""
    return _mode_unestablished(core, REASON_EMULATED_MODEL_UNRECORDED, because, model=model)


def _puae_class(core: str, extension: str | None) -> str | ModeChoice:
    """The class token this extension carries, or the refusal to name one."""
    if extension is None:
        return ModeChoice(
            None,
            caveats=(
                _mode_unestablished(
                    core,
                    REASON_CONTENT_CLASS_UNNAMED,
                    "the save story splits on the content's class (a floppy takes its writes "
                    "into its own image or into a write file under the save root, a CD writes "
                    "the emulated model's non-volatile memory), and no content was named",
                ),
            ),
        )
    if extension in _PUAE_FLOPPY_IN_PLACE:
        return _PUAE_CLASS_FLOPPY
    if extension in _PUAE_FLOPPY_READ_ONLY:
        return _PUAE_CLASS_FLOPPY_READ_ONLY
    if extension in _PUAE_CD:
        return _PUAE_CLASS_CD
    return ModeChoice(
        None,
        caveats=(
            _mode_unestablished(
                core,
                REASON_CONTENT_CLASS_UNRECORDED,
                f"the content's extension {extension!r} is outside every recorded class "
                "(floppy: adf/raw written in place, adz/dms/fdi/ipf read-only; CD: "
                "cue/ccd/nrg/mds/iso/chd), so which save story applies was never established",
                extension=extension,
            ),
        ),
    )


def _puae_floppy_mode(protect: str, redirect: str, unswitched: str) -> str:
    """Which floppy mode a pair of switch values selects — protection wins."""
    if protect == _PUAE_ENABLED:
        return _PUAE_FLOPPY_DISCARDED
    if redirect == _PUAE_ENABLED:
        return _PUAE_FLOPPY_REDIRECTED
    return unswitched


def _puae_floppy_alternatives(
    protect: str, redirect: str, unswitched: str
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """The one-edit neighbours — each floppy switch flipped on its own.

    Write protection outranks redirection, so one of the two flips can land
    back on the mode in force; such a neighbour is dropped rather than offered
    as a change that changes nothing.
    """
    here = _puae_floppy_mode(protect, redirect, unswitched)
    neighbours = (
        (
            _PUAE_PROTECT,
            _puae_flipped(protect),
            _puae_floppy_mode(_puae_flipped(protect), redirect, unswitched),
        ),
        (
            _PUAE_REDIRECT,
            _puae_flipped(redirect),
            _puae_floppy_mode(protect, _puae_flipped(redirect), unswitched),
        ),
    )
    return tuple((mode, ((key, value),)) for key, value, mode in neighbours if mode != here)


def _puae_floppy(core: str, reading: RuleReading, model: str, content_class: str) -> ModeChoice:
    """The floppy half: the two write switches, on a model that keeps no NVRAM."""
    if model in _PUAE_CD_MODELS:
        return ModeChoice(
            None,
            caveats=(
                _puae_model_unrecorded(
                    core,
                    f"the selected model {model!r} keeps a non-volatile memory file beside the "
                    "floppy's own save story — the flash_file line is appended for the model, "
                    "not for the content's class — and the recorded floppy modes state only the "
                    "floppy half",
                    model,
                ),
            ),
        )
    protect = reading.option_values[_PUAE_PROTECT]
    redirect = reading.option_values[_PUAE_REDIRECT]
    switches = ((_PUAE_PROTECT, protect), (_PUAE_REDIRECT, redirect))
    missing = _require_values(core, switches)
    if missing:
        return ModeChoice(None, caveats=missing)
    alien = _refuse_alien(core, tuple((key, value, _PUAE_TOGGLES) for key, value in switches))
    if alien:
        return ModeChoice(None, caveats=alien)
    unswitched = _PUAE_UNSWITCHED[content_class]
    here, there = protect or "", redirect or ""
    return ModeChoice(
        _puae_floppy_mode(here, there, unswitched),
        alternatives=_puae_floppy_alternatives(here, there, unswitched),
    )


def _puae_cd_nvram_mode(shared: str) -> str:
    """Which NVRAM mode the sharing switch selects."""
    return _PUAE_CD_SHARED if shared == _PUAE_ENABLED else _PUAE_CD_PER_GAME


def _puae_cd_model(core: str, reading: RuleReading, model: str, shared: str) -> ModeChoice | None:
    """The model a CD runs on — ``None`` where it keeps a non-volatile memory file.

    The alternative offered for a model without one names ``CD32`` rather than
    ``auto``: an explicit CD model reaches the NVRAM mode whatever the boot
    hard drive says (libretro-core.c:6263 writes the preset straight, and the
    CD branch's marker logic is skipped at :7000), while ``auto`` reaches it
    only with ``puae_use_boot_hd`` disabled — an alternative that does not
    select the mode it names would be worse than none.
    """
    if model in _PUAE_CD_MODELS:
        return None
    if model != _PUAE_AUTO:
        return ModeChoice(
            _PUAE_CD_NO_NVRAM,
            alternatives=((_puae_cd_nvram_mode(shared), ((_PUAE_MODEL, _PUAE_CD32),)),),
        )
    boot_hd = reading.option_values[_PUAE_BOOT_HD]
    if boot_hd == _PUAE_DISABLED:
        return None
    return ModeChoice(
        None,
        caveats=(
            _puae_model_unrecorded(
                core,
                "with the model on 'auto' and a boot hard drive configured, which machine runs "
                "a CD is decided first by markers in the content's own launch path and only "
                "otherwise by the hard-disk model, which keeps no non-volatile memory at all — "
                "and that launch path is not a value this answer reads",
                model,
            ),
        ),
    )


def _puae_cd_switches(core: str, reading: RuleReading, model: str) -> tuple[Caveat, ...]:
    """Every CD switch the rule will consult, refused together rather than one at a time.

    Which switches those are is the model's: ``puae_use_boot_hd`` decides the
    machine only where ``puae_model`` is ``auto``, so naming it for an explicit
    model would report a switch that did not matter here.
    """
    pairs = ((_PUAE_SHARED_NVRAM, reading.option_values[_PUAE_SHARED_NVRAM]),)
    known = ((_PUAE_SHARED_NVRAM, _PUAE_TOGGLES),)
    if model == _PUAE_AUTO:
        pairs += ((_PUAE_BOOT_HD, reading.option_values[_PUAE_BOOT_HD]),)
        known += ((_PUAE_BOOT_HD, _PUAE_BOOT_HD_VALUES),)
    missing = _require_values(core, pairs)
    if missing:
        return missing
    values = dict(pairs)
    return _refuse_alien(core, tuple((key, values[key], allowed) for key, allowed in known))


def _puae_cd(core: str, reading: RuleReading, model: str) -> ModeChoice:
    """The CD half: the sharing switch, once the model is one that has an NVRAM."""
    refused = _puae_cd_switches(core, reading, model)
    if refused:
        return ModeChoice(None, caveats=refused)
    shared = reading.option_values[_PUAE_SHARED_NVRAM] or ""
    without_nvram = _puae_cd_model(core, reading, model, shared)
    if without_nvram is not None:
        return without_nvram
    flipped = _puae_flipped(shared)
    return ModeChoice(
        _puae_cd_nvram_mode(shared),
        alternatives=((_puae_cd_nvram_mode(flipped), ((_PUAE_SHARED_NVRAM, flipped),)),),
    )


def _puae_rule(core: str) -> Callable[[RuleReading], ModeChoice]:
    """Both PUAE generations read the same switches; only the caveats' core differs."""

    def rule(reading: RuleReading) -> ModeChoice:
        content_class = _puae_class(core, reading.content_extension)
        if isinstance(content_class, ModeChoice):
            return content_class
        model = reading.option_values[_PUAE_MODEL]
        if model is None:
            return ModeChoice(None, caveats=(_value_unestablished(core, _PUAE_MODEL),))
        if model not in _PUAE_MODELS:
            return ModeChoice(None, caveats=(_unknown_value(core, _PUAE_MODEL, model),))
        if content_class == _PUAE_CLASS_CD:
            return _puae_cd(core, reading, model)
        return _puae_floppy(core, reading, model, content_class)

    return rule


# ---------------------------------------------------------------------------
# opera — the NVRAM file's name carries two option values, so the modes are
# their product. 'opera_nvram_storage' picks the directory and the naming
# scheme and 'opera_nvram_version' the number in the name: per game writes
# <save dir>/opera/per_game/<stem>.<version>.srm and shared writes
# <save dir>/opera/shared/nvram.<version>.srm (opera_lr_nvram.c:130-152,
# :154-175 at 67a29e6; the 'opera' segment joined onto the frontend's save
# directory at :115-128, the stem taken from the content path at :369-374).
# Ten registered versions times two storages is twenty modes naming one file
# each — the file-name vocabulary has no option-valued hole, so the product is
# enumerated in the card rather than parametrized (issue #387).
#
# Both switches are registered options, 'per game'/'shared' defaulting to
# 'per game' and '0' through '9' defaulting to '0' (libretro_core_options.c
# :177-187, :188-206), read through a helper that prefixes 'opera_' to the key
# (opera_lr_opts.c:53-71): the storage is a string comparison against 'shared'
# (:330-337), the version an atoi (:346-351, getval_as_int at :73-85). Either
# would still act on a value outside its registered set, and the rule refuses
# such a value instead of naming the file it would make — the record covers
# the twenty modes the core registers, and a value outside them says the
# record lags rather than that the mode is one of the twenty.
#
# Twenty is a space too large to list whole, so the alternatives are the
# one-edit neighbours — every switch, changed once: the other storage at this
# version, and each other version at this storage.
# ---------------------------------------------------------------------------

_OPERA_STORAGE = "opera_nvram_storage"
_OPERA_VERSION = "opera_nvram_version"
_OPERA_PER_GAME = "per game"
_OPERA_SHARED = "shared"
# What each storage value opens a mode name with, and the versions that close
# it — the two registered sets whose product the card states.
_OPERA_STORAGE_MODES = {_OPERA_PER_GAME: "per-game", _OPERA_SHARED: "shared"}
_OPERA_STORAGES = tuple(_OPERA_STORAGE_MODES)
_OPERA_VERSIONS = tuple(str(version) for version in range(10))


def _opera_mode(storage: str, version: str) -> str:
    """The mode one (storage, version) pair names."""
    return f"{_OPERA_STORAGE_MODES[storage]}-{version}"


def _opera_alternatives(
    storage: str, version: str
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """The one-edit neighbours: the other storage here, each other version here."""
    return tuple(
        (_opera_mode(other, version), ((_OPERA_STORAGE, other),))
        for other in _OPERA_STORAGES
        if other != storage
    ) + tuple(
        (_opera_mode(storage, other), ((_OPERA_VERSION, other),))
        for other in _OPERA_VERSIONS
        if other != version
    )


def _opera(reading: RuleReading) -> ModeChoice:
    storage = reading.option_values[_OPERA_STORAGE]
    version = reading.option_values[_OPERA_VERSION]
    missing = _require_values("opera", ((_OPERA_STORAGE, storage), (_OPERA_VERSION, version)))
    if missing:
        return ModeChoice(None, caveats=missing)
    alien = _refuse_alien(
        "opera",
        ((_OPERA_STORAGE, storage, _OPERA_STORAGES), (_OPERA_VERSION, version, _OPERA_VERSIONS)),
    )
    if alien:
        return ModeChoice(None, caveats=alien)
    # Both are stated past _require_values; the fallbacks are the checker's.
    chosen_storage, chosen_version = storage or "", version or ""
    return ModeChoice(
        _opera_mode(chosen_storage, chosen_version),
        alternatives=_opera_alternatives(chosen_storage, chosen_version),
    )


# The registry the card loader validates against: a card stating a
# ``governing_rule`` must have its function here, and the test suite holds
# the mirror claim — a rule with no card would be code describing nothing.
RULES: Mapping[str, Callable[[RuleReading], ModeChoice]] = {
    "mednafen_saturn": _mednafen_saturn,
    "hatari": _hatari,
    "scummvm": _scummvm,
    "swanstation": _swanstation,
    "mednafen_psx": _beetle_psx_rule("beetle_psx_", "mednafen_psx"),
    "mednafen_psx_hw": _beetle_psx_rule("beetle_psx_hw_", "mednafen_psx_hw"),
    "genesis_plus_gx": _genesis_plus_gx,
    "mame": _mame,
    "puae": _puae_rule("puae"),
    "puae2021": _puae_rule("puae2021"),
    "opera": _opera,
}
