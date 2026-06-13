"""Persistence adapter — pure I/O for ``settings.json`` and the legacy save-sync read.

Handles atomic writes, file locking, and schema version stamping for
``settings.json``, plus the one-time legacy ``save_sync_state.json`` read that
feeds the settings fold. Migration logic lives in
``domain/state_migrations.py``. No ``import decky``.
"""

import contextlib
import fcntl
import json
import logging
import os
from typing import Any

_SETTINGS_VERSION = 8
_LOCK_EXT = ".lock"

DEFAULT_SETTINGS: dict[str, Any] = {
    "romm_url": "",
    "romm_user": "",
    "romm_pass": "",
    "romm_api_token": None,
    "romm_api_token_id": None,
    "romm_api_token_origin": None,
    "enabled_platforms": {},
    "enabled_collections": {"user": {}, "smart": {}, "franchise": {}},
    "collection_create_platform_groups": False,
    "steam_input_mode": "default",
    "steamgriddb_api_key": "",
    "romm_allow_insecure_ssl": False,
    "log_level": "warn",
    "save_sync_enabled": False,
    "sync_before_launch": True,
    "sync_after_exit": True,
    "default_slot": "default",
    "autocleanup_limit": 10,
    "device_name": None,
    "platform_cores": {},
}


class PersistenceAdapter:
    """Thin I/O layer for ``settings.json`` and the legacy save-sync read.

    Reads and writes ``settings.json`` and performs the one-time legacy
    ``save_sync_state.json`` read consumed by the settings fold.

    Parameters
    ----------
    settings_dir:
        Absolute path to the directory that holds ``settings.json``
        (typically ``decky.DECKY_PLUGIN_SETTINGS_DIR``).
    runtime_dir:
        Absolute path to the directory that holds ``save_sync_state.json``
        (typically ``decky.DECKY_PLUGIN_RUNTIME_DIR``).
    logger:
        A standard-library ``logging.Logger`` instance.
    """

    def __init__(self, settings_dir: str, runtime_dir: str, logger: logging.Logger) -> None:
        self._settings_dir = settings_dir
        self._runtime_dir = runtime_dir
        self._logger = logger

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locked_write(self, path: str, data: dict[str, Any]) -> None:
        """Atomic write of *data* to *path* under an exclusive file lock."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + ".tmp"
        lock_fd = os.open(path + _LOCK_EXT, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, path)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
        finally:
            os.close(lock_fd)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def load_settings(self) -> dict[str, Any]:
        """Read ``settings.json``, apply defaults, and fix permissions.

        Migration logic (e.g. renaming old keys) is intentionally NOT
        included here — that belongs in ``domain/state_migrations.py``.
        If the ``version`` key is absent the returned dict has ``version: 0``
        to signal a pre-versioning file to callers.
        """
        settings_path = os.path.join(self._settings_dir, "settings.json")
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            settings = {}

        for key, default in DEFAULT_SETTINGS.items():
            settings.setdefault(key, default)

        # Backfill version=0 to signal pre-versioning file to migration layer
        settings.setdefault("version", 0)

        # Enforce 0600 on settings file (migrate from world-readable 0644)
        if os.path.exists(settings_path):
            current_mode = os.stat(settings_path).st_mode & 0o777
            if current_mode != 0o600:
                os.chmod(settings_path, 0o600)

        return settings

    def save_settings(self, data: dict[str, Any]) -> None:
        """Atomic write of *data* to ``settings.json`` with flock, stamping version."""
        data["version"] = _SETTINGS_VERSION
        settings_path = os.path.join(self._settings_dir, "settings.json")
        self._locked_write(settings_path, data)

    # ------------------------------------------------------------------
    # Save-sync state (legacy read — consumed only by the settings fold)
    # ------------------------------------------------------------------

    def load_save_sync_state(self) -> dict[str, Any] | None:
        """Read ``save_sync_state.json`` and return the raw dict.

        Returns ``None`` when the file is missing, corrupt, or not a
        JSON object. The sole remaining caller is the one-time settings
        fold in ``bootstrap`` (``fold_legacy_save_sync_settings``), which
        lifts the legacy save-sync toggles + device label out of this
        file into ``settings.json``.
        """
        state_path = os.path.join(self._runtime_dir, "save_sync_state.json")
        try:
            with open(state_path) as f:
                loaded = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if not isinstance(loaded, dict):
            return None
        return loaded


class SettingsPersisterAdapter:
    """Adapter view exposing the ``SettingsPersister`` Protocol.

    Binds a :class:`PersistenceAdapter` and the live ``settings`` dict
    so services receive a zero-arg ``save_settings()`` seam. Lives in
    the adapters layer so services depend only on the Protocol, never
    on this class.
    """

    def __init__(self, persistence: PersistenceAdapter, settings: dict[str, Any]) -> None:
        self._persistence = persistence
        self._settings = settings

    def save_settings(self) -> None:
        self._persistence.save_settings(self._settings)


class PlatformCoreReaderAdapter:
    """Adapter view exposing the ``PlatformCoreReader`` Protocol over settings.

    Binds the live ``settings`` dict so the per-platform core selection is
    always read from ``settings["platform_cores"]`` as it stands at call time.
    The bound reference is the same dict every writer mutates, so a fan-out
    that resolves a freshly-written platform core sees the new value rather
    than a stale snapshot.
    """

    def __init__(self, settings: dict[str, Any]) -> None:
        self._settings = settings

    def get_platform_core(self, platform_slug: str) -> str | None:
        return self._settings.get("platform_cores", {}).get(platform_slug)
