"""Install-first formats — the launchability question's world knowledge (issue #36).

A launchability 'no' has more than one meaning, and the accept-list can only
state one of them: the frontend will not scan this file. For some files the
honest continuation is "because it is an installer" — a PSN ``.pkg`` is the
distribution form of the content itself, and the emulator has to install it
before anything can launch. That fact is written nowhere on the machine, so
it lives here: marked, versioned, cited, keyed by the atlas system id and the
exact extension token ES-DE would derive. The resolver consults it only where
the extension is already outside the machine's own accept-list — a read is
never overridden by a table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ._data import packaged_text

FORMATS_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class InstallFirstFormat:
    """One format that needs an installation step before anything can launch."""

    system: str
    extension: str
    statement: str
    source: str


@dataclass(frozen=True, slots=True)
class StandaloneLaunchCard:
    """What one standalone emulator's own loader reads (issue #66).

    ``accepts`` are extension tokens in their dotted lowercase form, matched
    case-insensitively — the gate here is the emulator's loader, not ES-DE's
    case-exact scan. ``archives`` says whether the loader opens archive
    containers at all: a standalone gets the file itself, with no RetroArch
    in front of it to pick a matching entry out of a zip.
    """

    token: str
    accepts: tuple[str, ...]
    archives: bool
    source: str

    def takes(self, extension: str) -> bool:
        return extension.lower() in self.accepts


def _expect_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty string, got {value!r}")
    return value


def load_launch_formats(text: str | None = None) -> tuple[InstallFirstFormat, ...]:
    """Load the packaged install-first formats (or *text* when supplied, for tests)."""
    if text is None:
        text = _packaged_text()
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != FORMATS_SCHEMA:
        raise ValueError(
            f"launch_formats: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {FORMATS_SCHEMA})"
        )
    systems = raw.get("systems", {})
    if not isinstance(systems, dict):
        raise ValueError(f"launch_formats: systems must be an object, got {systems!r}")
    formats: list[InstallFirstFormat] = []
    for system, entries in systems.items():
        where = f"launch format system {system!r}"
        if not isinstance(entries, dict) or not entries:
            raise ValueError(f"{where}: expected a non-empty object, got {entries!r}")
        for extension, entry in entries.items():
            formats.append(_format(system, extension, entry, f"{where}: {extension!r}"))
    return tuple(formats)


def _format(system: str, extension: str, entry: Any, at: str) -> InstallFirstFormat:
    """One record — validated, never coerced."""
    if not extension.startswith(".") or extension == ".":
        # The key must be a token esde_extension() can ever answer: every
        # derived extension starts with the dot it was cut at, and the
        # bare-dot sentinel names "no extension", which is not a format.
        raise ValueError(f"{at}: an extension token starts with '.' and names one")
    if not isinstance(entry, dict) or set(entry) != {"statement", "source"}:
        raise ValueError(f"{at}: an entry names exactly 'statement' and 'source', got {entry!r}")
    return InstallFirstFormat(
        system=system,
        extension=extension,
        statement=_expect_str(entry["statement"], f"{at}: statement"),
        source=_expect_str(entry["source"], f"{at}: source"),
    )


def load_standalone_launch(text: str | None = None) -> tuple[StandaloneLaunchCard, ...]:
    """Load the packaged standalone launch cards (or *text* when supplied, for tests)."""
    if text is None:
        text = _packaged_text()
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != FORMATS_SCHEMA:
        raise ValueError(
            f"launch_formats: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {FORMATS_SCHEMA})"
        )
    emulators = raw.get("emulators", {})
    if not isinstance(emulators, dict):
        raise ValueError(f"launch_formats: emulators must be an object, got {emulators!r}")
    return tuple(_launch_card(token, entry) for token, entry in emulators.items())


def _launch_card(token: str, entry: Any) -> StandaloneLaunchCard:
    where = f"standalone launch card {token!r}"
    if not isinstance(entry, dict) or set(entry) != {"accepts", "archives", "source"}:
        raise ValueError(f"{where}: a card names exactly 'accepts', 'archives' and 'source'")
    accepts = entry["accepts"]
    if not isinstance(accepts, list) or not accepts:
        raise ValueError(f"{where}: accepts must be a non-empty list")
    for token_ext in accepts:
        if not isinstance(token_ext, str) or not token_ext.startswith(".") or token_ext == ".":
            raise ValueError(f"{where}: accepts[{token_ext!r}] — an extension token starts with '.' and names one")
        if token_ext != token_ext.lower():
            # The match is case-insensitive by lowercasing the file's token, so
            # a card token carrying upper case could never be hit.
            raise ValueError(f"{where}: accepts[{token_ext!r}] must be recorded lowercase")
    if not isinstance(entry["archives"], bool):
        raise ValueError(f"{where}: archives must be a JSON boolean")
    return StandaloneLaunchCard(
        token=token,
        accepts=tuple(accepts),
        archives=entry["archives"],
        source=_expect_str(entry["source"], f"{where}: source"),
    )


_PACKAGED: tuple[InstallFirstFormat, ...] | None = None
_PACKAGED_CARDS: tuple[StandaloneLaunchCard, ...] | None = None


def _packaged_text() -> str:
    return packaged_text("launch_formats.json")


def lookup_install_first(system: str, extension: str) -> InstallFirstFormat | None:
    """The packaged record for one (system, extension) — exact match, or ``None``."""
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_launch_formats()
    return next(
        (f for f in _PACKAGED if f.system == system and f.extension == extension), None
    )


def lookup_standalone_launch(token: str | None) -> StandaloneLaunchCard | None:
    """The packaged card for one emulator token, or ``None`` — no fuzzy matching."""
    global _PACKAGED_CARDS
    if _PACKAGED_CARDS is None:
        _PACKAGED_CARDS = load_standalone_launch()
    if token is None:
        return None
    return next((card for card in _PACKAGED_CARDS if card.token == token), None)
