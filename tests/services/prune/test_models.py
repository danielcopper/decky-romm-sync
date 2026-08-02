"""Tests for services/prune/_models.py — the cleanup context's shared state helpers."""

import asyncio

import pytest

from services.prune._models import BackupControl, cancellation_state, shielded


class TestCancellationState:
    def test_attaches_one_state_object_to_an_error_and_returns_it_again(self):
        error = asyncio.CancelledError()
        state = cancellation_state(error)
        state.group_result = {"status": "partial"}
        assert cancellation_state(error) is state
        assert cancellation_state(error).group_result == {"status": "partial"}

    def test_a_fresh_error_starts_with_an_empty_state(self):
        state = cancellation_state(asyncio.CancelledError())
        assert state.action_result is None
        assert state.group_result is None
        assert state.child_result is None
        assert state.child_completed is False
        assert state.child_fault is None

    def test_two_errors_never_share_a_state(self):
        first, second = asyncio.CancelledError(), asyncio.CancelledError()
        cancellation_state(first).group_result = {"status": "partial"}
        assert cancellation_state(second).group_result is None


@pytest.mark.asyncio
async def test_shielded_returns_the_child_result_when_nothing_is_cancelled():
    async def child():
        return "finished"

    assert await shielded(child()) == "finished"


@pytest.mark.asyncio
async def test_shielded_propagates_a_child_fault_when_nothing_is_cancelled():
    async def child():
        raise OSError("child failed")

    pending = child()
    with pytest.raises(OSError, match="child failed"):
        await shielded(pending)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [False, True])
async def test_shielded_child_finishes_then_reraises_cancellation_with_fault_state(fault):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child():
        entered.set()
        await release.wait()
        if fault:
            raise OSError("child failed after cancellation")
        return "finished"

    task = asyncio.create_task(shielded(child()))
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    state = cancellation_state(caught.value)
    assert task.cancelled()
    if fault:
        assert isinstance(state.child_fault, OSError)
        assert str(state.child_fault) == "child failed after cancellation"
    else:
        assert state.child_fault is None


@pytest.mark.asyncio
async def test_shielded_cancelled_child_keeps_the_original_cancellation_state():
    """A child that is itself cancelled must not replace the captured state.

    The child's own ``CancelledError`` carries whatever state happens to be
    attached to it — never what this run captured — so propagating it unchanged
    would hand the group handler a foreign record of what the child did.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child():
        entered.set()
        await release.wait()
        foreign = asyncio.CancelledError()
        cancellation_state(foreign).child_result = "state from the child's own cancellation"
        raise foreign

    task = asyncio.create_task(shielded(child()))
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    state = cancellation_state(caught.value)
    assert task.cancelled()
    assert state.child_result is None
    assert state.child_completed is False
    assert state.child_fault is None


class TestBackupControl:
    def test_starts_un_aborted_and_latches_once_asked(self):
        control = BackupControl()
        assert control.is_aborted() is False
        control.abort()
        assert control.is_aborted() is True

    def test_aborting_twice_is_the_same_as_aborting_once(self):
        control = BackupControl()
        control.abort()
        control.abort()
        assert control.is_aborted() is True

    def test_the_poll_is_a_callable_a_worker_thread_can_hold(self):
        """The worker is handed the bound method, not the control object."""
        control = BackupControl()
        poll = control.is_aborted
        assert poll() is False
        control.abort()
        assert poll() is True


@pytest.mark.asyncio
async def test_shielded_runs_on_cancel_before_awaiting_the_child():
    """The child must learn it should stop while it can still act on it."""
    observed: list[str] = []
    release = asyncio.Event()

    async def child():
        observed.append("child-started")
        await release.wait()
        observed.append("child-finished")
        return "done"

    def on_cancel() -> None:
        observed.append("on-cancel")
        release.set()

    task = asyncio.create_task(shielded(child(), on_cancel=on_cancel))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert observed == ["child-started", "on-cancel", "child-finished"]
    state = cancellation_state(caught.value)
    assert state.child_result == "done"
    assert state.child_completed is True


@pytest.mark.asyncio
async def test_shielded_without_on_cancel_still_waits_the_child_out():
    """The committed phase passes nothing and is awaited to its natural end."""
    finished = []

    async def child():
        await asyncio.sleep(0)
        finished.append("committed")
        return "committed"

    task = asyncio.create_task(shielded(child()))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert finished == ["committed"]
    assert cancellation_state(caught.value).child_result == "committed"


class _ExitRequest(BaseException):
    """Stands in for KeyboardInterrupt/SystemExit.

    A real one would abort the pytest session rather than be asserted on; this
    exercises the same branch — a BaseException that is not an Exception.
    """


@pytest.mark.asyncio
async def test_shielded_lets_an_interpreter_level_exit_through():
    """An exit request is not a fault this run reports — it takes the process.

    The fault capture exists so a group can say what its child did. An exit
    request is not that: absorbing it into a group result would keep the
    interpreter alive on the strength of a cleanup handler.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child():
        entered.set()
        await release.wait()
        raise _ExitRequest

    task = asyncio.create_task(shielded(child()))
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(_ExitRequest):
        await task


@pytest.mark.asyncio
async def test_shielded_still_captures_an_ordinary_child_fault():
    """The converse: an ordinary error is captured, and the cancellation wins."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child():
        entered.set()
        await release.wait()
        raise OSError("child failed")

    task = asyncio.create_task(shielded(child()))
    await entered.wait()
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError) as caught:
        await task

    assert isinstance(cancellation_state(caught.value).child_fault, OSError)
