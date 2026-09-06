"""DuckStation's own reads, shared by every route that asks it something.

Three things live here because two routes need them and neither owns them: the
**DataRoot probe** (which of two directories this launch keeps its settings in),
the ``LoadPathFromSettings`` shape (how one settings value becomes a directory),
and the **BIOS recognition table** packaged beside the code.

The last is the one worth naming. DuckStation names no BIOS file: it searches a
directory, skips every file whose size is not one of three, and recognises what
is left by hashing it against a table compiled into the binary
(``FindBIOSImageInDirectory``, bios.cpp:364-400 at the pin). So "is a BIOS here"
is a question about *content*, and answering it at all means carrying the table
— which is why ``atlas/data/duckstation_bios.json`` exists, generated from
upstream's source and pinned to the revision it was read at.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._data import packaged_text
from . import emulator_settings, qt_ini
from .machine import GLOB_COMPLETE, READ_MISSING, READ_OK, Machine
from .placement import (
    CAVEAT_CORE_MODE_UNESTABLISHED,
    CAVEAT_PER_GAME_LAYER_UNREAD,
    CAVEAT_PER_GAME_OVERRIDES_PRESENT,
    REASON_DATA_ROOT_DECIDED_BY_LAUNCH,
    Caveat,
)

# The emulator's own directory name below whichever XDG base the launch picked,
# and the settings file inside it.
CONFIG_DIRECTORY = "duckstation"
CONFIG_FILENAME = "settings.ini"
# The card token this emulator answers under, and the key its settings file is
# addressed by in atlas/data/emulator_settings.json.
TOKEN = "DUCKSTATION"


@dataclass(frozen=True, slots=True)
class SettingsRead:
    """One DataRoot probe, resolved.

    ``root`` is always usable — the probe falls back to the candidate the
    environment-unset branch picks — and the other three fields say how much
    the machine actually told us: ``stated_path`` is the settings file that
    spoke, ``unreadable`` the one that exists and could not be read, and
    ``ambiguous`` marks the state where neither candidate holds a file, so
    which root the launch would use is decided by an environment variable that
    no file records.

    ``ambiguous`` needs *two* candidates to be true. A flatpak launch has its
    ``XDG_CONFIG_HOME`` force-pinned, so only the config side is reachable and
    an empty tree there is a DataRoot with no settings file yet — not a
    question about the environment.
    """

    root: str
    values: Mapping[tuple[str, str], str]
    stated_path: str | None
    unreadable: str | None
    ambiguous: bool


def settings_candidates(
    *,
    config_home: str,
    data_home: str,
    flatpak: str | None = None,
    xdg_pinned: bool = False,
) -> tuple[str, ...]:
    """Where this launch may open ``settings.ini``, in the order the probe reads.

    The order and the two bases are the settings table's statement, not this
    module's: ``$XDG_CONFIG_HOME/duckstation`` where that variable is set and
    absolute, else ``~/.local/share/duckstation`` (qthost.cpp:562-582).
    Nothing on the machine records which branch a launch took, so both are
    candidates and the file that exists speaks for itself — unless the launch
    runs inside a flatpak, where the variable is pinned set and only the
    config side is reachable (*xdg_pinned*).
    """
    return emulator_settings.settings_file(TOKEN, CONFIG_FILENAME).locations(
        config_home=config_home,
        data_home=data_home,
        flatpak=flatpak,
        xdg_pinned=xdg_pinned,
    )


def data_root_candidates(
    *,
    config_home: str,
    data_home: str,
    flatpak: str | None = None,
    xdg_pinned: bool = False,
) -> tuple[str, ...]:
    """The DataRoots those candidates hang off — each settings file's own directory."""
    return tuple(
        os.path.dirname(path)
        for path in settings_candidates(
            config_home=config_home,
            data_home=data_home,
            flatpak=flatpak,
            xdg_pinned=xdg_pinned,
        )
    )


def read_settings(
    machine: Machine,
    *,
    config_home: str,
    data_home: str,
    flatpak: str | None = None,
    xdg_pinned: bool = False,
) -> SettingsRead:
    """Read ``settings.ini`` from whichever DataRoot holds one."""
    candidates = settings_candidates(
        config_home=config_home,
        data_home=data_home,
        flatpak=flatpak,
        xdg_pinned=xdg_pinned,
    )
    for path in candidates:
        root = os.path.dirname(path)
        result = machine.read_text(path)
        if result.status == READ_MISSING:
            continue
        if result.status != READ_OK:
            return SettingsRead(
                root=root, values={}, stated_path=None, unreadable=path, ambiguous=False
            )
        return SettingsRead(
            root=root,
            values=qt_ini.values(result.text or ""),
            stated_path=path,
            unreadable=None,
            ambiguous=False,
        )
    return SettingsRead(
        root=os.path.dirname(candidates[-1]),
        values={},
        stated_path=None,
        unreadable=None,
        ambiguous=len(candidates) > 1,
    )


