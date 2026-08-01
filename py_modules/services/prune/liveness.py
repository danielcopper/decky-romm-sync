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

    from services.protocols import RommLivenessApi
    from services.prune._models import CanaryRomIdsFn

_LIVENESS_CONCURRENCY = 4

# How many known-live ids a round may ask about before giving up on the ROM tier.
# Two rather than one because the first can have legitimately vanished since the
# last sync; two rather than more because this is a control, not a survey.
_CANARY_PROBES = 2

UNCONFIRMED_REASON = "unconfirmed_server"

# The tiers that can vouch for a round's 404s, strongest first. Named in the
# audit trail so a run's authority is readable after the fact.
PROOF_STILL_THERE = "still_there"
PROOF_CANARY_ROM = "canary_rom"
PROOF_CANARY_USER = "canary_user"


@dataclass(frozen=True)
class LivenessProberConfig:
    """Dependencies for one cleanup run's exact-ID liveness proofs."""

    loop: asyncio.AbstractEventLoop
    logger: logging.Logger
    romm_api: RommLivenessApi
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
        """Downgrade this round's 404s unless something proved the route in it.

        A 404 is only evidence that a ROM is gone if the thing answering is RomM
        and the request reached the ROM route. RomM is FastAPI, so a wrong path
        prefix makes RomM itself answer a clean JSON 404 for every id — including
        ids that plainly exist — which no body-shape check can tell apart from an
        entity 404. A round that saw no correct answer therefore has no business
        deleting anything on the strength of the 404s it did see.
        """
        if not any(verdict["status"] == "vanished" for verdict in verdicts.values()):
            return verdicts
        if any(verdict["status"] == "live" for verdict in verdicts.values()):
            tier, detail = PROOF_STILL_THERE, "a probed ROM answered"
        else:
            tier, detail = await self._proof_tier(set(verdicts))
        self._log_proof(tier, detail)
        if tier is not None:
            return verdicts
        return {
            rom_id: _unconfirmed() if verdict["status"] == "vanished" else verdict
            for rom_id, verdict in verdicts.items()
        }

    async def _proof_tier(self, exclude: set[int]) -> tuple[str | None, str]:
        """Find something that vouches for this round, and say which tier did.

        Preferred tier is the ROM route itself: ids the last complete fetch is
        recorded as returning are the best available liveness prior, and a
        200-carrying-its-own-id proves route, auth and server in one request.
        Only when the library holds no such id at all does the weaker
        authenticated-identity tier apply — a control that 404s is a proof
        failure, not a licence to go looking for a friendlier answer.
        """
        subjects = await self._loop.run_in_executor(None, self._canary_rom_ids, exclude, _CANARY_PROBES)
        refusals: list[str] = []
        for rom_id in subjects:
            verdict = await self._loop.run_in_executor(None, self._probe_one, rom_id)
            if verdict["status"] == "live":
                return PROOF_CANARY_ROM, f"rom {rom_id} answered"
            refusals.append(f"{rom_id}:{verdict['status']}/{verdict['reason']}")
        if subjects:
            return None, f"known-live rom(s) did not answer ({', '.join(refusals)})"
        if await self._user_answers():
            return PROOF_CANARY_USER, "no known-live rom; the authenticated user matched"
        return None, "no known-live rom, and the authenticated user did not match"

    async def _user_answers(self) -> bool:
        """Whether RomM still identifies the run's pinned user — the fallback proof.

        Weaker than a ROM answer on purpose: it shows the server is RomM and the
        token still belongs to the pinned user, but says nothing about the ROM
        route. Reached only when the library offers no known-live id.
        """
        expected = str(self._settings.get("romm_user_id") or "")
        if not expected or not self._namespace_holds():
            return False
        try:
            payload: Any = await self._loop.run_in_executor(None, self._romm_api.get_current_user)
        except Exception:
            return False
        if not self._namespace_holds() or not isinstance(payload, dict):
            return False
        return str(payload.get("id") or "") == expected

    def _namespace_holds(self) -> bool:
        return self._run_namespace is None or romm_namespace(self._settings) == self._run_namespace

    def _log_proof(self, tier: str | None, detail: str) -> None:
        """Record which tier vouched for this round, so a misroute is diagnosable."""
        if tier is not None:
            self._logger.info(f"Cleanup run {self._run_id} liveness proof: tier={tier} ({detail}); 404s stand")
            return
        self._logger.warning(
            f"Cleanup run {self._run_id} liveness proof: tier=none ({detail}); "
            f"every 404 in this check is unconfirmed and nothing will be removed"
        )

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


__all__ = [
    "PROOF_CANARY_ROM",
    "PROOF_CANARY_USER",
    "PROOF_STILL_THERE",
    "UNCONFIRMED_REASON",
    "LivenessProber",
    "LivenessProberConfig",
]
