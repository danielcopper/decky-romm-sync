"""Interpretation of ``retroarch.cfg`` and its override chain — the layout keys.

RetroArch's on-disk layout for save data is not a static path: it is governed by
live config values, resolved through a four-layer chain in which later files win
(``config_load_override()``, RetroArch ``configuration.c:7095``):

1. ``retroarch.cfg`` — global
2. ``config/<library_name>/<library_name>.cfg`` — core override
3. ``config/<library_name>/<content_dir>.cfg`` — content-dir override
4. ``config/<library_name>/<rom_name>.cfg`` — game override

"Later files win" is where the chain ends, not how it is walked: RetroArch
merges the whole chain into ONE config and reads each key from it once — see
:func:`_read_layers`, which is what every resolution here folds over.

This module resolves the four governing keys through that chain and reports both
the resolved value and the provenance of each — which file won, which default
applied. Defaults differ per install flavor and are passed in as
:class:`LayoutDefaults`; the shipped sets below are read from the respective
upstream sources, version-pinned in ``docs/research/retrodeck-save-placement.md``.

**Two families, one chain.** Savefiles and savestates are placed by four keys
each, and the two quartets go through the very same machinery: RetroArch reads
them side by side in one function (``runloop.c:8752-8979``), with the same
content fallback, the same two sorting stages in the same order and the same
silent revert. So the resolution here is parameterized by :class:`LayoutKeys`
rather than written twice — the family decides which keys are read and what the
provenance calls them, and nothing else.

Pure text in, value object out. No I/O — the machine seam supplies the texts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True, slots=True)
class LayoutKeys:
    """The four cfg keys that place one family of save data.

    RetroArch spells the same four questions twice, once per family, and reads
    both quartets in one pass (``runloop.c:8763-8768``). Naming them here is
    what lets one resolution answer for either: ``in_content_dir`` roots at the
    content, ``sort_by_content`` and ``sort_by_core`` append their components in
    that order, and ``directory`` is the configured root.

    ``label`` names the family in prose, and ``platform_default_subdir`` is the
    directory RetroArch seeds under its config tree before reading any config —
    ``saves`` for savefiles, ``states`` for savestates
    (``platform_unix.c:2133-2136``). Both travel with the keys because every
    caller that needs one needs the other.
    """

    in_content_dir: str
    sort_by_content: str
    sort_by_core: str
    directory: str
    label: str
    platform_default_subdir: str

    @property
    def governing(self) -> tuple[str, str, str, str]:
        """The four keys, in the order a resolution reads them.

        A line RetroArch drops is stated only when it aims at one of these:
        elsewhere a dropped line is simply a key that is not set, which is what
        atlas reports.
        """
        return (self.in_content_dir, self.sort_by_content, self.sort_by_core, self.directory)


SAVEFILE_KEYS = LayoutKeys(
    in_content_dir="savefiles_in_content_dir",
    sort_by_content="sort_savefiles_by_content_enable",
    sort_by_core="sort_savefiles_enable",
    directory="savefile_directory",
    label="savefile",
    platform_default_subdir="saves",
)

SAVESTATE_KEYS = LayoutKeys(
    in_content_dir="savestates_in_content_dir",
    sort_by_content="sort_savestates_by_content_enable",
    sort_by_core="sort_savestates_enable",
    directory="savestate_directory",
    label="savestate",
    platform_default_subdir="states",
)


@dataclass(frozen=True, slots=True)
class LayoutDefaults:
    """The per-flavor defaults applied when a key is absent from every layer.

    One set serves both families: upstream gives the savestate keys the same
    compile-time values as their savefile twins (``config.def.h:982-989``), and
    the two arrangements that ship their own defaults set both quartets.
    """

    in_content_dir: bool
    sort_by_content: bool
    sort_by_core: bool
    label: str


# RetroArch upstream compile-time defaults (config.def.h:982-989). Note that
# upstream sorts BY CORE by default — a bare install without the key set puts
# saves in per-library_name subdirectories. The savestate quartet is identical
# key for key: DEFAULT_SORT_SAVESTATES_ENABLE true (:983),
# DEFAULT_SORT_SAVESTATES_BY_CONTENT_ENABLE false (:985),
# DEFAULT_SAVESTATES_IN_CONTENT_DIR false (:988).
UPSTREAM_DEFAULTS = LayoutDefaults(
    in_content_dir=False,
    sort_by_content=False,
    sort_by_core=True,
    label="RetroArch upstream default (config.def.h)",
)

# What a setting's spelling can do wrong: the parser refuses the line, or the
# typed getter refuses the value. Either way the setting is not applied.
IGNORED_LINE_DROPPED = "line-dropped"
IGNORED_VALUE_REJECTED = "value-rejected"


@dataclass(frozen=True, slots=True)
class IgnoredSetting:
    """A setting a config states and RetroArch does not apply.

    ``kind`` is :data:`IGNORED_LINE_DROPPED` (the parser refused the line) or
    :data:`IGNORED_VALUE_REJECTED` (the value is outside the setting's
    vocabulary, so the value from before this layer stands). ``layer`` is the
    chain file it was written in, ``key`` the setting it aims at, and ``text``
    the offending spelling — the whole line for a dropped line, the value for
    a rejected one. atlas behaves exactly as RetroArch does; consumers state
    these as caveats because the gap between what the file says and what the
    emulator does is invisible in the answer itself.
    """

    kind: str
    layer: "CfgSource"
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class RejectedDirectory:
    """A configured directory RetroArch read and refused as a root.

    ``path_is_directory`` failed on it, so that read set nothing
    (``configuration.c:6920-6932``). ``layer`` is the chain file that stated
    the value, ``value`` the value as RetroArch tested it (``~`` expanded). Unlike
    :class:`IgnoredSetting` this is not a spelling mistake: the value is
    well-formed, the machine just has no such directory.

    ``superseded`` says where the root that ends up standing came from, which
    is not the same story in both directions. Normally the refusal is the last
    word and what stands is what preceded it — the global cfg's root, or the
    platform default. But the global cfg can be the one refused while an
    override then supplies a usable root: the standing root is then set *after*
    the refusal, and ``superseded`` is true. Saying it the other way round
    would invert the causality.
    """

    layer: "CfgSource"
    value: str
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class RetroArchCfg:
    """One family's layout decision resolved through the override chain, with provenance.

    ``keys`` says which family this is — which four cfg keys were read, and
    therefore what the fields below mean. ``directory`` is the resolved root
    value with ``~`` expanded, or ``None`` when the platform default applies
    (key absent, blank, or the literal ``"default"``). RetroArch initializes
    platform default directories before applying the config — on desktop Unix
    the SRAM default is ``saves`` and the savestate default ``states``, both
    under the RetroArch config tree (``platform_unix.c:2133-2136``; that tree is
    ``$XDG_CONFIG_HOME/retroarch`` or ``$HOME/.config/retroarch``,
    ``platform_unix.c:1943-1957``) — so an unset key means *that* directory,
    never the ROM's directory (the content fallback at ``runloop.c:8786`` and
    its savestate twin at ``:8799`` fire only when the effective dir is still
    empty, which the platform defaults prevent on desktop). The caller supplies
    the concrete platform default.
    ``sources`` records, per governing key, which file (or default) produced
    the value; when an override won, it names it. ``ignored`` carries the
    governing settings the configs *state* and RetroArch does not apply — a
    line its parser drops, a value its typed getter refuses — so a caller sees
    why the file and the answer disagree. ``rejected_directories`` is the same
    kind of gap for the root: values RetroArch read and found not to be
    directories.
    """

    keys: LayoutKeys
    in_content_dir: bool
    sort_by_content: bool
    sort_by_core: bool
    directory: str | None
    sources: tuple[str, ...]
    ignored: tuple[IgnoredSetting, ...] = ()
    rejected_directories: tuple[RejectedDirectory, ...] = ()


# RetroArch's parser, ported line for line from ``config_file.c`` (pinned
# revision a79435a). Its grammar is not "key = value with optional quotes": a
# key is a run of isgraph() characters, and the next non-whitespace character
# after it must be '='. Since '=' is itself a graph character, a key written
# tight against it (``savefile_directory="/x"``) swallows the '=' and the line
# is dropped — reading such a line would attest a path this emulator never uses.
_WHITESPACE = " \t\r\n"
_QUOTE = '"'
_COMMENT = "#"
_NUL = "\x00"


def _is_graph(char: str) -> bool:
    """``isgraph`` in the C locale: printable ASCII, space excluded.

    Bytes outside ASCII are not graph characters in the C locale, and a
    non-ASCII codepoint here stands for exactly those bytes — a run ends at
    the first one either way (``config_file.c:249``, ``:600``).
    """
    return "!" <= char <= "~"


def _graph_run(text: str) -> str:
    """The leading run of graph characters — what the C scan loops measure."""
    for index, char in enumerate(text):
        if not _is_graph(char):
            return text[:index]
    return text


def _strip_comment(line: str) -> tuple[str, bool]:
    """``config_file_strip_comment`` (``config_file.c:159-206``).

    Returns ``(line, is_comment_line)``. Only the **first two** quotes of the
    whole line are weighed against the first ``#``, before any key/value split:
    when the literal they open closes *after* the ``#``, the ``#`` is inside a
    string and the line is left untouched; otherwise everything from the ``#``
    is cut. A ``#`` in column zero (the raw line is not trimmed first) makes
    the whole line a comment — RetroArch reads ``include`` and ``reference``
    directives there, which atlas does not resolve: a stated gap (task list),
    not an approximation.
    """
    comment = line.find(_COMMENT)
    if comment < 0:
        return line, False
    if comment == 0:
        return "", True
    literal_start = line.find(_QUOTE)
    if 0 <= literal_start < comment and line.find(_QUOTE, literal_start + 1) > comment:
        return line, False
    return line[:comment], False


def _extract_value(text: str) -> str:
    """``config_file_extract_value`` (``config_file.c:208-260``).

    A quoted value ends at the *next* quote — a missing closing quote is not
    an error, the rest of the line is then the value. An unquoted value ends
    at the first non-graph character, so an unquoted ``/mnt/my saves`` is
    ``/mnt/my``. An empty value is a value: the key is set, to ``""``.
    """
    value = text.lstrip(_WHITESPACE)
    if value.startswith(_QUOTE):
        end = value.find(_QUOTE, 1)
        return value[1:] if end < 0 else value[1:end]
    return _graph_run(value)


@dataclass(frozen=True, slots=True)
class DroppedLine:
    """A line RetroArch's parser refuses — it sets nothing there, so nothing here.

    ``key`` is the setting the line appears to aim at (the key run up to its
    first ``=``), ``line`` the line as written. The common cause is a missing
    space before the ``=``: ``savefile_directory="/x"`` scans as one key named
    ``savefile_directory="/x"`` with no ``=`` behind it, and
    ``config_file_parse_line`` drops the entry (``config_file.c:596-623``).
    """

    key: str
    line: str


@dataclass(frozen=True, slots=True)
class ParsedCfg:
    """One cfg text as RetroArch reads it: the settings it makes, the lines it drops."""

    values: dict[str, str]
    dropped: tuple[DroppedLine, ...] = ()


def parse_cfg(text: str) -> ParsedCfg:
    """Parse a cfg text through RetroArch's own line pipeline.

    Strip the comment, take the key run, require the ``=``, extract the value
    — ``config_file_parse_line`` (``config_file.c:524-632``), one line at a
    time split on ``\\n`` alone as the C loop does. The **first** occurrence of
    a duplicate key wins (``config_file.c:496-507`` maps a key only when not
    already present). The key is the whole graph run, so ``savefile_directory``
    and ``savefiles_in_content_dir`` never collide.

    Nothing past the first NUL is read at all: the C parses a NUL-terminated
    buffer and cuts each line with ``strchr(line, '\\n')``, which stops at the
    NUL — that line ends there, and the missing newline then breaks the loop
    (``config_file.c:461-517``, identically ``:644-686`` for the from-string
    path). A cfg NUL-padded by an unclean shutdown therefore sets only what
    stands before the padding; reading on would attest settings RetroArch
    never applies.
    """
    # Everything past the first NUL is invisible to the C parser (see above).
    text = text.partition(_NUL)[0]
    values: dict[str, str] = {}
    dropped: list[DroppedLine] = []
    for raw_line in text.split("\n"):
        line, is_comment = _strip_comment(raw_line)
        if is_comment:
            continue
        line = line.lstrip(_WHITESPACE)
        key = _graph_run(line)
        if not key:
            continue
        rest = line[len(key) :].lstrip(_WHITESPACE)
        if not rest.startswith("="):
            aimed_at = key.split("=", 1)[0]
            if aimed_at:
                dropped.append(DroppedLine(aimed_at, raw_line.strip()))
            continue
        values.setdefault(key, _extract_value(rest[1:]))
    return ParsedCfg(values, tuple(dropped))


def parse_cfg_text(text: str) -> dict[str, str]:
    """The settings a cfg text makes — :func:`parse_cfg` without the dropped lines."""
    return parse_cfg(text).values


_BOOL_TRUE = ("1", "true")
_BOOL_FALSE = ("0", "false")


def cfg_bool(raw: str) -> bool | None:
    """RetroArch's boolean vocabulary — ``None`` when the value is outside it.

    ``config_get_bool`` accepts exactly ``1``, ``true``, ``0`` and ``false``,
    case-sensitively, and reports failure for anything else
    (``config_file.c:1227-1262``); the loop that applies boolean settings then
    writes nothing (``configuration.c:6412-6417``), so the setting keeps the
    value it already had. ``"TRUE"`` and ``"yes"`` are not booleans: they set
    nothing at all, they do not mean false.
    """
    if raw in _BOOL_TRUE:
        return True
    if raw in _BOOL_FALSE:
        return False
    return None


# ``strtoul``'s world: C's own whitespace, C's own digits, and the two limits
# the caller checks the result against on a 64-bit desktop build.
_C_WHITESPACE = " \t\n\v\f\r"
_ULONG_MAX = 0xFFFF_FFFF_FFFF_FFFF
_UINT_MAX = 0xFFFF_FFFF
_NOT_A_DIGIT = 36


def _digit_value(char: str) -> int:
    """The value of one ASCII alphanumeric as a digit — 36 when it is not one.

    ``strtoul`` reads digits in the C locale, so only ASCII counts: an
    Arabic-Indic ``٢`` ends the run rather than being a two.
    """
    if "0" <= char <= "9":
        return ord(char) - ord("0")
    if "a" <= char <= "z":
        return ord(char) - ord("a") + 10
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 10
    return _NOT_A_DIGIT


def _literal_base(text: str, start: int) -> tuple[int, int]:
    """Base 0's reading of a literal: where its digits begin, and in which base.

    C's own spelling rules — ``0x``/``0X`` before a hex digit is hexadecimal, a
    leading ``0`` is octal (and is itself the first digit, so ``0`` alone reads
    as zero), everything else decimal.
    """
    if text[start : start + 1] != "0":
        return start, 10
    if text[start + 1 : start + 2] in ("x", "X") and _digit_value(text[start + 2 : start + 3]) < 16:
        return start + 2, 16
    return start, 8


def _digit_run(text: str, start: int, base: int) -> int:
    """One past the last digit of *base* from *start* — how far the scan reads."""
    index = start
    while index < len(text) and _digit_value(text[index]) < base:
        index += 1
    return index


def cfg_uint(raw: str) -> int | None:
    """RetroArch's unsigned reading of a value — ``None`` when it is not one.

    ``config_get_uint`` hands the value to ``strtoul(value, &end, 0)`` and
    takes the result only when digits were found, nothing overflowed, the
    **whole** value was consumed (``*end == '\\0'``) and it fits an ``unsigned``
    (``config_file.c:1081-1103``). So ``2 files`` and a trailing space set
    nothing at all, while base 0 accepts C's other spellings of the same
    number: ``0x2`` and ``02`` are both two. Leading whitespace and a sign are
    skipped by ``strtoul`` itself, and a quoted value is where they can survive
    the parser (``config_file.c:222-240``).
    """
    start = len(raw) - len(raw.lstrip(_C_WHITESPACE))
    negative = raw[start : start + 1] == "-"
    if raw[start : start + 1] in ("+", "-"):
        start += 1
    digits_at, base = _literal_base(raw, start)
    end = _digit_run(raw, digits_at, base)
    if end == digits_at or end != len(raw):
        return None
    magnitude = int(raw[digits_at:end], base)
    if magnitude > _ULONG_MAX:
        return None
    # An unsigned negation wraps rather than failing, exactly as the C does.
    value = -magnitude % (_ULONG_MAX + 1) if negative else magnitude
    return value if value <= _UINT_MAX else None


_APP_DIR_PREFIX = ":"
_UNSET_VALUE = "default"


def is_app_relative(raw: str) -> bool:
    """Does this path value carry RetroArch's application-directory prefix?

    ``fill_pathname_expand_special`` expands a leading ``:`` against the
    directory of the *running* RetroArch executable (``file_path.c:1066-1101``
    → ``fill_pathname_application_dir``, ``file_path.c:1447-1455``, which reads
    ``/proc/<pid>/exe``, ``file_path.c:1421-1441``). Which executable that is
    is a property of the running process, not of anything on disk, so atlas
    states such a value unexpanded rather than inventing a directory for it.
    """
    return raw.startswith(_APP_DIR_PREFIX)


def expand_home(raw: str, *, home: str | None) -> str | None:
    """Expand a leading ``~`` against *home*; ``None`` when the value sets no path.

    Blank and the literal ``default`` are RetroArch's "unset" spellings, the
    latter compared case-sensitively (``string_is_equal``,
    ``configuration.c:6918``, ``:6825``). ``~`` is expanded by
    ``config_get_path`` → ``fill_pathname_expand_special``
    (``config_file.c:1202-1216``, ``file_path.c:1066-1101``) on desktop builds.
    The value is used exactly as parsed: a quoted value may legitimately carry
    leading or trailing spaces, and RetroArch does not trim them.

    *home* is what the running RetroArch's environment answers for ``HOME`` —
    the substitution reads ``getenv("HOME")`` (``fill_pathname_home_dir``,
    ``file_path.c:1457-1468``) — so it is whatever home the caller established
    for that process, not necessarily the machine's. ``None`` and the empty
    string are the absent and empty variable, and both leave the value exactly
    as written: an empty home skips the whole substitution block
    (``file_path.c:1081``) and the input is copied through unchanged
    (``:1100``). A *home* with a trailing slash is not doubled — the separator
    is inserted only when the copied home does not already end in one
    (``file_path.c:1088-1094``).

    This is the one place the port knowingly departs from upstream: once a home
    directory is filled, ``in_path += 2`` runs unconditionally
    (``file_path.c:1096``), so RetroArch turns ``~foo`` into ``<home>/oo`` and
    reads one byte past the terminator for a bare ``~``. atlas expands the
    ``~`` and ``~/`` spellings and leaves every other one alone — the upstream
    answer there is an out-of-bounds read, which is not a save directory atlas
    can state.
    """
    if raw in ("", _UNSET_VALUE):
        return None
    if not home:
        return raw
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home + raw[2:] if home.endswith("/") else home + raw[1:]
    return raw


# Which file of the override chain a value was read from — the kind, not the
# path. Four kinds and no more: RetroArch loads the global cfg and then, in
# this order, the core, content-directory and game override
# (``configuration.c:7095``). A client branching on where a refused root came
# from reads these; the file itself rides beside them.
CFG_LAYER_GLOBAL = "global"
CFG_LAYER_CORE_OVERRIDE = "core-override"
CFG_LAYER_CONTENT_DIR_OVERRIDE = "content-dir-override"
CFG_LAYER_GAME_OVERRIDE = "game-override"
CFG_LAYER_KINDS = (
    CFG_LAYER_GLOBAL,
    CFG_LAYER_CORE_OVERRIDE,
    CFG_LAYER_CONTENT_DIR_OVERRIDE,
    CFG_LAYER_GAME_OVERRIDE,
)
# How each kind reads in a sentence. The global cfg is named by its file alone
# — "retroarch.cfg: save_directory = …" — and an override says which kind it is
# in front of the file, which is the phrasing every provenance line and message
# in this package already uses.
_CFG_LAYER_PHRASE = {
    CFG_LAYER_GLOBAL: "",
    CFG_LAYER_CORE_OVERRIDE: "core override ",
    CFG_LAYER_CONTENT_DIR_OVERRIDE: "content-dir override ",
    CFG_LAYER_GAME_OVERRIDE: "game override ",
}


@dataclass(frozen=True, slots=True)
class CfgSource:
    """One file of the chain: which kind of layer it is, and which file it is.

    Two facts, kept apart because a client acts on each: ``kind`` is one of
    :data:`CFG_LAYER_KINDS` and says how the file got into the chain, ``file``
    is the file this answer names it by and says what to edit. ``label`` is the
    two of them as one phrase, for the provenance strings and messages — prose
    built from the data rather than data parsed back out of prose, which is
    what a caveat carrying ``"core override config/mGBA/mGBA.cfg"`` under one
    key asked every client to do.
    """

    kind: str
    file: str

    def __post_init__(self) -> None:
        if self.kind not in _CFG_LAYER_PHRASE:
            raise ValueError(f"CfgSource: {self.kind!r} is not one of {CFG_LAYER_KINDS}")

    @property
    def label(self) -> str:
        return f"{_CFG_LAYER_PHRASE[self.kind]}{self.file}"


# One layer of the chain: where it came from, its parsed content, and whether
# it is an override (the global cfg is not).
_Layer = tuple[CfgSource, ParsedCfg, bool]

# One layer as a caller holds it before parsing: source and text, in load order.
CfgLayer = tuple[CfgSource, str]

# ``path_is_directory`` as the machine answers it. A value the caller cannot
# test — a path that exists only inside a sandbox — must answer true: the
# emulator's own test still decides, atlas simply did not perform it, and
# answering false would reject a directory that is very likely there.
DirectoryCheck = Callable[[str], bool]


def _parse_layers(layers: Sequence[CfgLayer]) -> list[_Layer]:
    """Text layers as parsed layers — the global cfg first, its overrides after."""
    return [(source, parse_cfg(text), index > 0) for index, (source, text) in enumerate(layers)]


def _read_layers(layers: Sequence[_Layer], key: str) -> list[_Layer]:
    """The layers RetroArch actually reads *key* from, in the order it reads them.

    Not one load per override: ``config_load_override`` collects the override
    files that exist and makes a SINGLE reload of the global cfg
    (``configuration.c:7161-7243``), during which ``config_append_file`` merges
    each override into one config where "the key-value pairs of the new config
    file takes priority over the old" (``config_file.c:768-805``, appended at
    ``configuration.c:6355-6392``). A getter therefore sees exactly one entry
    per key — the last file that sets it.

    That reload runs without ``config_set_defaults`` (``:7243``), so whatever
    it refuses leaves standing what the BOOT load left: the global cfg alone,
    read the same way, on top of the compile-time defaults. Two reads, then,
    not four — a layer between those two is shadowed, and its value never
    reaches a getter at all.
    """
    reads: list[_Layer] = []
    if layers and key in layers[0][1].values:
        reads.append(layers[0])
    overriding = [layer for layer in layers[1:] if key in layer[1].values]
    if overriding:
        reads.append(overriding[-1])
    return reads


def _apply_bool(layer: _Layer, key: str, standing: bool) -> tuple[bool, IgnoredSetting | None]:
    """One read of a boolean setting: what stands after it, and any refusal.

    A value outside RetroArch's boolean vocabulary is a no-op — the loop that
    applies boolean settings writes nothing when ``config_get_bool`` fails
    (``configuration.c:6412-6417``), so *standing* stands on.
    """
    source, parsed, _ = layer
    raw = parsed.values[key]
    decoded = cfg_bool(raw)
    if decoded is None:
        return standing, IgnoredSetting(IGNORED_VALUE_REJECTED, source, key, raw)
    return decoded, None


def _dropped_lines(layers: Sequence[_Layer], key: str) -> tuple[IgnoredSetting, ...]:
    """Lines aimed at *key* that RetroArch's parser refused, across the chain."""
    return tuple(
        IgnoredSetting(IGNORED_LINE_DROPPED, source, line.key, line.line)
        for source, parsed, _ in layers
        for line in parsed.dropped
        if line.key == key
    )


