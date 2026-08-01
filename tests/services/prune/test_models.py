"""Tests for services/prune/_models.py — the cleanup context's shared state helpers."""

import asyncio

import pytest

from services.prune._models import cancellation_state, shielded


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

    with pytest.raises(OSError, match="child failed"):
        await shielded(child())


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
    """A child that is itself cancelled must not replace the captured cancellation.

    The child's own ``CancelledError`` carries whatever state happens to be
    attached to it — never what this run captured — so letting it propagate
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
