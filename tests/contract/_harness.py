"""Contract-test harness — the real ``Plugin`` over the real ``bootstrap()``.

This tier drives the actual ``main.py`` callable surface the way the
frontend does. Anything that builds, wires, or seeds the real plugin for
a contract test belongs here; the per-callable contract assertions live
in the sibling ``test_*.py`` modules.

What stays real vs. what is faked
=================================

The harness calls the **real** :func:`bootstrap` (real settings dict,
real SQLite database + migrations, real file-store adapters) rooted under
a pytest ``tmp_path`` and wires the **real** services via the real
:func:`wire_services`. Only the outermost edges are swapped:

* ``romm_api`` → :class:`FakeRommApi` (the network transport — so tests
  seed library/saves/server state and inject failures without HTTP).
* ``sgdb_adapter`` → :class:`FakeSteamGridDbApi` (the SteamGridDB
  network transport).
* ``clock`` / ``uuid_gen`` / ``sleeper`` → the deterministic fakes from
  ``tests.fakes.system_time`` so timestamped responses are assertable.
* ``http_adapter.with_retry`` → a single-attempt pass-through so a
  failure-injection test does not pay the real exponential backoff
  ``time.sleep`` (1s, 3s …). Everything else on the real
  ``RommHttpAdapter`` (``resolve_system``, settings binding) stays real —
  it is a pure settings/file read with no network on the read paths.
* ``emit`` → an ``AsyncMock`` so tests assert emissions.

The SQLite database, the file stores, and the settings file all write
into ``tmp_path``; that is intended and isolated per test (pytest hands
each test a fresh ``tmp_path``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from bootstrap import (
    RuntimeBundle,
    WiringConfig,
    bootstrap,
    wire_services,
)
from fakes.fake_romm_api import FakeRommApi
from fakes.fake_steamgrid_db_api import FakeSteamGridDbApi
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

if TYPE_CHECKING:
    from collections.abc import Callable

# Imported lazily inside the builder so ``conftest`` import order (which sets
# up ``sys.path`` and the mock ``decky`` module) is respected before ``main``
# is touched.

# The service attributes ``main.py:_main`` binds onto ``Plugin``. The harness
# binds the same set; the loud-failure assert below checks every one is present
# so a wiring drift (a renamed/added service key) fails the fixture instead of
# surfacing as a confusing ``AttributeError`` mid-test.
_BOUND_SERVICE_ATTRS = {
    "_save_sync_service": "save_sync_service",
    "_playtime_service": "playtime_service",
    "_sync_service": "sync_service",
    "_download_service": "download_service",
    "_rom_removal_service": "rom_removal_service",
    "_firmware_service": "firmware_service",
    "_sgdb_service": "sgdb_service",
    "_metadata_service": "metadata_service",
    "_achievements_service": "achievements_service",
    "_migration_service": "migration_service",
    "_game_detail_service": "game_detail_service",
    "_artwork_service": "artwork_service",
    "_shortcut_removal_service": "shortcut_removal_service",
    "_settings_service": "settings_service",
    "_core_service": "core_service",
    "_connection_service": "connection_service",
    "_startup_healing_service": "startup_healing_service",
    "_launch_gate_service": "launch_gate_service",
    "_session_lifecycle_service": "session_lifecycle_service",
}


@dataclass
class ContractHarness:
    """What a contract test reaches for: the wired plugin + the fake edges.

    ``plugin`` is the real :class:`main.Plugin` with every service wired.
    ``romm`` is the :class:`FakeRommApi` the plugin's services talk to —
    tests seed library/saves/server state on it and arm its failure seams.
    ``sgdb`` is the :class:`FakeSteamGridDbApi`. ``emit`` is the
    ``AsyncMock`` the runtime emits through. ``clock`` is the deterministic
    :class:`FakeClock`. ``tmp_path`` is the per-test root every real adapter
    writes under.
    """

    plugin: Any
    romm: FakeRommApi
    sgdb: FakeSteamGridDbApi
    emit: AsyncMock
    clock: FakeClock
    tmp_path: Any
    # The real SQLite Unit-of-Work factory the wired services use. Tests open it
    # to seed relational state (roms / rom_installs / rom_save_states / kv_config)
    # exactly as the services read it — same database, same connection contract.
    uow_factory: Any


def _single_attempt_pass_through(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``fn`` exactly once, no retry/backoff — replaces ``http_adapter.with_retry``.

    The real ``with_retry`` sleeps with exponential backoff on retryable
    transport errors; a failure-injection contract test would otherwise pay
    real wall-clock seconds. The single-attempt pass-through preserves the
    contract (the wrapped call's value or exception propagates verbatim).
    """
    return fn(*args, **kwargs)


