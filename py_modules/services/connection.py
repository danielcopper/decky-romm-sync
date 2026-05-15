"""ConnectionService — RomM server reachability and minimum-version probe.

Owns the ``test_connection`` flow: heartbeat + version sniff +
authenticated endpoint probe + minimum-version gate. Pure I/O happens
through the ``RommApiProtocol``; this service composes that I/O with the
response-shape contract the frontend depends on. The minimum version is
injected so the policy stays anchored at the plugin entrypoint while
this service remains a pure orchestration layer.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.version import meets_min_version
from lib.errors import error_response

if TYPE_CHECKING:
    import asyncio
    import logging

    from services.protocols import RommApiProtocol


@dataclass(frozen=True)
class ConnectionServiceConfig:
    """Frozen wiring bundle handed to ``ConnectionService.__init__``.

    Carries the live settings dict, the RomM API Protocol, the runtime
    infrastructure (event loop, logger), and the minimum-version policy
    tuple. Bundled here so the ctor stays within the S107 parameter
    budget and so the version constant stays declared once at the
    plugin entrypoint.
    """

    settings: dict
    romm_api: RommApiProtocol
    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    min_required_version: tuple[int, ...]


class ConnectionService:
    """Heartbeat, version sniff, auth probe, and minimum-version gate."""

    def __init__(self, *, config: ConnectionServiceConfig) -> None:
        self._settings = config.settings
        self._romm_api = config.romm_api
        self._loop = config.loop
        self._logger = config.logger
        self._min_required_version = config.min_required_version

    async def test_connection(self) -> dict:
        """Probe the configured server and return a frontend-shaped result dict.

        The result dict always carries ``success`` and ``message``. On
        failure, ``error_code`` classifies the cause (``config_error``,
        ``version_error``, or an :func:`lib.errors.error_response` code).
        On success or version failure, ``romm_version`` carries the
        detected server version when the heartbeat exposed one.
        """
        if not self._settings.get("romm_url"):
            return {"success": False, "message": "No server URL configured", "error_code": "config_error"}

        try:
            heartbeat = await self._loop.run_in_executor(None, self._romm_api.heartbeat)
        except Exception as e:
            self._romm_api.set_version(None)
            return error_response(e)

        version: str | None = None
        with contextlib.suppress(AttributeError, TypeError):
            version = heartbeat.get("SYSTEM", {}).get("VERSION")
        self._romm_api.set_version(version)
        if version:
            self._logger.info(f"RomM server version: {version}")

        try:
            await self._loop.run_in_executor(None, self._romm_api.list_platforms)
        except Exception as e:
            resp = error_response(e)
            if resp["error_code"] not in ("auth_error", "forbidden_error"):
                resp["message"] = f"Server reachable but API request failed: {resp['message']}"
            return resp

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

        result: dict = {"success": True, "message": "Connected to RomM"}
        if version and version != "development":
            result["message"] = f"Connected to RomM {version}"
            result["romm_version"] = version
        elif version == "development":
            result["romm_version"] = version
        return result