def dataroot_caveat(token: str, below: str) -> Caveat:
    """The one statement every DuckStation answer makes about an unrecorded launch.

    Two directories can be this emulator's DataRoot and an environment
    variable decides which — a fact no file on the machine holds. Four routes
    say it, and they have to say it in the same words with the same data, or a
    caller comparing the save, BIOS, texture and mod answers of one entry
    finds four tellings of one fact and no way to see they are the same. It
    lives here, in the module both ``installations`` and ``firmware`` already
    read this emulator through, because it was written out three times and the
    three had already begun to drift in their tails.

    *below* names what hangs off the chosen side — the differing half, and the
    only one a route supplies.
    """
    return Caveat(
        CAVEAT_CORE_MODE_UNESTABLISHED,
        "no settings.ini exists on either DataRoot candidate — DuckStation picks its root "
        "from the launch environment (XDG_CONFIG_HOME set routes it to the config side, "
        f"qthost.cpp:562-582), which no file records; {below} hangs off the "
        "environment-unset side",
        {"core": token, "reason": REASON_DATA_ROOT_DECIDED_BY_LAUNCH},
    )


def load_path(
    values: Mapping[tuple[str, str], str], root: str, section: str, name: str, default: str
) -> str:
    """``EmuFolders::LoadPathFromSettings`` (settings.cpp:1952-1962) as a read.

    An unset *or empty* value is the default, and anything relative — the
    default included — hangs off the DataRoot through ``Path::Combine``
    (:1958-1959), which is :func:`atlas.qt_ini.path_combine` and not
    ``os.path.join``: the combine collapses separator runs and strips a
    trailing separator, so a degenerate spelling like ``memcards//sub/``
    composes to the directory the emulator opens rather than to the same
    inode under a spelling it never uses (#325). The key is matched the way
    ``CSimpleIniA`` matches it — ASCII case-insensitively, last occurrence
    winning (:func:`atlas.qt_ini.simpleini_value` carries the chain, #295) —
    so a ``[folders]`` spelling governs here exactly as it does in the
    running emulator.

    Upstream then hands the composed path to ``Path::RealPath`` (:1960;
    file_system.cpp:301-476 at 64655818e), and what an answer mirrors of it
    is split, deliberately. RealPath walks the path a component at a time,
    replacing each symlink with its target (``lstat``/``readlink``,
    :397-464, a relative target resolved against the link's own directory)
    and giving up the walk at the first component that does not exist — that
    half atlas states at the answer instead of folding in, as
    ``physical_dir`` and the dead-link caveats, so a caller sees both the
    path the emulator composes and where it lands. RealPath then strips
    ``.``/``..`` components lexically (``Path::Canonicalize``, :474) —
    safe for upstream because every symlink in the leading portion was
    already replaced, upstream's own comment on the line — and that half
    atlas does not mirror at all: a configured value spelling ``..`` keeps
    its spelling in the answer, where the running emulator's folder string
    has it resolved against the symlink-replaced parent, which no lexical
    read reproduces (the reason :func:`atlas.qt_ini.path_combine` resolves
    no dot components).

    The absolute arm is returned as spelled, and that is the last piece of
    the split. Upstream hands an absolute value to ``RealPath`` too, and its
    rebuild from split components (:306-308; ``SplitNativePath`` skips
    consecutive separators and keeps no trailing element, :781-812, and
    ``Canonicalize`` re-joins through ``JoinNativePath``, :499-527 and
    :815-818) collapses separator runs and drops a trailing separator on the
    way — so ``[Folders] Textures = /mnt/packs//sub/`` keeps that spelling
    in the answer while the running emulator's folder string is
    ``/mnt/packs/sub``. Not mirrored, deliberately: ``RealPath`` also
    resolves the symlinks on that arm, so a runs-collapsed but
    link-unresolved spelling would match neither the configured value nor
    upstream's in-memory string — the answer keeps the configured spelling
    in ``dir`` and leaves the kernel truth to ``physical_dir``.
    """
    raw, _ = qt_ini.simpleini_value(values, section, name)
    value = raw or default
    return value if os.path.isabs(value) else qt_ini.path_combine(root, value)


