"""Fixtures for the callable contract-test tier.

Exposes the ``harness`` fixture: a real :class:`main.Plugin` wired through
the real :func:`bootstrap` with only the network edges faked. See
:mod:`tests.contract._harness` for the build recipe and the real-vs-faked
boundary.
"""

from __future__ import annotations

import pytest

from tests.contract._harness import ContractHarness, build_contract_harness


@pytest.fixture
async def harness(tmp_path) -> ContractHarness:
    """A wired real ``Plugin`` plus the fake edges a contract test drives.

    Async so the harness binds the test's *running* event loop into the
    services' ``RuntimeBundle.loop`` — the callables ``await`` on that loop,
    and a mismatched loop would raise "got Future attached to a different
    loop" on the first ``run_in_executor``.

    Each test gets a fresh ``tmp_path`` (own SQLite db, settings file, file
    stores), so state never leaks between contract tests.
    """
    return build_contract_harness(tmp_path)
