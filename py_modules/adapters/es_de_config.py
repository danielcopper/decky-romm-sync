"""ES-DE configuration adapter.

Owns the read-only I/O for resolving emulators from ES-DE's live
``es_systems.xml``. That file is the single source of truth: the system-layer
default emulator is the first *safely-bakeable* command in document order, the
picker offers every command annotated with its bakeability, and the libretro
active core (for the firmware BIOS filter) is the first RetroArch command. There
is no offline snapshot — RetroDECK is a hard prerequisite, so when
``es_systems.xml`` cannot be read there is no emulator to launch into and the
adapter reports "unavailable" rather than inventing a fallback. The retired
ES-DE gamelist is never read or written; the plugin-owned deviations
(per-platform core in ``settings.json``, per-game pin in the ``roms`` store) are
layered on top by :class:`services.active_core_resolver.ActiveCoreResolver`, not
here.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from adapters.flatpak_install import flatpak_app_files_dirs
from domain.emulator_commands import classify_command, option_to_invocation, select_default_option

if TYPE_CHECKING:
    import logging

    from domain.shortcut_data import EmulatorInvocation

_CORE_SO_RE = re.compile(r"%CORE_RETROARCH%/([\w-]+_libretro)\.so")

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

    def reset_cache(self) -> None:
        """Drop the cached ``es_systems.xml`` parse.

        Call after a per-platform core write so the next resolution re-reads
        from disk instead of returning a stale parse. The mtime guard in the
        loader already re-reads on a flatpak update; this forces it eagerly.
        """
        self._es_systems_cache = None
        self._es_systems_mtime = None
        self._es_systems_path = None

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
        is the system default. An unknown system on a readable file yields
        ``available: True`` with an empty list.
        """
        es_systems = self._load_es_systems()
        if not es_systems:
            return {"available": False, "options": []}
        system_info = es_systems.get(system_name)
        if not system_info:
            return {"available": True, "options": []}
        options = [classify_command(label, text) for label, text in system_info["commands"].items()]
        return {"available": True, "options": options}

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