# The per-game settings layer. Its directory is the usual LoadPathFromSettings
# shape, ``[Folders] GameSettings`` defaulting to ``gamesettings`` below the
# DataRoot (settings.cpp:1972, the compiled default the same join at :1941).
GAME_SETTINGS_SECTION = "Folders"
GAME_SETTINGS_KEY = "GameSettings"
GAME_SETTINGS_DEFAULT = "gamesettings"

# The switch that decides whether the layer is loaded at all, and its compiled
# default (settings.cpp:162; UpdateGameSettingsLayer loads nothing when it is
# false, system.cpp:1410).
APPLY_SECTION = "Main"
APPLY_KEY = "ApplyGameSettings"

_ANY_INI_GLOB = "*.ini"

# What every route citing the layer cites, written once so four answers cannot
# drift into four tellings of one fact — the reason :func:`dataroot_caveat`
# lives here too.
_LAYER = (
    "UpdateGameSettingsLayer, system.cpp:1407-1441 at stenzek/duckstation@64655818e — "
    "the file is <serial>.ini, folded onto a disc set's first serial "
    "(GetGameSettingsPath, :1145-1152)"
)
# Why a key can be answered differently at all: the read that consumes it goes
# through the LAYERED settings interface rather than through the base one. That
# is not one door — the memory-card keys arrive via Host::GetSettingsInterface
# and the BIOS ones via Host::GetStringSettingValue — so each route states the
# door it came through (*read_through*) rather than sharing a citation that
# would be right for one answer and wrong for the other. The folder keys go
# through neither: EmuFolders::LoadConfig is handed the base layer at every
# call site there is, which is why so many DuckStation answers stay silent.


def applies_game_settings(values: Mapping[tuple[str, str], str]) -> bool:
    """Would this launch load a per-game layer at all? ``[Main] ApplyGameSettings``.

    ``UpdateGameSettingsLayer`` loads nothing while ``apply_game_settings`` is
    false (system.cpp:1410), and that value is
    ``si.GetBoolValue("Main", "ApplyGameSettings", true)`` (settings.cpp:162).
    So an emulator whose own settings file switches the layer off has no
    per-game layer to state, and stating one would be a claim about a file
    that is never opened.

    Absent or unreadable-as-a-boolean is **not** off: ``GetBoolValue`` keeps
    the caller's default when ``FromChars<bool>`` yields nothing
    (settings_interface.h:77-81), and that default is ``true``. Only a value
    the emulator itself reads as false silences the statement, which is why
    this goes through :func:`atlas.qt_ini.from_chars_bool` rather than through
    a reasonable-looking test of its own.

    The value read is the one in ``settings.ini`` — the base layer — and that
    is the right one even though ``Settings::Load`` reads the key through the
    layered interface: at the read that matters the game layer is not
    installed yet, so the base value is what decides whether it ever is.
    """
    raw, _ = qt_ini.simpleini_value(values, APPLY_SECTION, APPLY_KEY)
    return qt_ini.from_chars_bool(raw) is not False


def _spelling(keys: tuple[str, ...]) -> tuple[str, str, str]:
    """``(the keys joined, "key"/"keys", "is"/"are")`` — one arity, three places.

    The joined spelling is for the SENTENCE only: ``data["key"]`` carries the
    keys themselves, as the list they are.
    """
    return ", ".join(keys), "keys" if len(keys) > 1 else "key", "are" if len(keys) > 1 else "is"


def per_game_unread_caveat(
    *,
    token: str,
    directory: str,
    keys: tuple[str, ...],
    governs: str,
    read_through: str,
    sandbox_value: str | None = None,
) -> Caveat:
    """Whether any game overrides this answer was **not** established.

    Not the same fact as "no game does", which is what silence means here, and
    the difference is what a client acts on: a failed listing can be retried
    with more permission, an answered one cannot be improved on. *directory*
    is the location the check would have been made at — a host path where the
    listing failed, the emulator's own sandbox spelling where there is no host
    path at all, which is what *sandbox_value* marks.
    """
    spelled, plural, _ = _spelling(keys)
    where = (
        f"[{GAME_SETTINGS_SECTION}] {GAME_SETTINGS_KEY} = {sandbox_value!r} names a location "
        "only the emulator's sandbox can spell"
        if sandbox_value is not None
        else f"{directory} could not be listed"
    )
    return Caveat(
        CAVEAT_PER_GAME_LAYER_UNREAD,
        f"{where}, so whether any game on this machine carries a per-game settings file is "
        f"unknown — DuckStation layers such a file over the whole configuration while that "
        f"game runs ({_LAYER}), and the {plural} {spelled} would be read through it "
        f"({read_through}). {governs}",
        {"core": token, "dir": directory, "key": keys},
    )


