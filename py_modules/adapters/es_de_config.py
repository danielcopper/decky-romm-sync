"""ES-DE configuration adapter.

Owns the read-only I/O for resolving emulators from ES-DE's live
``es_systems.xml``. That file is the single source of truth: the system-layer
default emulator is the first *safely-bakeable* command in document order, the
picker offers every command annotated with its bakeability, and the libretro
active core (for the firmware BIOS filter) is the first RetroArch command. There
is no offline snapshot — RetroDECK is a hard prerequisite, so when
``es_systems.xml`` cannot be read there is no emulator to launch into and the
adapter reports "unavailable" rather than inventing a fallback. Alongside
``es_systems.xml`` the adapter parses the sibling ``es_find_rules.xml`` (the same
systems dir, same mtime-cache discipline) to probe whether a standalone
emulator's binary is actually installed in RetroDECK — a bakeable standalone
whose emulator is missing is downgraded to ``needs_setup`` (reason
``not_installed``) so it never becomes the baked default (ADR-0020). The retired
ES-DE gamelist is never read or written; the plugin-owned deviations
(per-platform core in ``settings.json``, per-game pin in the ``roms`` store) are
layered on top by :class:`services.active_core_resolver.ActiveCoreResolver`, not
here.
"""

from __future__ import annotations

import glob
import os
import re
from typing import TYPE_CHECKING, Any

from adapters.flatpak_install import flatpak_app_files_dirs
from domain.emulator_commands import (
    EmulatorOption,
    classify_command,
    downgrade_if_not_installed,
    option_to_invocation,
    select_default_option,
)

if TYPE_CHECKING:
    import logging

    from domain.shortcut_data import EmulatorInvocation

_CORE_SO_RE = re.compile(r"%CORE_RETROARCH%/([\w-]+_libretro)\.so")

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


def _emulator_token(command: str) -> str | None:
    """Return the ``%EMULATOR_<NAME>%`` find-rule name in *command*, or ``None``.

    The name (e.g. ``RYUBING``) keys the ``es_find_rules.xml`` ``<emulator>``
    entry the existence probe resolves. A command with no ``%EMULATOR_*%`` token
    (there should be none among standalone commands) yields ``None`` — the caller
    then leaves the option unchanged (cannot probe → assume installed).
    """
    match = _EMULATOR_TOKEN_RE.search(command)
    return match.group(1) if match else None


# es_systems.xml lives under the RetroDECK flatpak's files tree. Prefer linux/
# (RetroDECK-customized, more complete), then unix/ as fallback — WITHIN each
# install root.
_ES_SYSTEMS_SUFFIXES = (
    os.path.join(
        "retrodeck", "components", "es-de", "share", "es-de", "resources", "systems", "linux", "es_systems.xml"
    ),
    os.path.join(
        "retrodeck", "components", "es-de", "share", "es-de", "resources", "systems", "unix", "es_systems.xml"
    ),
)


# ---------------------------------------------------------------------------
# CoreResolver — core resolution logic + caching
# ---------------------------------------------------------------------------


