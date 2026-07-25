from __future__ import annotations

import asyncio

import pytest

from lib.prune_gate import (
    acquire_prune_conflict_lease,
    prune_active_blocked,
    prune_exclusive_start,
    release_prune_conflict_lease,
    renew_prune_conflict_lease,
    retain_prune_conflict,
)


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


@pytest.mark.asyncio
async def test_detached_task_retains_conflict_claim_for_its_full_lifetime() -> None:
    release = asyncio.Event()

    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            return {"success": True}

    owner = Owner(False)
    task = asyncio.create_task(release.wait())
    await retain_prune_conflict(owner, task)
    assert (await owner.start_prune())["reason"] == "operation_active"

    release.set()
    await task
    await asyncio.sleep(0)
    assert await owner.start_prune() == {"success": True}


@pytest.mark.asyncio
async def test_concurrent_multicall_leases_are_reference_counted() -> None:
    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            return {"success": True}

    owner = Owner(False)
    first = await acquire_prune_conflict_lease(owner, "shortcut_removal")
    second = await acquire_prune_conflict_lease(owner, "shortcut_removal")
    await release_prune_conflict_lease(owner, first)
    assert (await owner.start_prune())["reason"] == "operation_active"

    await release_prune_conflict_lease(owner, second)
    assert await owner.start_prune() == {"success": True}


@pytest.mark.asyncio
async def test_abandoned_multicall_lease_expires_before_prune_admission(monkeypatch) -> None:
    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            return {"success": True}

    owner = Owner(False)
    monkeypatch.setattr("lib.prune_gate._LEASE_SECONDS", 0.0)
    await acquire_prune_conflict_lease(owner, "shortcut_removal")

    assert await owner.start_prune() == {"success": True}


@pytest.mark.asyncio
async def test_renewed_frontend_lease_cannot_expire_while_heartbeats_continue(monkeypatch) -> None:
    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            return {"success": True}

    owner = Owner(False)
    monkeypatch.setattr("lib.prune_gate._LEASE_SECONDS", 0.05)
    token = await acquire_prune_conflict_lease(owner, "long_rebake")
    await asyncio.sleep(0.03)

    assert await renew_prune_conflict_lease(owner, token) is True
    await asyncio.sleep(0.03)
    assert (await owner.start_prune())["reason"] == "operation_active"

    await asyncio.sleep(0.03)
    assert await owner.start_prune() == {"success": True}
    assert await renew_prune_conflict_lease(owner, token) is False
