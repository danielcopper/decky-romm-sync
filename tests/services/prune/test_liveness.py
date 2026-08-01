"""Tests for services/prune/liveness.py — the only source of deletion authority."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from lib.errors import RommApiError, RommConnectionError, RommNotFoundError
from lib.list_result import ErrorCode
from lib.url_host import romm_namespace
from services.prune.liveness import UNCONFIRMED_REASON, LivenessProber, LivenessProberConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

_NAMESPACE_SETTINGS = {"romm_url": "https://romm.example", "romm_user_id": 1}


class _FakeRomReader:
    """A RomM reader whose per-id answer, and identity answer, the test dictates."""

    def __init__(self, answers: Mapping[int, object], user: object = None) -> None:
        self._answers = answers
        self._user = user
        self.calls: list[int] = []
        self.user_calls = 0

    def get_current_user(self) -> object:
        self.user_calls += 1
        if isinstance(self._user, Exception):
            raise self._user
        return self._user

    def get_rom_once(self, rom_id: int) -> object:
        self.calls.append(rom_id)
        answer = self._answers[rom_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _prober(
    answers: Mapping[int, object],
    settings: dict[str, Any] | None = None,
    *,
    controls: list[int] | None = None,
    user: object = None,
) -> tuple[LivenessProber, _FakeRomReader]:
    reader = _FakeRomReader(answers, user)
    prober = LivenessProber(
        config=LivenessProberConfig(
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("prune-liveness-test"),
            romm_api=cast("Any", reader),
            settings=settings if settings is not None else dict(_NAMESPACE_SETTINGS),
            canary_rom_ids=lambda exclude, limit: [i for i in (controls or []) if i not in exclude][:limit],
        )
    )
    return prober, reader


class TestVerdicts:
    async def test_a_404_is_the_only_vanished_verdict(self):
        prober, _ = _prober({7: RommNotFoundError("gone"), 99: {"id": 99}}, controls=[99])
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
        prober.bind_run("run-1", "bound-to-a-server-that-is-gone")

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
                loop=asyncio.get_event_loop(),
                logger=logging.getLogger("prune-liveness-test"),
                romm_api=cast("Any", _SwitchingReader({})),
                settings=settings,
                canary_rom_ids=lambda _exclude, _limit: [],
            )
        )
        prober.bind_run("run-1", "")

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
                loop=asyncio.get_event_loop(),
                logger=logging.getLogger("prune-liveness-test"),
                romm_api=cast("Any", _SwitchingReader({})),
                settings=settings,
                canary_rom_ids=lambda _exclude, _limit: [],
            )
        )
        prober.bind_run("run-1", "")

        assert (await prober.probe_many({7}))[7]["reason"] == "server_namespace_changed"

    async def test_ending_a_run_releases_the_binding(self):
        prober, reader = _prober({7: {"id": 7}})
        prober.bind_run("run-1", "a-namespace-that-no-longer-matches")
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


class TestEndpointConfirmation:
    """A 404 only counts once this round has seen the endpoint answer correctly."""

    async def test_a_404_stands_when_a_control_answers(self):
        prober, reader = _prober({7: RommNotFoundError("gone"), 99: {"id": 99}}, controls=[99])

        verdicts = await prober.probe_many({7})

        assert verdicts[7]["status"] == "vanished"
        assert reader.calls == [7, 99]

    async def test_a_404_degrades_when_every_control_also_404s(self):
        answers = {7: RommNotFoundError("misrouted"), 99: RommNotFoundError("misrouted")}
        prober, _ = _prober(answers, controls=[99])

        verdicts = await prober.probe_many({7})

        assert verdicts[7]["status"] == "uncertain"
        assert verdicts[7]["reason"] == UNCONFIRMED_REASON
        assert "could not be confirmed" in verdicts[7]["message"]

    async def test_a_404_degrades_when_no_control_exists(self):
        prober, _ = _prober({7: RommNotFoundError("gone")}, controls=[])

        assert (await prober.probe_many({7}))[7]["reason"] == UNCONFIRMED_REASON

    async def test_a_live_verdict_in_the_round_needs_no_control(self):
        answers = {7: RommNotFoundError("gone"), 8: {"id": 8}, 99: {"id": 99}}
        prober, reader = _prober(answers, controls=[99])

        verdicts = await prober.probe_many({7, 8})

        assert verdicts[7]["status"] == "vanished"
        assert 99 not in reader.calls

    async def test_a_round_without_any_404_never_asks_a_control(self):
        prober, reader = _prober({7: RommConnectionError("down"), 99: {"id": 99}}, controls=[99])

        verdicts = await prober.probe_many({7})

        assert verdicts[7]["status"] == "uncertain"
        assert reader.calls == [7], "nothing was going to be deleted, so nothing needed proving"

    async def test_a_second_known_live_id_is_tried_when_the_first_has_also_gone(self):
        """One retry, because a single control can have legitimately vanished too."""
        answers = {7: RommNotFoundError("gone"), 97: RommNotFoundError("also gone"), 98: {"id": 98}}
        prober, reader = _prober(answers, controls=[97, 98])

        verdicts = await prober.probe_many({7})

        assert verdicts[7]["status"] == "vanished"
        assert reader.calls == [7, 97, 98]

    async def test_the_control_walk_stops_at_two(self):
        """Bounded: a control is a check, not a survey of the library."""
        answers = {7: RommNotFoundError("gone"), 97: RommNotFoundError("x"), 98: RommNotFoundError("x")}
        prober, reader = _prober(answers, controls=[97, 98, 99])

        assert (await prober.probe_many({7}))[7]["reason"] == UNCONFIRMED_REASON
        assert reader.calls == [7, 97, 98], "the third candidate control is never asked"

    async def test_the_probed_ids_are_never_used_as_their_own_control(self):
        seen: list[set[int]] = []

        def controls(exclude: set[int], limit: int) -> list[int]:
            seen.append(set(exclude))
            return []

        reader = _FakeRomReader({7: RommNotFoundError("gone"), 8: RommNotFoundError("gone")})
        prober = LivenessProber(
            config=LivenessProberConfig(
                loop=asyncio.get_event_loop(),
                logger=logging.getLogger("prune-liveness-test"),
                romm_api=cast("Any", reader),
                settings=dict(_NAMESPACE_SETTINGS),
                canary_rom_ids=controls,
            )
        )

        await prober.probe_many({7, 8})

        assert seen == [{7, 8}], "a questioned id cannot vouch for itself"

    async def test_an_untrustworthy_response_does_not_count_as_a_control(self):
        answers = {7: RommNotFoundError("gone"), 99: {"id": 12345}}
        prober, _ = _prober(answers, controls=[99])

        assert (await prober.probe_many({7}))[7]["reason"] == UNCONFIRMED_REASON

    async def test_a_refused_round_names_the_controls_it_asked(self, caplog):
        """A misroute has to be diagnosable from the log, not only from what survived."""
        answers = {7: RommNotFoundError("misrouted"), 99: RommNotFoundError("misrouted")}
        prober, _ = _prober(answers, controls=[99])
        prober.bind_run("run-7", romm_namespace(_NAMESPACE_SETTINGS))

        with caplog.at_level(logging.WARNING, logger="prune-liveness-test"):
            await prober.probe_many({7})

        assert any("run-7" in record.message and "99" in record.message for record in caplog.records)
        assert any("nothing will be removed" in record.message for record in caplog.records)

    async def test_a_confirmed_round_records_which_control_answered(self, caplog):
        answers = {7: RommNotFoundError("gone"), 99: {"id": 99}}
        prober, _ = _prober(answers, controls=[99])
        prober.bind_run("run-7", romm_namespace(_NAMESPACE_SETTINGS))

        with caplog.at_level(logging.INFO, logger="prune-liveness-test"):
            await prober.probe_many({7})

        assert any("run-7" in record.message and "404s stand" in record.message for record in caplog.records)

    async def test_a_round_with_no_proof_at_all_names_the_none_tier(self, caplog):
        prober, _ = _prober({7: RommNotFoundError("gone")}, controls=[])
        prober.bind_run("run-7", romm_namespace(_NAMESPACE_SETTINGS))

        with caplog.at_level(logging.WARNING, logger="prune-liveness-test"):
            await prober.probe_many({7})

        assert any("tier=none" in record.message for record in caplog.records)
        assert any("nothing will be removed" in record.message for record in caplog.records)

    @pytest.mark.parametrize(
        ("probed", "controls", "user", "expected"),
        [
            ({7: RommNotFoundError("gone"), 8: {"id": 8}}, [], None, "tier=still_there"),
            ({7: RommNotFoundError("gone"), 99: {"id": 99}}, [99], None, "tier=canary_rom"),
            ({7: RommNotFoundError("gone")}, [], {"id": 1}, "tier=canary_user"),
        ],
    )
    async def test_every_tier_names_itself_in_the_audit_trail(self, caplog, probed, controls, user, expected):
        prober, _ = _prober(probed, controls=controls, user=user)
        prober.bind_run("run-7", romm_namespace(_NAMESPACE_SETTINGS))

        with caplog.at_level(logging.INFO, logger="prune-liveness-test"):
            await prober.probe_many({key for key in probed if key != 99})

        assert any("run-7" in record.message and expected in record.message for record in caplog.records)


class TestUserFallbackTier:
    """Only when the library offers no known-live id at all."""

    async def test_a_matching_user_identity_vouches_for_the_round(self):
        prober, reader = _prober({7: RommNotFoundError("gone")}, controls=[], user={"id": 1})

        assert (await prober.probe_many({7}))[7]["status"] == "vanished"
        assert reader.user_calls == 1

    async def test_a_different_user_does_not_vouch(self):
        """A token that now belongs to someone else cannot authorize this run's deletions."""
        prober, _ = _prober({7: RommNotFoundError("gone")}, controls=[], user={"id": 999})

        assert (await prober.probe_many({7}))[7]["reason"] == UNCONFIRMED_REASON

    @pytest.mark.parametrize("user", [None, {}, {"id": None}, "not-a-dict", RommConnectionError("down")])
    async def test_an_absent_or_unusable_identity_does_not_vouch(self, user):
        prober, _ = _prober({7: RommNotFoundError("gone")}, controls=[], user=user)

        assert (await prober.probe_many({7}))[7]["reason"] == UNCONFIRMED_REASON

    async def test_it_is_never_reached_while_a_known_live_id_exists(self):
        """A control that 404s is a proof failure, not a reason to seek a softer answer."""
        answers = {7: RommNotFoundError("gone"), 99: RommNotFoundError("also gone")}
        prober, reader = _prober(answers, controls=[99], user={"id": 1})

        assert (await prober.probe_many({7}))[7]["reason"] == UNCONFIRMED_REASON
        assert reader.user_calls == 0

    async def test_it_is_not_reached_when_a_rom_control_answers(self):
        prober, reader = _prober({7: RommNotFoundError("gone"), 99: {"id": 99}}, controls=[99], user={"id": 1})

        assert (await prober.probe_many({7}))[7]["status"] == "vanished"
        assert reader.user_calls == 0
