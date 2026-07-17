"""RomM API adapter — requires RomM >= 4.9.0.

Single adapter covering the full RomM REST surface. All methods map
directly to HTTP endpoints via RommHttpAdapter.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import TYPE_CHECKING, Any

from lib.errors import (
    PairingCodeInvalidError,
    PairingCodeOwnerDisabledError,
    PairingCodeRateLimitedError,
    PairingCodeTokenGoneError,
    RommForbiddenError,
    RommNotFoundError,
    RommServerError,
)
from lib.romm_paging import LIST_PAGE_SIZE

if TYPE_CHECKING:
    from models.cover import CoverRevalidation
    from models.play_sessions import PlaySessionIngestEntry, PlaySessionIngestResponse
    from models.sync import (
        ClientSaveState,
        SyncCompleteResponse,
        SyncNegotiateResponse,
    )

    from adapters.romm.http import RommHttpAdapter

_logger = logging.getLogger(__name__)

# ``/api/play-sessions`` pagination guard: stop after this many pages so a server
# that never returns a short page (e.g. ignores ``offset``) can't loop forever.
_MAX_PLAY_SESSION_PAGES = 50

# RomM computes an unused character index and a filter-values aggregation on
# every ``/api/roms`` list request unless disabled. The plugin reads only
# ``items`` / ``total`` from the list endpoints, so both are turned off to skip
# that server-side work. Appended to every list-endpoint query string.
_LIST_AGGREGATIONS_DISABLED = "&with_char_index=false&with_filter_values=false"

# Public pairing-code exchange endpoint (unauthenticated — the code is the credential).
_PAIRING_CODE_ENDPOINT = "/api/client-tokens/exchange"
# Case-folded needle identifying the exchange 404 raised when the token a pairing
# code was minted for was deleted between pairing and exchange — distinct from the
# invalid/expired/used-code 404, which the two share only by their FastAPI
# ``detail`` string. Source string "Token no longer exists": RomM backend
# ``client_tokens.py`` (verified against RomM 4.9.0 and 4.9.2). Matched by
# case-insensitive substring containment so a wording/casing/punctuation tweak on
# the server doesn't silently reroute it to the invalid-code branch.
_PAIRING_TOKEN_GONE_NEEDLE = "token no longer exists"

# Scopes requested for the minted Client API Token. Deliberately excludes
# ``me.write`` so the token itself cannot mint or delete tokens — that
# stays a Basic-auth-only operation. ``roms.user.read`` is requested so the
# token can read per-user ROM data (native play-session history — #1219 /
# #1234 Phase 5); we already hold ``roms.user.write`` for the ingest POST.
_TOKEN_SCOPES = [
    "me.read",
    "platforms.read",
    "roms.read",
    "roms.user.read",
    "collections.read",
    "firmware.read",
    "assets.read",
    "devices.read",
    "assets.write",
    "devices.write",
    "roms.user.write",
]


class RommApiAdapter:
    """Concrete RomM API adapter for RomM >= 4.9.0."""

    def __init__(self, client: RommHttpAdapter) -> None:
        self._client = client
        self._version: str | None = None

    def set_version(self, version: str | None) -> None:
        """Store the detected server version string. ``None`` clears the cache."""
        self._version = version

    def get_version(self) -> str | None:
        """Return the detected server version string, or ``None`` if unset."""
        return self._version

    # ── Server / Auth ─────────────────────────────────────────────────

    # Fast-fail reachability probe: a single ~3s attempt, no retry. Keeps the
    # launch gate's "offline" verdict snappy instead of waiting through the
    # retrying heartbeat (3 attempts + up to ~90s of accumulated timeouts).
    _PROBE_TIMEOUT_SECONDS = 3

    def heartbeat(self) -> dict[str, Any]:
        return self._client.request("/api/heartbeat")

    def heartbeat_once(self) -> dict[str, Any]:
        """Single-attempt, short-timeout heartbeat for the reachability probe.

        Unlike :meth:`heartbeat` (3 retries, 30s/attempt), this fires one
        ``/api/heartbeat`` GET with a ~3s timeout so an offline verdict returns
        fast. The retrying :meth:`heartbeat` stays the path the version/sync
        flows use.
        """
        return self._client.request_once("/api/heartbeat", timeout=self._PROBE_TIMEOUT_SECONDS)

    def list_platforms(self) -> list[dict[str, Any]]:
        return self._client.request("/api/platforms")

    def get_current_user(self) -> dict[str, Any]:
        return self._client.request("/api/users/me")

    # ── ROMs ──────────────────────────────────────────────────────────

    def get_rom(self, rom_id: int) -> dict[str, Any]:
        return self._client.request(f"/api/roms/{rom_id}")

    def list_roms(self, platform_id: int, limit: int = LIST_PAGE_SIZE, offset: int = 0) -> dict[str, Any]:
        return self._client.request(
            f"/api/roms?platform_ids={platform_id}&limit={limit}&offset={offset}{_LIST_AGGREGATIONS_DISABLED}"
        )

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict[str, Any]:
        quoted_after = urllib.parse.quote(updated_after)
        return self._client.request(
            f"/api/roms?platform_ids={platform_id}&limit={limit}&offset={offset}"
            f"&updated_after={quoted_after}{_LIST_AGGREGATIONS_DISABLED}"
        )

    def download_rom_content(
        self,
        rom_id: int,
        filename: str,
        dest: str,
        progress_callback=None,
        *,
        resume: bool = False,
        on_meta: Any = None,
    ) -> None:
        quoted_filename = urllib.parse.quote(filename, safe="")
        self._client.download(
            f"/api/roms/{rom_id}/content/{quoted_filename}",
            dest,
            progress_callback,
            resume=resume,
            on_meta=on_meta,
        )

    def download_cover(
        self, cover_url: str, dest: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> CoverRevalidation:
        # Routes through the conditional GET (#1454): with a validator it may draw
        # a 304 (dest untouched); without one it is a plain download. Either way
        # the response's ETag/Last-Modified come back so the caller can record
        # them for the next sync's revalidation.
        return self._client.download_conditional(cover_url, dest, etag=etag, last_modified=last_modified)

    def download_cover_from_url(self, url: str, dest: str) -> None:
        self._client.download_external(url, dest)

    # ── Collections ───────────────────────────────────────────────────

    def list_collections(self) -> list[dict[str, Any]]:
        result = self._client.request("/api/collections")
        return result if isinstance(result, list) else []

    def list_virtual_collections(self, collection_type: str) -> list[dict[str, Any]]:
        result = self._client.request(f"/api/collections/virtual?type={collection_type}")
        return result if isinstance(result, list) else []

    def list_smart_collections(self) -> list[dict[str, Any]]:
        result = self._client.request("/api/collections/smart")
        return result if isinstance(result, list) else []

    def list_roms_by_collection(
        self, collection_id: int, limit: int = LIST_PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        return self._client.request(
            f"/api/roms?collection_id={collection_id}&limit={limit}&offset={offset}{_LIST_AGGREGATIONS_DISABLED}"
        )

    def list_roms_by_virtual_collection(
        self, virtual_id: str, limit: int = LIST_PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(str(virtual_id), safe="")
        return self._client.request(
            f"/api/roms?virtual_collection_id={encoded_id}&limit={limit}&offset={offset}{_LIST_AGGREGATIONS_DISABLED}"
        )

    def list_roms_by_smart_collection(
        self, smart_id: int, limit: int = LIST_PAGE_SIZE, offset: int = 0
    ) -> dict[str, Any]:
        return self._client.request(
            f"/api/roms?smart_collection_id={smart_id}&limit={limit}&offset={offset}{_LIST_AGGREGATIONS_DISABLED}"
        )

    # ── Firmware / BIOS ───────────────────────────────────────────────

    def list_firmware(self) -> list[dict[str, Any]]:
        return self._client.request("/api/firmware")

    def get_firmware(self, firmware_id: int) -> dict[str, Any]:
        return self._client.request(f"/api/firmware/{firmware_id}")

    def download_firmware(self, firmware_id: int, filename: str, dest: str) -> None:
        quoted_filename = urllib.parse.quote(filename, safe="")
        self._client.download(
            f"/api/firmware/{firmware_id}/content/{quoted_filename}",
            dest,
        )

    # ── Saves ─────────────────────────────────────────────────────────

    def list_saves(
        self,
        rom_id: int,
        *,
        device_id: str | None = None,
        slot: str | None = None,
    ) -> list[dict[str, Any]]:
        query = f"/api/saves?rom_id={rom_id}"
        if device_id is not None:
            query += f"&device_id={urllib.parse.quote(device_id, safe='')}"
        if slot is not None:
            query += f"&slot={urllib.parse.quote(slot, safe='')}"
        result = self._client.request(query)
        return result if isinstance(result, list) else []

    def upload_save(
        self,
        rom_id: int,
        file_path: str,
        emulator: str,
        save_id: int | None = None,
        *,
        device_id: str | None = None,
        slot: str | None = None,
        overwrite: bool = False,
        autocleanup_limit: int | None = None,
    ) -> dict[str, Any]:
        params = f"rom_id={rom_id}&emulator={urllib.parse.quote(emulator, safe='')}"
        if device_id is not None:
            params += f"&device_id={urllib.parse.quote(device_id, safe='')}"
        if slot is not None:
            params += f"&slot={urllib.parse.quote(slot, safe='')}"
        if overwrite:
            params += "&overwrite=true"
        if save_id is not None:
            return self._client.upload_multipart(f"/api/saves/{save_id}?{params}", file_path, method="PUT")
        # POST creates the save entry and is the only path that stacks versions —
        # PUT updates in place. RomM's autocleanup defaults OFF, so capping the
        # retained version count requires enabling it explicitly alongside the
        # limit; hence both params, and only here (POST).
        if autocleanup_limit is not None:
            params += f"&autocleanup=true&autocleanup_limit={autocleanup_limit}"
        return self._client.upload_multipart(f"/api/saves?{params}", file_path, method="POST")

    def download_save(self, save_id: int, dest_path: str) -> None:
        self._client.download(f"/api/saves/{save_id}/content", dest_path)

    def download_save_content(
        self,
        save_id: int,
        dest_path: str,
        *,
        device_id: str | None = None,
        optimistic: bool = True,
    ) -> None:
        path = f"/api/saves/{save_id}/content"
        if device_id is not None:
            opt = "true" if optimistic else "false"
            path += f"?device_id={urllib.parse.quote(device_id, safe='')}&optimistic={opt}"
        self._client.download(path, dest_path)

    def confirm_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        return self._client.post_json(
            f"/api/saves/{save_id}/downloaded",
            {"device_id": device_id},
        )

    def get_save_summary(self, rom_id: int, device_id: str | None = None) -> dict[str, Any]:
        query = f"/api/saves/summary?rom_id={rom_id}"
        if device_id is not None:
            query += f"&device_id={urllib.parse.quote(device_id, safe='')}"
        return self._client.request(query)

    def delete_server_saves(self, save_ids: list[int]) -> dict[str, Any]:
        return self._client.post_json("/api/saves/delete", {"saves": save_ids})

    # ── Sync sessions (4.9 negotiate) ─────────────────────────────────

    def negotiate_sync(self, device_id: str, saves: list[ClientSaveState]) -> SyncNegotiateResponse:
        """Open a sync session: POST this device's inventory, get the planned ops.

        The server compares *saves* (per ``(rom_id, slot)``) against its own
        state and returns a ``session_id`` plus the ``upload`` / ``download`` /
        ``conflict`` / ``no_op`` operations to execute. It detects but never
        resolves — a ``conflict`` carries no resolution directive. Opening a
        session cancels this device's prior in-flight sessions server-side.
        """
        return self._client.post_json("/api/sync/negotiate", {"device_id": device_id, "saves": saves})

    def complete_sync_session(
        self,
        session_id: int,
        *,
        operations_completed: int = 0,
        operations_failed: int = 0,
    ) -> SyncCompleteResponse:
        """Close the negotiated session, reporting how many ops ran."""
        return self._client.post_json(
            f"/api/sync/sessions/{session_id}/complete",
            {
                "operations_completed": operations_completed,
                "operations_failed": operations_failed,
            },
        )

    # ── Devices ───────────────────────────────────────────────────────

    def register_device(
        self,
        name: str,
        platform: str,
        client: str,
        client_version: str,
        hostname: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "platform": platform,
            "client": client,
            "client_version": client_version,
        }
        if hostname is not None:
            payload["hostname"] = hostname
        return self._client.post_json("/api/devices", payload)

    def list_devices(self) -> list[dict[str, Any]]:
        result = self._client.request("/api/devices")
        return result if isinstance(result, list) else []

    def update_device(self, device_id: str, **fields) -> dict[str, Any]:
        payload = {k: v for k, v in fields.items() if v is not None}
        return self._client.put_json(f"/api/devices/{urllib.parse.quote(device_id, safe='')}", payload)

    # ── Play sessions (native ingest, ADR-0018) ───────────────────────

    def ingest_play_sessions(self, device_id: str, sessions: list[PlaySessionIngestEntry]) -> PlaySessionIngestResponse:
        return self._client.post_json("/api/play-sessions", {"device_id": device_id, "sessions": sessions})

    def list_play_sessions(self, rom_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch every stored play-session row for a ROM, paginating on ``offset``.

        Loops accumulating pages until a short/empty page (fewer than ``limit``
        rows), so a ROM with more than ``limit`` sessions is fully summed by the
        caller. Accepts both a bare list and a paginated ``{items, total}``
        envelope; an unrecognized dict envelope logs a debug breadcrumb and ends
        the scan. Capped at ``_MAX_PLAY_SESSION_PAGES`` to guard against a server
        that never returns a short page.
        """
        sessions: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_PLAY_SESSION_PAGES):
            result = self._client.request(f"/api/play-sessions?rom_id={rom_id}&limit={limit}&offset={offset}")
            if isinstance(result, list):
                page = result
            elif isinstance(result, dict):
                items = result.get("items")
                if isinstance(items, list):
                    page = items
                else:
                    _logger.debug(
                        "list_play_sessions: unrecognized response envelope for rom %s (keys=%s)",
                        rom_id,
                        sorted(result.keys()),
                    )
                    break
            else:
                break
            sessions.extend(page)
            if len(page) < limit:
                break
            offset += limit
        else:
            _logger.debug(
                "list_play_sessions hit the %d-page cap for rom %s (%d rows) — possible truncation",
                _MAX_PLAY_SESSION_PAGES,
                rom_id,
                len(sessions),
            )
        return sessions

    # ── Client Tokens ─────────────────────────────────────────────────

    def mint_client_token(self, username: str, password: str, *, token_name: str) -> dict[str, Any]:
        """Mint a scoped, never-expiring Client API Token via Basic auth.

        ``username`` / ``password`` are passed straight to a one-off
        Basic-authenticated ``POST /api/client-tokens``; the minting
        identity needs ``me.write``, which the minted token deliberately
        lacks. Returns the server response including ``id`` and the
        one-time ``raw_token``.
        """
        return self._client.basic_auth_request(
            "/api/client-tokens",
            username,
            password,
            method="POST",
            data={"name": token_name, "scopes": _TOKEN_SCOPES, "expires_in": "never"},
        )

    def delete_client_token(self, username: str, password: str, *, token_id: int) -> None:
        """Delete a previously minted Client API Token via Basic auth.

        Swallows a not-found response (the token is already gone, which
        is the desired end state); any other error propagates.
        """
        try:
            self._client.basic_auth_request(
                f"/api/client-tokens/{token_id}",
                username,
                password,
                method="DELETE",
            )
        except RommNotFoundError:
            return

    def exchange_pairing_code(self, code: str) -> dict[str, Any]:
        """Exchange a short-lived RomM pairing code for a Client API Token.

        POSTs ``{"code": code}`` to the PUBLIC ``/api/client-tokens/exchange``
        endpoint with no Authorization header and no retry — the one-time code is
        itself the credential, and a replay would burn both the single-use code
        and the server-side rate limit. Returns RomM's token schema, whose
        ``raw_token`` is the freshly rotated bearer (the exchange regenerates the
        token server-side). Neither the code nor the returned token is logged.

        Failure mapping the caller branches on: a 404 for an invalid/expired/used
        code raises :class:`PairingCodeInvalidError`; a 404 whose ``detail`` says
        the token is gone raises :class:`PairingCodeTokenGoneError`; a 403 raises
        :class:`PairingCodeOwnerDisabledError`; a 429 raises
        :class:`PairingCodeRateLimitedError`. Transport failures propagate as
        their transport ``RommApiError`` subclass.
        """
        try:
            return self._client.unauthenticated_post_json(_PAIRING_CODE_ENDPOINT, {"code": code})
        except RommNotFoundError as exc:
            # Only a real string detail is inspected — a non-string / absent detail
            # falls through to the invalid/expired branch (never coerced in).
            detail = exc.detail
            if isinstance(detail, str) and _PAIRING_TOKEN_GONE_NEEDLE in detail.casefold():
                raise PairingCodeTokenGoneError("Pairing token no longer exists") from exc
            raise PairingCodeInvalidError("Pairing code is invalid or expired") from exc
        except RommForbiddenError as exc:
            raise PairingCodeOwnerDisabledError("Pairing token owner is disabled") from exc
        except RommServerError as exc:
            if exc.status_code == 429:
                raise PairingCodeRateLimitedError("Too many pairing-code exchange attempts") from exc
            raise
