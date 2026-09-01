"""Standalone firmware cards — what a standalone emulator expects beside its content.

A standalone emulator ships no ``.info`` for the firmware route to read, so
which file it probes and where is established from its source at the shipped
release and packaged here — the same split the standalone save cards make
(:mod:`atlas.standalone_saves`): the card states what *can* be, with
citations, and the resolver reads what *is* on this machine. A card names the
XDG base and the emulator-relative path the emulator itself probes — never
the staging spot an arrangement's installer uses, because the emulator's
probe is the only door that matters at run time (Cemu reads its keys at
``GetUserDataPath("keys.txt")`` no matter where an installer parked them).

Cards are keyed by the ``%EMULATOR_…%`` token, like the save cards beside
them, and reach the firmware answer through the catalogue entry's resolved
token — which on EmuDeck is the launcher route's word, variant-gated there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ._data import packaged_text

FIRMWARE_CARDS_SCHEMA = 1

_FILE_BASES = ("config", "data")
_FILE_NEEDS = ("required", "optional")


@dataclass(frozen=True, slots=True)
class StandaloneFirmwareFile:
    """One file a standalone emulator probes: the XDG base, the path below it, the claim."""

    name: str
    base: str
    subdir: str
    need: str
    purpose: str
    citation: str


@dataclass(frozen=True, slots=True)
class StandaloneFirmwareConfigFile:
    """One file whose whole path is a configuration value the emulator reads.

    The other shape a probe can take (melonDS's ``[DS] BIOS9Path`` names the
    file, wherever the user pointed it): the card states the config key and
    the claim, and the resolver registered for the token reads the value the
    way the emulator does — including which keys the current switches make
    the launch probe at all, which is code knowledge, never a card DSL.
    """

    key: str
    purpose: str
    citation: str


@dataclass(frozen=True, slots=True)
class StandaloneFirmwareSearch:
    """The third shape: no file is named at all, a directory is searched.

    DuckStation is the case it exists for. One setting names the directory,
    three more may name an image inside it per console region, and where they
    are empty — the shipped state — the emulator keeps every file of an
    accepted size and recognises it by its bytes. So the card can state where
    to look and which keys speak, and nothing about *which file*: that is a
    property of content, answered by the packaged recognition table beside the
    resolver.
    """

    directory_key: str
    directory_default: str
    region_keys: tuple[tuple[str, str], ...]
    purpose: str
    citation: str


@dataclass(frozen=True, slots=True)
class StandaloneFirmwareCard:
    """One standalone emulator's firmware expectations, with the systems they answer for.

    Exactly one of ``files``, ``config_files`` and ``search`` is populated: a
    card names fixed probe paths, or the config keys whose values are the
    paths, or the directory a search runs in — and a card of either of the
    latter two without a resolver registered in :mod:`atlas.firmware` fails
    loudly there, the way a save card without one does.
    """

    token: str
    systems: tuple[str, ...]
    files: tuple[StandaloneFirmwareFile, ...]
    config_files: tuple[StandaloneFirmwareConfigFile, ...]
    search: StandaloneFirmwareSearch | None
    provenance: str


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _file(token: str, index: int, entry: Any) -> StandaloneFirmwareFile:
    where = f"standalone firmware card {token!r}: files[{index}]"
    if not isinstance(entry, dict) or set(entry) != {
        "name",
        "base",
        "subdir",
        "need",
        "purpose",
        "citation",
    }:
        raise ValueError(
            f"{where}: expected exactly name/base/subdir/need/purpose/citation, got {entry!r}"
        )
    base = _expect_str(entry["base"], f"{where}.base")
    if base not in _FILE_BASES:
        raise ValueError(f"{where}.base must be one of {_FILE_BASES}, got {base!r}")
    need = _expect_str(entry["need"], f"{where}.need")
    if need not in _FILE_NEEDS:
        raise ValueError(f"{where}.need must be one of {_FILE_NEEDS}, got {need!r}")
    return StandaloneFirmwareFile(
        name=_expect_str(entry["name"], f"{where}.name"),
        base=base,
        subdir=_expect_str(entry["subdir"], f"{where}.subdir"),
        need=need,
        purpose=_expect_str(entry["purpose"], f"{where}.purpose"),
        citation=_expect_str(entry["citation"], f"{where}.citation"),
    )


def _config_file(token: str, index: int, entry: Any) -> StandaloneFirmwareConfigFile:
    where = f"standalone firmware card {token!r}: config_files[{index}]"
    if not isinstance(entry, dict) or set(entry) != {"key", "purpose", "citation"}:
        raise ValueError(f"{where}: expected exactly key/purpose/citation, got {entry!r}")
    return StandaloneFirmwareConfigFile(
        key=_expect_str(entry["key"], f"{where}.key"),
        purpose=_expect_str(entry["purpose"], f"{where}.purpose"),
        citation=_expect_str(entry["citation"], f"{where}.citation"),
    )


def _search(token: str, entry: Any) -> StandaloneFirmwareSearch:
    where = f"standalone firmware card {token!r}: search"
    if not isinstance(entry, dict) or set(entry) != {
        "directory_key",
        "directory_default",
        "region_keys",
        "purpose",
        "citation",
    }:
        raise ValueError(
            f"{where}: expected exactly directory_key/directory_default/region_keys/purpose/"
            f"citation, got {entry!r}"
        )
    region_keys = entry["region_keys"]
    if not isinstance(region_keys, list) or not region_keys:
        raise ValueError(f"{where}.region_keys must be a non-empty list, got {region_keys!r}")
    pairs = []
    for index, pair in enumerate(region_keys):
        at = f"{where}.region_keys[{index}]"
        if not isinstance(pair, dict) or set(pair) != {"region", "key"}:
            raise ValueError(f"{at}: expected exactly region/key, got {pair!r}")
        pairs.append(
            (_expect_str(pair["region"], f"{at}.region"), _expect_str(pair["key"], f"{at}.key"))
        )
    return StandaloneFirmwareSearch(
        directory_key=_expect_str(entry["directory_key"], f"{where}.directory_key"),
        directory_default=_expect_str(entry["directory_default"], f"{where}.directory_default"),
        region_keys=tuple(pairs),
        purpose=_expect_str(entry["purpose"], f"{where}.purpose"),
        citation=_expect_str(entry["citation"], f"{where}.citation"),
    )


def _card(token: str, entry: Any) -> StandaloneFirmwareCard:
    where = f"standalone firmware card {token!r}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {entry!r}")
    systems = entry.get("systems")
    if not isinstance(systems, list) or not systems:
        raise ValueError(f"{where}: systems must be a non-empty list, got {systems!r}")
    files = entry.get("files")
    config_files = entry.get("config_files")
    search = entry.get("search")
    stated = [shape for shape in (files, config_files, search) if shape is not None]
    if len(stated) != 1:
        raise ValueError(
            f"{where}: exactly one of 'files', 'config_files' and 'search' must be given"
        )
    if files is not None and (not isinstance(files, list) or not files):
        raise ValueError(f"{where}: files must be a non-empty list, got {files!r}")
    if config_files is not None and (not isinstance(config_files, list) or not config_files):
        raise ValueError(f"{where}: config_files must be a non-empty list, got {config_files!r}")
    provenance = entry.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError(f"{where}: expected a 'provenance' object, got {provenance!r}")
    return StandaloneFirmwareCard(
        token=token,
        systems=tuple(_expect_str(s, f"{where}: systems[]") for s in systems),
        files=tuple(_file(token, i, f) for i, f in enumerate(files or [])),
        config_files=tuple(_config_file(token, i, f) for i, f in enumerate(config_files or [])),
        search=None if search is None else _search(token, search),
        provenance=_expect_str(provenance.get("source"), f"{where}: provenance.source"),
    )


def load_standalone_firmware(text: str | None = None) -> tuple[StandaloneFirmwareCard, ...]:
    """Load the packaged standalone firmware cards (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("standalone_firmware.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != FIRMWARE_CARDS_SCHEMA:
        raise ValueError(
            f"standalone_firmware: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {FIRMWARE_CARDS_SCHEMA})"
        )
    return tuple(_card(token, entry) for token, entry in raw.get("emulators", {}).items())


_PACKAGED: tuple[StandaloneFirmwareCard, ...] | None = None


def lookup_standalone_firmware_card(token: str | None) -> StandaloneFirmwareCard | None:
    """The packaged card for one emulator token, or ``None`` — no fuzzy matching."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_standalone_firmware()
    if token is None:
        return None
    return next((card for card in _PACKAGED if card.token == token), None)
