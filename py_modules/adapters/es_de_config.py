"""ES-DE configuration adapters.

Owns the I/O for resolving active RetroArch cores from ES-DE's
``gamelist.xml`` / ``es_systems.xml`` / ``core_defaults.json``, and for
writing the per-system core override back to ``gamelist.xml``. Both the
read and write sides operate at the system layer only (per-system
``<alternativeEmulator>`` → es_systems default → ``core_defaults``);
per-game core selection lives in the ``roms`` store, not gamelist.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

_CORE_SO_RE = re.compile(r"%CORE_RETROARCH%/([\w-]+_libretro)\.so")

_GAMELIST_FILENAME = "gamelist.xml"

_FLATPAK_SYSTEMS_DIR = (
    "/var/lib/flatpak/app/net.retrodeck.retrodeck/current/active"
    "/files/retrodeck/components/es-de/share/es-de/resources/systems"
)

# Prefer linux/ (RetroDECK-customized, more complete), then unix/ as fallback.
_ES_SYSTEMS_CANDIDATES = [
    _FLATPAK_SYSTEMS_DIR + "/linux/es_systems.xml",
    _FLATPAK_SYSTEMS_DIR + "/unix/es_systems.xml",
]


# ---------------------------------------------------------------------------
# CoreResolver — core resolution logic + caching
# ---------------------------------------------------------------------------


class CoreResolver:
    """Resolves active RetroArch cores for ES-DE systems.

    Reads ``es_systems.xml`` from the RetroDECK flatpak install, falls back
    to a bundled ``core_defaults.json``, and honours the per-system
    ``<alternativeEmulator>`` override written into ``gamelist.xml``.
    Caches its file reads as instance attributes; call :meth:`reset_cache`
    after editing the underlying files.

    Implements the ``CoreInfoProvider`` Protocol structurally.
    """

    def __init__(
        self,
        plugin_dir: str,
        logger: logging.Logger,
        get_retrodeck_home: Callable[[], str | None] | None = None,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._logger = logger
        self._get_retrodeck_home = get_retrodeck_home
        self._es_systems_cache: dict[str, Any] | None = None
        self._es_systems_mtime: float | None = None
        self._es_systems_path: str | None = None
        self._core_defaults_cache: dict[str, Any] | None = None
        self._core_defaults_mtime: float | None = None
        self._core_defaults_path: str | None = None

    def reset_cache(self) -> None:
        """Drop cached ``es_systems.xml`` and ``core_defaults.json`` reads.

        Call after any process (including this plugin) edits a
        ``gamelist.xml`` override, so the next resolution re-reads from
        disk instead of returning a stale label.
        """
        self._es_systems_cache = None
        self._es_systems_mtime = None
        self._es_systems_path = None
        self._core_defaults_cache = None
        self._core_defaults_mtime = None
        self._core_defaults_path = None

    # -- public API ----------------------------------------------------------

    def _resolve_label(self, system_name, system_info, override_label):
        """Resolve a core label to (core_so, label) tuple, or None."""
        if system_info and override_label in system_info.get("label_to_core", {}):
            core_so = system_info["label_to_core"][override_label]
            return (core_so, override_label)
        # Try core_defaults fallback for label resolution
        defaults = self._load_core_defaults()
        default_cores = defaults.get(system_name, {}).get("cores", {})
        for core_so, label in default_cores.items():
            if label == override_label:
                return (core_so, override_label)
        return None

    def _try_gamelist_overrides(self, system_name, system_info):
        """Try the per-system override from gamelist.xml.

        Returns (core_so, label) or None.
        """
        try:
            if self._get_retrodeck_home is not None:
                retrodeck_home = self._get_retrodeck_home()
            else:
                return None
        except Exception:
            return None

        if not retrodeck_home:
            return None

        override_label = self._read_system_override(retrodeck_home, system_name)
        if not override_label:
            return None
        return self._resolve_label(system_name, system_info, override_label)

    def get_active_core(self, system_name):
        """Resolve the active core for a system.

        Resolution chain:
        1. Per-system override (gamelist.xml alternativeEmulator)
        2. Live es_systems.xml default
        3. Static core_defaults.json fallback
        4. (None, None) if all fail

        Returns: (core_so_name, label) or (None, None).
        """
        es_systems = self._load_es_systems()
        system_info = es_systems.get(system_name)

        # Try the system-level gamelist.xml override first
        override = self._try_gamelist_overrides(system_name, system_info)
        if override:
            return override

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

    def _read_system_override(self, retrodeck_home, system_name):
        """Check for per-system alternative emulator override in gamelist.xml.

        Reads ``{retrodeck_home}/ES-DE/gamelists/{system}/gamelist.xml``
        looking for ``<alternativeEmulator><label>X</label></alternativeEmulator>``.

        Returns the label string or None.
        """
        gamelist_path = os.path.join(retrodeck_home, "ES-DE", "gamelists", system_name, _GAMELIST_FILENAME)
        if not os.path.exists(gamelist_path):
            return None

        try:
            from xml.parsers import expat
        except ImportError:
            return None

        try:
            with open(gamelist_path, "rb") as f:
                data = f.read()
        except OSError:
            return None

        result = {"label": None}
        state = {"path": [], "text": ""}

        def start_element(name, _attrs):
            state["path"].append(name)
            state["text"] = ""

        def end_element(_name):
            text = state["text"].strip()
            if (
                len(state["path"]) >= 2
                and state["path"][-1] == "label"
                and state["path"][-2] == "alternativeEmulator"
                and text
            ):
                result["label"] = text
            state["path"].pop()
            state["text"] = ""

        def char_data(data):
            state["text"] += data

        parser = expat.ParserCreate()
        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        parser.CharacterDataHandler = char_data

        try:
            parser.Parse(data, True)
        except expat.ExpatError:
            return None

        return result["label"]

    # -- static helpers (no instance state needed) ---------------------------

    @staticmethod
    def find_es_systems_xml():
        """Locate es_systems.xml inside the RetroDECK flatpak installation.

        Uses the flatpak 'active' symlink to find the current version.
        Searches linux/ first (RetroDECK-customized), then unix/ as fallback.
        Works on SteamOS, Bazzite, and other Linux distros with flatpak.

        Returns the path or None.
        """
        for path in _ES_SYSTEMS_CANDIDATES:
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
            }
        elif name == "command":
            state["current_label"] = attrs.get("label", "")

    @staticmethod
    def _handle_es_system_name(sys, text):
        """Handle </name> inside a <system> element."""
        sys["name"] = text

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
        core_so}}}``.

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


# ---------------------------------------------------------------------------
# GamelistXmlEditorAdapter — gamelist.xml read/write operations
# ---------------------------------------------------------------------------


class GamelistXmlEditorAdapter:
    """Writes the per-system core override into ES-DE's gamelist.xml.

    Implements ``GamelistXmlEditor`` Protocol structurally. Reads
    happen through :class:`CoreResolver`; this class only writes.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    # -- public API ----------------------------------------------------------

    def set_system_override(self, retrodeck_home, system_name, core_label):
        """Set or clear the system-wide core override in gamelist.xml.

        Writes ``<alternativeEmulator><label>X</label></alternativeEmulator>``.
        If ``core_label`` is None or empty, removes the
        ``alternativeEmulator`` element. Preserves all existing
        ``<game>`` entries. Creates file/directories if they don't
        exist.
        """
        path = self.gamelist_path(retrodeck_home, system_name)
        raw = self.read_gamelist_raw(path)

        if raw:
            parsed = self.parse_gamelist_preserving(raw)
            if parsed is None:
                self._logger.warning("es_de_config: failed to parse %s for writing", path)
                return False
            games_xml = [g["raw_xml"] for g in parsed["games"]]
        else:
            games_xml = []

        content = self.reconstruct_gamelist(core_label or None, games_xml)
        self.write_gamelist_atomic(path, content)
        action = "cleared" if not core_label else f"set to '{core_label}'"
        self._logger.info("es_de_config: system override for %s %s (%s)", system_name, action, path)
        return True

    # -- internal helpers (static, used by CoreResolver too) -----------------

    @staticmethod
    def gamelist_path(retrodeck_home, system_name):
        """Return the gamelist.xml path for a system."""
        return os.path.join(retrodeck_home, "ES-DE", "gamelists", system_name, _GAMELIST_FILENAME)

    @staticmethod
    def read_gamelist_raw(path):
        """Read gamelist.xml and return raw bytes, or None if not found."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    @staticmethod
    def write_gamelist_atomic(path, content):
        """Write gamelist.xml content atomically via tmp file."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.replace(tmp_path, path)

    @staticmethod
    def _build_attr_str(attrs):
        """Build an XML attribute string from a dict."""
        parts = []
        for k, v in attrs.items():
            parts.append(f' {k}="{GamelistXmlEditorAdapter.escape_xml(v)}"')
        return "".join(parts)

    @staticmethod
    def _handle_game_start(state, name, attrs):
        """Handle start_element when inside or entering a <game> tag."""
        if name == "game" and state["path"] == ["gameList", "game"]:
            state["in_game"] = True
            state["game_depth"] = len(state["path"])
            state["game_xml_parts"] = []
            state["game_path"] = None
            state["game_altemulator"] = None
            attr_str = GamelistXmlEditorAdapter._build_attr_str(attrs)
            state["game_xml_parts"].append(f"<game{attr_str}>")
        elif state["in_game"]:
            attr_str = GamelistXmlEditorAdapter._build_attr_str(attrs)
            state["game_xml_parts"].append(f"<{name}{attr_str}>")

    @staticmethod
    def _handle_game_end(state, result, name):
        """Handle end_element for game content. Returns True if handled."""
        if not state["in_game"]:
            return False

        text = state["text"].strip()
        if name == "game" and len(state["path"]) == state["game_depth"]:
            state["game_xml_parts"].append("</game>")
            result["games"].append(
                {
                    "path": state["game_path"],
                    "altemulator": state["game_altemulator"],
                    "raw_xml": "".join(state["game_xml_parts"]),
                }
            )
            state["in_game"] = False
        else:
            if state["text"]:
                state["game_xml_parts"].append(GamelistXmlEditorAdapter.escape_xml(state["text"]))
            state["game_xml_parts"].append(f"</{name}>")
            if name == "path":
                state["game_path"] = text
            elif name == "altemulator":
                state["game_altemulator"] = text
        return True

    @staticmethod
    def parse_gamelist_preserving(data):
        """Parse gamelist.xml into a structured representation that can be modified and reconstructed.

        Returns: ``{"alt_emulator_label": str | None, "games": [{"path":
        str, "altemulator": str | None, "raw_xml": str}],
        "other_content": str}`` or ``None`` on parse failure.
        """
        try:
            from xml.parsers import expat
        except ImportError:
            return None

        result: dict[str, Any] = {
            "alt_emulator_label": None,
            "games": [],
        }
        state = {
            "path": [],
            "text": "",
            "in_game": False,
            "game_depth": 0,
            "game_xml_parts": [],
            "game_path": None,
            "game_altemulator": None,
            "game_tag_name": None,
        }

        def start_element(name, attrs):
            state["path"].append(name)
            state["text"] = ""
            GamelistXmlEditorAdapter._handle_game_start(state, name, attrs)

        def end_element(name):
            if not GamelistXmlEditorAdapter._handle_game_end(state, result, name):
                # Outside game: look for alternativeEmulator/label
                text = state["text"].strip()
                if (
                    len(state["path"]) >= 2
                    and state["path"][-1] == "label"
                    and state["path"][-2] == "alternativeEmulator"
                    and text
                ):
                    result["alt_emulator_label"] = text
            state["path"].pop()
            state["text"] = ""

        def char_data(data):
            state["text"] += data

        parser = expat.ParserCreate()
        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        parser.CharacterDataHandler = char_data

        try:
            parser.Parse(data, True)
        except expat.ExpatError:
            return None

        return result

    @staticmethod
    def escape_xml(text):
        """Escape special XML characters."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    @staticmethod
    def reconstruct_gamelist(alt_label, games_xml_list):
        """Reconstruct gamelist.xml from components.

        ``alt_label``: the ``alternativeEmulator`` label, or ``None`` to omit.
        ``games_xml_list``: list of raw ``<game>...</game>`` XML strings.
        """
        parts = ['<?xml version="1.0"?>\n<gameList>']
        if alt_label:
            escaped = GamelistXmlEditorAdapter.escape_xml(alt_label)
            parts.append(f"\n  <alternativeEmulator>\n    <label>{escaped}</label>\n  </alternativeEmulator>")
        parts.extend(f"\n  {game_xml}" for game_xml in games_xml_list)
        parts.append("\n</gameList>\n")
        return "".join(parts)
