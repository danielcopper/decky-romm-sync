"""Probe a libretro core — run as a subprocess.

Invoked as ``python -m atlas._core_probe <core.so>`` by
:meth:`atlas.machine.RealMachine.query_core`. The subprocess *is* the crash
isolation: a core that segfaults takes this process down, not the host.

The probe emits one JSON object per line and the parent uses the LAST valid
line — whatever reached it before the process stopped, however it stopped — a
deliberate two-phase design:

1. ``retro_get_system_info`` (safe on every core) → the base line with
   ``library_name`` / ``library_version`` / ``valid_extensions``.
2. ``retro_set_environment`` with a capturing environment callback → an
   enriched line adding ``options``: the option definitions the core
   *registers* (key, default, values), in every format the API knows
   (``SET_VARIABLES``, ``SET_CORE_OPTIONS``/``_INTL``, v2, v2 ``_INTL``;
   command numbers and struct layouts per libretro.h @ RetroArch a79435a).

Phase 2 is how the LRPS2 generation question becomes observable: which option
keys a core registers identifies its generation better than any version
string — and the registered defaults turn card defaults into live reads. A
core that crashes in phase 2, or registers its options only later (e.g. in
``retro_init``), simply yields no ``options`` — the caller treats that as
*unknown*, never as "registers nothing".

This is the same read RetroArch performs when it loads a core — a live read of
the binary on disk, not a lookup.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from typing import Any, Callable

RETRO_ENVIRONMENT_EXPERIMENTAL = 0x10000
RETRO_ENVIRONMENT_SET_VARIABLES = 16
RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION = 52
RETRO_ENVIRONMENT_SET_CORE_OPTIONS = 53
RETRO_ENVIRONMENT_SET_CORE_OPTIONS_INTL = 54
RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2 = 67
RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2_INTL = 68

RETRO_NUM_CORE_OPTION_VALUES_MAX = 128
_MAX_DEFINITIONS = 4096  # defensive walk limit over NULL-terminated arrays


class _RetroSystemInfo(ctypes.Structure):
    _fields_ = [
        ("library_name", ctypes.c_char_p),
        ("library_version", ctypes.c_char_p),
        ("valid_extensions", ctypes.c_char_p),
        ("need_fullpath", ctypes.c_bool),
        ("block_extract", ctypes.c_bool),
    ]


class _RetroVariable(ctypes.Structure):
    _fields_ = [("key", ctypes.c_char_p), ("value", ctypes.c_char_p)]


class _RetroCoreOptionValue(ctypes.Structure):
    _fields_ = [("value", ctypes.c_char_p), ("label", ctypes.c_char_p)]


class _RetroCoreOptionDefinition(ctypes.Structure):  # v1
    _fields_ = [
        ("key", ctypes.c_char_p),
        ("desc", ctypes.c_char_p),
        ("info", ctypes.c_char_p),
        ("values", _RetroCoreOptionValue * RETRO_NUM_CORE_OPTION_VALUES_MAX),
        ("default_value", ctypes.c_char_p),
    ]


class _RetroCoreOptionsIntl(ctypes.Structure):
    _fields_ = [
        ("us", ctypes.POINTER(_RetroCoreOptionDefinition)),
        ("local", ctypes.POINTER(_RetroCoreOptionDefinition)),
    ]


class _RetroCoreOptionV2Definition(ctypes.Structure):
    _fields_ = [
        ("key", ctypes.c_char_p),
        ("desc", ctypes.c_char_p),
        ("desc_categorized", ctypes.c_char_p),
        ("info", ctypes.c_char_p),
        ("info_categorized", ctypes.c_char_p),
        ("category_key", ctypes.c_char_p),
        ("values", _RetroCoreOptionValue * RETRO_NUM_CORE_OPTION_VALUES_MAX),
        ("default_value", ctypes.c_char_p),
    ]


class _RetroCoreOptionsV2(ctypes.Structure):
    _fields_ = [
        ("categories", ctypes.c_void_p),
        ("definitions", ctypes.POINTER(_RetroCoreOptionV2Definition)),
    ]


class _RetroCoreOptionsV2Intl(ctypes.Structure):
    _fields_ = [
        ("us", ctypes.POINTER(_RetroCoreOptionsV2)),
        ("local", ctypes.POINTER(_RetroCoreOptionsV2)),
    ]


_ENV_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)


def _decode(value: bytes | None) -> str | None:
    return value.decode("utf-8", "replace") if value else None


def _option_values(values: "ctypes.Array[_RetroCoreOptionValue]") -> list[str]:
    out: list[str] = []
    for entry in values:
        if not entry.value:
            break
        out.append(entry.value.decode("utf-8", "replace"))
    return out


class _OptionCapture:
    """Collects option definitions from whichever SET call the core makes.

    RetroArch applies the *last* registration when a core sends several
    formats; walking every call and letting later ones overwrite earlier keys
    mirrors that. ``seen`` stays False until any registration arrives — the
    difference between "registers nothing" and "did not register here".
    """

    def __init__(self) -> None:
        self.options: dict[str, dict[str, object]] = {}
        self.seen = False
        # One entry per registration call the API knows; the command number
        # picks the reader, and a call arriving without data is answered
        # (True) without reading anything.
        self._readers: dict[int, Callable[[int], None]] = {
            RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION: self._answer_options_version,
            RETRO_ENVIRONMENT_SET_VARIABLES: self._read_variables,
            RETRO_ENVIRONMENT_SET_CORE_OPTIONS: self._read_v1,
            RETRO_ENVIRONMENT_SET_CORE_OPTIONS_INTL: self._read_v1_intl,
            RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2: self._read_v2,
            RETRO_ENVIRONMENT_SET_CORE_OPTIONS_V2_INTL: self._read_v2_intl,
        }

    def _add(self, key: bytes | None, default: bytes | None, values: list[str]) -> None:
        decoded = _decode(key)
        if decoded:
            self.options[decoded] = {"default": _decode(default), "values": values}

    def handle(self, cmd: int, data: int | None) -> bool:
        reader = self._readers.get(cmd & ~RETRO_ENVIRONMENT_EXPERIMENTAL)
        if reader is None:
            # Everything else is honestly unsupported; cores handle a false return.
            return False
        if data:
            reader(data)
        return True

    def _answer_options_version(self, data: int) -> None:
        ctypes.cast(data, ctypes.POINTER(ctypes.c_uint))[0] = 2

    def _read_variables(self, data: int) -> None:
        self.seen = True
        variables = ctypes.cast(data, ctypes.POINTER(_RetroVariable))
        for i in range(_MAX_DEFINITIONS):
            var = variables[i]
            if not var.key:
                break
            # "Description; default|second|third" — the first listed
            # value is the default (libretro.h, SET_VARIABLES).
            raw = _decode(var.value) or ""
            _, _, value_part = raw.partition("; ")
            values = value_part.split("|") if value_part else []
            self._add(var.key, values[0].encode() if values else None, values)

    def _read_v1(self, data: int) -> None:
        self._walk_v1(ctypes.cast(data, ctypes.POINTER(_RetroCoreOptionDefinition)))

    def _read_v1_intl(self, data: int) -> None:
        intl = ctypes.cast(data, ctypes.POINTER(_RetroCoreOptionsIntl))[0]
        if intl.us:
            self._walk_v1(intl.us)

    def _read_v2(self, data: int) -> None:
        self._walk_v2(ctypes.cast(data, ctypes.POINTER(_RetroCoreOptionsV2))[0])

    def _read_v2_intl(self, data: int) -> None:
        intl = ctypes.cast(data, ctypes.POINTER(_RetroCoreOptionsV2Intl))[0]
        if intl.us:
            self._walk_v2(intl.us[0])

    def _walk_v1(self, definitions: Any) -> None:
        self.seen = True
        for i in range(_MAX_DEFINITIONS):
            definition = definitions[i]
            if not definition.key:
                break
            self._add(definition.key, definition.default_value, _option_values(definition.values))

    def _walk_v2(self, options: _RetroCoreOptionsV2) -> None:
        self.seen = True
        if not options.definitions:
            return
        for i in range(_MAX_DEFINITIONS):
            definition = options.definitions[i]
            if not definition.key:
                break
            self._add(definition.key, definition.default_value, _option_values(definition.values))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m atlas._core_probe <core.so>", file=sys.stderr)
        return 2
    so_path = argv[1]
    try:
        lib = ctypes.CDLL(so_path, mode=os.RTLD_LAZY)
        info = _RetroSystemInfo()
        lib.retro_get_system_info(ctypes.byref(info))
    except OSError as exc:
        print(f"cannot load core: {exc}", file=sys.stderr)
        return 1
    name = _decode(info.library_name)
    if not name:
        print("core reported no library_name", file=sys.stderr)
        return 1
    base = {
        "library_name": name,
        "library_version": _decode(info.library_version),
        "valid_extensions": _decode(info.valid_extensions),
        # bool(...) because ctypes hands back a c_bool whose truthiness is the
        # fact; RetroArch reads the same struct field to decide whether an
        # archive is passed through raw (task_content.c:742).
        "block_extract": bool(info.block_extract),
    }
    # Phase 1: the base line survives even if phase 2 crashes the process.
    print(json.dumps(base), flush=True)

    capture = _OptionCapture()
    callback = _ENV_CALLBACK(lambda cmd, data: capture.handle(cmd, data))
    try:
        lib.retro_set_environment(callback)
    except (OSError, AttributeError):
        return 0
    if capture.seen:
        print(json.dumps({**base, "options": capture.options}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
