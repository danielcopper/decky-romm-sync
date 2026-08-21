"""Attempt policy for RomM requests — the retry ladder and the reachability state.

Owns how often a request is attempted and how long the gaps are: the ladder
itself, the per-call-stack scope that keeps two levels of wrapping from
multiplying, the transport bit that says the RomM server is known unreachable,
and the optional listener that surfaces retry progress to the UI. The transport
module (``http.py``) owns the requests themselves and delegates every
attempt-count question here.
"""

import logging
import threading
import time
import urllib.error
from collections.abc import Callable

from lib.errors import (
    RommApiError,
    RommConnectionError,
    RommServerError,
    RommTimeoutError,
    classify_error,
)
from lib.list_result import ErrorCode

# Optional per-retry listener: ``(attempt, max_attempts, delay_s) -> None`` where
# *attempt* is the 1-based OVERALL attempt ordinal about to run (the initial try
# is 1, so the first retry is 2, the second 3 — it reads as "attempt N/M"). Fired
# from the ladder just before it sleeps ahead of a retry. Injected at the
# composition root (``bootstrap``) so the adapter stays service-free: it surfaces
# retry progress without importing ``decky`` or any service (the composition root
# marshals the call onto the loop and emits the ``server_retry_progress`` event).
RetryListener = Callable[[int, int, float], None]


