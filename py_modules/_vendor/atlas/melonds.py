"""melonDS's configuration, read the way the emulator reads it.

One module for the read chain both questions share — the save placement and
the firmware expectations both hang off ``melonDS.toml``, and the emulator
reads that file one way (``Config::Load``, Config.cpp:785-803 at 1.1): the
TOML under ``<config home>/melonDS`` wherever it exists — even unparseable,
because the emulator catches the syntax error and runs on factory defaults
rather than falling back — and the pre-1.0 ``melonDS.ini`` beside it, line by
line, only where no TOML exists (the built-in migration, :682-775). A
``portable`` directory beside the executable would outrank the config home
(pathInit, main.cpp:180-214), and neither arrangement has one.

The accessors mirror melonDS's own ``Table`` reads (Config.cpp:555-598): a
value of the wrong type falls to the compiled default — the empty string,
``false``, ``0`` for everything this module reads, none of which appear in
the emulator's default lists — and ``Emu.ConsoleType`` is clamped to its
declared range (:562-568, IntRanges :79).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from typing import Any, Mapping

from . import emulator_settings
from .machine import Machine, READ_MISSING, READ_OK

CONFIG_DIRNAME = "melonDS"
# The card token this emulator answers under, and the key its settings files
# are addressed by in atlas/data/emulator_settings.json.
TOKEN = "MELONDS"
CONFIG_FILENAME = "melonDS.toml"
LEGACY_FILENAME = "melonDS.ini"

# Which branch of Config::Load produced the document — stated on the read so
# a resolver's provenance can say which file spoke without re-deriving it.
SOURCE_TOML = "toml"
SOURCE_TOML_INVALID = "toml-invalid"
SOURCE_LEGACY = "legacy"
SOURCE_DEFAULTS = "defaults"

# The TOML paths this module reads, named once each so the legacy key table
# and the probe sets below cannot spell one of them differently.
KEY_SAVE_FILE_PATH = "Instance0.SaveFilePath"
KEY_SAVESTATE_PATH = "Instance0.SavestatePath"
KEY_EXTERNAL_BIOS = "Emu.ExternalBIOSEnable"
KEY_CONSOLE_TYPE = "Emu.ConsoleType"
KEY_DS_BIOS9 = "DS.BIOS9Path"
KEY_DS_BIOS7 = "DS.BIOS7Path"
KEY_DS_FIRMWARE = "DS.FirmwarePath"
KEY_DSI_BIOS9 = "DSi.BIOS9Path"
KEY_DSI_BIOS7 = "DSi.BIOS7Path"
KEY_DSI_FIRMWARE = "DSi.FirmwarePath"
KEY_DSI_NAND = "DSi.NANDPath"

# The slice of melonDS's legacy-key table this module reads (``LegacyFile``,
# Config.cpp at 1.1): legacy name -> (TOML path, type), the type as the table
# spells it (0 int, 1 bool, 2 string; LoadLegacyFile :747-767 converts with
# strtol). ``SaveFilePath`` and its sibling ``SavestatePath`` are
# instance-unique and land in ``Instance0`` when read from the base file
# (:301-302, :688-700); the rest are global.
_LEGACY_KEYS: Mapping[str, tuple[str, int]] = {
    "SaveFilePath": (KEY_SAVE_FILE_PATH, 2),  # :301
    "SavestatePath": (KEY_SAVESTATE_PATH, 2),  # :302
    "ExternalBIOSEnable": (KEY_EXTERNAL_BIOS, 1),  # :237
    "ConsoleType": (KEY_CONSOLE_TYPE, 0),  # :226
    "BIOS9Path": (KEY_DS_BIOS9, 2),  # :239
    "BIOS7Path": (KEY_DS_BIOS7, 2),  # :240
    "FirmwarePath": (KEY_DS_FIRMWARE, 2),  # :241
    "DSiBIOS9Path": (KEY_DSI_BIOS9, 2),  # :243
    "DSiBIOS7Path": (KEY_DSI_BIOS7, 2),  # :244
    "DSiFirmwarePath": (KEY_DSI_FIRMWARE, 2),  # :245
    "DSiNANDPath": (KEY_DSI_NAND, 2),  # :246
}

# The one range that matters here (IntRanges, Config.cpp:79): a console type
# outside {0, 1} reads as the nearest bound, never as a refusal.
_CONSOLE_TYPE_RANGE = (0, 1)


@dataclass(frozen=True, slots=True)
class MelonConfig:
    """The document ``Config::Load`` produced, and where it came from.

    ``document`` is TOML-shaped whichever file spoke — a legacy read is
    mapped through the emulator's own key table first, so one set of
    accessors serves both. ``stated_file`` is the file that was read
    (``None`` where neither exists), ``source`` the branch of ``Load()``
    that produced the document.
    """

    document: Mapping[str, Any]
    stated_file: str | None
    source: str


@dataclass(frozen=True, slots=True)
class MelonConfigRead:
    """One config read: the document, or the path that could not be read."""

    config: MelonConfig | None
    unreadable: str | None


def _settings_path(name: str, config_home: str, flatpak: str | None) -> str:
    """Where this launch opens one of melonDS's two settings files.

    The address is the settings table's, not this module's: the save answer,
    the BIOS answer and this reader all open the same file, and a copy per
    reader is how two of them came to disagree about another emulator's.

    Every caller of this module holds one home, the config one, because that
    is where melonDS keeps both its files. Passing it as the data home too
    would answer the config tree for a file the table moved to the data one —
    silently, and only for melonDS — so the base the table states is checked
    rather than assumed.
    """
    settings = emulator_settings.settings_file(TOKEN, name)
    if settings.bases != ("config",):
        raise ValueError(
            f"the settings table states bases {settings.bases} for melonDS's {name!r} and this "
            "reader is given only the config home — the table and the code shipped out of step"
        )
    return settings.only(config_home=config_home, data_home=config_home, flatpak=flatpak)


def config_dir(config_home: str, flatpak: str | None) -> str:
    """``emuDirectory`` for a non-portable launch — the directory the TOML sits in.

    *flatpak* has no default, for the reason
    :func:`atlas.emulator_settings.user_directory` has none: a route that
    simply forgot it would answer the host tree's address and look right on
    every emulator whose directory does not vary by installation, which is
    every one of them until the first that does.
    """
    return os.path.dirname(_settings_path(CONFIG_FILENAME, config_home, flatpak))


def _strtol(raw: str) -> int:
    """``strtol(raw, nullptr, 10)`` — sign, leading digits, 0 where none."""
    text = raw.lstrip(" \t")
    sign = 1
    if text[:1] in ("+", "-"):
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    digits = ""
    for ch in text:
        if ch not in "0123456789":
            break
        digits += ch
    return sign * int(digits) if digits else 0


def _legacy_scan(text: str) -> dict[str, str]:
    """Every recognized legacy line's raw value, a later line overwriting.

    One scan per line the way ``LoadLegacyFile`` scans (Config.cpp:705-717):
    the key, an immediate ``=``, then everything up to a tab or the line's
    end — at least one byte of it, because an empty value fails the scan and
    leaves the entry untouched.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition("=")
        if not separator or name not in _LEGACY_KEYS:
            continue
        candidate = rest.partition("\t")[0]
        if candidate:
            values[name] = candidate
    return values


