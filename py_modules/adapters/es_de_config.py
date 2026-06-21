"""ES-DE configuration adapter.

Owns the read-only I/O for resolving RetroArch cores from ES-DE's
``es_systems.xml`` and the bundled ``core_defaults.json``. The system-layer
active core is the es_systems default with ``core_defaults`` as fallback; the
retired ES-DE gamelist is never read or written. The plugin-owned deviations
(per-platform core in ``settings.json``, per-game pin in the ``roms`` store) are
layered on top by :class:`services.active_core_resolver.ActiveCoreResolver`, not
here.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

from adapters.flatpak_install import flatpak_app_files_dirs

if TYPE_CHECKING:
    import logging

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
    """Resolves the system-layer active RetroArch core for ES-DE systems.

    Reads ``es_systems.xml`` from the RetroDECK flatpak install and falls back
    to a bundled ``core_defaults.json``. This is the system layer only — the
    es_systems default, ``core_defaults`` fallback, and the available-cores
    enumeration. The plugin-owned per-platform/per-game deviations are layered
    on top by :class:`services.active_core_resolver.ActiveCoreResolver`. Caches
    its file reads as instance attributes; call :meth:`reset_cache` to force a
    re-read.

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
        self._core_defaults_cache: dict[str, Any] | None = None
        self._core_defaults_mtime: float | None = None
        self._core_defaults_path: str | None = None

    def reset_cache(self) -> None:
        """Drop cached ``es_systems.xml`` and ``core_defaults.json`` reads.

        Call after a per-platform core write so the next resolution re-reads
        from disk instead of returning a stale parse. The mtime guards in the
        loaders already re-read on a flatpak update; this forces it eagerly.
        """
        self._es_systems_cache = None
        self._es_systems_mtime = None
        self._es_systems_path = None
        self._core_defaults_cache = None
        self._core_defaults_mtime = None
        self._core_defaults_path = None

    # -- public API ----------------------------------------------------------

    def get_active_core(self, system_name):
        """Resolve the system-layer active core for a system.

        Resolution chain:
        1. Live es_systems.xml default
        2. Static core_defaults.json fallback
        3. (None, None) if both fail

        Returns: (core_so_name, label) or (None, None).
        """
        es_systems = self._load_es_systems()
        system_info = es_systems.get(system_name)

        # Use live es_systems.xml default
        if system_info and system_info.get("default_core"):
            return (system_info["default_core"], system_info["default_label"])

        # Fallback to core_defaults.json
        defaults = self._load_core_defaults()
        default_info = defaults.get(system_name, {})
        if default_info.get("default_core"):
            return (default_info["default_core"], default_info.get("default_label"))

        return (None, None)

    def get_available_cores(self, system_name):
        """Return available RetroArch cores for a system.

        Merges live es_systems.xml data with core_defaults.json fallback.
        Returns: [{"core_so": str, "label": str, "is_default": bool}, ...]
        Empty list if system is unknown.
        """
        es_systems = self._load_es_systems()
        system_info = es_systems.get(system_name)

        if system_info and system_info.get("cores"):
            default_core = system_info.get("default_core")
            cores = [
                {"core_so": core_so, "label": label, "is_default": core_so == default_core}
                for core_so, label in system_info["cores"].items()
            ]
            self._logger.debug(
                "es_de_config: get_available_cores(%s) -> %d cores from es_systems.xml",
                system_name,
                len(cores),
            )
            return cores

        # Fallback to core_defaults.json
        defaults = self._load_core_defaults()
        default_info = defaults.get(system_name, {})
        if default_info.get("cores"):
            default_core = default_info.get("default_core")
            cores = [
                {"core_so": core_so, "label": label, "is_default": core_so == default_core}
                for core_so, label in default_info["cores"].items()
            ]
            self._logger.debug(
                "es_de_config: get_available_cores(%s) -> %d cores from core_defaults.json (fallback)",
                system_name,
                len(cores),
            )
            return cores

        self._logger.debug("es_de_config: get_available_cores(%s) -> no cores found", system_name)
        return []

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
        """Handle </command> inside a <system> — extract core info."""
        match = _CORE_SO_RE.search(text)
        if not match:
            return
        core_so = match.group(1)
        label = state["current_label"]
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
        core_so}, "extensions": set[str]}}``. ``extensions`` holds the
        lowercased ``<extension>`` tokens ES-DE uses to decide directory-collapse.

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

    def _load_core_defaults(self) -> dict[str, Any]:
        """Load the static core_defaults.json fallback.

        Re-reads from disk if the file's mtime has changed (handles plugin updates).
        """
        # Check plugin root first (Decky CLI moves defaults/ contents to root),
        # then defaults/ subdirectory (dev deploys via mise run deploy)
        root_path = os.path.join(self._plugin_dir, "core_defaults.json")
        dev_path = os.path.join(self._plugin_dir, "defaults", "core_defaults.json")
        defaults_path = root_path if os.path.exists(root_path) else dev_path

        try:
            current_mtime = os.path.getmtime(defaults_path)
        except OSError:
            current_mtime = None

        if (
            self._core_defaults_cache is not None
            and self._core_defaults_path == defaults_path
            and self._core_defaults_mtime == current_mtime
        ):
            return self._core_defaults_cache

        try:
            with open(defaults_path) as f:
                data = json.load(f)
            self._core_defaults_cache = data.get("systems", {})
        except (OSError, json.JSONDecodeError) as e:
            self._logger.warning("es_de_config: failed to load core_defaults.json: %s", e)
            self._core_defaults_cache = {}

        self._core_defaults_path = defaults_path
        self._core_defaults_mtime = current_mtime
        return self._core_defaults_cache or {}

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
                self._logger.info("es_de_config: es_systems.xml not found, using core_defaults.json fallback")
            self._es_systems_cache = {}
            self._es_systems_path = None
            self._es_systems_mtime = None

        return self._es_systems_cache or {}