class RetryLadder:
    """The attempt ladder RomM requests run on, and the reachability verdict it consults.

    Parameters
    ----------
    logger:
        Logger instance (replaces ``decky.logger``).
    on_retry:
        Optional :data:`RetryListener` invoked once per retry (before the
        backoff sleep) so the UI can surface "connecting… (attempt N/M)". A
        settable attribute, not a hard dependency: ``bootstrap`` wires the
        loop-threadsafe emit after construction (the loop/emit only exist at
        service-wiring time), and tests can pass a spy at construction.
    """

    _BACKOFF_POLL_INTERVAL = 0.25

    def __init__(self, logger: logging.Logger, on_retry: RetryListener | None = None) -> None:
        self._logger = logger
        self.on_retry = on_retry
        # Per-thread "a ladder is already running below me" flag. The blocking
        # work runs in the loop's default ThreadPoolExecutor, one worker per
        # concurrent call, so a thread is exactly one call stack.
        self._ladder = threading.local()
        # Shared across every lane: the adapter that owns this ladder is a single
        # process-wide instance handed by reference to all services. Written from
        # executor threads and read by all of them — a bare bool assignment is
        # atomic, and a stale read only ever costs one extra attempt.
        self._server_unreachable = False

    @staticmethod
    def is_retryable(exc: Exception) -> bool:
        """Check if an exception is a transient error worth retrying."""
        # TokenHostMismatchError is intentionally absent: a wrong-origin token
        # can never become right by retrying, so with_retry re-raises it at once.
        if isinstance(exc, RommServerError | RommConnectionError | RommTimeoutError):
            return True
        # Backward compat for non-RomM exceptions
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code >= 500
        return isinstance(exc, urllib.error.URLError | ConnectionError | TimeoutError | OSError)

    def with_retry(self, fn, *args, max_attempts: int = 3, base_delay: int = 1, **kwargs):
        """Call fn(*args, **kwargs) with exponential backoff retry.

        Delays: base_delay * 3^attempt — 1s then 3s for the 3-attempt default,
        which has only two gaps. Only retries on transient errors (see
        is_retryable).

        The OUTERMOST ladder wins, always. Services wrap adapter calls through
        the ``RetryStrategy`` protocol and most of those adapter methods already
        wrap themselves, so a nested ladder would run 3x3 attempts with both
        backoffs stacked; a nested call therefore runs *fn* straight through.
        The service-level wrap stays the ONLY ladder for the adapter methods
        that deliberately have none (``upload_multipart``, ``request_once``,
        ``unauthenticated_post_json``, ``basic_auth_request``), and for the
        coarse wraps whose callee paginates or chains several requests — those
        are then retried as the whole unit their caller meant.

        While the server is known unreachable the ladder shrinks to one attempt
        with no backoff. The call is still really made: that is what makes the
        state self-healing and what makes the UI's Retry button work with no
        path of its own.
        """
        return self.enter(fn, args, kwargs, max_attempts, base_delay, romm_origin=True)

    def enter(self, fn, args, kwargs, max_attempts: int = 3, base_delay: int = 1, *, romm_origin: bool):
        """Run *fn* through one ladder, or straight through when one is already active on this thread.

        *romm_origin* says whether the request this ladder wraps talks to the
        configured RomM server. Only such a ladder takes part in the
        known-unreachable state — ``download_external`` reaches a third-party
        cover CDN, whose failure says nothing about RomM and whose success
        proves nothing about it either.
        """
        if getattr(self._ladder, "active", False):
            return fn(*args, **kwargs)
        self._ladder.active = True
        try:
            return self._run_ladder(fn, args, kwargs, max_attempts, base_delay, romm_origin=romm_origin)
        finally:
            self._ladder.active = False

    def _run_ladder(self, fn, args, kwargs, max_attempts: int, base_delay: int, *, romm_origin: bool):
        """Run the attempt ladder for *fn* — :meth:`enter`'s body, once re-entrancy is ruled out."""
        attempts = 1 if (romm_origin and self._server_unreachable) else max_attempts
        for attempt in range(attempts):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                self._give_up_unless_worth_another_attempt(exc, attempt, attempts, romm_origin=romm_origin)
                delay = base_delay * (3**attempt)
                self._logger.info(f"Retry {attempt + 1}/{attempts} after {delay}s: {exc}")
                self._notify_retry(attempt + 2, attempts, float(delay))
                if not self._backoff(float(delay), romm_origin=romm_origin):
                    raise
        raise AssertionError("attempt ladder ran zero attempts")  # pragma: no cover

    def _give_up_unless_worth_another_attempt(
        self, exc: Exception, attempt: int, attempts: int, *, romm_origin: bool
    ) -> None:
        """Re-raise *exc* unless another attempt on the ladder is worth making.

        Another one is worth making only while the ladder has an attempt left
        AND the failure is transient (:meth:`is_retryable`). Otherwise this is
        where the ladder gives up, and a RomM-origin ladder records the
        reachability verdict on its way out — a give-up is the only place that
        bit is ever set.
        """
        if attempt < attempts - 1 and self.is_retryable(exc):
            return
        if romm_origin:
            self._note_unreachable(exc)
        raise exc

    def note_reachable(self) -> None:
        """Record that the RomM server answered, restoring the full ladder.

        The transport calls this from its single ``_urlopen`` choke point, so a
        response arriving on ANY path clears the state — the paths that
        deliberately skip :meth:`with_retry` included
        (``.claude/rules/romm-http.md``).
        """
        self._server_unreachable = False

    def _note_unreachable(self, exc: Exception) -> None:
        """Record that the ladder gave up because the server could not be reached.

        An error carrying a **4xx status code** is peeled off first: the server
        answered, whatever it answered. ``classify_error`` cannot draw that line
        on its own — it is a user-messaging classifier that folds every
        unbranched ``RommApiError`` onto ``server_unreachable`` so a display
        string always exists, which would sweep in the routine 409 that every
        ``overwrite=false`` save upload is designed to provoke, and the 422 and
        429 alongside it. Past the peel, ``classify_error`` stays the single
        definition of unreachable rather than a second, coarser one here.

        A 404 that arrives as a plain ``RommApiError`` carries no status code
        and therefore still counts: nothing proved RomM answered it, which is
        the same fail-open reading that denies it deletion authority
        (``.claude/rules/romm-http.md``).
        """
        status = exc.status_code if isinstance(exc, RommApiError) else None
        if status is not None and 400 <= status < 500:
            return
        if classify_error(exc)[0] == ErrorCode.SERVER_UNREACHABLE.value:
            self._server_unreachable = True

    def _backoff(self, delay: float, *, romm_origin: bool) -> bool:
        """Sleep *delay* seconds, returning ``False`` if the server went unreachable meanwhile.

        Polled rather than slept in one go because a game-detail page opens all
        of its lanes at once: the first lane to give up has to be able to cut
        the backoff of the ones already sleeping short, or only the SECOND page
        load after an outage is fast. A non-RomM ladder sleeps the gap out.
        """
        remaining = delay
        while remaining > 0 and not (romm_origin and self._server_unreachable):
            slice_s = min(self._BACKOFF_POLL_INTERVAL, remaining)
            time.sleep(slice_s)
            remaining -= slice_s
        return not (romm_origin and self._server_unreachable)

    def _notify_retry(self, attempt: int, max_attempts: int, delay_s: float) -> None:
        """Fire the optional retry listener, swallowing any listener failure.

        The listener surfaces best-effort UI progress; a raise from it (e.g. a
        closed loop during plugin unload) must never abort the real HTTP retry
        underway, so it is caught and logged rather than propagated.
        """
        if self.on_retry is None:
            return
        try:
            self.on_retry(attempt, max_attempts, delay_s)
        except Exception:  # progress emit is best-effort — never break a real retry
            self._logger.debug("on_retry listener raised; ignoring", exc_info=True)
