"""Tests for SaveSyncGate — the device-level single-owner serialization gate.

Covers the bounded-acquire discipline around the gate's lock: in-flight
reporting, queue serialization across overlapping runs, the bounded-wait
timeout, release-on-body-exception, and the no-lock-leak invariant that
guards the acquire/timeout race.
"""

import asyncio

import pytest

from services.saves.sync_engine import SaveSyncGate, SaveSyncTimeoutError


class TestBoundedRunHappyPath:
    @pytest.mark.asyncio
    async def test_in_flight_true_inside_false_outside(self):
        gate = SaveSyncGate()
        assert gate.is_in_flight() is False
        async with gate.bounded_run(max_wait=5.0):
            assert gate.is_in_flight() is True
        assert gate.is_in_flight() is False

    @pytest.mark.asyncio
    async def test_body_value_and_clean_release(self):
        """A run completes, releases, and a second uncontended run acquires."""
        gate = SaveSyncGate()
        async with gate.bounded_run(max_wait=5.0):
            pass
        # Lock is free again — a second run acquires without waiting.
        async with gate.bounded_run(max_wait=5.0):
            assert gate.is_in_flight() is True
        assert gate.is_in_flight() is False


class TestBoundedRunTimeout:
    @pytest.mark.asyncio
    async def test_second_caller_times_out(self):
        """While one coroutine holds the gate, a bounded second caller times out."""
        gate = SaveSyncGate()
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with gate.bounded_run(max_wait=5.0):
                holding.set()
                await release.wait()

        task = asyncio.create_task(holder())
        await holding.wait()
        try:
            with pytest.raises(SaveSyncTimeoutError):
                async with gate.bounded_run(max_wait=0.05):
                    pytest.fail("body must not run when the gate cannot be acquired")
        finally:
            release.set()
            await task


class TestBoundedRunReleaseOnException:
    @pytest.mark.asyncio
    async def test_body_exception_still_releases(self):
        """An exception inside the body still releases the lock."""
        gate = SaveSyncGate()

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with gate.bounded_run(max_wait=5.0):
                raise Boom

        # The lock was released despite the body raising — a later run acquires.
        assert gate.is_in_flight() is False
        async with gate.bounded_run(max_wait=5.0):
            assert gate.is_in_flight() is True
        assert gate.is_in_flight() is False


class TestBoundedRunSerialization:
    @pytest.mark.asyncio
    async def test_two_overlapping_runs_serialize(self):
        """Two overlapping bounded runs both complete, one strictly after the other."""
        gate = SaveSyncGate()
        events: list[str] = []
        first_inside = asyncio.Event()

        async def first():
            async with gate.bounded_run(max_wait=5.0):
                events.append("first-enter")
                first_inside.set()
                # Hold long enough that ``second`` is forced to wait its turn.
                await asyncio.sleep(0.05)
                events.append("first-exit")

        async def second():
            # Make sure ``first`` is already inside before we contend.
            await first_inside.wait()
            async with gate.bounded_run(max_wait=5.0):
                events.append("second-enter")
                events.append("second-exit")

        await asyncio.gather(first(), second())

        # Serialized: second only entered after first fully exited.
        assert events == ["first-enter", "first-exit", "second-enter", "second-exit"]


class TestBoundedRunNoLockLeak:
    @pytest.mark.asyncio
    async def test_no_lock_leak_after_timeout(self):
        """After a timeout the gate is free once the holder releases — no phantom hold.

        Guards the acquire/timeout race: a timed-out acquire must not leave a
        leaked hold that survives the legitimate holder releasing.
        """
        gate = SaveSyncGate()
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder():
            async with gate.bounded_run(max_wait=5.0):
                holding.set()
                await release.wait()

        task = asyncio.create_task(holder())
        await holding.wait()

        # A bounded caller times out while the holder is in flight.
        with pytest.raises(SaveSyncTimeoutError):
            async with gate.bounded_run(max_wait=0.05):
                pytest.fail("body must not run on timeout")

        # Release the legitimate holder; the gate must now be fully free.
        release.set()
        await task
        assert gate.is_in_flight() is False

        # And a fresh run acquires immediately — the timed-out acquire left no leak.
        async with gate.bounded_run(max_wait=1.0):
            assert gate.is_in_flight() is True
        assert gate.is_in_flight() is False

    @pytest.mark.asyncio
    async def test_acquire_winning_photo_finish_is_released(self, monkeypatch):
        """An acquire that wins a photo-finish with the deadline is still released.

        Simulates the race where ``acquire()`` completes (so the lock IS held)
        but the bounding context raises ``TimeoutError`` on exit anyway. The gate
        must release the just-acquired lock before raising, leaving no leak.
        """
        gate = SaveSyncGate()

        class _RaiseOnExitTimeout:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                raise TimeoutError

        monkeypatch.setattr(
            "services.saves.sync_engine._gate.asyncio.timeout",
            lambda _t: _RaiseOnExitTimeout(),
        )

        with pytest.raises(SaveSyncTimeoutError):
            async with gate.bounded_run(max_wait=5.0):
                pytest.fail("body must not run when the bounding context times out")

        # The just-acquired lock was released despite the photo-finish timeout.
        monkeypatch.undo()
        assert gate.is_in_flight() is False
        async with gate.bounded_run(max_wait=1.0):
            assert gate.is_in_flight() is True
        assert gate.is_in_flight() is False
