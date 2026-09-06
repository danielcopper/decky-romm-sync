"""Fixtures for the callable contract-test tier.

Exposes the ``harness`` fixture: a real :class:`main.Plugin` wired through
the real :func:`bootstrap` with only the network edges faked. See
:mod:`tests.contract._harness` for the build recipe and the real-vs-faked
boundary.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tests.contract._harness import ContractHarness, build_contract_harness


@pytest.fixture(autouse=True)
def _isolate_system_flatpak_root(tmp_path):
    """Keep the contract harness hermetic — never read the host's real RetroDECK.

    **Three readers can reach the host's system flatpak root, through two
    constants.** The plugin's own two — ``EsFindRulesAdapter`` and the RetroArch
    core-info reader — go through ``adapters.flatpak_install.SYSTEM_FLATPAK_ROOT``.
    The third is the vendored resolver, which resolves the RetroDECK deploy its
    ``/app`` paths live in through its own ``_running_deploy``
    (``_vendor/atlas/installations.py``) off ``_FLATPAK_DEPLOY_SYSTEM``, and has
    never heard of the plugin's constant. So the emulator catalogue needs the
    second patch: with only the first, a ``tmp_path`` home carrying just
    ``retrodeck.json`` answers out of the dev box's real ES-DE tree — measured,
    five ``gba`` entries and 172 systems.

    Both are repointed under ``tmp_path`` so the per-user seed is the only
    source and contract tests are deterministic on any machine, with or without
    RetroDECK installed.

    Patching a **private vendored name** is the accepted cost here. The clean
    seam upstream offers is a fixture ``Machine`` passed to
    ``detect(home, machine=...)``, and this tier cannot reach it: the contract
    harness builds the real ``bootstrap()``, whose chooser calls ``detect`` with
    the real machine by design — injecting a fake would make the tier test
    something other than production wiring. A rename upstream fails this fixture
    loudly on the next version bump, which is the failure mode to prefer over a
    silent re-leak.
    """
    with (
        mock.patch("adapters.flatpak_install.SYSTEM_FLATPAK_ROOT", str(tmp_path / "no_system_flatpak")),
        mock.patch("_vendor.atlas.installations._FLATPAK_DEPLOY_SYSTEM", str(tmp_path / "no_system_flatpak" / "app")),
    ):
        yield


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