def _legacy_document(text: str) -> dict[str, Any]:
    """The legacy INI as the TOML-shaped tree ``LoadLegacyFile`` builds."""
    document: dict[str, Any] = {}
    for name, raw in _legacy_scan(text).items():
        path, kind = _LEGACY_KEYS[name]
        value: Any = raw
        if kind == 0:
            value = _strtol(raw)
        elif kind == 1:
            value = _strtol(raw) != 0
        node = document
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return document


def read_config(machine: Machine, config_home: str, flatpak: str | None) -> MelonConfigRead:
    """``Config::Load``, performed as reads — the TOML, the legacy INI, or defaults."""
    toml_path = _settings_path(CONFIG_FILENAME, config_home, flatpak)
    result = machine.read_text(toml_path)
    if result.status not in (READ_OK, READ_MISSING):
        return MelonConfigRead(config=None, unreadable=toml_path)
    if result.status == READ_OK:
        try:
            document: Mapping[str, Any] = tomllib.loads(result.text or "")
        except tomllib.TOMLDecodeError:
            return MelonConfigRead(
                config=MelonConfig({}, toml_path, SOURCE_TOML_INVALID), unreadable=None
            )
        return MelonConfigRead(
            config=MelonConfig(document, toml_path, SOURCE_TOML), unreadable=None
        )
    ini_path = _settings_path(LEGACY_FILENAME, config_home, flatpak)
    legacy = machine.read_text(ini_path)
    if legacy.status not in (READ_OK, READ_MISSING):
        return MelonConfigRead(config=None, unreadable=ini_path)
    if legacy.status == READ_OK:
        return MelonConfigRead(
            config=MelonConfig(_legacy_document(legacy.text or ""), ini_path, SOURCE_LEGACY),
            unreadable=None,
        )
    return MelonConfigRead(config=MelonConfig({}, None, SOURCE_DEFAULTS), unreadable=None)