def build_contract_harness(tmp_path: Any) -> ContractHarness:
    """Build the real ``Plugin`` over the real ``bootstrap()``, faking only the edges.

    Mirrors ``main.py:_main`` bindings exactly (see ``_BOUND_SERVICE_ATTRS``)
    so the wired plugin behaves as it does in production, then asserts every
    expected service attribute is bound — a wiring drift fails loudly here.
    """
    from main import Plugin

    logger = logging.getLogger("contract")

    # 1. Real bootstrap — real settings dict, real SQLite + migrations, real
    #    file-store adapters, all rooted under tmp_path.
    result = bootstrap(
        settings_dir=str(tmp_path / "settings"),
        runtime_dir=str(tmp_path / "runtime"),
        plugin_dir=str(tmp_path / "plugin"),
        user_home=str(tmp_path / "home"),
        logger=logger,
    )

    # 2. Swap only the network edges in the returned AdapterBundle. The bundle
    #    is frozen, so rebuild it with dataclasses.replace. The real
    #    http_adapter is kept (resolve_system is a pure read) but its with_retry
    #    is neutralised so failure-injection tests don't sleep.
    fake_romm = FakeRommApi()
    fake_sgdb = FakeSteamGridDbApi()
    real_http = result.adapters.http_adapter
    real_http.with_retry = _single_attempt_pass_through  # type: ignore[method-assign]
    patched_adapters = dataclasses.replace(
        result.adapters,
        romm_api=fake_romm,
        sgdb_adapter=fake_sgdb,
    )

    # Deterministic time/uuid/sleep seams so timestamped responses assert cleanly.
    fake_clock = FakeClock()
    fake_uuid = FakeUuidGen()
    fake_sleeper = FakeSleeper()

    emit = AsyncMock()
    # The running loop the test executes in. The harness is built from inside an
    # async fixture so this is the *same* loop the callables will await on —
    # binding the loop captured at module import (a different loop) would make
    # ``run_in_executor`` raise "got Future attached to a different loop".
    loop = asyncio.get_event_loop()

    # 3. Wire the real services with the patched bundle.
    cfg = WiringConfig(
        adapters=patched_adapters,
        stores=result.stores,
        runtime=RuntimeBundle(
            loop=loop,
            logger=logger,
            plugin_dir=str(tmp_path / "plugin"),
            runtime_dir=str(tmp_path / "runtime"),
            emit=emit,
            clock=fake_clock,
            uuid_gen=fake_uuid,
            sleeper=fake_sleeper,
            hostname_provider=result.runtime_adapters.hostname_provider,
            machine_id_provider=result.runtime_adapters.machine_id_provider,
        ),
        callbacks=result.callbacks,
        min_required_version=Plugin._MIN_REQUIRED_VERSION,
    )
    services = wire_services(cfg)

    # 4. Construct the real Plugin and bind exactly as main.py:_main does.
    plugin = Plugin()
    plugin.loop = loop
    plugin.settings = result.stores.settings
    plugin._debug_logger = result.handles.debug_logger
    plugin._persistence = result.handles.persistence
    plugin._retrodeck_paths = result.callbacks.retrodeck_paths
    for attr, key in _BOUND_SERVICE_ATTRS.items():
        setattr(plugin, attr, services[key])

    # Loud-failure guard: a wiring drift (renamed/added service) must fail the
    # fixture here, not as a confusing AttributeError deep in a contract test.
    missing = [attr for attr in _BOUND_SERVICE_ATTRS if getattr(plugin, attr, None) is None]
    assert not missing, f"contract harness wiring drift — unbound service attrs: {missing}"

    return ContractHarness(
        plugin=plugin,
        romm=fake_romm,
        sgdb=fake_sgdb,
        emit=emit,
        clock=fake_clock,
        tmp_path=tmp_path,
        uow_factory=result.callbacks.uow_factory,
    )
