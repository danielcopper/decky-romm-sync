"""Tests for services/prune/save_locks.py — locks over a proven-stable owner set."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from services.prune.save_locks import SaveLockCoordinator, SaveLockCoordinatorConfig


class _FakeSaveCoordinator:
    """A save coordinator whose reported ownership the test can move mid-flight."""

    def __init__(self, inventories: list[dict[str, Any]]) -> None:
        self._inventories = inventories
        self.inventory_calls: list[list[int]] = []
        self.locked: list[list[int]] = []
        self.held: list[int] | None = None
        self.released: list[list[int]] = []

    def inventory_prune_saves(self, purge_rom_ids: list[int]) -> dict[str, Any]:
        self.inventory_calls.append(list(purge_rom_ids))
        index = min(len(self.inventory_calls) - 1, len(self._inventories) - 1)
        return self._inventories[index]

    @contextlib.asynccontextmanager
    async def lock_prune_roms(self, rom_ids: list[int]):
        self.locked.append(list(rom_ids))
        self.held = list(rom_ids)
        try:
            yield
        finally:
            self.released.append(list(rom_ids))
            self.held = None

    def quarantine_prune_saves(self, files, claims=None) -> dict[str, Any]:  # pragma: no cover - unused here
        raise NotImplementedError

    def validate_prune_absences(self, claims) -> bool:  # pragma: no cover - unused here
        raise NotImplementedError


def _coordinator(inventories: list[dict[str, Any]]) -> tuple[SaveLockCoordinator, _FakeSaveCoordinator]:
    saves = _FakeSaveCoordinator(inventories)
    return (
        SaveLockCoordinator(config=SaveLockCoordinatorConfig(loop=asyncio.get_event_loop(), save_coordinator=saves)),
        saves,
    )


class TestStableLocks:
    async def test_locks_the_owner_set_and_yields_the_inventory_it_protects(self):
        inventory = {"lock_rom_ids": [1, 2], "warnings": []}
        locks, saves = _coordinator([inventory])

        async with locks.stable_locks({1}) as held:
            assert held == inventory
            assert saves.held == [1, 2], "the widened owner set must be locked, not just the purge id"

        assert saves.released == [[1, 2]]

    async def test_retries_when_ownership_widens_under_the_lock(self):
        """The yielded inventory must be the one the held locks actually cover."""
        narrow = {"lock_rom_ids": [1]}
        widened = {"lock_rom_ids": [1, 2]}
        # read → lock [1] → re-read says [1, 2] → retry → lock [1, 2] → stable.
        locks, saves = _coordinator([narrow, widened, widened, widened])

        async with locks.stable_locks({1}) as held:
            assert held == widened
            assert saves.held == [1, 2]

        assert saves.locked == [[1], [1, 2]]

    async def test_refuses_rather_than_acting_on_an_ownership_set_that_never_settles(self):
        alternating = [{"lock_rom_ids": [1]}, {"lock_rom_ids": [1, 2]}] * 6
        locks, saves = _coordinator(alternating)

        stable = locks.stable_locks({1})
        with pytest.raises(RuntimeError, match="Save ownership kept changing"):
            async with stable:
                pytest.fail("the body must never run on an unstable ownership set")

        assert saves.held is None, "every attempt's lock is released"
        assert len(saves.locked) == 3, "bounded retries, not an unbounded loop"

    async def test_falls_back_to_the_requested_ids_when_no_owner_set_is_reported(self):
        locks, saves = _coordinator([{"warnings": []}])

        async with locks.stable_locks({2, 1}):
            assert saves.held == [1, 2]

    async def test_releases_the_lock_when_the_body_raises(self):
        locks, saves = _coordinator([{"lock_rom_ids": [1]}])

        stable = locks.stable_locks({1})
        with pytest.raises(ValueError, match="body failed"):
            async with stable:
                raise ValueError("body failed")

        assert saves.released == [[1]]
        assert saves.held is None


class TestInventoryWarnings:
    def test_coerces_every_warning_to_a_string(self):
        assert SaveLockCoordinator.inventory_warnings({"warnings": ["a", 7]}) == ["a", "7"]

    @pytest.mark.parametrize("inventory", [{}, {"warnings": None}, {"warnings": "not a list"}])
    def test_a_missing_or_malformed_warnings_field_yields_none(self, inventory):
        assert SaveLockCoordinator.inventory_warnings(inventory) == []