def _resolve(document: Mapping[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def raw_value(config: MelonConfig, path: str) -> Any:
    """The value at *path* as the document holds it — for provenance, not reads."""
    return _resolve(config.document, path)


def get_string(config: MelonConfig, path: str) -> str:
    """``Table::GetString`` — a non-string value reads as the empty default."""
    value = _resolve(config.document, path)
    return value if isinstance(value, str) else ""


def get_bool(config: MelonConfig, path: str) -> bool:
    """``Table::GetBool`` — a non-boolean value reads as ``false``."""
    value = _resolve(config.document, path)
    return value if isinstance(value, bool) else False


def console_type(config: MelonConfig) -> int:
    """``Emu.ConsoleType`` the way ``GetInt`` reads it: default 0, clamped to {0, 1}.

    ``bool`` is excluded the way toml11 keeps integers and booleans apart —
    a TOML ``true`` is not an integer, so it falls to the default.
    """
    value = _resolve(config.document, "Emu.ConsoleType")
    if not isinstance(value, int) or isinstance(value, bool):
        value = 0
    low, high = _CONSOLE_TYPE_RANGE
    return min(max(value, low), high)


# The subsets of path keys ``verifySetup`` probes, in the emulator's own order
# (EmuInstance.cpp:633-667 at 1.1): the DS BIOS pair whenever the external-BIOS
# switch is on; then in DSi mode the DSi BIOS pair, the DSi firmware (external
# BIOS only) and the NAND; and in DS mode the DS firmware (external BIOS only).
# What is not probed is not required, and an answer says so by leaving it out.
_DS_BIOS_KEYS = (KEY_DS_BIOS9, KEY_DS_BIOS7)
_DSI_KEYS_EXTBIOS = (KEY_DSI_BIOS9, KEY_DSI_BIOS7, KEY_DSI_FIRMWARE, KEY_DSI_NAND)
_DSI_KEYS_BUILTIN = (KEY_DSI_BIOS9, KEY_DSI_BIOS7, KEY_DSI_NAND)


def probed_firmware_keys(extbios: bool, console: int) -> tuple[str, ...]:
    """``verifySetup``'s probe set for the two switches, in its own order.

    The emulator's gating, not the firmware model's: which files a launch
    really opens is decided here, and the model states requirements for
    exactly those.
    """
    keys: tuple[str, ...] = _DS_BIOS_KEYS if extbios else ()
    if console == 1:
        return keys + (_DSI_KEYS_EXTBIOS if extbios else _DSI_KEYS_BUILTIN)
    if extbios:
        return keys + (KEY_DS_FIRMWARE,)
    return keys


def local_file_path(config_home: str, value: str, flatpak: str | None) -> str:
    """``Platform::GetLocalFilePath`` — absolute stays, anything else joins the config dir.

    Platform.cpp:157-172 at 1.1: a relative spelling (the empty string
    included) is opened below ``emuDirectory``, which for a non-portable
    launch is the melonDS config directory. This is the door the BIOS and
    firmware paths go through (``OpenLocalFile``, :176-178) — unlike the
    save path, which is opened verbatim by the process and therefore
    anchors at the launch's working directory.
    """
    if os.path.isabs(value):
        return value
    directory = config_dir(config_home, flatpak)
    return os.path.join(directory, value) if value else directory
