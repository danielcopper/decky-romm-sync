"""RomM API adapter — requires RomM >= 4.8.1.

Single adapter covering the full RomM REST surface. All methods map
directly to HTTP endpoints via RommHttpAdapter.
"""

from __future__ import annotations

import urllib.parse

from adapters.romm.http import RommHttpAdapter


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

    def heartbeat(self) -> dict:
        return self._client.request("/api/heartbeat")

    def list_platforms(self) -> list[dict]:
        return self._client.request("/api/platforms")

    def get_current_user(self) -> dict:
        return self._client.request("/api/users/me")

    # ── ROMs ──────────────────────────────────────────────────────────

    def get_rom(self, rom_id: int) -> dict:
        return self._client.request(f"/api/roms/{rom_id}")

    def list_roms(self, platform_id: int, limit: int = 50, offset: int = 0) -> dict:
        return self._client.request(f"/api/roms?platform_ids={platform_id}&limit={limit}&offset={offset}")

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict:
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
    ) -> None:
        quoted_filename = urllib.parse.quote(filename, safe="")
        self._client.download(
            f"/api/roms/{rom_id}/content/{quoted_filename}",
            dest,
            progress_callback,
        )

    def download_cover(self, cover_url: str, dest: str) -> None:
        self._client.download(cover_url, dest)

    # ── Collections ───────────────────────────────────────────────────

    def list_collections(self) -> list[dict]:
        result = self._client.request("/api/collections")
        return result if isinstance(result, list) else []

    def list_virtual_collections(self, collection_type: str) -> list[dict]:
        result = self._client.request(f"/api/collections/virtual?type={collection_type}")
        return result if isinstance(result, list) else []

    def list_roms_by_collection(self, collection_id: int, limit: int = 50, offset: int = 0) -> dict:
        return self._client.request(f"/api/roms?collection_id={collection_id}&limit={limit}&offset={offset}")

    def list_roms_by_virtual_collection(self, virtual_id: str, limit: int = 50, offset: int = 0) -> dict:
        encoded_id = urllib.parse.quote(str(virtual_id), safe="")
        return self._client.request(f"/api/roms?virtual_collection_id={encoded_id}&limit={limit}&offset={offset}")

    # ── Firmware / BIOS ───────────────────────────────────────────────

    def list_firmware(self) -> list[dict]:
        return self._client.request("/api/firmware")

    def get_firmware(self, firmware_id: int) -> dict:
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
    ) -> list[dict]:
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
    ) -> dict:
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

    def confirm_download(self, save_id: int, device_id: str) -> dict:
        return self._client.post_json(
            f"/api/saves/{save_id}/downloaded",
            {"device_id": device_id},
        )

    def get_save_metadata(self, save_id: int) -> dict:
        return self._client.request(f"/api/saves/{save_id}")

    def get_save_summary(self, rom_id: int, device_id: str | None = None) -> dict:
        query = f"/api/saves/summary?rom_id={rom_id}"
        if device_id is not None:
            query += f"&device_id={urllib.parse.quote(device_id, safe='')}"
        return self._client.request(query)

    def delete_server_saves(self, save_ids: list[int]) -> dict:
        return self._client.post_json("/api/saves/delete", {"saves": save_ids})

    # ── Devices ───────────────────────────────────────────────────────

    def register_device(self, name: str, platform: str, client: str, client_version: str) -> dict:
        return self._client.post_json(
            "/api/devices",
            {
                "name": name,
                "platform": platform,
                "client": client,
                "client_version": client_version,
            },
        )

    def list_devices(self) -> list[dict]:
        result = self._client.request("/api/devices")
        return result if isinstance(result, list) else []

    def update_device(self, device_id: str, **fields) -> dict:
        payload = {k: v for k, v in fields.items() if v is not None}
        return self._client.put_json(f"/api/devices/{urllib.parse.quote(device_id, safe='')}", payload)

    # ── Notes / Playtime ──────────────────────────────────────────────

    def get_rom_with_notes(self, rom_id: int) -> dict:
        return self._client.request(f"/api/roms/{rom_id}")

    def create_note(self, rom_id: int, data: dict) -> dict:
        return self._client.post_json(f"/api/roms/{rom_id}/notes", data)

    def update_note(self, rom_id: int, note_id: int, data: dict) -> dict:
        return self._client.put_json(f"/api/roms/{rom_id}/notes/{note_id}", data)
