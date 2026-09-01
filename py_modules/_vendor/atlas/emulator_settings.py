"""Where a standalone emulator keeps its own things — stated once, read by every route.

Four questions read one emulator's settings file, and until this table each of
them carried the address: a save card, a texture card and a mod card between
them named one path up to three times, with a fourth copy as a constant in the
firmware resolver. Two of those statements had already drifted in shipped
releases — one route reading the config home while another read the data home
for the same file (#250, #256) — and nothing could notice, because nothing
said the two were the same file.

So a card names a file by **name** and the address lives here. What is here is
only *where*: the emulator's own directory, the bases a launch may pick, in the
order it picks them, and the path below. What the file means for a question
stays with the card and the code that reads it — which keys govern a save tree,
whether a switch exists, how a legacy file is migrated.

The **directory** is stated once per emulator rather than once per path,
because it is one fact: Dolphin's ``Dolphin.ini``, its ``Load/Textures`` tree
and its ``GC`` cards all hang off the directory Dolphin itself calls the user
directory, and spelling it into every path is how the same name comes to be
written down four times. For one emulator it is not even a property of the
emulator: PrimeHack renamed its user directory and later renamed it back, so
which name a launch uses depends on which build it runs, and the installation
that carries the other build states its own name here beside the default
(#246).

The **app id** an arrangement installs the emulator as lives here for the same
reason, one step earlier: it is a property of the *installation*, not of any
one question. It used to sit on the save card, so ``~/.var/app/<id>`` could
only be reached by an emulator that had one — and MAME, whose savestate card
is complete and whose save card does not exist, refused a question it could
have answered (#288). The rows the directory names are keyed by that very id
already, so this is where it belongs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._data import packaged_text
from .textures import expect_table_anchors, path_segments

EMULATOR_SETTINGS_SCHEMA = 3

_BASES = ("config", "data")


@dataclass(frozen=True, slots=True)
class DirectoryName:
    """One spelling of an emulator's own directory, and the evidence for it.

    ``anchors`` pins the name to bytes in the binary that spells it, the same
    tripwire the texture and mod rows carry (#105): the name is a compiled-in
    constant, so a build that renamed it stops carrying the literal and the
    tripwire says so instead of the answer quietly moving to a directory
    nothing writes to. ``binary`` is a path below RetroDECK's components tree,
    or below the named ``flatpak``'s own files where the build lives in an app
    of its own.
    """

    name: str
    citation: str
    anchors: Mapping[str, Any]

    @property
    def binary(self) -> str:
        """Where the binary that spells this name sits, relative to its tree."""
        return str(self.anchors["binary"])

    @property
    def flatpak(self) -> str | None:
        """The app whose files that path is below, or ``None`` for a component."""
        stated = self.anchors.get("flatpak")
        return str(stated) if stated is not None else None

    @property
    def literals(self) -> tuple[tuple[str, str, str], ...]:
        """``(segment, literal, encoding)`` for every anchored segment."""
        return tuple(
            (segment, anchor["literal"], anchor.get("encoding", "utf-8"))
            for segment, anchor in self.anchors["names"].items()
            if "literal" in anchor
        )


@dataclass(frozen=True, slots=True)
class InstalledFlatpak:
    """Which flatpak app an arrangement installs this emulator as, with its evidence.

    ``app_id`` is the id whose per-app XDG trees below ``~/.var/app`` a launch
    of that install reads. It is ``None`` where no arrangement is established
    to install the emulator as a flatpak at all — EmuDeck fetches Cemu,
    DuckStation and PCSX2 as AppImages — and that ``None`` is a **stated** no,
    with the same citation requirement as a stated id: an emulator whose
    installation nobody looked at and one that demonstrably installs no flatpak
    must not read the same.

    A launch that runs some *other*, hand-installed flatpak of the emulator is
    not this fact: EmuDeck's launcher would run any installed id carrying the
    emulator's name, and which id that is has been established only for the
    apps EmuDeck installs itself.
    """

    app_id: str | None
    citation: str


@dataclass(frozen=True, slots=True)
class EmulatorDirectory:
    """The emulator's own directory below an XDG base — one name, or one per build.

    ``installations`` is keyed by the flatpak app id whose build spells the
    directory differently, and is empty for every emulator whose name is the
    same wherever it is installed. It exists for the case where the name
    belongs to the build rather than to the emulator: PrimeHack's user
    directory is ``primehack`` in the revision RetroDECK ships and
    ``dolphin-emu`` in the one Flathub ships, and a single stated name would be
    wrong for one of the two arrangements.
    """

    token: str
    default: DirectoryName
    installations: Mapping[str, DirectoryName] = field(default_factory=dict)

    def stated(self, flatpak: str | None = None) -> DirectoryName:
        """The name this installation uses, with the evidence that established it."""
        return self.installations.get(flatpak or "", self.default)

    def name(self, flatpak: str | None = None) -> str:
        """The name alone — what a path join needs."""
        return self.stated(flatpak).name


@dataclass(frozen=True, slots=True)
class SettingsFile:
    """One settings file of one emulator: its name, and where a launch opens it.

    ``path`` is relative to the emulator's own ``directory``, never to the XDG
    base — the directory is stated once for the emulator and joined here.
    ``bases`` holds more than one entry exactly where the root is a property of
    the launch rather than of the emulator — DuckStation's DataRoot is the
    config home where ``XDG_CONFIG_HOME`` is set and absolute and the data home
    otherwise — and a reader probes them in this order.
    """

    token: str
    name: str
    bases: tuple[str, ...]
    path: str
    citation: str
    directory: EmulatorDirectory

    def locations(
        self,
        *,
        config_home: str,
        data_home: str,
        flatpak: str | None,
        xdg_pinned: bool = False,
    ) -> tuple[str, ...]:
        """The absolute candidates, in probe order, against one launch's XDG pair.

        The pair is the launch's own — on EmuDeck the picked binary's, which
        is why this takes two homes rather than reading an arrangement's — and
        so is *flatpak*, the app id the launch runs under where it runs one at
        all, because that is what decides the directory's spelling.

        *xdg_pinned* says the launch happens inside a flatpak sandbox, whose
        ``XDG_*_HOME`` variables flatpak force-pins to the per-app directories
        after applying every override (flatpak-context.c:3158-3187 at 1.16.6;
        the env composition section of
        docs/research/retrodeck-save-placement.md). A file has several bases
        precisely because the emulator picks its root from whether
        ``XDG_CONFIG_HOME`` is set, and inside a sandbox it always is — so the
        candidates collapse to the config side. There is no second place such
        a launch could open the file, and probing one would read a path this
        emulator never opens.

        It is its own word rather than ``flatpak is not None`` because the two
        are different facts: an arrangement's bundled build runs inside the
        arrangement's sandbox under no app id of its own, and that launch is
        pinned all the same.
        """
        homes = {"config": config_home, "data": data_home}
        below = os.path.join(self.directory.name(flatpak), self.path)
        bases = ("config",) if xdg_pinned and "config" in self.bases else self.bases
        return tuple(os.path.join(homes[base], below) for base in bases)

    def only(self, *, config_home: str, data_home: str, flatpak: str | None) -> str:
        """The single location, for a file whose root does not vary.

        Raises for a file with several candidates rather than silently
        answering the first: a caller that cannot probe must not be handed a
        guess dressed as an address.
        """
        if len(self.bases) != 1:
            raise ValueError(
                f"settings file {self.token}/{self.name} states {len(self.bases)} bases — "
                "its location is decided by the launch, so a caller must probe them in order"
            )
        return self.locations(config_home=config_home, data_home=data_home, flatpak=flatpak)[0]


def _relative(value: Any, where: str) -> str:
    """A path below a base: relative, no climbing out, no trailing separator."""
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.endswith("/")
        or ".." in value.split("/")
    ):
        raise ValueError(f"{where} must be a relative path below the base, got {value!r}")
    return value


def _directory_name(where: str, entry: Any) -> DirectoryName:
    if not isinstance(entry, dict) or set(entry) != {"name", "citation", "anchors"}:
        raise ValueError(f"{where}: expected exactly name/citation/anchors, got {entry!r}")
    citation = entry["citation"]
    if not isinstance(citation, str) or not citation:
        raise ValueError(f"{where}.citation: expected a non-empty string, got {citation!r}")
    name = _relative(entry["name"], f"{where}.name")
    anchors = entry["anchors"]
    expect_table_anchors(
        anchors,
        where=where,
        vocabulary=frozenset(path_segments(name)),
        binary_required=True,
        extra_keys=frozenset({"flatpak"}),
    )
    assert isinstance(anchors, dict)  # expect_table_anchors refuses anything else
    return DirectoryName(name=name, citation=citation, anchors=anchors)


def _directory(token: str, entry: Any) -> EmulatorDirectory:
    where = f"emulator settings {token}: directory"
    if not isinstance(entry, dict) or not {"name", "citation", "anchors"} <= set(entry) <= {
        "name",
        "citation",
        "anchors",
        "installations",
    }:
        raise ValueError(
            f"{where}: expected name/citation/anchors and an optional installations, got {entry!r}"
        )
    default = _directory_name(
        where, {k: entry.get(k) for k in ("name", "citation", "anchors")}
    )
    raw = entry.get("installations", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{where}.installations: expected an object, got {raw!r}")
    installations: dict[str, DirectoryName] = {}
    for app_id, spec in raw.items():
        if not isinstance(app_id, str) or not app_id:
            raise ValueError(f"{where}.installations: expected an app id key, got {app_id!r}")
        stated = _directory_name(f"{where}.installations[{app_id}]", spec)
        if stated.name == default.name:
            raise ValueError(
                f"{where}.installations[{app_id}] repeats the default name {default.name!r} — "
                "an override that states nothing hides that nothing was established"
            )
        installations[app_id] = stated
    return EmulatorDirectory(token=token, default=default, installations=installations)


def _file(token: str, name: str, entry: Any, directory: EmulatorDirectory) -> SettingsFile:
    where = f"emulator settings {token}/{name}"
    if not isinstance(entry, dict) or set(entry) != {"bases", "path", "citation"}:
        raise ValueError(f"{where}: expected exactly bases/path/citation, got {entry!r}")
    bases = entry["bases"]
    if not isinstance(bases, list) or not bases:
        raise ValueError(f"{where}.bases must be a non-empty list, got {bases!r}")
    for base in bases:
        if base not in _BASES:
            raise ValueError(f"{where}.bases: must be one of {list(_BASES)}, got {base!r}")
    if len(set(bases)) != len(bases):
        raise ValueError(f"{where}.bases repeats a base, so a probe would read one twice")
    path = _relative(entry["path"], f"{where}.path")
    # Every stated spelling, not just the default: a path spelled with an
    # installation's own directory name would smuggle in exactly the second
    # copy this guard exists to refuse.
    for stated in (directory.default, *directory.installations.values()):
        if path.split("/")[0] == stated.name.split("/")[0]:
            raise ValueError(
                f"{where}.path begins with the emulator's own directory {stated.name!r} — "
                "the path is stated below that directory, which is named once for the emulator"
            )
    citation = entry["citation"]
    if not isinstance(citation, str) or not citation:
        raise ValueError(f"{where}.citation: expected a non-empty string, got {citation!r}")
    if os.path.basename(path) != name:
        raise ValueError(
            f"{where}: the key is the file's own name, and {path!r} ends in "
            f"{os.path.basename(path)!r} — two spellings of one file is what this table exists "
            "to prevent"
        )
    return SettingsFile(
        token=token,
        name=name,
        bases=tuple(bases),
        path=path,
        citation=citation,
        directory=directory,
    )


@dataclass(frozen=True, slots=True)
class EmulatorEntry:
    """One emulator's row: how it installs, its own directory, and the files below it.

    ``directory`` is ``None`` — and ``files`` then empty — for an emulator whose
    settings file this table deliberately cannot address. MAME's is one: which
    ``mame.ini`` governs is the launch's own fact, decided by ``-inipath`` and a
    compiled search path, so its card takes the ``launch_ini`` shape instead of
    naming an address here. The row still exists, because how the arrangement
    installs MAME is a fact of exactly this file's kind.
    """

    flatpak: InstalledFlatpak
    directory: EmulatorDirectory | None
    files: Mapping[str, SettingsFile]


def _installed_flatpak(token: str, entry: Any) -> InstalledFlatpak:
    """One row's install statement — an id or a stated no, and never a silence.

    The key is required on every row, with no default. A row that simply left
    it out would read as "installs no flatpak" while establishing nothing, and
    the answer that reads it would refuse a launch whose trees are right there
    — which is the failure #288 was.
    """
    where = f"emulator settings {token}: flatpak"
    if not isinstance(entry, dict) or set(entry) != {"app_id", "citation"}:
        raise ValueError(f"{where}: expected exactly app_id/citation, got {entry!r}")
    app_id = entry["app_id"]
    if app_id is not None and (not isinstance(app_id, str) or not app_id):
        raise ValueError(f"{where}.app_id: expected an app id or null, got {app_id!r}")
    citation = entry["citation"]
    if not isinstance(citation, str) or not citation:
        raise ValueError(f"{where}.citation: expected a non-empty string, got {citation!r}")
    return InstalledFlatpak(app_id=app_id, citation=citation)


def _entry(token: str, entry: Any) -> EmulatorEntry:
    """One emulator's row: the install statement, and the addresses where there are any."""
    where = f"emulator settings {token}"
    if not isinstance(entry, dict) or not {"flatpak"} <= set(entry) <= {
        "flatpak",
        "directory",
        "files",
    }:
        raise ValueError(
            f"{where}: expected a flatpak and an optional directory/files pair, got {entry!r}"
        )
    flatpak = _installed_flatpak(token, entry["flatpak"])
    if ("directory" in entry) != ("files" in entry):
        raise ValueError(
            f"{where}: a directory and the files stated below it are one statement — "
            "one without the other addresses nothing, or addresses it from nowhere"
        )
    if "directory" not in entry:
        if flatpak.app_id is None:
            raise ValueError(
                f"{where}: states neither an app id nor an address — a row that says "
                "nothing about an emulator is a row nobody can read anything off"
            )
        return EmulatorEntry(flatpak=flatpak, directory=None, files={})
    directory = _directory(token, entry["directory"])
    files = entry["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError(f"{where}: states no file at all")
    return EmulatorEntry(
        flatpak=flatpak,
        directory=directory,
        files={name: _file(token, name, spec, directory) for name, spec in files.items()},
    )


def load_emulator_settings(text: str | None = None) -> dict[str, EmulatorEntry]:
    """Load the packaged table (or *text* when supplied, for tests)."""
    if text is None:
        text = packaged_text("emulator_settings.json")
    raw = json.loads(text)
    if not isinstance(raw, dict) or raw.get("schema") != EMULATOR_SETTINGS_SCHEMA:
        raise ValueError(
            f"emulator_settings: unsupported schema "
            f"{raw.get('schema') if isinstance(raw, dict) else None!r} "
            f"(this atlas reads schema {EMULATOR_SETTINGS_SCHEMA})"
        )
    table: dict[str, EmulatorEntry] = {}
    for token, entry in raw.get("emulators", {}).items():
        table[token] = _entry(token, entry)
    return table


_PACKAGED: dict[str, EmulatorEntry] | None = None


def _packaged() -> dict[str, EmulatorEntry]:
    global _PACKAGED
    if _PACKAGED is None:
        _PACKAGED = load_emulator_settings()
    return _PACKAGED


def emulator_directory(token: str | None) -> EmulatorDirectory:
    """The emulator's own directory statement — loudly, or not at all.

    A card asking about an emulator this table does not carry is a build
    mistake in the same class the card loaders already refuse: the two shipped
    out of step, and joining a path onto a directory nobody stated would be the
    exact failure this table exists to remove.
    """
    entry = _packaged().get(token or "")
    if entry is None or entry.directory is None:
        raise ValueError(
            f"no directory is stated for {token!r} — a card names it and "
            "atlas/data/emulator_settings.json does not carry it"
        )
    return entry.directory


def installed_flatpak(token: str | None) -> str | None:
    """The app id this emulator's flatpak install carries, where one is established.

    ``None`` says no app id is established for the token, which the table
    reaches two ways: a row whose ``flatpak`` states ``null`` — the arrangement
    installs the emulator some other way, cited — and a token the table carries
    no row for at all, which is every emulator atlas states nothing about. A
    caller that gets ``None`` refuses with the variant named; it must never
    join a path onto an id nobody stated, which is the whole point of asking
    here rather than guessing.

    The loud half of the rule is the loader's, because it is the half that can
    be loud without lying: a row that omits the key fails the load outright
    (``_installed_flatpak``), so "the table does not state this" can only ever
    mean somebody wrote down that it does not.
    """
    entry = _packaged().get(token or "")
    return entry.flatpak.app_id if entry is not None else None


def user_directory(token: str | None, *, flatpak: str | None) -> str:
    """The name of the emulator's own directory, for the launch that runs it.

    *flatpak* has no default on purpose. It is ``None`` for every launch that
    runs the emulator outside a flatpak of its own — an arrangement's bundled
    build, an AppImage, a host install — and a route that simply forgot it
    would answer the default name and look right on every emulator whose name
    does not vary. Making it a required word forces each caller to say which
    installation it is asking about.
    """
    return emulator_directory(token).name(flatpak)


def settings_file(token: str | None, name: str) -> SettingsFile:
    """The named settings file of one emulator — loudly, or not at all."""
    entry = _packaged().get(token or "")
    if entry is None or name not in entry.files:
        raise ValueError(
            f"no settings file {name!r} is stated for {token!r} — a card names it and "
            "atlas/data/emulator_settings.json does not carry it"
        )
    return entry.files[name]


def settings_files(token: str | None) -> dict[str, SettingsFile]:
    """Every settings file stated for one emulator, or an empty mapping."""
    entry = _packaged().get(token or "")
    return dict(entry.files) if entry is not None else {}
