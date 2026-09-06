"""ES-DE find-rules adapter — where an emulator's binary lives on this machine.

Owns the read-only I/O for ``es_find_rules.xml``, the sibling of the catalogue
ES-DE resolves a ``%EMULATOR_<NAME>%`` token through. The catalogue itself is
the vendored resolver's to read (:mod:`adapters.atlas_catalogue`); this file
answers the two questions the resolver does not, because both are about the
plugin's own launcher rather than about the machine's emulator knowledge:

- **Is a standalone emulator installed at all?** A command ES-DE lists may name
  an emulator RetroDECK ships no binary for. Baking such a command produces a
  Steam shortcut that dies in ~0.4 s, so the option is downgraded to
  ``needs_setup`` before it can become the system default (ADR-0020).
- **Which path does the folder-boot bake exec inside the sandbox?** A game that
  boots from a directory cannot go through RetroDECK's ``run_game.sh`` at all,
  so the bake needs the emulator's own component launcher (ADR-0019).

Atlas states the token a command names, not the host path it resolves to, so
both stay here until it answers them (emu-atlas#84).

The file is located by probing the flatpak install roots directly rather than
beside a resolved ``es_systems.xml``: the catalogue's own path is the resolver's
business now and is not necessarily one of these two. Both probes prefer
``linux/`` over ``unix/`` within each root, which is ES-DE's own per-flavor
layout and the order the catalogue read used to guarantee the sibling by
construction.
"""

from __future__ import annotations

import glob
import os
import re
from typing import TYPE_CHECKING, Any

from adapters.flatpak_install import flatpak_app_files_dirs

if TYPE_CHECKING:
    import logging

# The first ``%EMULATOR_<NAME>%`` token in a command names the ES-DE find-rule
# entry that resolves the emulator binary (e.g. ``%EMULATOR_RYUBING%`` →
# ``<emulator name="RYUBING">``). Captures ``<NAME>``.
_EMULATOR_TOKEN_RE = re.compile(r"%EMULATOR_([A-Z0-9_-]+)%")

# RetroDECK app id — its data/config trees back the sandbox ``/var/data`` and
# ``/var/config`` prefixes the find rules use for user-installed (external)
# components and per-emulator config.
_RETRODECK_APP_ID = "net.retrodeck.retrodeck"

# A find-rule ``staticpath`` entry that names one of these path fragments points
# at a RetroDECK-managed component (bundled under the flatpak's ``/app`` tree, or
# a user-installed external component under ``/var/data``). When an emulator has
# such an entry the component's presence on disk is authoritative for RetroDECK:
# if none of the emulator's staticpaths exist, it is genuinely not launchable.
_RETRODECK_COMPONENT_MARKERS = ("retrodeck/components/", "retrodeck/external_components/")

# es_find_rules.xml lives under the RetroDECK flatpak's files tree, in ES-DE's
# per-flavor systems dir. Prefer linux/ (RetroDECK-customized, more complete),
# then unix/ as fallback — WITHIN each install root.
_ES_FIND_RULES_SUFFIXES = (
    os.path.join(
        "retrodeck", "components", "es-de", "share", "es-de", "resources", "systems", "linux", "es_find_rules.xml"
    ),
    os.path.join(
        "retrodeck", "components", "es-de", "share", "es-de", "resources", "systems", "unix", "es_find_rules.xml"
    ),
)


def emulator_token(command: str) -> str | None:
    """Return the ``%EMULATOR_<NAME>%`` find-rule name in *command*, or ``None``.

    The name (e.g. ``RYUBING``) keys the ``es_find_rules.xml`` ``<emulator>``
    entry the existence probe resolves. A command with no ``%EMULATOR_*%`` token
    (there should be none among standalone commands) yields ``None`` — the caller
    then leaves the option unchanged (cannot probe → assume installed).
    """
    match = _EMULATOR_TOKEN_RE.search(command)
    return match.group(1) if match else None


