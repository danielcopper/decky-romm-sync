"""Device registration with the RomM server.

Owns the calls that establish (and refresh) this device's identity on
the RomM server so save-sync can attribute uploads and filter
server-side per-slot views. Anything that creates, updates, or lists
RomM ``DeviceSaveSync`` rows lives here. Per-rom sync orchestration
and the file-level transfer logic live elsewhere in the package
(:mod:`services.saves.sync_engine.engine` and
:mod:`services.saves.sync_engine.matrix`).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        DebugLogger,
        HostnameProvider,
        RetryStrategy,
        RommSyncApi,
    )
    from services.saves.state import StateService


class DeviceRegistry:
    """Device registration entry points for every save-sync flow.

    Co-locates the device-identity fallback used by every sync callable
    (pre_launch_sync, post_exit_sync, sync_rom_saves, sync_all_saves)
    when ``device_id`` is missing. Kept beside SyncEngine because the
    fallback is reached from inside those callables and pushing it out
    to a peer service would require an extra constructor callback.

    The async entry points take ``loop`` and ``hostname_provider`` per
    call so :class:`SyncEngine` can pass its live (test-rebindable)
    attributes without having to thread reassignments through this
    sub-module.
    """

    def __init__(
        self,
        *,
        state_svc: StateService,
        romm_api: RommSyncApi,
        retry: RetryStrategy,
        logger: logging.Logger,
        log_debug: DebugLogger,
        plugin_version: str,
    ) -> None:
        self._state_svc = state_svc
        self._romm_api = romm_api
        self._retry = retry
        self._logger = logger
        self._log_debug = log_debug
        self._plugin_version = plugin_version

    async def ensure_device_registered(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        hostname_provider: HostnameProvider,
    ) -> dict:
        """Ensure this device is registered with the RomM server for save sync tracking."""
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "device_id": "", "device_name": "", "disabled": True}

        # Probe the RomM version when it has not been observed yet. Device
        # registration is the entrypoint reached from background launchers
        # that never call test_connection first, so the version on the API
        # adapter would otherwise stay None and version-gated server-side
        # features couldn't be enabled until the next manual connection
        # test. Probe failures are non-fatal — the registration call below
        # still proceeds and the adapter just retains its current version.
        if not self._romm_api.get_version():
            try:
                heartbeat = await loop.run_in_executor(None, self._romm_api.heartbeat)
                with contextlib.suppress(AttributeError, TypeError):
                    version = heartbeat.get("SYSTEM", {}).get("VERSION")
                    if version:
                        self._romm_api.set_version(version)
            except Exception as e:
                self._logger.debug(f"ensure_device_registered: version probe failed (non-fatal): {e}")

        sync_state = self._state_svc.state
        has_device_id = sync_state.device_id
        has_server_id = sync_state.server_device_id
        if has_device_id and has_server_id:
            server_id_str = str(has_server_id)
            with contextlib.suppress(Exception):
                await loop.run_in_executor(
                    None,
                    lambda: self._romm_api.update_device(server_id_str, client_version=self._plugin_version),
                )
            return {
                "success": True,
                "device_id": sync_state.device_id,
                "device_name": sync_state.device_name or "",
                "server_device_id": has_server_id,
            }

        hostname = hostname_provider.get()

        try:
            result = await loop.run_in_executor(
                None,
                lambda: self._romm_api.register_device(
                    name=hostname,
                    platform="linux",
                    client="decky-romm-sync",
                    client_version=self._plugin_version,
                ),
            )
            server_device_id = result.get("id") or result.get("device_id")
            if server_device_id:
                sync_state.device_id = str(server_device_id)
                sync_state.device_name = hostname
                sync_state.server_device_id = str(server_device_id)
                self._state_svc.save_state()
                self._logger.info(f"Device registered with server: {server_device_id} ({hostname})")
                return {
                    "success": True,
                    "device_id": str(server_device_id),
                    "device_name": hostname,
                    "server_device_id": str(server_device_id),
                }
        except Exception as e:
            self._logger.warning(f"Server device registration failed: {e}")

        return {"success": False, "device_id": "", "device_name": "", "error": "registration_failed"}

    async def list_devices(self, *, loop: asyncio.AbstractEventLoop) -> dict:
        """List all devices registered with the RomM server for this user."""
        if not self._state_svc.is_save_sync_enabled():
            return {"success": False, "devices": [], "disabled": True}
        try:
            own_id = self._state_svc.get_server_device_id()
            devices = await loop.run_in_executor(
                None,
                lambda: self._retry.with_retry(lambda: self._romm_api.list_devices()),
            )
            own_id_str = str(own_id or "")
            enriched = [
                {**d, "is_current_device": bool(own_id_str) and (str(d.get("id") or "")) == own_id_str} for d in devices
            ]
            return {"success": True, "devices": enriched}
        except Exception as e:
            self._log_debug(f"list_devices failed: {e}")
            return {"success": False, "devices": [], "error": "list_failed"}
