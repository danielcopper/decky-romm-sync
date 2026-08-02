from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lib.prune_gate import (
    acquire_prune_conflict_lease,
    prune_active_blocked,
    prune_exclusive_start,
    release_orphaned_frontend_leases,
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

    unwired = Unwired()
    with pytest.raises(RuntimeError, match="_prune_service is unwired"):
        await unwired.mutate()


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
async def test_conflicting_operation_is_refused_without_awaiting_a_slow_prune_admission() -> None:
    admitting = asyncio.Event()
    finish_admission = asyncio.Event()
    order: list[str] = []

    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            admitting.set()
            await finish_admission.wait()
            order.append("admission")
            return {"success": True}

    owner = Owner(False)
    admission = asyncio.create_task(owner.start_prune())
    await admitting.wait()

    result = await owner.mutate()
    order.append("mutation")

    assert result["success"] is False
    assert result["reason"] == "prune_active"
    assert owner.called is False
    assert order == ["mutation"]

    finish_admission.set()
    assert await admission == {"success": True}
    assert order == ["mutation", "admission"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["refused", "raised"])
async def test_prune_claim_is_released_when_admission_does_not_start_a_run(outcome) -> None:
    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            if outcome == "raised":
                raise RuntimeError("admission blew up")
            return {"success": False, "reason": "stale_preview", "message": "stale"}

    owner = Owner(False)
    if outcome == "raised":
        with pytest.raises(RuntimeError, match="admission blew up"):
            await owner.start_prune()
    else:
        assert (await owner.start_prune())["reason"] == "stale_preview"

    assert await owner.mutate() == {"success": True}
    assert owner.called is True


@pytest.mark.asyncio
async def test_detached_task_retains_conflict_claim_for_its_full_lifetime() -> None:
    release = asyncio.Event()

    class Owner(_Owner):
        @prune_exclusive_start
        async def start_prune(self):
            return {"success": True}

    owner = Owner(False)
    task = asyncio.create_task(release.wait())
    await retain_prune_conflict(owner, task, "start_download")
    assert (await owner.start_prune())["reason"] == "operation_active"

    release.set()
    await task
    await asyncio.sleep(0)
    assert await owner.start_prune() == {"success": True}


class _RecordingLogger:
    def __init__(self) -> None:
        self.info_lines: list[str] = []

    def info(self, message: str) -> None:
        self.info_lines.append(message)


class _LoggingOwner(_Owner):
    def __init__(self) -> None:
        super().__init__(False)
        self._prune_gate_logger = _RecordingLogger()
        self.debug_lines: list[str] = []

    def _log_debug(self, msg: str) -> None:
        self.debug_lines.append(msg)

    @prune_exclusive_start
    async def start_prune(self) -> dict[str, Any]:
        return {"success": True}


@pytest.mark.asyncio
async def test_refusal_logs_the_holder_that_is_actually_blocking() -> None:
    owner = _LoggingOwner()
    await acquire_prune_conflict_lease(owner, "launch_reconfirm")

    result = await owner.start_prune()

    assert result["reason"] == "operation_active"
    # The line that would have identified F9's holder instantly.
    refusal = next(line for line in owner._prune_gate_logger.info_lines if "admission refused" in line)
    assert "launch_reconfirm" in refusal
    assert "lease launch_reconfirm:1" in refusal
    assert "held 0s" in refusal
    assert "expires in" in refusal


@pytest.mark.asyncio
async def test_refusal_message_names_a_labelled_holder_in_plain_language() -> None:
    owner = _LoggingOwner()
    await acquire_prune_conflict_lease(owner, "launch_reconfirm")

    result = await owner.start_prune()

    assert "checking a game's launch settings" in result["message"]
    assert "wait for it to finish" in result["message"]


@pytest.mark.asyncio
async def test_refusal_message_stays_generic_for_an_unnamed_holder() -> None:
    owner = _LoggingOwner()
    await acquire_prune_conflict_lease(owner, "some_internal_key")

    result = await owner.start_prune()

    # An internal token must never reach the user as if it were a sentence.
    assert "some_internal_key" not in result["message"]
    assert result["message"] == (
        "Another local-data operation is in progress; wait for it to finish before starting cleanup."
    )
    # …but the log still names it, because that is where diagnosis happens.
    assert any("some_internal_key" in line for line in owner._prune_gate_logger.info_lines)


@pytest.mark.asyncio
async def test_refusal_names_a_blocking_callable_registration() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Owner(_LoggingOwner):
        @prune_active_blocked
        async def set_game_core(self):
            entered.set()
            await release.wait()
            return {"success": True}

    owner = Owner()
    running = asyncio.create_task(owner.set_game_core())
    await entered.wait()

    await owner.start_prune()

    refusal = next(line for line in owner._prune_gate_logger.info_lines if "admission refused" in line)
    assert "set_game_core (operation" in refusal
    release.set()
    await running


@pytest.mark.asyncio
async def test_lease_lifecycle_is_traceable_at_debug() -> None:
    owner = _LoggingOwner()

    token = await acquire_prune_conflict_lease(owner, "sgdb_artwork")
    await renew_prune_conflict_lease(owner, token)
    await release_prune_conflict_lease(owner, token)

    joined = "\n".join(owner.debug_lines)
    assert f"acquired lease {token}" in joined
    assert f"renewed lease {token}" in joined
    assert f"released lease {token}" in joined
    # Lifecycle is debug-only; it must not spam the INFO log.
    assert owner._prune_gate_logger.info_lines == []


@pytest.mark.asyncio
async def test_expired_lease_is_reported_at_info_as_never_released(monkeypatch) -> None:
    owner = _LoggingOwner()
    monkeypatch.setattr("lib.prune_gate._LEASE_SECONDS", 0.0)
    await acquire_prune_conflict_lease(owner, "installed_reconcile")

    assert await owner.start_prune() == {"success": True}

    # A lease reaching its deadline means its owner leaked it — visible without
    # having to turn debug logging on first.
    assert any(
        "installed_reconcile" in line and "expired" in line and "without being released" in line
        for line in owner._prune_gate_logger.info_lines
    )


@pytest.mark.asyncio
async def test_detached_retention_is_labelled_by_its_originating_callable() -> None:
    owner = _LoggingOwner()
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    await retain_prune_conflict(owner, task, "start_download")

    await owner.start_prune()

    refusal = next(line for line in owner._prune_gate_logger.info_lines if "admission refused" in line)
    assert "start_download (operation" in refusal
    release.set()
    await task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_new_frontend_disowns_a_lease_its_predecessor_stranded() -> None:
    owner = _LoggingOwner()
    # The double mount at plugin load: mount 1 acquires, its context dies before
    # the continuation that would release, so nothing ever releases or renews.
    await acquire_prune_conflict_lease(owner, "installed_reconcile")
    assert (await owner.start_prune())["reason"] == "operation_active"

    released = await release_orphaned_frontend_leases(owner)

    assert released == 1
    assert await owner.start_prune() == {"success": True}
    assert any(
        "orphaned lease installed_reconcile:1" in line and "no longer mounted" in line
        for line in owner._prune_gate_logger.info_lines
    )


@pytest.mark.asyncio
async def test_disowning_leaves_callable_registrations_and_run_claims_alone() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Owner(_LoggingOwner):
        @prune_active_blocked
        async def set_game_core(self):
            entered.set()
            await release.wait()
            return {"success": True}

    owner = Owner()
    running = asyncio.create_task(owner.set_game_core())
    await entered.wait()

    # Only the frontend's own leases are the frontend's to disown; a live
    # callable still holds the gate on its own account.
    assert await release_orphaned_frontend_leases(owner) == 0
    assert (await owner.start_prune())["reason"] == "operation_active"

    release.set()
    await running


@pytest.mark.asyncio
async def test_disowning_an_empty_gate_is_a_silent_no_op() -> None:
    owner = _LoggingOwner()

    assert await release_orphaned_frontend_leases(owner) == 0

    # An ordinary mount must not log as though it cleaned something up.
    assert owner._prune_gate_logger.info_lines == []


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
