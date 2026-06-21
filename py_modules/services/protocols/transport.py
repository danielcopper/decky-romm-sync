"""External system client Protocols.

Domain-oriented interfaces for the HTTP and IPC surfaces the plugin
talks to: RomM's REST API, SteamGridDB's REST API, and the Steam
client's local IPC. Each Protocol declares the semantic operations
services need; concrete implementations live in adapters and own the
raw transport (HTTP requests, file writes, Steam IPC calls).
"""

from __future__ import annotations

from typing import Any, Protocol


class SteamConfigStore(Protocol):
    """Protocol for Steam configuration operations."""

    def grid_dir(self) -> str | None: ...
    def read_shortcuts(self) -> dict[str, Any]: ...
    def write_shortcuts(self, data: dict[str, Any]) -> None: ...
    def set_steam_input_config(self, app_ids: list[int], mode: str = "default") -> None: ...
    def write_shortcut_icon(self, app_id: int, icon_bytes: bytes) -> str: ...
    def check_retroarch_input_driver(self) -> dict[str, Any] | None: ...
    def fix_retroarch_input_driver(self) -> dict[str, Any]: ...


class RommDeviceApi(Protocol):
    """RomM device registration / sync API surface."""

    def register_device(
        self,
        name: str,
        platform: str,
        client: str,
        client_version: str,
        hostname: str | None = None,
    ) -> dict[str, Any]:
        """Register this client as a sync device on the RomM server.

        ``name`` is the friendly display label. ``hostname`` is the stable
        machine-derived fingerprint the server dedupes on (``mac_address``
        OR ``hostname`` + ``platform``); when ``None`` it is omitted from
        the payload and registration degrades to no-fingerprint behaviour.

        Returns device dict with id, name, created_at.
        """
        ...

    def list_devices(self) -> list[dict[str, Any]]:
        """List all devices registered with the RomM server for the current user.

        Returns a list of device dicts from /api/devices.
        """
        ...

    def update_device(self, device_id: str, **fields) -> dict[str, Any]:
        """Update a registered device's metadata on the RomM server.

        Currently the plugin only sends ``client_version`` via the reconciliation
        loop; the server accepts additional fields per its OpenAPI schema (name,
        platform, client, ip_address, mac_address, hostname, sync_enabled) but
        they are not exercised by this plugin.
        """
        ...


class RommFirmwareApi(Protocol):
    """RomM firmware/BIOS API surface."""

    def list_firmware(self) -> list[dict[str, Any]]:
        """Fetch all available firmware/BIOS files from the server.

        Returns a list of firmware dicts from /api/firmware.
        """
        ...

    def get_firmware(self, firmware_id: int) -> dict[str, Any]:
        """Fetch metadata for a single firmware file.

        Returns firmware dict from /api/firmware/{firmware_id}.
        """
        ...

    def download_firmware(self, firmware_id: int, filename: str, dest: str) -> None:
        """Download a firmware/BIOS file to a local path.

        Streams /api/firmware/{firmware_id}/content/{filename} to dest.
        """
        ...


class RommPlatformReader(Protocol):
    """Read-only RomM platform listing surface."""

    def list_platforms(self) -> list[dict[str, Any]]:
        """Fetch all platforms configured on the RomM server.

        Returns a list of platform dicts from /api/platforms.
        """
        ...