def chain_bool(
    layers: Sequence[CfgLayer], key: str, *, default: bool
) -> tuple[bool, tuple[IgnoredSetting, ...]]:
    """One boolean key as RetroArch reads it through the chain (:func:`_read_layers`).

    Returns the effective value and the settings the configs state that it does
    not reflect — values the typed getter refuses, lines the parser drops.
    """
    parsed_layers = _parse_layers(layers)
    value = default
    ignored: list[IgnoredSetting] = []
    for layer in _read_layers(parsed_layers, key):
        value, refused = _apply_bool(layer, key, value)
        if refused is not None:
            ignored.append(refused)
    return value, (*ignored, *_dropped_lines(parsed_layers, key))


def chain_value(
    layers: Sequence[CfgLayer], key: str
) -> tuple[str | None, tuple[IgnoredSetting, ...]]:
    """One raw key as RetroArch reads it through the chain — ``None`` when unset.

    The generic path/array loops write whatever the merged config holds
    (``configuration.c:6532-6537``), so the last layer that sets the key is the
    answer; only ``savefile_directory`` and its savestate twin are validated
    before they are applied (``:6914-6933`` and ``:6935-6960``). The dropped
    lines travel along: a line the parser refused sets nothing, and nothing else
    in the answer says so.
    """
    parsed_layers = _parse_layers(layers)
    reads = _read_layers(parsed_layers, key)
    value = reads[-1][1].values[key] if reads else None
    return value, _dropped_lines(parsed_layers, key)


