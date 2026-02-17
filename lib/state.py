import os
import json
import time
from typing import TYPE_CHECKING

import decky

if TYPE_CHECKING:
    from typing import Protocol

    class _StateDeps(Protocol):
        settings: dict
        _state: dict
        _metadata_cache: dict


class StateMixin:
    def _load_settings(self):
        settings_path = os.path.join(
            decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"
        )
        try:
            with open(settings_path, "r") as f:
                self.settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.settings = {}
        self.settings.setdefault("romm_url", "")
        self.settings.setdefault("romm_user", "")
        self.settings.setdefault("romm_pass", "")
        self.settings.setdefault("enabled_platforms", {})
        self.settings.setdefault("steam_input_mode", "default")
        self.settings.setdefault("steamgriddb_api_key", "")
        self.settings.setdefault("debug_logging", False)
        # Migrate old boolean setting
        if "disable_steam_input" in self.settings:
            if self.settings.pop("disable_steam_input"):
                self.settings["steam_input_mode"] = "force_off"
            self._save_settings_to_disk()

    def _save_settings_to_disk(self):
        settings_dir = decky.DECKY_PLUGIN_SETTINGS_DIR
        os.makedirs(settings_dir, exist_ok=True)
        settings_path = os.path.join(settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump(self.settings, f, indent=2)

    def _log_debug(self, msg):
        """Log a message only when debug_logging is enabled in settings."""
        if self.settings.get("debug_logging", False):
            decky.logger.info(msg)

    def _load_state(self):
        state_path = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "state.json")
        try:
            with open(state_path, "r") as f:
                saved = json.load(f)
            self._state.update(saved)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _prune_stale_state(self):
        """Remove installed_roms entries whose files no longer exist on disk."""
        pruned = []
        for rom_id, entry in list(self._state["installed_roms"].items()):
            file_path = entry.get("file_path", "")
            rom_dir = entry.get("rom_dir", "")
            # Keep if either the file or the rom_dir still exists
            if (file_path and os.path.exists(file_path)) or (rom_dir and os.path.exists(rom_dir)):
                continue
            decky.logger.info(f"Pruned stale installed_roms entry: {rom_id} ({file_path})")
            pruned.append(rom_id)
        for rom_id in pruned:
            del self._state["installed_roms"][rom_id]
        if pruned:
            self._save_state()

    def _save_state(self):
        state_dir = decky.DECKY_PLUGIN_RUNTIME_DIR
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "state.json")
        tmp_path = state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._state, f, indent=2)
        os.replace(tmp_path, state_path)

    def _load_metadata_cache(self):
        cache_path = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "metadata_cache.json")
        try:
            with open(cache_path, "r") as f:
                self._metadata_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._metadata_cache = {}

    def _save_metadata_cache(self):
        cache_dir = decky.DECKY_PLUGIN_RUNTIME_DIR
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "metadata_cache.json")
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._metadata_cache, f, indent=2)
        os.replace(tmp_path, cache_path)
