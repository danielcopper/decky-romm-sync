"""Tests for the RomM retry ladder — attempt counts, backoff, and the reachability state.

The ladder is driven through ``RommHttpAdapter``, the way production reaches it:
the delegation and the clear-on-success choke point are part of what is pinned
here. Retry behavior that belongs to one request method (``request_once`` and
the upload/auth paths that carry no ladder at all) stays with that method in
``test_http.py``.
"""

import http.client
import io
import logging
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from adapters.romm.http import RommHttpAdapter
from lib.errors import (
    RommAuthError,
    RommConflictError,
    RommConnectionError,
    RommForbiddenError,
    RommNotFoundError,
    RommServerError,
    RommUnprocessableEntityError,
    classify_error,
)
from lib.list_result import ErrorCode


@pytest.fixture
def adapter():
    """A real adapter over its own settings dict, pointed at a configured RomM URL."""
    return RommHttpAdapter(
        {"romm_url": "http://romm.local"},
        "/tmp",
        logging.getLogger("test"),
        "decky-romm-sync/9.9.9",
    )


def _ok_response(payload: bytes = b'{"ok": true}') -> MagicMock:
    """A urlopen return value usable both as a context manager and directly."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = payload
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _streamed_response(body: bytes) -> MagicMock:
    """A urlopen response the streaming download path can read to completion."""
    resp = MagicMock()
    resp.status = 200
    resp.headers = {"Content-Length": str(len(body))}
    resp.fp = None  # short-circuit the raw-socket settimeout chain
    stream = io.BytesIO(body)
    resp.read = stream.read
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _unattributable_404() -> urllib.error.HTTPError:
    """A 404 with neither a JSON content type nor a body — nothing attributes it to RomM."""
    return urllib.error.HTTPError("http://romm.local/api/roms/4375", 404, "Not Found", http.client.HTTPMessage(), None)


class TestWithRetryOnRetryListener:
    """The optional ``on_retry`` listener fires once per retry so the saves UI
    can surface "Connecting to RomM… (attempt N/M)" progress (#1345)."""

    def _adapter(self, on_retry=None):
        return RommHttpAdapter(
            {"romm_url": ""},
            "/tmp",
            logging.getLogger("test"),
            "decky-romm-sync/9.9.9",
            on_retry=on_retry,
        )

    def test_fires_per_retry_with_1_based_attempt_numbers(self):
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter(on_retry=lambda a, m, d: calls.append((a, m, d)))
        tries = {"n": 0}

        def fn():
            tries["n"] += 1
            raise ConnectionError("server down")

        with patch("adapters.romm.retry.time.sleep") as sleep_mock, pytest.raises(ConnectionError):
            adapter.with_retry(fn, max_attempts=3)

        # 3 attempts total; the listener fires just before each of the 2 backoff
        # sleeps, naming the retry about to run (attempt 2/3, then 3/3) and its delay.
        assert calls == [(2, 3, 1.0), (3, 3, 3.0)]
        assert sum(call.args[0] for call in sleep_mock.call_args_list) == pytest.approx(4.0)
        assert tries["n"] == 3

    def test_not_fired_when_first_attempt_succeeds(self):
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter(on_retry=lambda a, m, d: calls.append((a, m, d)))
        assert adapter.with_retry(lambda: "ok") == "ok"
        assert calls == []

    def test_not_fired_on_non_retryable_error(self):
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter(on_retry=lambda a, m, d: calls.append((a, m, d)))

        def fn():
            raise ValueError("bad request")  # non-retryable — raises immediately

        with pytest.raises(ValueError):
            adapter.with_retry(fn, max_attempts=3)
        assert calls == []

    def test_listener_exception_never_breaks_the_retry(self):
        # A raising listener (e.g. a closed loop at plugin unload) must be
        # swallowed so it can't abort the real HTTP retry underway.
        def boom(*_a):
            raise RuntimeError("loop closed")

        adapter = self._adapter(on_retry=boom)
        tries = {"n": 0}

        def fn():
            tries["n"] += 1
            if tries["n"] < 2:
                raise ConnectionError("server down")
            return "recovered"

        with patch("adapters.romm.retry.time.sleep"):
            assert adapter.with_retry(fn, max_attempts=3) == "recovered"

    def test_none_listener_is_a_noop_across_a_retry(self):
        adapter = self._adapter(on_retry=None)
        tries = {"n": 0}

        def fn():
            tries["n"] += 1
            if tries["n"] < 2:
                raise ConnectionError("server down")
            return "ok"

        with patch("adapters.romm.retry.time.sleep"):
            assert adapter.with_retry(fn, max_attempts=3) == "ok"

    def test_a_listener_assigned_after_construction_reaches_the_ladder(self):
        """``bootstrap`` wires the emit onto the adapter once the loop exists."""
        calls: list[tuple[int, int, float]] = []
        adapter = self._adapter()
        adapter.on_retry = lambda a, m, d: calls.append((a, m, d))

        fn = MagicMock(side_effect=[ConnectionError("server down"), "ok"])
        with patch("adapters.romm.retry.time.sleep"):
            assert adapter.with_retry(fn, max_attempts=3) == "ok"
        assert calls == [(2, 3, 1.0)]


class TestRetryLadderReentrancy:
    """One ladder per call stack — the OUTERMOST one wins (#1758).

    Services wrap adapter calls through the ``RetryStrategy`` protocol, and most
    of the adapter methods they wrap already wrap themselves. Without the guard
    the two levels multiply into 9 HTTP attempts with both backoffs stacked.
    """

    def _transient(self) -> MagicMock:
        """A callable failing with a retryable error that is NOT a reachability verdict.

        A bare ``OSError`` is retryable but classifies as ``unknown`` rather than
        ``server_unreachable``, which isolates the re-entrancy guard from the
        known-unreachable fast path — that path would otherwise cut the outer
        ladder short and hide a missing guard.
        """
        return MagicMock(side_effect=OSError("transient"))

    def test_a_nested_ladder_does_not_multiply_the_attempts(self, adapter):
        inner = self._transient()
        with patch("time.sleep"), pytest.raises(OSError):
            adapter.with_retry(lambda: adapter.with_retry(inner))
        assert inner.call_count == 3  # 3 x 3 without the guard

    def test_only_the_outer_ladder_backs_off(self, adapter):
        with patch("time.sleep") as sleep_mock, pytest.raises(OSError):
            adapter.with_retry(lambda: adapter.with_retry(self._transient()), base_delay=1)
        # Two gaps, 1s and 3s — not the six a nested ladder would sleep.
        assert sum(call.args[0] for call in sleep_mock.call_args_list) == pytest.approx(4.0)

    def test_a_service_wrap_over_a_retrying_adapter_method_costs_three_http_attempts(self, adapter):
        """The #1758 outcome: one game-detail lane against a dead server is 3 HTTP attempts, not 9.

        A combined-outcome test, NOT guard coverage: remove only the re-entrancy
        guard and this still passes, because the inner ladder's give-up marks
        the server unreachable and the outer ``_backoff`` then aborts. The two
        tests above are the ones that isolate the guard.
        """
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")) as urlopen,
            patch("time.sleep"),
            pytest.raises(RommConnectionError),
        ):
            adapter.with_retry(lambda: adapter.request("/api/roms"))
        assert urlopen.call_count == 3

    def test_a_nested_ladder_raises_the_callee_error_unchanged(self, adapter):
        inner = MagicMock(side_effect=RommAuthError("401"))
        with pytest.raises(RommAuthError):
            adapter.with_retry(lambda: adapter.with_retry(inner))
        inner.assert_called_once()

    def test_depth_is_released_after_a_raising_ladder(self, adapter):
        """A ladder that raised must not leave the thread marked as "inside a ladder"."""
        raising = MagicMock(side_effect=RommAuthError("401"))
        with pytest.raises(RommAuthError):
            adapter.with_retry(raising)

        fn = MagicMock(side_effect=[RommConnectionError("refused"), RommConnectionError("refused"), "ok"])
        with patch("time.sleep"):
            assert adapter.with_retry(fn, base_delay=0) == "ok"
        assert fn.call_count == 3

    def test_each_thread_carries_its_own_depth(self, adapter):
        """The guard is thread-local: a lane running concurrently still gets its own full ladder."""
        import threading

        other_lane_attempts: list[int] = []

        def other_lane() -> None:
            fn = MagicMock(side_effect=RommServerError("500"))
            with pytest.raises(RommServerError):
                adapter.with_retry(fn, base_delay=0)
            other_lane_attempts.append(fn.call_count)

        def inside_a_ladder() -> str:
            worker = threading.Thread(target=other_lane)
            worker.start()
            worker.join()
            return "ok"

        with patch("time.sleep"):
            assert adapter.with_retry(inside_a_ladder) == "ok"
        assert other_lane_attempts == [3]


