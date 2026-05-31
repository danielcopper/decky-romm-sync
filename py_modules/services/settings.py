"""SettingsService — user-facing settings reads/writes and frontend-log routing.

Owns every callable that reads or mutates the live ``settings`` dict
from the frontend. Adapter-level I/O (Steam Input config, RetroArch
input driver) is reached via the ``SteamConfigStore`` Protocol;
on-disk persistence is fired through the injected
``save_settings_to_disk`` callable so the service never touches the
filesystem directly.

Frontend-log routing also lives here — it reads the configured level
from the live settings dict and dispatches to the runtime logger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    import logging

    from services.protocols import SettingsPersister, SteamConfigStore, UnitOfWorkFactory


_MASK_PLACEHOLDER = "••••"
_VALID_LOG_LEVELS = ("debug", "info", "warn", "error")
_VALID_STEAM_INPUT_MODES = ("default", "force_on", "force_off")


@dataclass(frozen=True)
class SettingsServiceConfig:
    """Frozen wiring bundle handed to ``SettingsService.__init__``.

    Carries the live settings dict, the SQLite Unit-of-Work factory (the
    read seam over the ``roms`` aggregate for the bound-shortcut app_ids
    ``apply_steam_input_setting`` re-skins), plus the runtime
    infrastructure (logger, settings persister, steam-config adapter).
    Bundled here so the ctor stays within the S107 parameter budget.
    """

    settings: dict[str, Any]
    uow_factory: UnitOfWorkFactory
    logger: logging.Logger
    settings_persister: SettingsPersister
    steam_config: SteamConfigStore


class SettingsService:
    """User-facing settings reads/writes, masking, and frontend-log routing."""

    LOG_LEVELS: ClassVar[dict[str, int]] = {"debug": 0, "info": 1, "warn": 2, "error": 3}

    def __init__(self, *, config: SettingsServiceConfig) -> None:
        self._settings = config.settings
        self._uow_factory = config.uow_factory
        self._logger = config.logger
        self._settings_persister = config.settings_persister
        self._steam_config = config.steam_config

    # ── Server connection settings ───────────────────────────────────────

    def save_server_url(self, romm_url: str, allow_insecure_ssl: bool | None = None) -> dict[str, Any]:
        """Persist the server URL and optional SSL flag.

        Credentials and tokens are never touched here — minting and
        storing the Client API Token is ``ConnectionService``'s job.
        ``allow_insecure_ssl=None`` leaves the SSL flag unchanged.
        """
        try:
            self._settings["romm_url"] = romm_url
            if allow_insecure_ssl is not None:
                self._settings["romm_allow_insecure_ssl"] = bool(allow_insecure_ssl)
            self._settings_persister.save_settings()
            return {"success": True, "message": "Settings saved"}
        except Exception as e:
            self._logger.error(f"Failed to save settings: {e}")
            return {"success": False, "message": f"Save failed: {e}"}

    def get_settings(self) -> dict[str, Any]:
        """Return the read-shape settings dict for the frontend.

        Reports whether a Client API Token is stored via ``has_token``;
        the token itself is never sent to the frontend. The SteamGridDB
        API key is reported as a masked placeholder.
        """
        return {
            "romm_url": self._settings.get("romm_url", ""),
            "has_token": bool(self._settings.get("romm_api_token")),
            "steam_input_mode": self._settings.get("steam_input_mode", "default"),
            "sgdb_api_key_masked": _MASK_PLACEHOLDER if self._settings.get("steamgriddb_api_key") else "",
            "retroarch_input_check": self._steam_config.check_retroarch_input_driver(),
            "log_level": self._settings.get("log_level", "warn"),
            "romm_allow_insecure_ssl": self._settings.get("romm_allow_insecure_ssl", False),
            "collection_create_platform_groups": self._settings.get("collection_create_platform_groups", False),
        }

    # ── Log level ────────────────────────────────────────────────────────

    def save_log_level(self, level: str) -> dict[str, Any]:
        """Validate and persist the runtime log level."""
        if level not in _VALID_LOG_LEVELS:
            return {"success": False, "message": "Invalid log level"}
        self._settings["log_level"] = level
        self._settings_persister.save_settings()
        return {"success": True}

    def frontend_log(self, level: str, message: str) -> None:
        """Log a frontend message respecting the configured log_level threshold.

        Messages below the configured threshold are dropped silently.
        Unknown level strings are treated as ``debug`` (the lowest
        threshold) so misrouted frontend calls still surface when
        ``log_level=debug``.
        """
        configured = self._settings.get("log_level", "warn")
        if self.LOG_LEVELS.get(level, 0) >= self.LOG_LEVELS.get(configured, 2):
            if level == "error":
                self._logger.error(f"[FE] {message}")
            elif level == "warn":
                self._logger.warning(f"[FE] {message}")
            else:
                self._logger.info(f"[FE] {message}")

    # ── Steam Input ──────────────────────────────────────────────────────

    def save_steam_input_setting(self, mode: str) -> dict[str, Any]:
        """Validate and persist the Steam Input mode preference."""
        if mode not in _VALID_STEAM_INPUT_MODES:
            return {"success": False, "message": f"Invalid mode: {mode}"}
        self._settings["steam_input_mode"] = mode
        self._settings_persister.save_settings()
        return {"success": True}

    def apply_steam_input_setting(self) -> dict[str, Any]:
        """Apply the current Steam Input mode to every bound ROM shortcut."""
        mode = self._settings.get("steam_input_mode", "default")
        with self._uow_factory() as uow:
            app_ids = [rom.shortcut_app_id for rom in uow.roms.iter_all() if rom.shortcut_app_id is not None]
        if not app_ids:
            return {"success": True, "message": "No shortcuts to update"}
        try:
            self._steam_config.set_steam_input_config(app_ids, mode=mode)
            return {"success": True, "message": f"Steam Input set to '{mode}' for {len(app_ids)} shortcuts"}
        except Exception as e:
            self._logger.error(f"Failed to apply Steam Input setting: {e}")
            return {"success": False, "message": "Operation failed"}

    # ── RetroArch input driver ──────────────────────────────────────────

    def fix_retroarch_input_driver(self) -> dict[str, Any]:
        """Repair a problematic RetroArch ``input_driver`` value (``x`` -> ``sdl2``)."""
        return self._steam_config.fix_retroarch_input_driver()

    # ── Whitelist (non-Steam shortcut removal) ──────────────────────────

    def get_whitelist_settings(self) -> dict[str, Any]:
        """Return whitelist settings used by the non-Steam game removal feature."""
        return {
            "disabled_defaults": self._settings.get("whitelist_disabled_defaults", []),
            "custom_names": self._settings.get("whitelist_custom_names", []),
        }

    def update_whitelist_settings(self, disabled_defaults: object, custom_names: object) -> dict[str, Any]:
        """Validate and persist whitelist settings.

        Both arguments must be lists of strings. Anything else is
        rejected with an error response so a malformed frontend call
        cannot corrupt the on-disk shape.
        """
        if not isinstance(disabled_defaults, list) or not all(isinstance(s, str) for s in disabled_defaults):
            return {"success": False, "message": "disabled_defaults must be a list of strings"}
        if not isinstance(custom_names, list) or not all(isinstance(s, str) for s in custom_names):
            return {"success": False, "message": "custom_names must be a list of strings"}
        self._settings["whitelist_disabled_defaults"] = disabled_defaults
        self._settings["whitelist_custom_names"] = custom_names
        self._settings_persister.save_settings()
        return {"success": True}

    # ── Collection grouping ─────────────────────────────────────────────

    def save_collection_platform_groups(self, enabled: bool) -> dict[str, Any]:
        """Persist the collection platform-group toggle."""
        self._settings["collection_create_platform_groups"] = bool(enabled)
        self._settings_persister.save_settings()
        return {"success": True}
