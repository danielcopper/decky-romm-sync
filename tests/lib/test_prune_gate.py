from __future__ import annotations

import asyncio

import pytest

from lib.prune_gate import prune_active_blocked, prune_exclusive_start


class _PruneState:
    def __init__(self, active: bool) -> None:
        self.active = active

    def is_active(self) -> bool:
        return self.active


class _Owner:
    def __init__(self, active: bool) -> None:
        self._prune_service = _PruneState(active)
        self.called = False

    @prune_active_blocked
    async def mutate(self):
        self.called = True
        return {"success": True}


@pytest.mark.asyncio
async def test_blocks_conflicting_operation_with_canonical_shape() -> None:
    owner = _Owner(True)
    result = await owner.mutate()
    assert result["success"] is False
    assert result["reason"] == "prune_active"
    assert result["message"]
    assert owner.called is False


@pytest.mark.asyncio
async def test_allows_operation_when_prune_is_idle() -> None:
    owner = _Owner(False)
    assert await owner.mutate() == {"success": True}
    assert owner.called is True


@pytest.mark.asyncio
async def test_missing_prune_wiring_fails_loud() -> None:
    class Unwired:
        @prune_active_blocked
        async def mutate(self):
            return {"success": True}

    with pytest.raises(RuntimeError, match="_prune_service is unwired"):
        await Unwired().mutate()


@pytest.mark.asyncio
async def test_prune_start_refuses_operation_that_entered_before_it() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Owner(_Owner):
        @prune_active_blocked
        async def slow_mutation(self):
            entered.set()
            await release.wait()
            return {"success": True}

        @prune_exclusive_start
        async def start_prune(self):
            return {"success": True}

    owner = Owner(False)
    mutation = asyncio.create_task(owner.slow_mutation())
    await entered.wait()
    result = await owner.start_prune()
    assert result["success"] is False
    assert result["reason"] == "operation_active"
    release.set()
    await mutation