def _resolve_flag(
    layers: Sequence[_Layer], key: str, *, default: bool, defaults_label: str
) -> tuple[bool, str, tuple[IgnoredSetting, ...]]:
    """One boolean key through the chain, with provenance — see :func:`chain_bool`."""
    value, source = default, f"default: {key} = {str(default).lower()} ({defaults_label})"
    ignored: list[IgnoredSetting] = []
    for layer in _read_layers(layers, key):
        source_layer, parsed, is_override = layer
        value, refused = _apply_bool(layer, key, value)
        if refused is not None:
            ignored.append(refused)
            continue
        raw = parsed.values[key]
        source = f'{source_layer.label}: {key} = "{raw}"' + (" (override wins)" if is_override else "")
    return value, source, tuple(ignored)


def _resolve_directory(
    layers: Sequence[_Layer], *, keys: LayoutKeys, home: str | None, is_directory: DirectoryCheck | None
) -> tuple[str | None, str, tuple[RejectedDirectory, ...]]:
    """The family's root through the chain — ``None`` when the platform default applies.

    Each read is validated the way ``config_load_file`` validates it
    (``configuration.c:6914-6933`` for ``savefile_directory``, and its
    line-for-line twin at ``:6935-6960`` for ``savestate_directory``): the
    literal ``default`` resets to the platform default unconditionally, and
    every other value must pass ``path_is_directory`` or the read sets nothing
    and what stood before it stands on. Without an *is_directory* check the
    values are taken as written — except the empty string, which
    ``path_is_directory`` refuses on every machine.

    **Blank means opposite things for different directory keys, and that is not
    a bug in any of them.** One boolean in the settings table decides it — the
    ``handle_setting`` argument of ``SETTING_PATH``:

    - ``savefile_directory`` passes ``false`` (``configuration.c:1709``), so
      the generic path loop skips it (``:6534-6535``) and the special block
      above is the only thing that sets it. ``config_get_path`` returns true
      for an entry whose value is empty and hands the empty string on
      unchanged (``config_file.c:1202-1216``), ``path_is_directory("")``
      fails, and the read is a no-op — blank **keeps** the standing root.
    - ``savestate_directory`` passes ``false`` too (``configuration.c:1710``),
      so both save families behave identically here. Worth stating because the
      neighbouring keys do not: this is a per-key flag, not a property of
      "directory settings", and the savestate row had to be read to know it.
    - ``rgui_config_directory`` passes ``true`` (``configuration.c:1736``), so
      the generic path loop writes whatever the config holds with no test at
      all (``:6536-6537``) — blank **clears** it, exactly as the literal
      ``default`` does two hundred lines later (``:6825-6826``), and an empty
      ``directory_menu_config`` then falls back to the directory of
      ``retroarch.cfg`` (``file_path_special.c:203-206``).

    So a blank root in an override changes nothing, while a blank override
    directory moves the entire override tree. atlas reproduces both
    (:func:`atlas.installations._override_directory` holds the second).

    An application-relative value (``:`` prefix) is kept as written: it names a
    directory only the running process knows, and the consumer states that
    rather than expanding it into a host path atlas cannot verify.
    """
    directory: str | None = None
    source = (
        f"default: {keys.directory} unset — RetroArch platform default applies "
        f"({keys.platform_default_subdir} under the config tree, platform_unix.c:2133-2136)"
    )
    # Which read produced the root that stands decides how a refusal reads: a
    # refusal the chain never got past fell back to what preceded it, while one
    # a later read overwrote did not — see RejectedDirectory.superseded.
    refusals: list[tuple[int, CfgSource, str]] = []
    last_set = -1
    for index, (layer, parsed, is_override) in enumerate(_read_layers(layers, keys.directory)):
        raw = parsed.values[keys.directory]
        suffix = " (override wins)" if is_override else ""
        if raw == _UNSET_VALUE:
            directory, last_set = None, index
            source = (
                f'{layer.label}: {keys.directory} = "{raw}" — resets to the RetroArch '
                f"platform default{suffix}"
            )
            continue
        candidate = expand_home(raw, home=home)
        if candidate is None or (is_directory is not None and not is_directory(candidate)):
            refusals.append((index, layer, candidate if candidate is not None else raw))
            continue
        directory, last_set = candidate, index
        source = f'{layer.label}: {keys.directory} = "{raw}"{suffix}'
    rejected = [
        RejectedDirectory(layer, value, superseded=index < last_set)
        for index, layer, value in refusals
    ]
    return directory, source, tuple(rejected)