class CoreResolver:
    """Resolves the system-layer emulator for ES-DE systems from live es_systems.

    Reads ``es_systems.xml`` from the RetroDECK flatpak install — the single
    source of truth. This is the system layer only: the libretro active core
    (for the firmware BIOS filter), the default emulator (first safely-bakeable
    command), and the full classified command list for the picker. The
    plugin-owned per-platform/per-game deviations are layered on top by
    :class:`services.active_core_resolver.ActiveCoreResolver`. Caches its file
    read as an instance attribute; call :meth:`reset_cache` to force a re-read.

    Implements the ``CoreInfoProvider`` Protocol structurally.
    """

    def __init__(
        self,
        plugin_dir: str,
        logger: logging.Logger,
        user_home: str,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._logger = logger
        self._user_home = user_home
        self._es_systems_cache: dict[str, Any] | None = None
        self._es_systems_mtime: float | None = None
        self._es_systems_path: str | None = None
        # es_find_rules.xml parse ({EMULATOR_NAME: (staticpath entries, ...)}),
        # mtime-cached alongside es_systems for the standalone existence probe.
        self._find_rules_cache: dict[str, tuple[str, ...]] | None = None
        self._find_rules_mtime: float | None = None
        self._find_rules_path: str | None = None

    def reset_cache(self) -> None:
        """Drop the cached ``es_systems.xml`` and ``es_find_rules.xml`` parses.

        Call after a per-platform core write so the next resolution re-reads
        from disk instead of returning a stale parse. The mtime guard in the
        loader already re-reads on a flatpak update; this forces it eagerly.
        """
        self._es_systems_cache = None
        self._es_systems_mtime = None
        self._es_systems_path = None
        self._find_rules_cache = None
        self._find_rules_mtime = None
        self._find_rules_path = None

    # -- public API ----------------------------------------------------------

    def get_active_core(self, system_name):
        """Resolve the system-layer libretro active core for a system.

        The first RetroArch ``<command>`` in ``es_systems.xml`` (the libretro
        ``default_core``). Libretro-only by design — this feeds the firmware
        layer's system-level BIOS filter, which keys on a RetroArch core, not
        the launch-layer default (which may be a standalone emulator). Returns
        ``(core_so_name, label)`` or ``(None, None)`` when the system is unknown
        or ``es_systems.xml`` cannot be read.
        """
        system_info = self._load_es_systems().get(system_name)
        if system_info and system_info.get("default_core"):
            return (system_info["default_core"], system_info["default_label"])
        return (None, None)

    def get_default_emulator(self, system_name: str) -> EmulatorInvocation | None:
        """Resolve the system-layer default **emulator** (libretro OR standalone).

        The first *safely-bakeable* command in ``es_systems.xml`` document order
        (:func:`domain.emulator_commands.select_default_option`) rendered into an
        :class:`EmulatorInvocation` — a libretro core or a standalone emulator,
        whichever ES-DE lists first that the plugin can bake. Returns ``None``
        when nothing is bakeable (or ``es_systems.xml`` cannot be read); the
        caller bakes the plain RetroDECK launch and lets RetroDECK resolve the
        emulator itself.

        Keeps the read-path/launch-path invariant: the resolved emulator is both
        what the ROM launches with and what derived values key on.
        """
        result = self.get_emulator_options(system_name)
        if not result["available"]:
            return None
        return option_to_invocation(select_default_option(result["options"]))

    def get_emulator_options(self, system_name: str) -> dict[str, Any]:
        """Return every ES-DE ``<command>`` for a system, classified for bakeability.

        Returns ``{"available": bool, "options": [EmulatorOption, ...]}``.
        ``available`` is ``False`` when ``es_systems.xml`` cannot be found or
        parsed — the caller surfaces that as "emulator list unavailable" (the
        launch-gate / health-banner owns the failure UX) rather than seeing an
        empty list it can't distinguish from a system with no commands.
        ``options`` preserves ES-DE's document order, so the first bakeable entry
        is the system default. A bakeable **standalone** option whose emulator is
        not installed in RetroDECK is downgraded to ``needs_setup`` (reason
        ``not_installed``) via the ``es_find_rules.xml`` probe, so it neither
        becomes the default nor bakes into a shortcut. An unknown system on a
        readable file yields ``available: True`` with an empty list.
        """
        es_systems = self._load_es_systems()
        if not es_systems:
            return {"available": False, "options": []}
        system_info = es_systems.get(system_name)
        if not system_info:
            return {"available": True, "options": []}
        options = [
            self._probe_installed(classify_command(label, text)) for label, text in system_info["commands"].items()
        ]
        return {"available": True, "options": options}

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
        token = _emulator_token(command)
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

    def _probe_installed(self, option: EmulatorOption) -> EmulatorOption:
        """Downgrade a bakeable standalone whose emulator is not installed.

        Libretro options and already-non-bakeable options pass through untouched
        (RetroArch ships with RetroDECK; the domain rule guards the kind). For a
        bakeable standalone, resolve its ``%EMULATOR_*%`` token against
        ``es_find_rules.xml`` and hand the on-disk verdict to
        :func:`domain.emulator_commands.downgrade_if_not_installed`.
        """
        if option.status != "bakeable" or option.kind != "standalone":
            return option
        token = _emulator_token(option.command)
        if token is None:
            return option
        return downgrade_if_not_installed(option, self._emulator_installed(token))

    def system_supports_m3u(self, system_name: str) -> bool:
        """True iff ES-DE lists ``.m3u`` as a supported extension for *system_name*.

        Reads the same ``es_systems.xml`` ES-DE uses to decide directory-collapse,
        so the answer can never disagree with ES-DE. Returns ``False`` when the
        system is unknown or ``es_systems.xml`` cannot be found (default-safe: a
        missing playlist only degrades; a wrong one breaks the launch).
        """
        es_systems = self._load_es_systems()
        system_info = es_systems.get(system_name)
        if not system_info:
            return False
        return ".m3u" in system_info.get("extensions", set())

    def get_supported_extensions(self, system_name: str) -> frozenset[str]:
        """Return the extensions ES-DE accepts for *system_name* (lowercased).

        Reads the same per-system ``<extension>`` list in ``es_systems.xml`` ES-DE
        consults, so a caller can intersect it with the disc-image set and never
        offer a disc the emulator cannot launch. Returns an empty frozenset for an
        unknown system or when ``es_systems.xml`` cannot be found (default-safe:
        the caller falls back to the full disc set).
        """
        es_systems = self._load_es_systems()
        system_info = es_systems.get(system_name)
        if not system_info:
            return frozenset()
        return frozenset(system_info.get("extensions", set()))

    # -- helpers -------------------------------------------------------------

    def find_es_systems_xml(self) -> str | None:
        """Locate es_systems.xml inside the RetroDECK flatpak installation.

        Probes each flatpak install root (system, then per-user) and, within
        each, searches linux/ first (RetroDECK-customized) then unix/ as
        fallback. Works on SteamOS, Bazzite, and other Linux distros with
        flatpak. Returns the path or ``None``.
        """
        for files_dir in flatpak_app_files_dirs(self._user_home):
            for suffix in _ES_SYSTEMS_SUFFIXES:
                path = os.path.join(files_dir, suffix)
                if os.path.exists(path):
                    return path
        return None

    def find_es_find_rules_xml(self) -> str | None:
        """Locate ``es_find_rules.xml`` beside the resolved ``es_systems.xml``.

        ES-DE ships both files in the same per-flavor ``systems/<flavor>/`` dir,
        so the find rules are resolved as the ``es_systems.xml`` sibling — this
        guarantees the same linux/-vs-unix flavor the emulator commands came from.
        Uses the already-resolved cached path when available. Returns the path or
        ``None`` (find rules absent → the probe assumes every emulator installed).
        """
        es_systems_path = self._es_systems_path or self.find_es_systems_xml()
        if es_systems_path is None:
            return None
        candidate = os.path.join(os.path.dirname(es_systems_path), "es_find_rules.xml")
        return candidate if os.path.exists(candidate) else None

    @staticmethod
    def _handle_es_system_start(state, name, attrs):
        """Handle start_element for es_systems.xml parsing."""
        state["path"].append(name)
        state["text"] = ""
        if state["root_tag"] is None:
            state["root_tag"] = name
        if name == "system":
            state["current_system"] = {
                "name": None,
                "default_core": None,
                "default_label": None,
                "cores": {},
                "label_to_core": {},
                "commands": {},
                "extensions": set(),
            }
        elif name == "command":
            state["current_label"] = attrs.get("label", "")

    @staticmethod
    def _handle_es_system_name(sys, text):
        """Handle </name> inside a <system> element."""
        sys["name"] = text

    @staticmethod
    def _handle_es_extension_end(sys, text):
        """Handle </extension> inside a <system> — capture supported extensions.

        The element text is a whitespace-separated list (e.g.
        ``.nsp .NSP .xci``). Tokens are lowercased so membership checks are
        case-insensitive against ES-DE's mixed-case lists.
        """
        sys["extensions"].update(token.lower() for token in text.split())

    @staticmethod
    def _handle_es_command_end(state, sys, text):
        """Handle </command> inside a <system> — capture the command + any core info.

        Records EVERY ``<command>`` (libretro AND standalone) as ``label →
        command text`` in ``commands`` (in document order — the classifier reads
        that order to pick the default emulator). Standalone commands (PCSX2,
        RPCS3, …) carry no ``%CORE_RETROARCH%/<core>.so`` token, so they don't
        populate the libretro ``cores`` / ``default_core`` maps — those stay
        libretro-only for the firmware BIOS filter — but their command text
        feeds the standalone launch bake and the picker.
        """
        label = state["current_label"]
        sys["commands"][label] = text

        match = _CORE_SO_RE.search(text)
        if not match:
            return
        core_so = match.group(1)
        sys["cores"][core_so] = label
        sys["label_to_core"][label] = core_so
        if sys["default_core"] is None:
            sys["default_core"] = core_so
            sys["default_label"] = label

    @staticmethod
    def _finalize_es_system(state, systems):
        """Handle </system> — store the completed system entry."""
        sys = state["current_system"]
        if sys is not None and sys["name"]:
            systems[sys["name"]] = {
                "default_core": sys["default_core"],
                "default_label": sys["default_label"],
                "cores": sys["cores"],
                "label_to_core": sys["label_to_core"],
                "commands": sys["commands"],
                "extensions": sys["extensions"],
            }
        state["current_system"] = None

    @staticmethod
    def _handle_es_system_end(state, systems, name):
        """Handle end_element for es_systems.xml parsing."""
        text = state["text"].strip()
        path = state["path"]
        sys = state["current_system"]

        if path == ["systemList", "system", "name"] and sys is not None:
            CoreResolver._handle_es_system_name(sys, text)
        elif path == ["systemList", "system", "extension"] and sys is not None:
            CoreResolver._handle_es_extension_end(sys, text)
        elif path == ["systemList", "system", "command"] and sys is not None:
            CoreResolver._handle_es_command_end(state, sys, text)
        elif name == "system":
            CoreResolver._finalize_es_system(state, systems)

        state["path"].pop()
        state["text"] = ""

    def parse_es_systems(self, xml_path):
        """Parse es_systems.xml and return per-system core info.

        Uses xml.parsers.expat (SAX-style) instead of xml.etree.ElementTree
        because Decky's PyInstaller-frozen Python does not bundle xml.etree.

        Returns: ``{system_name: {"default_core": str | None, "default_label":
        str | None, "cores": {core_so: label}, "label_to_core": {label:
        core_so}, "commands": {label: command_text}, "extensions": set[str]}}``.
        ``cores``/``default_core`` are libretro-only (the firmware BIOS filter);
        ``commands`` holds every ``<command>`` (libretro AND standalone) in
        document order — the classifier reads that order to pick the default
        emulator. ``extensions`` holds the lowercased ``<extension>`` tokens
        ES-DE uses to decide directory-collapse.

        Returns empty dict if file can't be parsed or fails structural validation.
        """
        try:
            from xml.parsers import expat
        except ImportError:
            self._logger.warning("es_de_config: xml.parsers.expat not available")
            return {}

        try:
            with open(xml_path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._logger.warning("es_de_config: failed to read %s: %s", xml_path, e)
            return {}

        systems: dict[str, Any] = {}
        state = {
            "path": [],  # element name stack
            "text": "",  # accumulated character data
            "root_tag": None,
            "current_system": None,
            "current_label": "",
        }

        def char_data(data):
            state["text"] += data

        parser = expat.ParserCreate()
        parser.StartElementHandler = lambda name, attrs: CoreResolver._handle_es_system_start(state, name, attrs)
        parser.EndElementHandler = lambda name: CoreResolver._handle_es_system_end(state, systems, name)
        parser.CharacterDataHandler = char_data

        try:
            parser.Parse(data, True)
        except expat.ExpatError as e:
            self._logger.warning("es_de_config: failed to parse %s: %s", xml_path, e)
            return {}

        if state["root_tag"] != "systemList":
            self._logger.warning(
                "es_de_config: unexpected root tag '%s' (expected 'systemList')",
                state["root_tag"],
            )
            return {}

        return systems

    # -- internal cache methods ----------------------------------------------

    def _load_es_systems(self) -> dict[str, Any]:
        """Load and cache es_systems.xml parse result.

        Re-reads from disk if the file's mtime has changed (handles flatpak updates).
        """
        xml_path = self.find_es_systems_xml()
        if xml_path:
            try:
                current_mtime = os.path.getmtime(xml_path)
            except OSError:
                current_mtime = None

            if (
                self._es_systems_cache is not None
                and self._es_systems_path == xml_path
                and self._es_systems_mtime == current_mtime
            ):
                return self._es_systems_cache

            self._es_systems_cache = self.parse_es_systems(xml_path)
            self._es_systems_path = xml_path
            self._es_systems_mtime = current_mtime
        else:
            if self._es_systems_cache is None:
                self._logger.info(
                    "es_de_config: es_systems.xml not found — emulator resolution unavailable "
                    "(RetroDECK installation not detected)"
                )
            self._es_systems_cache = {}
            self._es_systems_path = None
            self._es_systems_mtime = None

        return self._es_systems_cache or {}

    # -- es_find_rules.xml: standalone existence probe -----------------------

    def parse_es_find_rules(self, xml_path: str) -> dict[str, tuple[str, ...]]:
        """Parse ``es_find_rules.xml`` into ``{EMULATOR_NAME: (staticpath, ...)}``.

        Only the ``staticpath`` entries are captured — the on-disk paths (bundled
        ``/app`` components, external ``/var/data`` components, host installs) the
        probe can honestly check. ``systempath`` binaries resolve on RetroDECK's
        own sandbox ``PATH`` and are not verifiable from outside the sandbox, so
        they are deliberately not recorded (see :meth:`_emulator_installed`). An
        emulator with no ``staticpath`` rule maps to an empty tuple. Uses
        ``xml.parsers.expat`` for the same PyInstaller-frozen-Python reason as
        :meth:`parse_es_systems`. Returns ``{}`` if the file cannot be read or
        parsed (the probe then assumes every emulator installed).
        """
        try:
            from xml.parsers import expat
        except ImportError:
            self._logger.warning("es_de_config: xml.parsers.expat not available for find rules")
            return {}

        try:
            with open(xml_path, "rb") as f:
                data = f.read()
        except OSError as e:
            self._logger.warning("es_de_config: failed to read %s: %s", xml_path, e)
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
            self._logger.warning("es_de_config: failed to parse %s: %s", xml_path, e)
            return {}

        return {name: tuple(entries) for name, entries in rules.items()}

    def _load_find_rules(self) -> dict[str, tuple[str, ...]]:
        """Load and cache the ``es_find_rules.xml`` parse (mtime-guarded).

        Mirrors :meth:`_load_es_systems`: re-reads on an mtime change (flatpak
        update) and returns ``{}`` when the file is absent so the probe defaults
        to "installed" (no find rules → cannot disprove).
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