class EsFindRulesAdapter:
    """Resolves emulator binaries from ES-DE's live ``es_find_rules.xml``.

    Caches its file read as an instance attribute, re-reading on an mtime change
    so a flatpak update is picked up. The on-disk probes themselves are not
    cached: a component the user installs mid-session is seen on the next call.
    """

    def __init__(self, *, logger: logging.Logger, user_home: str) -> None:
        self._logger = logger
        self._user_home = user_home
        # es_find_rules.xml parse ({EMULATOR_NAME: (staticpath entries, ...)}),
        # mtime-cached for the standalone existence probe and the sandbox
        # launcher lookup.
        self._find_rules_cache: dict[str, tuple[str, ...]] | None = None
        self._find_rules_mtime: float | None = None
        self._find_rules_path: str | None = None

    def resolve_sandbox_launcher(self, command: str) -> str | None:
        """Resolve a standalone *command*'s emulator to its RetroDECK sandbox launcher path.

        For the folder-boot direct bake (ADR-0019): extracts the command's
        ``%EMULATOR_<NAME>%`` token, looks it up in ``es_find_rules.xml``, and
        returns the ``staticpath`` entry that is a sandbox-absolute RetroDECK
        component launcher — a ``/app/…`` (bundled) or ``/var/{data,config}/…``
        (external) path naming a RetroDECK component, e.g.
        ``/app/retrodeck/components/rpcs3/component_launcher.sh``. That is the
        path ``flatpak run --command=`` execs INSIDE the sandbox; the bundled
        ``/app`` entry is preferred. Host-native entries (``~/…`` AppImages, host
        flatpak exports) are skipped — they are not reachable as a sandbox
        ``--command``. Returns ``None`` when the command carries no emulator
        token, the find rule is absent, or no sandbox component launcher is
        listed (the caller then keeps the standalone ``run_game.sh`` form).
        """
        token = emulator_token(command)
        if token is None:
            return None
        staticpaths = self._load_find_rules().get(token)
        if not staticpaths:
            return None
        candidates = [entry.split("|", 1)[0].strip() for entry in staticpaths]
        sandbox = [path for path in candidates if self._is_sandbox_component(path)]
        if not sandbox:
            return None
        return next((path for path in sandbox if path.startswith("/app/")), sandbox[0])

    def command_emulator_installed(self, command: str) -> bool:
        """Whether the emulator *command* names is installed in RetroDECK.

        The I/O half of ADR-0020's existence probe, keyed by the command so the
        caller never has to know that a find-rule token is what backs the answer.
        A command naming no ``%EMULATOR_*%`` token cannot be probed and answers
        ``True`` — the probe only ever reports a positive absence.
        """
        token = emulator_token(command)
        if token is None:
            return True
        return self._emulator_installed(token)

    @staticmethod
    def _is_sandbox_component(path: str) -> bool:
        """Whether *path* is a sandbox-absolute RetroDECK component launcher.

        True for a ``/app/…`` (bundled) or ``/var/{data,config}/…`` (external)
        ``staticpath`` naming a RetroDECK component — the entries that exist
        INSIDE the RetroDECK sandbox and can be run via ``flatpak run
        --command=``. Host-native paths (``~/…``, host flatpak exports) are not.
        """
        sandbox_prefixes = ("/app/", "/var/data/", "/var/config/")
        if not any(path.startswith(prefix) for prefix in sandbox_prefixes):
            return False
        return any(marker in path for marker in _RETRODECK_COMPONENT_MARKERS)

    def find_es_find_rules_xml(self) -> str | None:
        """Locate ``es_find_rules.xml`` inside the RetroDECK flatpak installation.

        Probes each flatpak install root (system, then per-user) and, within
        each, searches linux/ first (RetroDECK-customized) then unix/ as
        fallback. Returns the path or ``None`` (find rules absent → the probe
        assumes every emulator installed).
        """
        for files_dir in flatpak_app_files_dirs(self._user_home):
            for suffix in _ES_FIND_RULES_SUFFIXES:
                path = os.path.join(files_dir, suffix)
                if os.path.exists(path):
                    return path
        return None

    def parse_es_find_rules(self, xml_path: str) -> dict[str, tuple[str, ...]]:
        """Parse ``es_find_rules.xml`` into ``{EMULATOR_NAME: (staticpath, ...)}``.

        Only the ``staticpath`` entries are captured — the on-disk paths (bundled
        ``/app`` components, external ``/var/data`` components, host installs) the
        probe can honestly check. ``systempath`` binaries resolve on RetroDECK's
        own sandbox ``PATH`` and are not verifiable from outside the sandbox, so
        they are deliberately not recorded (see :meth:`_emulator_installed`). An
        emulator with no ``staticpath`` rule maps to an empty tuple. Uses
        ``xml.parsers.expat`` because Decky's PyInstaller-frozen Python does not
        bundle ``xml.etree``. Returns ``{}`` if the file cannot be read or parsed
        (the probe then assumes every emulator installed).
        """
        try:
            from xml.parsers import expat
        except ImportError:
            self._logger.warning("es_find_rules: xml.parsers.expat not available")
            return {}

        try:
            with open(xml_path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._logger.warning("es_find_rules: failed to read %s: %s", xml_path, e)
            return {}

        rules: dict[str, list[str]] = {}
        state: dict[str, Any] = {"text": "", "name": None, "rule_type": None}

        def start(name: str, attrs: dict[str, str]) -> None:
            state["text"] = ""
            if name == "emulator":
                state["name"] = attrs.get("name")
                if state["name"]:
                    rules.setdefault(state["name"], [])
            elif name == "rule":
                state["rule_type"] = attrs.get("type")

        def end(name: str) -> None:
            if name == "entry" and state["name"] and state["rule_type"] == "staticpath":
                rules[state["name"]].append(state["text"].strip())
            elif name == "emulator":
                state["name"] = None
            elif name == "rule":
                state["rule_type"] = None
            state["text"] = ""

        def char_data(data: str) -> None:
            state["text"] += data

        parser = expat.ParserCreate()
        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.CharacterDataHandler = char_data

        try:
            parser.Parse(data, True)
        except expat.ExpatError as e:
            self._logger.warning("es_find_rules: failed to parse %s: %s", xml_path, e)
            return {}

        return {name: tuple(entries) for name, entries in rules.items()}

    def _load_find_rules(self) -> dict[str, tuple[str, ...]]:
        """Load and cache the ``es_find_rules.xml`` parse (mtime-guarded).

        Re-reads on an mtime change (flatpak update) and returns ``{}`` when the
        file is absent so the probe defaults to "installed" (no find rules →
        cannot disprove).
        """
        xml_path = self.find_es_find_rules_xml()
        if xml_path is None:
            self._find_rules_cache = {}
            self._find_rules_path = None
            self._find_rules_mtime = None
            return {}

        try:
            current_mtime = os.path.getmtime(xml_path)
        except OSError:
            current_mtime = None

        if (
            self._find_rules_cache is not None
            and self._find_rules_path == xml_path
            and self._find_rules_mtime == current_mtime
        ):
            return self._find_rules_cache

        self._find_rules_cache = self.parse_es_find_rules(xml_path)
        self._find_rules_path = xml_path
        self._find_rules_mtime = current_mtime
        return self._find_rules_cache

    def _emulator_installed(self, token: str) -> bool:
        """Return whether the ``%EMULATOR_<token>%`` emulator is installed in RetroDECK.

        Honest, absence-only rule — the probe reports "not installed" **only** on
        positive evidence that a RetroDECK-managed component is missing, so it
        never falsely downgrades an emulator it cannot verify:

        - No find rule for the token, or the emulator has no ``staticpath`` entry
          (``systempath``-only) → ``True``. A ``systempath`` binary resolves on
          RetroDECK's sandbox ``PATH``, which is not visible from outside the
          sandbox; we cannot disprove it, so assume installed.
        - Any of its ``staticpath`` entries exists on disk (after mapping the
          sandbox ``/app`` and ``/var/{data,config}`` prefixes to their host
          locations, glob-aware) → ``True``.
        - No staticpath exists **and** at least one names a RetroDECK component
          (``/app/retrodeck/components/…`` bundled or
          ``/var/data/retrodeck/external_components/…`` user-installed) → ``False``.
          For RetroDECK the component's presence is authoritative: the sandbox
          launches the component, not an arbitrary host binary.
        - No staticpath exists and none is a RetroDECK component (host-native
          install paths only) → ``True`` (cannot verify a host install honestly;
          don't downgrade).
        """
        rules = self._load_find_rules()
        if token not in rules:
            return True
        staticpaths = rules[token]
        if not staticpaths:
            return True
        if any(self._static_entry_exists(entry) for entry in staticpaths):
            return True
        has_retrodeck_component = any(
            marker in entry for entry in staticpaths for marker in _RETRODECK_COMPONENT_MARKERS
        )
        return not has_retrodeck_component

    def _static_entry_exists(self, entry: str) -> bool:
        """Whether a find-rule ``staticpath`` entry resolves to a real file on disk.

        Maps the sandbox-relative prefixes the find rules use to their host
        locations, then glob-matches (entries carry ``*`` wildcards, e.g.
        ``~/Applications/Cemu*.AppImage``). A trailing ``|<launch-command>`` (the
        ES-DE ``found-path|actual-command`` form) is stripped — only the probe
        path matters.
        """
        path = entry.split("|", 1)[0].strip()
        if not path:
            return False
        return any(glob.glob(candidate) for candidate in self._map_static_path(path))

    def _map_static_path(self, path: str) -> list[str]:
        """Map one sandbox-relative ``staticpath`` to its host candidate path(s).

        ``/app`` is RetroDECK's flatpak files tree (checked under every install
        root); ``/var/data`` and ``/var/config`` are the RetroDECK app's host
        data/config dirs; ``~`` expands to the user home; any other absolute path
        (host flatpak exports, ``/run`` …) is taken literally.
        """
        if path.startswith("/app/"):
            rest = path[len("/app/") :]
            return [os.path.join(files_dir, rest) for files_dir in flatpak_app_files_dirs(self._user_home)]
        if path.startswith("/var/data/"):
            return [self._retrodeck_var_dir("data", path[len("/var/data/") :])]
        if path.startswith("/var/config/"):
            return [self._retrodeck_var_dir("config", path[len("/var/config/") :])]
        if path == "~":
            return [self._user_home]
        if path.startswith("~/"):
            return [os.path.join(self._user_home, path[2:])]
        return [path]

    def _retrodeck_var_dir(self, kind: str, rest: str) -> str:
        """Host path for a sandbox ``/var/<kind>/<rest>`` RetroDECK path.

        RetroDECK's sandbox ``/var/data`` and ``/var/config`` are backed by the
        flatpak app's per-user ``~/.var/app/<app-id>/{data,config}`` trees.
        """
        return os.path.join(self._user_home, ".var", "app", _RETRODECK_APP_ID, kind, rest)