def _dropped_governing_lines(layers: Sequence[_Layer], keys: LayoutKeys) -> tuple[IgnoredSetting, ...]:
    """Lines RetroArch dropped that aimed at one of *this family's* layout keys.

    RetroArch drops them silently and so does atlas — but a dropped line
    naming the very setting this answer is about is the one case where the
    file and the emulator disagree about the location, so it is stated. A line
    aimed at the *other* family's keys is not this answer's business: it moves
    a directory this answer never reports.
    """
    governing = keys.governing
    return tuple(
        IgnoredSetting(IGNORED_LINE_DROPPED, source, line.key, line.line)
        for source, parsed, _ in layers
        for line in parsed.dropped
        if line.key in governing
    )


def resolve_layout(
    global_text: str | None,
    *,
    keys: LayoutKeys,
    home: str | None,
    cfg_label: str,
    defaults: LayoutDefaults,
    overrides: Sequence[CfgLayer] = (),
    is_directory: DirectoryCheck | None = None,
) -> RetroArchCfg:
    """Resolve one family's layout through the override chain, as RetroArch reads it.

    The overrides are merged into the global cfg and each key is read from the
    result — the last file that sets it wins, and a value RetroArch refuses
    falls back to the global cfg's own (:func:`_read_layers`).

    Parameters
    ----------
    global_text:
        The global cfg's content, or ``None`` when no cfg was found (``None``
        and an empty file both yield the all-defaults decision).
    keys:
        Which family to resolve — :data:`SAVEFILE_KEYS` or
        :data:`SAVESTATE_KEYS`. Deliberately without a default: it is the one
        argument that decides *what the answer is about*, and a resolution
        silently defaulting to the savefile quartet would answer the wrong
        question in the one shape that looks right.
    home:
        What the running RetroArch's environment answers for ``HOME``, used to
        expand a leading ``~`` in the root value (:func:`expand_home`) —
        usually the machine home, and ``None`` where that environment carries
        no usable HOME, which leaves a ``~`` value exactly as written.
    cfg_label:
        The global cfg's file name, woven into provenance strings and carried
        as that layer's :class:`CfgSource`.
    defaults:
        The flavor's defaults, applied when no layer sets a key.
    overrides:
        ``(CfgSource, text)`` pairs in load order (core, content-dir, game) —
        exactly the files that exist, already read through the machine seam.
        Each layer overrides only the keys it actually sets.
    is_directory:
        ``path_is_directory`` as this machine answers it, used to validate each
        root value RetroArch reads. Omitted, the values are taken as written
        and ``rejected_directories`` reports only the blank one.
    """
    empty = ParsedCfg({})
    layers: list[_Layer] = [
        (
            CfgSource(CFG_LAYER_GLOBAL, cfg_label),
            parse_cfg(global_text) if global_text is not None else empty,
            False,
        )
    ]
    layers.extend((source, parse_cfg(text), True) for source, text in overrides)

    defaults_label = defaults.label
    in_content_dir, s1, i1 = _resolve_flag(
        layers, keys.in_content_dir, default=defaults.in_content_dir, defaults_label=defaults_label
    )
    sort_by_content, s2, i2 = _resolve_flag(
        layers, keys.sort_by_content, default=defaults.sort_by_content, defaults_label=defaults_label
    )
    sort_by_core, s3, i3 = _resolve_flag(
        layers, keys.sort_by_core, default=defaults.sort_by_core, defaults_label=defaults_label
    )
    directory, dir_source, rejected = _resolve_directory(
        layers, keys=keys, home=home, is_directory=is_directory
    )

    return RetroArchCfg(
        keys=keys,
        in_content_dir=in_content_dir,
        sort_by_content=sort_by_content,
        sort_by_core=sort_by_core,
        directory=directory,
        sources=(s1, s2, s3, dir_source),
        ignored=(*_dropped_governing_lines(layers, keys), *i1, *i2, *i3),
        rejected_directories=rejected,
    )
