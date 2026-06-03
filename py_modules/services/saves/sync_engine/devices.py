"""Device registration with the RomM server.

Owns the calls that establish (and refresh) this device's identity on
the RomM server so save-sync can attribute uploads and filter
server-side per-slot views. Anything that creates, updates, or lists
RomM ``DeviceSaveSync`` rows lives here. The single server device id is
the truly-singleton scalar persisted in ``kv_config["device_id"]``; the
device label lives in ``settings.json``. Per-rom sync orchestration and
the file-level transfer logic live elsewhere in the package
(:mod:`services.saves.sync_engine.engine` and
:mod:`services.saves.sync_engine.matrix`).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from services.saves._settings import save_sync_enabled

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import (
        DebugLogger,
        HostnameReader,
        MachineIdReader,
        RetryStrategy,
        RommSyncApi,
        SettingsPersister,
        UnitOfWorkFactory,
    )


class DeviceRegistry:
    """Device registration entry points for every save-sync flow.

    Co-locates the device-identity fallback used by every sync callable
    (pre_launch_sync, post_exit_sync, sync_rom_saves, sync_all_saves)
    when ``device_id`` is missing. Kept beside SyncEngine because the
    fallback is reached from inside those callables and pushing it out
    to a peer service would require an extra constructor callback.

    The server device id is read from and written to
    ``kv_config["device_id"]`` through the injected Unit-of-Work factory;
    the device label flows through ``settings.json``. The async entry
    points take ``loop``, ``hostname_provider``, and ``machine_id_provider``
    per call so :class:`SyncEngine` can pass its live (test-rebindable)
    attributes without having to thread reassignments through this
    sub-module.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        settings: dict[str, Any],
        romm_api: RommSyncApi,
        retry: RetryStrategy,
        logger: logging.Logger,
        log_debug: DebugLogger,
        settings_persister: SettingsPersister,
        plugin_version: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._settings = settings
        self._romm_api = romm_api
        self._retry = retry
        self._logger = logger
        self._log_debug = log_debug
        self._settings_persister = settings_persister
        self._plugin_version = plugin_version

    def _get_device_id(self) -> str | None:
        with self._uow_factory() as uow:
            return uow.kv_config.get("device_id")

    def _set_device_id(self, device_id: str) -> None:
        with self._uow_factory() as uow:
            uow.kv_config.set("device_id", device_id)

    def _get_device_name(self) -> str | None:
        return self._settings.get("device_name")

    def _set_device_name(self, name: str) -> None:
        self._settings["device_name"] = name
        self._settings_persister.save_settings()

    async def ensure_device_registered(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        hostname_provider: HostnameReader,
        machine_id_provider: MachineIdReader,
    ) -> dict[str, Any]:
        """Ensure this device is registered with the RomM server for save sync tracking.

        ``hostname_provider`` supplies the friendly display ``name``;
        ``machine_id_provider`` supplies the stable ``/etc/machine-id``
        fingerprint sent as the RomM ``hostname`` so the server dedupes
        this device across reinstalls. When the machine id is unreadable
        (``None``) the call degrades to no-fingerprint registration.
        """
        if not save_sync_enabled(self._settings):
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

        device_id = await loop.run_in_executor(None, self._get_device_id)
        if device_id:
            server_id_str = str(device_id)
            with contextlib.suppress(Exception):
                await loop.run_in_executor(
                    None,
                    lambda: self._romm_api.update_device(server_id_str, client_version=self._plugin_version),
                )
            return {
                "success": True,
                "device_id": device_id,
                "device_name": self._get_device_name() or "",
                "server_device_id": device_id,
            }

        hostname = hostname_provider.get()
        machine_id = machine_id_provider.get()

        try:
            result = await loop.run_in_executor(
                None,
                lambda: self._romm_api.register_device(
                    name=hostname,
                    platform="linux",
                    client="decky-romm-sync",
                    client_version=self._plugin_version,
                    hostname=machine_id,
                ),
            )
            server_device_id = result.get("id") or result.get("device_id")
            if server_device_id:
                new_id = str(server_device_id)
                await loop.run_in_executor(None, self._set_device_id, new_id)
                self._set_device_name(hostname)
                self._logger.info(f"Device registered with server: {server_device_id} ({hostname})")
                return {
                    "success": True,
                    "device_id": new_id,
                    "device_name": hostname,
                    "server_device_id": new_id,
                }
        except Exception as e:
            self._logger.warning(f"Server device registration failed: {e}")

        return {"success": False, "device_id": "", "device_name": "", "error": "registration_failed"}

    async def list_devices(self, *, loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
        """List all devices registered with the RomM server for this user."""
        if not save_sync_enabled(self._settings):
            return {"success": False, "devices": [], "disabled": True}
        try:
            own_id = await loop.run_in_executor(None, self._get_device_id)
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