def per_game_caveats(
    machine: Machine,
    *,
    token: str,
    directory: str,
    keys: tuple[str, ...],
    governs: str,
    read_through: str,
) -> list[Caveat]:
    """The per-game layer stated beside one answer, from a listing of *directory*.

    The PCSX2 vocabulary unchanged, because the situation is unchanged: which
    game runs is not a fact atlas holds, so the layer cannot be read *for* an
    answer — but whether one exists at all is a directory listing.
    ``per-game-overrides-present`` says how many and where, a failed listing
    says the check did not happen, and silence means this answer holds for
    every game. DuckStation has no second, build-shipped layer the way Dolphin
    does, so silence is available here and is the shipped state.

    Nothing claims a key **is** set: a game ini may carry any section, so the
    honest statement is that these keys CAN be answered differently for a game
    this answer cannot name. *keys* are section-qualified the way that file
    must spell them; *governs* is a whole sentence of the answer's own, saying
    what a per-game value does there and — the half worth as much — which part
    of the answer no per-game value can touch. It trails the statement rather
    than sitting inside it: these clauses carry their own citations, and
    nesting one between a subject and its verb left the verb stranded a line
    away from what it belonged to.

    The caller decides whether to ask at all: :func:`applies_game_settings`
    is the gate, and it is one gate for every answer because the switch it
    reads gates the layer as a whole rather than any single key.
    """
    listing = machine.glob(os.path.join(directory, _ANY_INI_GLOB))
    if listing.status != GLOB_COMPLETE:
        return [
            per_game_unread_caveat(
                token=token,
                directory=directory,
                keys=keys,
                governs=governs,
                read_through=read_through,
            )
        ]
    if not listing.matches:
        return []
    spelled, plural, are = _spelling(keys)
    return [
        Caveat(
            CAVEAT_PER_GAME_OVERRIDES_PRESENT,
            f"{len(listing.matches)} game(s) on this machine carry a per-game settings file in "
            f"{directory}, which DuckStation layers over the whole configuration while that "
            f"game runs ({_LAYER}) — the {plural} {spelled} {are} read through that layer "
            f"({read_through}), so this answer is the one that holds for every game without "
            f"such a file. {governs}",
            {
                "core": token,
                "count": str(len(listing.matches)),
                "dir": directory,
                "key": keys,
            },
        )
    ]


@dataclass(frozen=True, slots=True)
class BiosImage:
    """One row of DuckStation's table: what these bytes are, and how much it wants them.

    ``priority`` is upstream's own de-prioritisation and reads backwards from
    the word: **lower wins**. Launch-console images sit at 50, PS2 ones at 100
    and PAL PS2 ones at 150, each with a comment saying why (bios.cpp:42-45).
    """

    name: str
    region: str
    md5: str
    priority: int
    fast_boot_patch: str


@dataclass(frozen=True, slots=True)
class BiosCandidate:
    """One file the search kept: it is of an accepted size, and this is what it is.

    ``image`` is ``None`` for bytes the table does not know — a state
    DuckStation boots anyway, with a warning, so it belongs among the
    candidates rather than outside them.

    ``unreadable`` keeps that state apart from the one it used to be collapsed
    into: bytes atlas could not read are not bytes the table does not know.
    The first is a read failure and settles nothing; the second is a verdict
    about content that was actually seen. Both leave ``image`` at ``None``,
    which is why the flag is here rather than being inferred from it.
    """

    path: str
    image: BiosImage | None
    unreadable: bool = False


@dataclass(frozen=True, slots=True)
class BiosPick:
    """The file a launch would boot, and every file that ranks exactly with it."""

    chosen: BiosCandidate
    tied: tuple[BiosCandidate, ...]

    @property
    def decided(self) -> bool:
        """Did the files alone decide it? ``False`` when only directory order would."""
        return len(self.tied) == 1


