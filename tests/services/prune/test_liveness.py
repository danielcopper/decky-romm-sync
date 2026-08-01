"""Tests for services/prune/liveness.py — the only source of deletion authority."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from lib.errors import RommApiError, RommConnectionError, RommNotFoundError
from lib.list_result import ErrorCode
from services.prune.liveness import LivenessProber, LivenessProberConfig

_NAMESPACE_SETTINGS = {"romm_url": "https://romm.example", "romm_user_id": 1}


class _FakeRomReader:
    """A RomM ROM reader whose per-id answer the test dictates."""

    def __init__(self, answers: dict[int, object]) -> None:
        self._answers = answers
        self.calls: list[int] = []

    def get_rom_once(self, rom_id: int) -> object:
        self.calls.append(rom_id)
        answer = self._answers[rom_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _prober(
    answers: dict[int, object], settings: dict[str, Any] | None = None
) -> tuple[LivenessProber, _FakeRomReader]:
    reader = _FakeRomReader(answers)
    prober = LivenessProber(
        config=LivenessProberConfig(
            loop=asyncio.get_event_loop(),
            romm_api=cast("Any", reader),
            settings=settings if settings is not None else dict(_NAMESPACE_SETTINGS),
        )
    )
    return prober, reader


class TestVerdicts:
    async def test_a_404_is_the_only_vanished_verdict(self):
        prober, _ = _prober({7: RommNotFoundError("gone")})
        verdicts = await prober.probe_many({7})
        assert verdicts[7]["status"] == "vanished"
        assert verdicts[7]["reason"] == ErrorCode.NOT_FOUND.value

    async def test_an_exact_id_match_is_live(self):
        prober, _ = _prober({7: {"id": 7, "name": "Some Game"}})
        assert (await prober.probe_many({7}))[7]["status"] == "live"

    async def test_a_transport_failure_is_uncertain_not_vanished(self):
        prober, _ = _prober({7: RommConnectionError("no route")})
        verdict = (await prober.probe_many({7}))[7]
        assert verdict["status"] == "uncertain"
        assert verdict["reason"] != ErrorCode.NOT_FOUND.value

    async def test_a_server_error_is_uncertain(self):
        prober, _ = _prober({7: RommApiError("boom")})
        assert (await prober.probe_many({7}))[7]["status"] == "uncertain"

    @pytest.mark.parametrize("payload", [None, {}, [], "", {"id": None}, {"name": "no id"}])
    async def test_a_malformed_response_is_uncertain(self, payload):
        prober, _ = _prober({7: payload})
        verdict = (await prober.probe_many({7}))[7]
        assert verdict["status"] == "uncertain"
        assert verdict["reason"] == "untrustworthy_response"

    async def test_a_wrong_id_in_the_response_is_uncertain(self):
        """RomM answering about a different ROM proves nothing about this one."""
        prober, _ = _prober({7: {"id": 8}})
        assert (await prober.probe_many({7}))[7]["reason"] == "untrustworthy_response"

    async def test_a_boolean_id_is_not_accepted_as_an_int_match(self):
        prober, _ = _prober({1: {"id": True}})
        assert (await prober.probe_many({1}))[1]["reason"] == "untrustworthy_response"


class TestNamespaceBinding:
    async def test_a_namespace_change_before_the_probe_is_uncertain(self):
        settings = dict(_NAMESPACE_SETTINGS)
        prober, reader = _prober({7: RommNotFoundError("gone")}, settings)
        prober.bind_run("bound-to-a-server-that-is-gone")

        verdict = (await prober.probe_many({7}))[7]

        assert verdict["status"] == "uncertain"
        assert verdict["reason"] == "server_namespace_changed"
        assert reader.calls == [], "no request may be issued under a changed namespace"

    async def test_a_namespace_change_during_a_404_refuses_the_deletion_authority(self):
        settings = dict(_NAMESPACE_SETTINGS)

        class _SwitchingReader(_FakeRomReader):
            def get_rom_once(self, rom_id: int) -> object:
                settings["romm_url"] = "https://other.example"
                raise RommNotFoundError("gone")

        prober = LivenessProber(
            config=LivenessProberConfig(
                loop=asyncio.get_event_loop(), romm_api=cast("Any", _SwitchingReader({})), settings=settings
            )
        )
        prober.bind_run("")

        verdict = (await prober.probe_many({7}))[7]

        assert verdict["status"] == "uncertain"
        assert verdict["reason"] == "server_namespace_changed"

    async def test_a_namespace_change_during_a_live_answer_is_uncertain(self):
        settings = dict(_NAMESPACE_SETTINGS)

        class _SwitchingReader(_FakeRomReader):
            def get_rom_once(self, rom_id: int) -> object:
                settings["romm_url"] = "https://other.example"
                return {"id": rom_id}

        prober = LivenessProber(
            config=LivenessProberConfig(
                loop=asyncio.get_event_loop(), romm_api=cast("Any", _SwitchingReader({})), settings=settings
            )
        )
        prober.bind_run("")

        assert (await prober.probe_many({7}))[7]["reason"] == "server_namespace_changed"

    async def test_ending_a_run_releases_the_binding(self):
        prober, reader = _prober({7: {"id": 7}})
        prober.bind_run("a-namespace-that-no-longer-matches")
        prober.end_run()

        assert (await prober.probe_many({7}))[7]["status"] == "live"
        assert reader.calls == [7]


class TestProbeMany:
    async def test_returns_one_verdict_per_requested_id(self):
        prober, reader = _prober({1: {"id": 1}, 2: RommNotFoundError("gone"), 3: RommConnectionError("down")})

        verdicts = await prober.probe_many({3, 1, 2})

        assert set(verdicts) == {1, 2, 3}
        assert sorted(reader.calls) == [1, 2, 3]
        assert [verdicts[i]["status"] for i in (1, 2, 3)] == ["live", "vanished", "uncertain"]

    async def test_an_empty_id_set_asks_nothing(self):
        prober, reader = _prober({})
        assert await prober.probe_many(set()) == {}
        assert reader.calls == []
