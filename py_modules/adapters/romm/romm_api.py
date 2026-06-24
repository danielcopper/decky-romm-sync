"""RomM API adapter — requires RomM >= 4.8.1.

Single adapter covering the full RomM REST surface. All methods map
directly to HTTP endpoints via RommHttpAdapter.
"""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any

from lib.errors import RommNotFoundError

if TYPE_CHECKING:
    from adapters.romm.http import RommHttpAdapter

# Scopes requested for the minted Client API Token. Deliberately excludes
# ``me.write`` so the token itself cannot mint or delete tokens — that
# stays a Basic-auth-only operation.
_TOKEN_SCOPES = [
    "me.read",
    "platforms.read",
    "roms.read",
    "collections.read",
    "firmware.read",
    "assets.read",
    "devices.read",
    "assets.write",
    "devices.write",
    "roms.user.write",
]


class RommApiAdapter:
    """Concrete RomM API adapter for RomM >= 4.8.1."""

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

    def list_roms(self, platform_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self._client.request(f"/api/roms?platform_ids={platform_id}&limit={limit}&offset={offset}")

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict[str, Any]:
        quoted_after = urllib.parse.quote(updated_after)
        return self._client.request(
            f"/api/roms?platform_ids={platform_id}&limit={limit}&offset={offset}&updated_after={quoted_after}"
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

    def download_cover(self, cover_url: str, dest: str) -> None:
        self._client.download(cover_url, dest)

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

    def list_roms_by_collection(self, collection_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self._client.request(f"/api/roms?collection_id={collection_id}&limit={limit}&offset={offset}")

    def list_roms_by_virtual_collection(self, virtual_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        encoded_id = urllib.parse.quote(str(virtual_id), safe="")
        return self._client.request(f"/api/roms?virtual_collection_id={encoded_id}&limit={limit}&offset={offset}")

    def list_roms_by_smart_collection(self, smart_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self._client.request(f"/api/roms?smart_collection_id={smart_id}&limit={limit}&offset={offset}")

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

    # ── Notes / Playtime ──────────────────────────────────────────────

    def get_rom_with_notes(self, rom_id: int) -> dict[str, Any]:
        return self._client.request(f"/api/roms/{rom_id}")

    def create_note(self, rom_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self._client.post_json(f"/api/roms/{rom_id}/notes", data)

    def update_note(self, rom_id: int, note_id: int, data: dict[str, Any]) -> dict[str, Any]:
        return self._client.put_json(f"/api/roms/{rom_id}/notes/{note_id}", data)

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
