"""The only source of deletion authority: namespace-bound exact-ID liveness proof.

A row may be removed only because RomM answered a fresh, single-attempt, exact-ID
request with a 404, under the same namespace the run's candidates were discovered
in, **and** only while that round holds positive evidence that the ROM endpoint is
answering correctly at all. Everything that turns a RomM answer into ``vanished``
/ ``live`` / ``uncertain`` belongs here, so no caller can invent authority from a
different kind of response; what a run *does* with a verdict belongs to its phases.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lib.errors import RommNotFoundError, classify_error
from lib.list_result import ErrorCode
from lib.url_host import romm_namespace

if TYPE_CHECKING:
    import logging

    from services.protocols import RommRomReader
    from services.prune._models import CanaryRomIdsFn

_LIVENESS_CONCURRENCY = 4

# How many known-served ids a round may ask about before giving up on proving the
# endpoint. More than one because a single control can have legitimately vanished
# too; bounded because this is a control, not a survey.
_CANARY_SUBJECTS = 3

UNCONFIRMED_REASON = "unconfirmed_server"


@dataclass(frozen=True)
class LivenessProberConfig:
    """Dependencies for one cleanup run's exact-ID liveness proofs."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    romm_api: RommRomReader
    settings: dict[str, Any]
    canary_rom_ids: CanaryRomIdsFn


class LivenessProber:
    """Prove whether an exact rom_id is gone, still there, or unconfirmed."""

    def __init__(self, *, config: LivenessProberConfig) -> None:
        self._loop = config.loop
        self._logger = config.logger
        self._romm_api = config.romm_api
        self._settings = config.settings
        self._canary_rom_ids = config.canary_rom_ids
        self._run_id: str | None = None
        self._run_namespace: str | None = None

    def bind_run(self, run_id: str, namespace: str) -> None:
        """Pin the namespace every later proof in this run must still be answered under."""
        self._run_id = run_id
        self._run_namespace = namespace

    def end_run(self) -> None:
        """Release the run's binding once no further proof can be issued."""
        self._run_id = None
        self._run_namespace = None

    async def probe_many(self, rom_ids: set[int]) -> dict[int, dict[str, str]]:
        """Prove every id concurrently, bounded, and return one confirmed verdict each."""
        semaphore = asyncio.Semaphore(_LIVENESS_CONCURRENCY)

        async def one(rom_id: int) -> tuple[int, dict[str, str]]:
            async with semaphore:
                verdict = await self._loop.run_in_executor(None, self._probe_one, rom_id)
                return rom_id, verdict

        verdicts = dict(await asyncio.gather(*(one(rom_id) for rom_id in sorted(rom_ids))))
        return await self._confirmed(verdicts)

    async def _confirmed(self, verdicts: dict[int, dict[str, str]]) -> dict[int, dict[str, str]]:
        """Downgrade this round's 404s unless the ROM endpoint proved itself in it.

        A 404 is only evidence that a ROM is gone if the thing answering is RomM
        and it is answering about ROMs correctly. A misrouted request answers 404
        for every id — including ids that plainly exist — so a round that saw no
        correct answer at all has no business deleting anything on the strength of
        the 404s it did see.
        """
        if not any(verdict["status"] == "vanished" for verdict in verdicts.values()):
            return verdicts
        if any(verdict["status"] == "live" for verdict in verdicts.values()):
            return verdicts
        if await self._endpoint_answers(set(verdicts)):
            return verdicts
        return {
            rom_id: _unconfirmed() if verdict["status"] == "vanished" else verdict
            for rom_id, verdict in verdicts.items()
        }

    async def _endpoint_answers(self, exclude: set[int]) -> bool:
        """Ask about ids RomM served most recently, and report whether any came back.

        Deliberately the same request the real probes make, against ids this
        device last saw RomM return: only an answer from the very endpoint whose
        404s are about to be trusted can vouch for those 404s. A control that is
        itself genuinely gone reads as unproven, which retains data rather than
        removing it.
        """
        subjects = await self._loop.run_in_executor(None, self._canary_rom_ids, exclude, _CANARY_SUBJECTS)
        if not subjects:
            self._logger.warning(
                f"Cleanup run {self._run_id} liveness control: none available; "
                f"every 404 in this check is unconfirmed and nothing will be removed"
            )
            return False
        refusals: list[str] = []
        for rom_id in subjects:
            verdict = await self._loop.run_in_executor(None, self._probe_one, rom_id)
            if verdict["status"] == "live":
                self._logger.info(f"Cleanup run {self._run_id} liveness control: ROM {rom_id} answered; 404s stand")
                return True
            refusals.append(f"{rom_id}:{verdict['status']}/{verdict['reason']}")
        self._logger.warning(
            f"Cleanup run {self._run_id} liveness control: no control answered ({', '.join(refusals)}); "
            f"every 404 in this check is unconfirmed and nothing will be removed"
        )
        return False

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


def _unconfirmed() -> dict[str, str]:
    return {
        "status": "uncertain",
        "reason": UNCONFIRMED_REASON,
        "message": "RomM's answers could not be confirmed during this check.",
    }


__all__ = ["UNCONFIRMED_REASON", "LivenessProber", "LivenessProberConfig"]