class TestKnownUnreachableFastPath:
    """A server already known unreachable costs one attempt, not a full ladder (#1758)."""

    def _failing_ladder(self, adapter, exc: Exception) -> None:
        fn = MagicMock(side_effect=exc)
        with patch("time.sleep"), pytest.raises(type(exc)):
            adapter.with_retry(fn, base_delay=0)

    def test_ladder_giving_up_unreachable_shrinks_the_next_ladder_to_one_attempt(self, adapter):
        self._failing_ladder(adapter, RommConnectionError("refused"))

        retries: list[tuple[int, int, float]] = []
        adapter.on_retry = lambda a, m, d: retries.append((a, m, d))
        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep") as sleep_mock, pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=1)
        fn.assert_called_once()
        sleep_mock.assert_not_called()
        # The ladder is one attempt long, so no "connecting… (attempt 2/3)" is
        # promised to the UI either.
        assert retries == []

    def test_the_single_attempt_still_really_calls_out(self, adapter):
        """Self-healing rests on this: the state degrades the ladder, it never skips the call."""
        self._failing_ladder(adapter, RommConnectionError("refused"))

        fn = MagicMock(return_value="ok")
        assert adapter.with_retry(fn) == "ok"
        fn.assert_called_once()

    def test_a_successful_response_clears_the_state(self, adapter):
        self._failing_ladder(adapter, RommConnectionError("refused"))

        with patch("urllib.request.urlopen", return_value=_ok_response()):
            adapter.request("/api/roms")

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        assert fn.call_count == 3

    def test_request_once_clears_the_state_although_it_bypasses_the_ladder(self, adapter):
        """The reachability probe and the 30s heartbeat are the paths that see the server come back."""
        self._failing_ladder(adapter, RommConnectionError("refused"))

        with patch("urllib.request.urlopen", return_value=_ok_response()):
            adapter.request_once("/api/heartbeat", timeout=3)

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        assert fn.call_count == 3

    def test_upload_multipart_clears_the_state(self, adapter, tmp_path):
        """The upload path skips the ladder too, so it needs the same choke point."""
        self._failing_ladder(adapter, RommConnectionError("refused"))
        src = tmp_path / "save.srm"
        src.write_bytes(b"payload")

        with patch("urllib.request.urlopen", return_value=_ok_response()):
            adapter.upload_multipart("/api/saves", str(src))

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        assert fn.call_count == 3

    @pytest.mark.parametrize(
        "exc",
        [
            RommAuthError("401"),
            RommForbiddenError("403"),
            RommNotFoundError("Rom with id '4375' not found"),
            RommConflictError("409"),
            RommUnprocessableEntityError("422"),
            RommServerError("rate limited", status_code=429),
        ],
        ids=["401", "403", "entity-404", "409", "422", "429"],
    )
    def test_a_server_answer_never_counts_as_unreachable(self, adapter, exc):
        """Any 4xx means the server answered, whatever it answered.

        The 404 case is deletion authority downstream (#1570). The 409 is the
        one that bites in normal operation: every automatic save upload POSTs
        ``overwrite=false`` precisely so RomM rejects a stale head with one, and
        that upload runs inside a ladder — so folding it into a transport
        verdict would mark the server unreachable on a routine conflict.
        """
        self._failing_ladder(adapter, exc)

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        assert fn.call_count == 3

    def test_a_dead_cover_cdn_never_marks_romm_unreachable(self, adapter, tmp_path):
        """``download_external`` fetches ``url_cover`` from a third-party CDN, not from RomM."""
        with (
            patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("cdn down")),
            patch("time.sleep"),
            pytest.raises(RommConnectionError),
        ):
            adapter.download_external("https://cdn.invalid/cover.png", str(tmp_path / "cover.png"))

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        assert fn.call_count == 3

    def test_reaching_the_cover_cdn_never_clears_the_romm_state(self, adapter, tmp_path):
        """The CDN answering proves nothing about RomM, so it must not restore the full ladder."""
        self._failing_ladder(adapter, RommConnectionError("refused"))

        with patch("urllib.request.urlopen", return_value=_streamed_response(b"PNG")):
            adapter.download_external("https://cdn.invalid/cover.png", str(tmp_path / "cover.png"))

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        fn.assert_called_once()

    def test_the_cover_cdn_keeps_its_full_ladder_while_romm_is_down(self, adapter, tmp_path):
        """RomM being unreachable is no reason to give the CDN one shot — it is a different host."""
        self._failing_ladder(adapter, RommConnectionError("refused"))

        with (
            patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("cdn down")) as urlopen,
            patch("time.sleep"),
            pytest.raises(RommConnectionError),
        ):
            adapter.download_external("https://cdn.invalid/cover.png", str(tmp_path / "cover.png"))
        assert urlopen.call_count == 3

    def test_an_unproven_404_does_count_as_unreachable(self, adapter):
        """The other side of the polarity: a 404 nobody can attribute to RomM is a transport verdict.

        It degrades to a plain ``RommApiError``, which is what ``classify_error``
        already maps to ``server_unreachable`` everywhere else — the same
        fail-open reading that denies it deletion authority.
        """
        degraded = adapter.translate_http_error(_unattributable_404(), "http://romm.local/api/roms/4375")
        assert not isinstance(degraded, RommNotFoundError)
        assert classify_error(degraded)[0] == ErrorCode.SERVER_UNREACHABLE.value
        self._failing_ladder(adapter, degraded)

        fn = MagicMock(side_effect=RommConnectionError("refused"))
        with patch("time.sleep"), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=0)
        fn.assert_called_once()

    def test_a_sleeping_ladder_gives_up_once_another_lane_reports_unreachable(self, adapter):
        """The eight lanes a game-detail page opens start together — this is what makes the FIRST load fast."""
        fn = MagicMock(side_effect=RommConnectionError("refused"))

        def other_lane_gives_up(_seconds):
            adapter._retry._note_unreachable(RommConnectionError("refused"))

        with patch("time.sleep", side_effect=other_lane_gives_up), pytest.raises(RommConnectionError):
            adapter.with_retry(fn, base_delay=1)
        # Attempt 1 failed, the backoff was cut short, attempt 2 never ran.
        fn.assert_called_once()
