"""ConnectionService — RomM server reachability, version gate, and token auth.

Owns the ``test_connection`` reachability flow and the Client API Token
lifecycle: ``establish_token`` mints a scoped token from a one-time
username/password and discards the credentials, while
``migrate_legacy_credentials`` upgrades a stored-password install to a
token on startup. Pure I/O happens through the ``RommConnectionApi``
Protocol and disk writes through the ``SettingsPersister`` Protocol; this
service composes that I/O with the response-shape contract the frontend
depends on. The minimum version is injected so the policy stays anchored
at the plugin entrypoint while this service remains a pure orchestration
layer.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.version import meets_min_version
from lib.errors import RommForbiddenError, error_response

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import RommConnectionApi, SettingsPersister


_FORBIDDEN_TOKEN_MESSAGE = (
    "Your RomM account cannot create API tokens — ask your admin to grant "
    "token permissions or use an account with a higher role."
)


@dataclass(frozen=True)
class ConnectionServiceConfig:
    """Frozen wiring bundle handed to ``ConnectionService.__init__``.

    Carries the live settings dict, the RomM API Protocol, the settings
    persister, the runtime infrastructure (event loop, logger), and the
    minimum-version policy tuple. Bundled here so the ctor stays within
    the S107 parameter budget and so the version constant stays declared
    once at the plugin entrypoint.
    """

    settings: dict[str, Any]
    romm_api: RommConnectionApi
    settings_persister: SettingsPersister
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    min_required_version: tuple[int, ...]


class ConnectionService:
    """Heartbeat, version gate, auth probe, and Client API Token lifecycle."""

    def __init__(self, *, config: ConnectionServiceConfig) -> None:
        self._settings = config.settings
        self._romm_api = config.romm_api
        self._settings_persister = config.settings_persister
        self._loop = config.loop
        self._logger = config.logger
        self._min_required_version = config.min_required_version

    async def test_connection(self) -> dict[str, Any]:
        """Probe the configured server and return a frontend-shaped result dict.

        The result dict always carries ``success`` and ``message``. On
        failure, ``error_code`` classifies the cause (``config_error`` when
        the server URL is unset or no token has been minted yet,
        ``version_error``, or an :func:`lib.errors.error_response` code).
        On success or version failure, ``romm_version`` carries the
        detected server version when the heartbeat exposed one.
        """
        if not self._settings.get("romm_url"):
            return {"success": False, "message": "No server URL configured", "error_code": "config_error"}

        if not self._settings.get("romm_api_token"):
            return {
                "success": False,
                "message": "Not signed in — sign in to RomM first",
                "error_code": "config_error",
            }

        try:
            version = await self._loop.run_in_executor(None, self._probe_version)
        except Exception as e:
            self._romm_api.set_version(None)
            return error_response(e)

        try:
            await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            resp = error_response(e)
            if resp["error_code"] not in ("auth_error", "forbidden_error"):
                resp["message"] = f"Server reachable but API request failed: {resp['message']}"
            return resp

        version_error = self._version_gate_error(version)
        if version_error is not None:
            return version_error

        return self._success_result(version)

    async def establish_token(
        self,
        romm_url: str,
        username: str,
        password: str,
        allow_insecure_ssl: bool | None = None,
    ) -> dict[str, Any]:
        """Mint a Client API Token from one-time credentials and store it.

        Writes the server URL (and optional SSL flag), confirms the
        server is reachable and recent enough, deletes any token this
        device previously minted, mints a fresh scoped token, and
        persists ``romm_api_token`` / ``romm_api_token_id``. The
        username/password are never persisted. Returns the same
        ``success`` / ``message`` / ``error_code`` shape as
        :meth:`test_connection`.
        """
        if not romm_url:
            return {"success": False, "message": "No server URL configured", "error_code": "config_error"}

        self._settings["romm_url"] = romm_url
        if allow_insecure_ssl is not None:
            self._settings["romm_allow_insecure_ssl"] = bool(allow_insecure_ssl)
        try:
            self._settings_persister.save_settings()
        except Exception as e:
            return error_response(e)

        try:
            version = await self._loop.run_in_executor(None, self._probe_version)
        except Exception as e:
            self._romm_api.set_version(None)
            return error_response(e)

        version_error = self._version_gate_error(version)
        if version_error is not None:
            return version_error

        await self._delete_existing_token(username, password)

        try:
            minted = await self._loop.run_in_executor(None, self._mint, username, password)
        except RommForbiddenError:
            return {"success": False, "message": _FORBIDDEN_TOKEN_MESSAGE, "error_code": "forbidden_error"}
        except Exception as e:
            return error_response(e)

        raw_token = minted.get("raw_token")
        token_id = minted.get("id")
        if not raw_token or token_id is None:
            return {
                "success": False,
                "message": "RomM did not return a usable token",
                "error_code": "api_error",
            }

        try:
            self._persist_token(raw_token, token_id)
        except Exception as e:
            return error_response(e)

        return self._success_result(version)

    async def migrate_legacy_credentials(self) -> None:
        """Upgrade a stored-password install to a Client API Token on startup.

        When the settings carry a legacy ``romm_user`` / ``romm_pass``
        pair and no token yet, mint a token from those credentials, then
        wipe the credentials. Any failure leaves the credentials intact
        and the plugin inert — there is no Basic-auth fallback. Never
        raises; never logs the token or password.
        """
        if self._settings.get("romm_api_token"):
            return
        username = self._settings.get("romm_user")
        password = self._settings.get("romm_pass")
        if not username or not password:
            return

        try:
            minted = await self._loop.run_in_executor(None, self._mint, username, password)
        except Exception as e:
            self._logger.warning(f"Legacy credential migration failed: {e}")
            return

        raw_token = minted.get("raw_token")
        token_id = minted.get("id")
        if not raw_token or token_id is None:
            self._logger.warning("Legacy credential migration failed: RomM did not return a usable token")
            return

        try:
            self._persist_token(raw_token, token_id)
        except Exception as e:
            self._logger.warning(f"Legacy credential migration failed: {e}")
            return
        self._logger.info("Migrated legacy credentials to a Client API Token")

    # ── Internal helpers ─────────────────────────────────────────────────

    def _persist_token(self, raw_token: str, token_id: int) -> None:
        """Persist a freshly minted token and retire the legacy credentials.

        Stores the token + its id, drops any stored ``romm_user`` /
        ``romm_pass`` (a token fully supersedes them — nothing reads the
        stored credentials at runtime once a token exists), and saves.
        """
        self._settings["romm_api_token"] = raw_token
        self._settings["romm_api_token_id"] = token_id
        self._settings.pop("romm_user", None)
        self._settings.pop("romm_pass", None)
        self._settings_persister.save_settings()

    def _probe_version(self) -> str | None:
        """Heartbeat the server, cache the detected version, and return it.

        Runs on the executor thread. A missing/malformed ``SYSTEM.VERSION``
        yields ``None`` (and clears the cached version).
        """
        heartbeat = self._romm_api.heartbeat()
        version: str | None = None
        with contextlib.suppress(AttributeError, TypeError):
            version = heartbeat.get("SYSTEM", {}).get("VERSION")
        self._romm_api.set_version(version)
        if version:
            self._logger.info(f"RomM server version: {version}")
        return version

    def _version_gate_error(self, version: str | None) -> dict[str, Any] | None:
        """Return a ``version_error`` dict when *version* is below the minimum.

        ``development`` builds and an absent version bypass the gate.
        """
        if version and version != "development" and not meets_min_version(version, self._min_required_version):
            min_str = ".".join(str(v) for v in self._min_required_version)
            return {
                "success": False,
                "message": (
                    f"This plugin requires RomM {min_str} or newer. "
                    f"Your server is running {version}. "
                    "Please update your RomM server to continue using this plugin."
                ),
                "error_code": "version_error",
                "romm_version": version,
            }
        return None

    @staticmethod
    def _success_result(version: str | None) -> dict[str, Any]:
        """Build the success response, carrying ``romm_version`` when detected."""
        result: dict[str, Any] = {"success": True, "message": "Connected to RomM"}
        if version and version != "development":
            result["message"] = f"Connected to RomM {version}"
            result["romm_version"] = version
        elif version == "development":
            result["romm_version"] = version
        return result

    async def _delete_existing_token(self, username: str, password: str) -> None:
        """Best-effort delete of a token this device previously minted.

        Runs on the executor thread. Failures are logged and swallowed so
        re-establishing auth never fails on a stale-token cleanup.
        """
        old_id = self._settings.get("romm_api_token_id")
        if old_id is None:
            return
        try:
            await self._loop.run_in_executor(None, self._delete, username, password, old_id)
        except Exception as e:
            self._logger.warning(f"Failed to delete previous Client API Token: {e}")

    def _mint(self, username: str, password: str) -> dict[str, Any]:
        """Synchronous mint worker invoked on the executor thread."""
        return self._romm_api.mint_client_token(username, password, token_name=self._token_name())

    def _delete(self, username: str, password: str, token_id: int) -> None:
        """Synchronous delete worker invoked on the executor thread."""
        self._romm_api.delete_client_token(username, password, token_id=token_id)

    def _token_name(self) -> str:
        """Build the device-scoped token name from the configured device name."""
        device_name = self._settings.get("device_name") or "Steam Deck"
        return f"decky-romm-sync ({device_name})"
