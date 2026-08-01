"""The only source of deletion authority: namespace-bound exact-ID liveness proof.

A row may be removed only because RomM answered a fresh, single-attempt, exact-ID
request with a 404, under the same namespace the run's candidates were discovered
in. Everything that turns a RomM answer into ``vanished`` / ``live`` /
``uncertain`` belongs here, so no caller can invent authority from a different
kind of response; what a run *does* with a verdict belongs to its phases.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode
from lib.url_host import romm_namespace

if TYPE_CHECKING:
    from services.protocols import RommRomReader

_LIVENESS_CONCURRENCY = 4


@dataclass(frozen=True)
class LivenessProberConfig:
    """Dependencies for one cleanup run's exact-ID liveness proofs."""

    loop: asyncio.AbstractEventLoop
    romm_api: RommRomReader
    settings: dict[str, Any]


class LivenessProber:
    """Prove whether an exact rom_id is gone, still there, or unconfirmed."""

    def __init__(self, *, config: LivenessProberConfig) -> None:
        self._loop = config.loop
        self._romm_api = config.romm_api
        self._settings = config.settings
        self._run_namespace: str | None = None

    def bind_run(self, namespace: str) -> None:
        """Pin the namespace every later proof in this run must still be answered under."""
        self._run_namespace = namespace

    def end_run(self) -> None:
        """Release the run's namespace binding once no further proof can be issued."""
        self._run_namespace = None

    async def probe_many(self, rom_ids: set[int]) -> dict[int, dict[str, str]]:
        """Prove every id concurrently, bounded, and return one verdict each."""
        semaphore = asyncio.Semaphore(_LIVENESS_CONCURRENCY)

        async def one(rom_id: int) -> tuple[int, dict[str, str]]:
            async with semaphore:
                verdict = await self._loop.run_in_executor(None, self._probe_one, rom_id)
                return rom_id, verdict

        return dict(await asyncio.gather(*(one(rom_id) for rom_id in sorted(rom_ids))))

    def _probe_one(self, rom_id: int) -> dict[str, str]:
        expected_namespace = self._run_namespace or romm_namespace(self._settings)
        if romm_namespace(self._settings) != expected_namespace:
            return {
                "status": "uncertain",
                "reason": "server_namespace_changed",
                "message": "The RomM server or user changed before the exact-ID proof.",
            }
        try:
            payload: Any = self._romm_api.get_rom_once(rom_id)
        except RommNotFoundError:
            if romm_namespace(self._settings) != expected_namespace:
                return {
                    "status": "uncertain",
                    "reason": "server_namespace_changed",
                    "message": "The RomM server or user changed during the exact-ID proof.",
                }
            return {"status": "vanished", "reason": ErrorCode.NOT_FOUND.value, "message": "RomM confirmed 404."}
        except Exception as exc:
            reason, message = classify_error(exc)
            return {"status": "uncertain", "reason": reason, "message": message}
        if romm_namespace(self._settings) != expected_namespace:
            return {
                "status": "uncertain",
                "reason": "server_namespace_changed",
                "message": "The RomM server or user changed during the exact-ID proof.",
            }
        payload_id = payload.get("id") if isinstance(payload, dict) else None
        if type(payload_id) is int and payload_id == rom_id:
            return {"status": "live", "reason": "live", "message": "RomM returned the exact ROM."}
        return {
            "status": "uncertain",
            "reason": "untrustworthy_response",
            "message": "RomM returned an empty, malformed, or wrong-id response.",
        }


__all__ = ["LivenessProber", "LivenessProberConfig"]