class BiosTable:
    """The packaged recognition table, read-only, by content.

    ``sizes`` is the pre-filter and is part of the same rule: a file of any
    other size is skipped before a byte of it is read.
    """

    def __init__(
        self,
        images: tuple[BiosImage, ...],
        sizes: tuple[int, ...],
        openbios: Mapping[str, Any],
        meta: Mapping[str, Any],
    ) -> None:
        self._images = images
        self._by_md5 = {image.md5: image for image in images}
        self._sizes = sizes
        self._openbios = dict(openbios)
        self._meta = dict(meta)

    @property
    def meta(self) -> dict[str, Any]:
        """The table's ``_meta`` block — upstream revision and generation date."""
        return dict(self._meta)

    @property
    def images(self) -> tuple[BiosImage, ...]:
        """Every row, in upstream's order."""
        return self._images

    @property
    def sizes(self) -> tuple[int, ...]:
        """The file sizes the search accepts, ascending."""
        return self._sizes

    @property
    def openbios(self) -> dict[str, Any]:
        """The signature-recognised replacement BIOS: its bytes and their offset."""
        return dict(self._openbios)

    def accepts_size(self, size: int | None) -> bool:
        """Would the search keep a file of this size? ``None`` (unknown) is not a yes."""
        return size is not None and size in self._sizes

    def identify(self, md5: str) -> BiosImage | None:
        """The row these bytes are, or ``None`` — which is not a verdict on the file.

        DuckStation boots an unrecognised image and says so in a warning
        (``Using an unknown BIOS: {}``), so "not in the table" means unknown,
        never wrong.
        """
        return self._by_md5.get(md5.lower())

    def pick(self, candidates: Sequence[BiosCandidate], region: str) -> BiosPick | None:
        """Which candidate the console of *region* would boot, and what ties with it.

        DuckStation's own three tests, in its own order (bios.cpp:387-395): a
        known image is never displaced by an unknown one, a region match is
        never displaced by a mismatch, and between two known images the lower
        ``priority`` number holds. What upstream does with what is left is the
        part atlas cannot copy — it keeps the **last** equally ranked file the
        directory handed it, so two of them make the answer a property of
        ``readdir`` order. The seam enumerates sorted, so a tie is reported as
        a tie rather than resolved into a claim.
        """
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda c: self._rank(c, region))
        best = self._rank(ranked[0], region)
        tied = tuple(c for c in ranked if self._rank(c, region) == best)
        return BiosPick(chosen=tied[0], tied=tied)

    def _rank(self, candidate: BiosCandidate, region: str) -> tuple[int, int, int]:
        image = candidate.image
        if image is None:
            return (1, 1, 0)
        return (0, 0 if self.matches_region(image, region) else 1, image.priority)

    @staticmethod
    def matches_region(image: BiosImage, region: str) -> bool:
        """``IsValidBIOSForRegion`` (bios.cpp:228-231): ``any`` on either side matches."""
        return region == "any" or image.region == "any" or image.region == region


def _image(entry: Any, index: int) -> BiosImage:
    where = f"duckstation_bios: images[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    missing = {"name", "region", "md5", "priority", "fast_boot_patch"} - set(entry)
    if missing:
        raise ValueError(f"{where}: missing {sorted(missing)}")
    return BiosImage(
        name=str(entry["name"]),
        region=str(entry["region"]),
        md5=str(entry["md5"]).lower(),
        priority=int(entry["priority"]),
        fast_boot_patch=str(entry["fast_boot_patch"]),
    )


def load_bios_table(text: str | None = None) -> BiosTable:
    """Load the packaged table (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("duckstation_bios.json")
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("duckstation_bios: expected an object at the top level")
    images = raw.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("duckstation_bios: images must be a non-empty list")
    sizes = raw.get("sizes")
    if not isinstance(sizes, dict) or not sizes:
        raise ValueError("duckstation_bios: sizes must be a non-empty object")
    rows = tuple(_image(entry, index) for index, entry in enumerate(images))
    # Loudly, like the blocks above: both feed answers (the OpenBIOS offset
    # speaks in a caveat's sentence, the revision in its data), so a table
    # without them would ship "offset None" and an empty pin instead of
    # failing the load.
    openbios = raw.get("openbios")
    if not isinstance(openbios, dict) or {"signature", "offset"} - set(openbios):
        raise ValueError("duckstation_bios: openbios must state signature and offset")
    meta = raw.get("_meta")
    if not isinstance(meta, dict) or "revision" not in meta:
        raise ValueError("duckstation_bios: _meta must state the upstream revision")
    return BiosTable(
        images=rows,
        sizes=tuple(sorted(int(size) for size in sizes.values())),
        openbios=openbios,
        meta=meta,
    )


_TABLE: BiosTable | None = None


def bios_table() -> BiosTable:
    """The packaged table, loaded once."""
    global _TABLE
    if _TABLE is None:
        _TABLE = load_bios_table()
    return _TABLE