class RommPlaytimeApi(Protocol):
    """RomM Notes API surface for playtime tracking."""

    def get_rom_with_notes(self, rom_id: int) -> object:
        """Fetch full ROM detail including user notes.

        Returns ``object`` — the JSON payload is unvalidated at this seam, so
        the single consumer narrows it with ``isinstance`` before reading
        ``all_user_notes``. Used for playtime tracking.
        """
        ...

    def create_note(self, rom_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """Create a note on a ROM.

        Used for playtime tracking. POST /api/roms/{rom_id}/notes.
        """
        ...

    def update_note(self, rom_id: int, note_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """Update an existing note on a ROM.

        PUT /api/roms/{rom_id}/notes/{note_id}.
        """
        ...


class RommRomReader(Protocol):
    """RomM ROM-listing, ROM-download, and cover-download surface."""

    def get_rom(self, rom_id: int) -> dict[str, Any]:
        """Fetch a single ROM by ID.

        Returns the ROM dict from /api/roms/{rom_id}.
        """
        ...

    def list_roms(self, platform_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List ROMs for a platform with pagination.

        Returns paginated response {"items": [...], "total": N}
        from /api/roms filtered by platform_ids.
        """
        ...

    def list_roms_updated_after(
        self,
        platform_id: int,
        updated_after: str,
        limit: int = 1,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List ROMs updated after a given timestamp.

        Used for incremental sync to detect changes since last sync.
        Returns paginated response filtered by updated_after parameter.
        """
        ...

    def list_roms_by_collection(self, collection_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List ROMs belonging to a user-created collection with pagination.

        Returns paginated response {"items": [...], "total": N}
        from /api/roms filtered by collection_id.
        """
        ...

    def list_roms_by_virtual_collection(self, virtual_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List ROMs belonging to a virtual (autogenerated) collection with pagination.

        Returns paginated response {"items": [...], "total": N}
        from /api/roms filtered by virtual_collection_id.
        """
        ...

    def list_roms_by_smart_collection(self, smart_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List ROMs belonging to a smart (filter-defined) collection with pagination.

        Returns paginated response {"items": [...], "total": N}
        from /api/roms filtered by smart_collection_id. The server
        resolves the membership from the stored filter on every call,
        so the result reflects the current library state.
        """
        ...

    def list_collections(self) -> list[dict[str, Any]]:
        """Fetch all user-created collections from the RomM server."""
        ...

    def list_virtual_collections(self, collection_type: str) -> list[dict[str, Any]]:
        """Fetch virtual (autogenerated) collections of a given type (e.g., 'franchise')."""
        ...

    def list_smart_collections(self) -> list[dict[str, Any]]:
        """Fetch all user-defined smart collections from the RomM server.

        Smart collections are filter-defined: the server resolves
        membership at query time from a stored filter, so the returned
        ``rom_count`` reflects the current library state.
        """
        ...

    def download_rom_content(
        self,
        rom_id: int,
        filename: str,
        dest: str,
        progress_callback: Any = None,
        *,
        resume: bool = False,
        on_meta: Any = None,
    ) -> None:
        """Download a ROM file to a local destination.

        Streams /api/roms/{rom_id}/content/{filename} to dest.
        Filename is URL-encoded. Optional progress_callback for tracking.
        ``resume=True`` appends onto an existing partial transfer when the
        server honours the ``Range`` request; ``on_meta`` is invoked once with
        ``range_supported: bool`` when the response headers arrive.
        """
        ...

    def download_cover(self, cover_url: str, dest: str) -> None:
        """Download a ROM cover image to a local path.

        cover_url is the relative path from the RomM server.
        Spaces in the URL are encoded before downloading.
        """
        ...


class RommSaveApi(Protocol):
    """RomM saves API surface (list, up/download, confirm, summary, delete)."""

    def list_saves(
        self,
        rom_id: int,
        *,
        device_id: str | None = None,
        slot: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return saves for ``rom_id``; ``device_id`` enriches with device_syncs and ``slot`` filters."""
        ...

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
        """Upload (or replace) a save; raises ``RommConflictError`` on 409 unless ``overwrite=True``."""
        ...

    def download_save_content(
        self,
        save_id: int,
        dest_path: str,
        *,
        device_id: str | None = None,
        optimistic: bool = True,
    ) -> None:
        """Stream save content; ``optimistic=False`` with ``device_id`` defers the sync ack to ``confirm_download``."""
        ...

    def confirm_download(self, save_id: int, device_id: str) -> dict[str, Any]:
        """Acknowledge a deferred-sync save download (paired with ``optimistic=False``)."""
        ...

    def get_save_summary(self, rom_id: int, device_id: str | None = None) -> dict[str, Any]:
        """Return ``/api/saves/summary`` grouped by slot; ``device_id`` includes per-device sync status."""
        ...

    def download_save(self, save_id: int, dest_path: str) -> None:
        """Stream a single save file to ``dest_path`` via ``/api/saves/{save_id}/content``."""
        ...

    def delete_server_saves(self, save_ids: list[int]) -> dict[str, Any]:
        """Delete the given save ids via ``POST /api/saves/delete``."""
        ...


class RommVersion(Protocol):
    """RomM server identity & health-check surface."""

    def set_version(self, version: str | None) -> None:
        """Store the detected RomM server version string.

        Passing ``None`` clears the cached version (used when the server
        becomes unreachable and the cached version should no longer be
        trusted).
        """
        ...

    def get_version(self) -> str | None:
        """Return the detected RomM server version string, or ``None`` if unset."""
        ...

    def heartbeat(self) -> dict[str, Any]:
        """Check server connectivity and retrieve version info.

        Returns the raw heartbeat response dict from /api/heartbeat.
        """
        ...

    def get_current_user(self) -> dict[str, Any]:
        """Fetch the currently authenticated user profile.

        Returns user dict from /api/users/me.
        """
        ...


class RommTokenApi(Protocol):
    """RomM Client API Token mint/delete surface.

    The runtime Bearer token deliberately lacks ``me.write``, so both
    operations need a transient Basic-auth identity built from the
    username/password passed at call time — never from stored state.
    """

    def mint_client_token(self, username: str, password: str, *, token_name: str) -> dict[str, Any]:
        """Mint a scoped, never-expiring Client API Token.

        Returns the server response including ``id`` and the one-time
        ``raw_token``.
        """
        ...

    def delete_client_token(self, username: str, password: str, *, token_id: int) -> None:
        """Delete a Client API Token by id; a missing token is treated as success."""
        ...


class RommAchievementsApi(RommRomReader, RommVersion, Protocol):
    """RomM surface for AchievementsService — ROM detail + server identity."""


class RommConnectionApi(RommPlatformReader, RommVersion, RommTokenApi, Protocol):
    """RomM surface for ConnectionService — platform listing, version/heartbeat, token mint/delete."""


class RommLibraryApi(RommPlatformReader, RommRomReader, Protocol):
    """RomM surface for LibraryService — platforms, collections, ROM listing & downloads."""


class RommSyncApi(RommSaveApi, RommVersion, RommDeviceApi, Protocol):
    """RomM surface for save-sync — saves cluster + server identity + device registration."""


class RommApi(
    RommSyncApi,
    RommLibraryApi,
    RommConnectionApi,
    RommAchievementsApi,
    RommFirmwareApi,
    RommPlaytimeApi,
    Protocol,
):
    """Umbrella Protocol composing all per-domain RomM API Protocols."""


class SteamGridDbApi(Protocol):
    """SteamGridDB HTTP API — search, artwork fetch, key verification."""

    def request(self, path: str) -> dict[str, Any] | None:
        """Authenticated GET to SGDB API v2. Returns parsed JSON or None if no API key."""
        ...

    def download_image(self, url: str, dest_path: str) -> bool:
        """Download image from URL to dest_path with atomic write. Returns True on success."""
        ...

    def verify_api_key(self, api_key: str) -> dict[str, Any]:
        """Verify an API key against SGDB. Returns parsed JSON response.

        Raises ``lib.errors.SgdbApiError`` on non-2xx HTTP responses
        (e.g. 401/403 for an invalid key).
        """
        ...
