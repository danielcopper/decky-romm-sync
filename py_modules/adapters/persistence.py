"""Persistence adapter — pure I/O for settings, state, and cache files.

Handles atomic writes, file locking, and schema version stamping.
Migration logic lives in ``domain/state_migrations.py``.
No ``import decky``.
"""

import contextlib
import fcntl
import json
import logging
import os

_STATE_VERSION = 1
_METADATA_CACHE_VERSION = 1
_FIRMWARE_CACHE_VERSION = 1
_SETTINGS_VERSION = 1
_LOCK_EXT = ".lock"

DEFAULT_SETTINGS: dict = {
    "romm_url": "",
    "romm_user": "",
    "romm_pass": "",
    "enabled_platforms": {},
    "enabled_collections": {},
    "collection_create_platform_groups": False,
    "steam_input_mode": "default",
    "steamgriddb_api_key": "",
    "romm_allow_insecure_ssl": False,
    "log_level": "warn",
}


class PersistenceAdapter:
    """Thin I/O layer for JSON persistence files used by the plugin.

    Parameters
    ----------
    settings_dir:
        Absolute path to the directory that holds ``settings.json``
        (typically ``decky.DECKY_PLUGIN_SETTINGS_DIR``).
    runtime_dir:
        Absolute path to the directory that holds ``state.json`` and
        ``metadata_cache.json`` (typically ``decky.DECKY_PLUGIN_RUNTIME_DIR``).
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

    def _locked_write(self, path: str, data: dict) -> None:
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

    def load_settings(self) -> dict:
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

    def save_settings(self, data: dict) -> None:
        """Atomic write of *data* to ``settings.json`` with flock, stamping version."""
        data["version"] = _SETTINGS_VERSION
        settings_path = os.path.join(self._settings_dir, "settings.json")
        self._locked_write(settings_path, data)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def load_state(self, defaults: dict) -> dict:
        """Read ``state.json`` and merge with *defaults*.

        Returns the merged dict.  If the file is missing or corrupt the
        returned dict is a copy of *defaults* with the version stamp.
        """
        state_path = os.path.join(self._runtime_dir, "state.json")
        state = dict(defaults)
        try:
            with open(state_path) as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                saved = {}
            if "version" not in saved:
                saved["version"] = _STATE_VERSION
            state.update(saved)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        state.setdefault("version", _STATE_VERSION)
        return state

    def save_state(self, data: dict) -> None:
        """Atomic write of *data* to ``state.json`` with flock, stamping version."""
        data["version"] = _STATE_VERSION
        state_path = os.path.join(self._runtime_dir, "state.json")
        self._locked_write(state_path, data)

    # ------------------------------------------------------------------
    # Metadata cache
    # ------------------------------------------------------------------

    def load_metadata_cache(self) -> dict:
        """Read ``metadata_cache.json`` with version check.

        Returns an empty cache dict if the file is missing, corrupt, or
        has a version mismatch (stale/incompatible schema).
        """
        cache_path = os.path.join(self._runtime_dir, "metadata_cache.json")
        try:
            with open(cache_path) as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return {"version": _METADATA_CACHE_VERSION}
            if loaded.get("version") != _METADATA_CACHE_VERSION:
                return {"version": _METADATA_CACHE_VERSION}
            return loaded
        except (FileNotFoundError, json.JSONDecodeError):
            return {"version": _METADATA_CACHE_VERSION}

    def save_metadata_cache(self, data: dict) -> None:
        """Atomic write of *data* to ``metadata_cache.json`` with flock, stamping version."""
        data["version"] = _METADATA_CACHE_VERSION
        cache_path = os.path.join(self._runtime_dir, "metadata_cache.json")
        self._locked_write(cache_path, data)

    # ------------------------------------------------------------------
    # Firmware cache
    # ------------------------------------------------------------------

    def load_firmware_cache(self) -> dict:
        """Read ``firmware_cache.json`` with version check.

        Returns an empty cache dict if the file is missing, corrupt, or
        has a version mismatch (stale/incompatible schema).
        """
        cache_path = os.path.join(self._runtime_dir, "firmware_cache.json")
        try:
            with open(cache_path) as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return {"version": _FIRMWARE_CACHE_VERSION}
            if loaded.get("version") != _FIRMWARE_CACHE_VERSION:
                return {"version": _FIRMWARE_CACHE_VERSION}
            return loaded
        except (FileNotFoundError, json.JSONDecodeError):
            return {"version": _FIRMWARE_CACHE_VERSION}

    def save_firmware_cache(self, data: dict) -> None:
        """Atomic write of *data* to ``firmware_cache.json`` with flock, stamping version."""
        data["version"] = _FIRMWARE_CACHE_VERSION
        cache_path = os.path.join(self._runtime_dir, "firmware_cache.json")
        self._locked_write(cache_path, data)

    # ------------------------------------------------------------------
    # Save-sync state
    # ------------------------------------------------------------------

    def save_save_sync_state(self, data: dict) -> None:
        """Atomic write of *data* to ``save_sync_state.json`` with flock.

        Does not stamp a version — ``save_sync_state.json`` carries its
        own ``version`` key managed by the service-side migration layer
        (``StateService._migrate_loaded_state``). Adapters do dumb I/O.
        """
        state_path = os.path.join(self._runtime_dir, "save_sync_state.json")
        self._locked_write(state_path, data)

    def load_save_sync_state(self) -> dict | None:
        """Read ``save_sync_state.json`` and return the raw dict.

        Returns ``None`` when the file is missing, corrupt, or not a
        JSON object — callers (``StateService.load_state``) treat that
        as "first run, keep defaults". Migrations on the returned payload
        live in the service layer.
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


class SaveSyncStatePersisterAdapter:
    """Adapter view exposing the ``SaveSyncStatePersister`` Protocol.

    Wraps a :class:`PersistenceAdapter` and forwards ``save`` / ``load``
    to its ``save_save_sync_state`` / ``load_save_sync_state`` methods.
    Lives in the adapters layer so services depend only on the Protocol,
    never on this class.
    """

    def __init__(self, persistence: PersistenceAdapter) -> None:
        self._persistence = persistence

    def save(self, data: dict) -> None:
        self._persistence.save_save_sync_state(data)

    def load(self) -> dict | None:
        return self._persistence.load_save_sync_state()


class FirmwareCachePersisterAdapter:
    """Adapter view exposing the ``FirmwareCachePersister`` Protocol.

    Wraps a :class:`PersistenceAdapter` and forwards ``save`` / ``load``
    to its ``save_firmware_cache`` / ``load_firmware_cache`` methods.
    Lives in the adapters layer so services depend only on the Protocol,
    never on this class.
    """

    def __init__(self, persistence: PersistenceAdapter) -> None:
        self._persistence = persistence

    def save(self, data: dict) -> None:
        self._persistence.save_firmware_cache(data)

    def load(self) -> dict:
        return self._persistence.load_firmware_cache()
