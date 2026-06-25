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

from lib.errors import classify_error
from lib.list_result import ErrorCode
from services.saves._settings import save_sync_enabled

# Both device callables short-circuit with the identical failure shape when
# save sync is disabled — kept as one constant so the two branches never drift
# into a per-call mini-dialect.
_SYNC_DISABLED_REASON = "sync_disabled"
_SYNC_DISABLED_MESSAGE = "Save sync is disabled"

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
    """Sole owner of the server device identity for every save-sync flow.

    The single source of truth for ``kv_config["device_id"]`` — the
    server-issued device id every sync, slot, status, and version flow
    needs to attribute uploads and filter per-slot server views. Every
    consumer reads the id through :meth:`get_device_id` (one cached
    Unit-of-Work read, reused for the process lifetime) rather than
    opening its own transaction, and registration is the only writer.
    The device *label* lives in ``settings.json`` (ADR-0003: server
    identity in ``kv_config``, user-set label in settings) and flows
    through the injected ``SettingsPersister``.

    Also co-locates the device-identity fallback every sync callable
    reaches when no id is registered yet. The async entry points take
    ``loop``, ``hostname_provider``, and ``machine_id_provider`` per call
    so :class:`SyncEngine` can pass its live (test-rebindable) attributes
    without having to thread reassignments through this sub-module.
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
        # Cached server device id. ``_device_id_loaded`` distinguishes
        # "never read" from "read and found absent" (``None``) so an
        # unregistered device is not re-queried on every call. The id is a
        # truly-singleton scalar that only registration mutates, so a one-shot
        # read reused for the process lifetime is safe; the cache is refreshed
        # by :meth:`_set_device_id` on (re)registration and can be dropped via
        # :meth:`invalidate_device_id_cache`.
        self._cached_device_id: str | None = None
        self._device_id_loaded: bool = False

    def get_device_id(self) -> str | None:
        """Return the server device id (``None`` when unregistered).

        The single device-id accessor for the whole save-sync bounded
        context: reads ``kv_config["device_id"]`` once through a narrow
        Unit of Work, caches it, and returns the cached value thereafter —
        no per-call transaction. Synchronous so sync-worker callers can
        invoke it directly inside their ``run_in_executor`` blocks.
        """
        if not self._device_id_loaded:
            with self._uow_factory() as uow:
                self._cached_device_id = uow.kv_config.get("device_id")
            self._device_id_loaded = True
        return self._cached_device_id

    def invalidate_device_id_cache(self) -> None:
        """Drop the cached device id so the next :meth:`get_device_id` re-reads.

        For the rare case where ``kv_config["device_id"]`` is mutated outside
        this registry (registration is the only in-process writer, so today
        this is reached only from test backdoors that seed the row directly) —
        invalidating keeps the cache from serving a stale value.
        """
        self._cached_device_id = None
        self._device_id_loaded = False

    def _set_device_id(self, device_id: str) -> None:
        with self._uow_factory() as uow:
            uow.kv_config.set("device_id", device_id)
        # Keep the cache coherent with the write so callers that read the id
        # immediately after (re)registration see the new value, not a stale
        # ``None`` left from a pre-registration probe.
        self._cached_device_id = device_id
        self._device_id_loaded = True

    def _get_device_name(self) -> str | None:
        return self._settings.get("device_name")

    def _set_device_name(self, name: str) -> None:
        """Persist the device label to settings.json, atomic on failure.

        Mutates the in-memory settings dict and triggers the persist; if the
        persist raises, the in-memory dict is rolled back to its prior value so
        an unsaved label never lingers in memory (a later unrelated
        ``save_settings`` would otherwise commit it). The caller treats the
        re-raised failure as a best-effort miss — the device id is already the
        authoritative registered signal.
        """
        had_name = "device_name" in self._settings
        prior = self._settings.get("device_name")
        self._settings["device_name"] = name
        try:
            self._settings_persister.save_settings()
        except Exception:
            if had_name:
                self._settings["device_name"] = prior
            else:
                self._settings.pop("device_name", None)
            raise

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
            return {
                "success": False,
                "reason": _SYNC_DISABLED_REASON,
                "message": _SYNC_DISABLED_MESSAGE,
                "device_id": "",
                "device_name": "",
                "disabled": True,
            }

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

        device_id = await loop.run_in_executor(None, self.get_device_id)
        if device_id:
            server_id_str = str(device_id)
            # Best-effort touch of the server-side client_version. A failure here
            # is non-fatal (the device is already registered), but log it at debug
            # so the swallow leaves a breadcrumb instead of vanishing silently.
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._romm_api.update_device(server_id_str, client_version=self._plugin_version),
                )
            except Exception as e:
                self._log_debug(f"ensure_device_registered: update_device failed (non-fatal): {e}")
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
                # The kv_config device id is the AUTHORITATIVE "registered"
                # signal — write it first. The device label is a best-effort
                # write to settings.json AFTER: ADR-0003 keeps the two in
                # separate stores, so the writes can't be one atomic op. If the
                # label write fails (e.g. a settings.json fsync error) the
                # device is still fully registered and usable — the prior/default
                # label persists — instead of being left in a broken half-state.
                await loop.run_in_executor(None, self._set_device_id, new_id)
                device_name = hostname
                try:
                    await loop.run_in_executor(None, self._set_device_name, hostname)
                except Exception as e:
                    device_name = self._get_device_name() or ""
                    self._log_debug(
                        f"ensure_device_registered: device_name write failed (non-fatal, "
                        f"device usable with id {new_id}): {e}"
                    )
                self._logger.info(f"Device registered with server: {server_device_id} ({hostname})")
                return {
                    "success": True,
                    "device_id": new_id,
                    "device_name": device_name,
                    "server_device_id": new_id,
                }
        except Exception as e:
            # Classify the failure so a revoked token (401 → AUTH_FAILED) or an
            # SSL misconfig carries its OWN reason + message instead of every
            # failure collapsing onto a generic "Could not register device"
            # offline slug (#971).
            self._logger.warning(f"Server device registration failed: {e}")
            reason, message = classify_error(e)
            return {
                "success": False,
                "reason": reason,
                "message": message,
                "device_id": "",
                "device_name": "",
            }

        return {
            "success": False,
            "reason": ErrorCode.SERVER_UNREACHABLE.value,
            "message": "Could not register device",
            "device_id": "",
            "device_name": "",
        }

    async def list_devices(self, *, loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
        """List all devices registered with the RomM server for this user."""
        if not save_sync_enabled(self._settings):
            return {
                "success": False,
                "reason": _SYNC_DISABLED_REASON,
                "message": _SYNC_DISABLED_MESSAGE,
                "devices": [],
                "disabled": True,
            }
        try:
            own_id = await loop.run_in_executor(None, self.get_device_id)
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
            return {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Could not load devices",
                "devices": [],
            }
