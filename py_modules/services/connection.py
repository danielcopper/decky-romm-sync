"""ConnectionService — RomM server reachability, version gate, and token auth.

Owns the ``test_connection`` reachability flow and the Client API Token
lifecycle: ``establish_token`` mints a scoped token from a one-time
username/password and discards the credentials, ``establish_user_token``
validates and stores a token the user pasted (the OIDC path, which has no
password to mint from), and ``migrate_legacy_credentials`` upgrades a
stored-password install to a token on startup. Pure I/O happens through the ``RommConnectionApi``
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
from lib.errors import RommAuthError, RommForbiddenError, error_response
from lib.list_result import ErrorCode
from lib.url_host import is_valid_server_url, normalize_origin, same_origin

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        DeviceForgetFn,
        PlaytimeScopeNoticeClearFn,
        RommConnectionApi,
        SettingsPersister,
    )


_NO_SERVER_URL_MESSAGE = "No server URL configured"

_FORBIDDEN_TOKEN_MESSAGE = (
    "Your RomM account cannot create API tokens — ask your admin to grant "
    "token permissions or use an account with a higher role."
)

# Pasted-token validation (``establish_user_token``): a 401 means the token
# string itself is wrong or was revoked; a 403 means the token authenticates
# but lacks a scope the validation probe needs (``me.read`` — see the docs for
# the full scope list the plugin requires).
_USER_TOKEN_INVALID_MESSAGE = "The API token is invalid or has been revoked. Create a new token in RomM and try again."
_USER_TOKEN_SCOPE_MESSAGE = (
    "The API token is missing required permissions (scopes). Grant the scopes listed in the plugin docs and try again."
)


@dataclass(frozen=True)
class ConnectionServiceConfig:
    """Frozen wiring bundle handed to ``ConnectionService.__init__``.

    Carries the live settings dict, the RomM API Protocol, the settings
    persister, the runtime infrastructure (event loop, logger), the
    minimum-version policy tuple, the device-forget callback fired on a
    server-origin change, and the playtime scope-notice clear callback fired on
    a fresh sign-in. Bundled here so the ctor stays within the S107 parameter
    budget and so the version constant stays declared once at the plugin
    entrypoint.
    """

    settings: dict[str, Any]
    romm_api: RommConnectionApi
    settings_persister: SettingsPersister
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    min_required_version: tuple[int, ...]
    forget_device: DeviceForgetFn
    clear_playtime_scope_notice: PlaytimeScopeNoticeClearFn


class ConnectionService:
    """Heartbeat, version gate, auth probe, and Client API Token lifecycle."""

    def __init__(self, *, config: ConnectionServiceConfig) -> None:
        self._settings = config.settings
        self._romm_api = config.romm_api
        self._settings_persister = config.settings_persister
        self._loop = config.loop
        self._logger = config.logger
        self._min_required_version = config.min_required_version
        self._forget_device = config.forget_device
        self._clear_playtime_scope_notice = config.clear_playtime_scope_notice

    async def test_connection(self) -> dict[str, Any]:
        """Probe the configured server and return a frontend-shaped result dict.

        The result dict always carries ``success`` and ``message``. On
        failure, ``reason`` classifies the cause (``config_error`` when
        the server URL is unset or no token has been minted yet,
        :data:`ErrorCode.VERSION_ERROR`, or an
        :func:`lib.errors.error_response` slug). On success or version
        failure, ``romm_version`` carries the detected server version when
        the heartbeat exposed one.
        """
        if not self._settings.get("romm_url"):
            return {"success": False, "reason": "config_error", "message": _NO_SERVER_URL_MESSAGE}

        if not self._settings.get("romm_api_token"):
            return {
                "success": False,
                "reason": "config_error",
                "message": "Not signed in — sign in to RomM first",
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
            if resp["reason"] != ErrorCode.AUTH_FAILED.value:
                resp["message"] = f"Server reachable but API request failed: {resp['message']}"
            return resp

        version_error = self._version_gate_error(version)
        if version_error is not None:
            return version_error

        return self._success_result(version)

    async def probe_reachability(self) -> dict[str, Any]:
        """Probe server reachability with a fresh heartbeat — no version assertion, no cache.

        A bare connectivity check the launch path runs at the decision point
        instead of trusting a possibly-stale cached connection state. Fires a
        SINGLE-attempt, short-timeout ``/api/heartbeat`` request
        (``heartbeat_once``) on the executor thread — NOT the retrying
        ``heartbeat`` the version/sync flows use — so an offline verdict returns
        in ~3s instead of 3 retries x 30s. Reports ``{"online": True}`` on
        success, ``{"online": False}`` on any exception (transport error, auth
        failure, malformed response). It deliberately does NOT gate on version
        or persist anything — it answers only "can we reach the server right
        now?".
        """
        try:
            await self._loop.run_in_executor(None, self._romm_api.heartbeat_once)
        except Exception as e:
            # Logged so a genuine code/wiring bug in the heartbeat path is
            # diagnosable rather than masquerading silently as "server offline".
            self._logger.debug(f"probe_reachability heartbeat failed: {e}")
            return {"online": False}
        return {"online": True}

    async def establish_token(
        self,
        romm_url: str,
        username: str,
        password: str,
        allow_insecure_ssl: bool | None = None,
    ) -> dict[str, Any]:
        """Mint a Client API Token from one-time credentials and store it.

        Validates the server URL, probes the server, and only on a successful
        mint commits ``romm_url`` / SSL flag / token / id / minting origin to
        disk in a single atomic save. Nothing is persisted before the mint
        succeeds, so a failed sign-in leaves the previous working URL and token
        untouched (#1015). The candidate URL is held only in memory while
        probing, and the old token is cleared in memory first so it never
        leaks to the candidate host (#1039). The username/password are never
        persisted. On a successful sign-in whose origin differs from the
        previous token's, the registered server device id is forgotten
        (best-effort) — it is bound to its minting origin and would otherwise
        404 against the new server's negotiate. Returns the same ``success`` /
        ``reason`` / ``message`` shape as :meth:`test_connection`.
        """
        if not romm_url:
            return {"success": False, "reason": "config_error", "message": _NO_SERVER_URL_MESSAGE}
        trimmed = romm_url.strip()
        if not is_valid_server_url(trimmed):
            return {"success": False, "reason": "config_error", "message": "Enter a valid http(s):// server URL"}

        snapshot = self._snapshot_auth_state()
        old_token_id = snapshot["romm_api_token_id"]
        old_token_origin = snapshot["romm_api_token_origin"]
        old_token_source = snapshot["romm_api_token_source"]

        # Hold the candidate URL in memory only; clear the stored token so the
        # version probe never carries the old server's bearer to this host (and
        # the auth-header origin guard stays quiet during sign-in).
        self._settings["romm_url"] = trimmed
        if allow_insecure_ssl is not None:
            self._settings["romm_allow_insecure_ssl"] = bool(allow_insecure_ssl)
        self._settings["romm_api_token"] = None
        self._settings["romm_api_token_id"] = None
        self._settings["romm_api_token_origin"] = None

        try:
            version = await self._loop.run_in_executor(None, self._probe_version)
        except Exception as e:
            self._restore_auth_state(snapshot)
            self._romm_api.set_version(None)
            return error_response(e)

        version_error = self._version_gate_error(version)
        if version_error is not None:
            self._restore_auth_state(snapshot)
            return version_error

        # #1309: never DELETE a user-supplied token — it belongs to the user, not
        # this device (and it has no stored id to delete by anyway).
        # #1038: only replay the DELETE against the same server the old token
        # was minted on. A different (or unknown) origin would delete an
        # unrelated token on the new host, so skip it.
        if old_token_source != "user" and old_token_id is not None:
            if same_origin(old_token_origin, trimmed):
                await self._delete_existing_token(username, password, old_token_id)
            else:
                self._logger.info(
                    "Previous token was minted for a different/unknown server; "
                    "skipping DELETE to avoid replaying it against the current server"
                )

        try:
            minted = await self._loop.run_in_executor(None, self._mint, username, password)
        except RommForbiddenError:
            # 403 on token mint: same AUTH_FAILED slug as a 401, but a distinct
            # message — the account lacks token-creation permission (or a
            # Cloudflare bot-fight 403 at the edge), not wrong credentials.
            self._restore_auth_state(snapshot)
            return {"success": False, "reason": ErrorCode.AUTH_FAILED.value, "message": _FORBIDDEN_TOKEN_MESSAGE}
        except Exception as e:
            self._restore_auth_state(snapshot)
            return error_response(e)

        raw_token = minted.get("raw_token")
        token_id = minted.get("id")
        if not raw_token or token_id is None:
            self._restore_auth_state(snapshot)
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "RomM did not return a usable token",
            }

        try:
            self._persist_token(raw_token, token_id, origin=normalize_origin(trimmed), source="minted")
        except Exception as e:
            self._restore_auth_state(snapshot)
            return error_response(e)

        await self._forget_device_on_origin_change(old_token_origin, trimmed)
        await self._clear_playtime_scope_notice_best_effort()

        return self._success_result(version)

    async def establish_user_token(
        self,
        romm_url: str,
        token: str,
        allow_insecure_ssl: bool | None = None,
    ) -> dict[str, Any]:
        """Store a user-supplied Client API Token after validating it.

        The sign-in path for OIDC accounts, which have no password to mint a
        token from: the user creates a token in RomM's web UI and pastes it
        here. Structurally mirrors :meth:`establish_token` (validate URL → probe
        version → gate → validate credential → persist on success only, rolling
        the in-memory auth state back on any failure), but the credential is the
        pasted token rather than a fresh mint, so there is no mint and no
        server-side DELETE of any prior token. The token is host-bound to the
        entered URL's origin during probing so the auth-header guard attaches it
        and the old server's bearer never leaks to the candidate host. The token
        is validated with an authenticated ``/api/users/me`` probe — a 401 means
        the token is invalid/revoked, a 403 means it authenticates but lacks a
        required scope. The token value is never logged. Returns the same
        ``success`` / ``reason`` / ``message`` shape as :meth:`test_connection`.
        """
        if not romm_url:
            return {"success": False, "reason": "config_error", "message": _NO_SERVER_URL_MESSAGE}
        trimmed = romm_url.strip()
        if not is_valid_server_url(trimmed):
            return {"success": False, "reason": "config_error", "message": "Enter a valid http(s):// server URL"}
        trimmed_token = token.strip()
        if not trimmed_token:
            return {"success": False, "reason": "config_error", "message": "Enter your RomM API token"}

        snapshot = self._snapshot_auth_state()
        old_token_origin = snapshot["romm_api_token_origin"]

        # Hold the candidate URL + pasted token in memory only; stamp the origin
        # so the auth-header guard attaches this token (and not the old server's)
        # to the validation probe. Nothing is persisted until validation passes.
        self._settings["romm_url"] = trimmed
        if allow_insecure_ssl is not None:
            self._settings["romm_allow_insecure_ssl"] = bool(allow_insecure_ssl)
        self._settings["romm_api_token"] = trimmed_token
        self._settings["romm_api_token_id"] = None
        self._settings["romm_api_token_origin"] = normalize_origin(trimmed)
        self._settings["romm_api_token_source"] = "user"

        try:
            version = await self._loop.run_in_executor(None, self._probe_version)
        except Exception as e:
            self._restore_auth_state(snapshot)
            self._romm_api.set_version(None)
            return error_response(e)

        version_error = self._version_gate_error(version)
        if version_error is not None:
            self._restore_auth_state(snapshot)
            return version_error

        try:
            await self._loop.run_in_executor(None, self._romm_api.get_current_user)
        except RommAuthError:
            self._restore_auth_state(snapshot)
            return {"success": False, "reason": ErrorCode.AUTH_FAILED.value, "message": _USER_TOKEN_INVALID_MESSAGE}
        except RommForbiddenError:
            self._restore_auth_state(snapshot)
            return {"success": False, "reason": ErrorCode.AUTH_FAILED.value, "message": _USER_TOKEN_SCOPE_MESSAGE}
        except Exception as e:
            self._restore_auth_state(snapshot)
            return error_response(e)

        try:
            self._persist_token(trimmed_token, None, origin=normalize_origin(trimmed), source="user")
        except Exception as e:
            self._restore_auth_state(snapshot)
            return error_response(e)

        await self._forget_device_on_origin_change(old_token_origin, trimmed)
        await self._clear_playtime_scope_notice_best_effort()

        return self._success_result(version)

    async def _clear_playtime_scope_notice_best_effort(self) -> None:
        """Clear the durable playtime read-scope notice after a fresh sign-in.

        The freshly minted token carries ``roms.user.read`` (#1280), so any "sign
        in again to enable cross-device playtime" notice a prior 403 raised is now
        stale. Best-effort + local-only: a clear failure must never turn a
        successful sign-in into a failure.
        """
        try:
            await self._loop.run_in_executor(None, self._clear_playtime_scope_notice)
        except Exception as e:
            self._logger.warning(f"Could not clear playtime scope notice after sign-in: {e}")

    async def _forget_device_on_origin_change(self, old_token_origin: str | None, new_url: str) -> None:
        """Forget the registered device id when the sign-in origin changed.

        A device id is bound to the origin it was minted against, so a server
        switch must drop it (RomM's negotiate hard-404s a foreign id) and let
        the next sync re-register. Called on the success path only — a failed
        sign-in keeps the still-current server's id. Best-effort: the new token
        is already valid, so a failed local clear must not turn a successful
        sign-in into a failure.
        """
        if same_origin(old_token_origin, new_url):
            return
        try:
            await self._loop.run_in_executor(None, self._forget_device)
        except Exception as e:
            self._logger.warning(f"Could not clear device id after origin change: {e}")

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
            self._persist_token(
                raw_token,
                token_id,
                origin=normalize_origin(self._settings.get("romm_url") or ""),
                source="minted",
            )
        except Exception as e:
            self._logger.warning(f"Legacy credential migration failed: {e}")
            return
        self._logger.info("Migrated legacy credentials to a Client API Token")

    # ── Internal helpers ─────────────────────────────────────────────────

    _AUTH_STATE_KEYS = (
        "romm_url",
        "romm_allow_insecure_ssl",
        "romm_api_token",
        "romm_api_token_id",
        "romm_api_token_origin",
        "romm_api_token_source",
    )

    def _snapshot_auth_state(self) -> dict[str, Any]:
        """Capture the in-memory auth-relevant settings for restore-on-failure."""
        return {key: self._settings.get(key) for key in self._AUTH_STATE_KEYS}

    def _restore_auth_state(self, snapshot: dict[str, Any]) -> None:
        """Restore the in-memory auth-relevant settings from *snapshot*.

        Disk is untouched (no ``save_settings``), so a failed sign-in rolls the
        live dict back to the previous working URL + token without clobbering
        the on-disk state.
        """
        for key, value in snapshot.items():
            self._settings[key] = value

    def _persist_token(self, raw_token: str, token_id: int | None, *, origin: str | None, source: str) -> None:
        """Persist a token and retire the legacy credentials.

        Stores the token + its id (``None`` for a user-supplied token, which
        carries no server id) + its *origin* + its *source* provenance
        (``"minted"`` or ``"user"``), drops any stored ``romm_user`` /
        ``romm_pass`` (a token fully supersedes them — nothing reads the stored
        credentials at runtime once a token exists), and saves.
        """
        self._settings["romm_api_token"] = raw_token
        self._settings["romm_api_token_id"] = token_id
        self._settings["romm_api_token_origin"] = origin
        self._settings["romm_api_token_source"] = source
        self._settings.pop("romm_user", None)
        self._settings.pop("romm_pass", None)
        self._settings_persister.save_settings()

    def _probe_version(self) -> str | None:
        """Heartbeat the server, cache the detected version, and return it.

        Runs on the executor thread. ``SYSTEM.VERSION`` is server-controlled and
        untrusted: a missing, non-string, or otherwise malformed value yields
        ``None`` (and clears the cached version), so the version gate downstream
        only ever sees a real version string or ``None``.
        """
        heartbeat = self._romm_api.heartbeat()
        version: str | None = None
        with contextlib.suppress(AttributeError, TypeError):
            raw = heartbeat.get("SYSTEM", {}).get("VERSION")
            if isinstance(raw, str):
                version = raw
        self._romm_api.set_version(version)
        if version:
            self._logger.info(f"RomM server version: {version}")
        return version

    def _version_gate_error(self, version: str | None) -> dict[str, Any] | None:
        """Return a :data:`ErrorCode.VERSION_ERROR` dict when *version* is below the minimum.

        ``development`` builds and an absent version bypass the gate.
        """
        if version and version != "development" and not meets_min_version(version, self._min_required_version):
            min_str = ".".join(str(v) for v in self._min_required_version)
            return {
                "success": False,
                "reason": ErrorCode.VERSION_ERROR.value,
                "message": (
                    f"This plugin requires RomM {min_str} or newer. "
                    f"Your server is running {version}. "
                    "Please update your RomM server to continue using this plugin."
                ),
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

    async def _delete_existing_token(self, username: str, password: str, token_id: int) -> None:
        """Best-effort delete of the token this device previously minted on this server.

        Runs on the executor thread via Basic auth (unaffected by the cleared
        bearer). Failures are logged and swallowed so re-establishing auth
        never fails on a stale-token cleanup. The caller is responsible for the
        same-origin guard (#1038) — this only fires the request.
        """
        try:
            await self._loop.run_in_executor(None, self._delete, username, password, token_id)
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
