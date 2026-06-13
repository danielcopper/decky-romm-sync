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
from typing import Any, Protocol

from adapters.system_clock import SystemClock

_SETTINGS_VERSION = 8
_LOCK_EXT = ".lock"


class _ClockPort(Protocol):
    """Minimal wall-clock port the persistence adapter consumes.

    Adapters must not import ``services.protocols`` (import-linter forbids
    ``adapters -> services``), so this declares the single Clock method the
    adapter needs — ``time()`` for the corrupt-file backup stamp. The
    production ``adapters.system_clock.SystemClock`` and the test
    ``FakeClock`` both satisfy it structurally.
    """

    def time(self) -> float:
        """Return the current Unix timestamp in seconds since the epoch."""
        ...


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
    clock:
        Wall-clock source for the corrupt-file backup stamp. Keyword-only;
        defaults to a real :class:`adapters.system_clock.SystemClock` so the
        many existing 3-arg construction sites need no change. Bootstrap
        passes the shared instance so the whole composition root reads one
        clock.
    """

    def __init__(
        self,
        settings_dir: str,
        runtime_dir: str,
        logger: logging.Logger,
        *,
        clock: _ClockPort | None = None,
    ) -> None:
        self._settings_dir = settings_dir
        self._runtime_dir = runtime_dir
        self._logger = logger
        self._clock: _ClockPort = clock if clock is not None else SystemClock()
        # Transient, never-persisted record of a corrupt-settings reset that
        # happened on the last ``load_settings``. ``None`` means no reset;
        # otherwise ``{"backed_up_to": <basename>}``. The frontend drains this
        # once per process via ``consume_settings_reset_notice`` to toast the
        # user; it is never written to ``settings.json``.
        self._corrupt_reset: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locked_write(self, path: str, data: dict[str, Any]) -> None:
        """Crash-safe atomic write of *data* to *path* under an exclusive lock.

        Uses the standard write-tmp → fsync(tmp) → rename → fsync(dir) recipe so
        a power loss (a real hazard on the Steam Deck) can never leave a
        truncated or empty ``settings.json``: the tmp file's bytes are forced to
        disk *before* the rename, and the directory entry the rename creates is
        forced to disk *after* it. The directory fsync is best-effort — on the
        rare filesystem where it raises, the error is logged and swallowed so it
        never fails an otherwise-successful write.
        """
        dirname = os.path.dirname(path)
        os.makedirs(dirname, exist_ok=True)
        tmp_path = path + ".tmp"
        lock_fd = os.open(path + _LOCK_EXT, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
                raise
            # fsync the directory so the rename's directory-entry update is
            # durable. Best-effort: some filesystems reject a dir fsync.
            try:
                dir_fd = os.open(dirname, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                self._logger.debug("directory fsync after settings write failed (non-fatal): %s", exc)
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

        A missing file is a legitimate first run — defaults are returned
        silently with no backup and no reset flag. An *unparseable* file is
        the data-loss hazard: rather than silently returning defaults (which
        the immediate bootstrap save would then write over the corrupt file,
        destroying the user's server URL, token, and selections), the
        unparseable file is backed up to ``settings.json.corrupt-<ts>`` and a
        transient :attr:`_corrupt_reset` flag is set so the frontend can toast
        the user. The error is logged loudly. If the backup rename itself
        fails, the error is logged and defaults are still returned so boot
        never crashes.
        """
        settings_path = os.path.join(self._settings_dir, "settings.json")
        try:
            with open(settings_path) as f:
                settings = json.load(f)
        except FileNotFoundError:
            # Legitimate first run: no file, no backup, no reset flag.
            settings = {}
        except json.JSONDecodeError:
            self._logger.error("settings.json is corrupt/unparseable — backing up and resetting to defaults")
            self._quarantine_corrupt_settings(settings_path)
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

    def _quarantine_corrupt_settings(self, settings_path: str) -> None:
        """Rename an unparseable ``settings.json`` aside and record the reset.

        The backup name is ``settings.json.corrupt-<ts>`` where ``<ts>`` is the
        injected clock's epoch seconds (an int — filesystem-safe, no ``:``), so
        the original bytes survive for manual recovery while the immediate
        bootstrap save writes fresh defaults to ``settings.json``. If that name
        is already taken (two corruptions in the same wall-clock second, or a
        clock rollback onto an existing stamp) a ``-<n>`` suffix disambiguates
        so an older backup is never clobbered. Sets :attr:`_corrupt_reset` so
        the frontend can toast the user once. A failed rename (e.g. perms) is
        logged and swallowed — boot must not crash.
        """
        stamp = int(self._clock.time())
        backup_name = f"settings.json.corrupt-{stamp}"
        backup_path = os.path.join(self._settings_dir, backup_name)
        n = 1
        while os.path.exists(backup_path):
            backup_name = f"settings.json.corrupt-{stamp}-{n}"
            backup_path = os.path.join(self._settings_dir, backup_name)
            n += 1
        try:
            os.replace(settings_path, backup_path)
        except OSError as exc:
            self._logger.error("could not back up corrupt settings.json to %s: %s", backup_name, exc)
            return
        self._corrupt_reset = {"backed_up_to": backup_name}

    def consume_settings_reset_notice(self) -> dict[str, Any]:
        """Drain the one-shot corrupt-settings-reset notice for the frontend.

        Returns ``{"reset": bool, "backed_up_to": str | None}`` and clears the
        transient flag so the toast fires at most once per process. On a clean
        boot (no corruption) returns ``{"reset": False, "backed_up_to": None}``.
        """
        notice = self._corrupt_reset
        self._corrupt_reset = None
        if notice is None:
            return {"reset": False, "backed_up_to": None}
        return {"reset": True, "backed_up_to": notice["backed_up_to"]}

    def save_settings(self, data: dict[str, Any]) -> None:
        """Atomic write of *data* to ``settings.json`` with flock, stamping version.

        The version stamp never *down*-stamps: ``data["version"]`` is set to
        ``max(stored_version, _SETTINGS_VERSION)``. A file written by a newer
        plugin (stored version > current) is preserved as-is so a later
        re-upgrade does not re-run migrations against down-stamped data;
        an absent or older version is stamped up to ``_SETTINGS_VERSION``.
        A non-numeric stored version (e.g. a hand-edited ``"abc"``) coerces to
        ``0`` rather than raising — the stamp must never crash the boot-time save.
        """
        try:
            stored = int(data.get("version", 0) or 0)
        except (TypeError, ValueError):
            stored = 0
        data["version"] = max(stored, _SETTINGS_VERSION)
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
