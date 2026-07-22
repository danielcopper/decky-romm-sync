import atexit
import logging
import os
import shutil
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck, settings

# CI-safe hypothesis profile: deadline=None avoids timing flakes on shared CI
# runners; 200 examples balances coverage against suite runtime. Loaded by
# default for every property test (#1028).
settings.register_profile(
    "ci",
    deadline=None,
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("ci")

# Mirror Decky's sys.path setup: add py_modules/ so `from lib.xxx import` works
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tests_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_project_root, "py_modules"))
# Add tests/ root so subdirectory tests can still import from fakes/ and conftest
sys.path.insert(0, _tests_root)


# Import-time temp dirs, so the mock module is complete before anything imports
# main. The settings/runtime pair is replaced per test by `_reset_decky_mock_paths`;
# the log dir lives for the whole process.
_process_settings_dir = tempfile.mkdtemp()
_process_runtime_dir = tempfile.mkdtemp()
_process_log_dir = tempfile.mkdtemp()


@atexit.register
def _cleanup_process_temp_dirs() -> None:
    """Drop the import-time temp dirs — ``mkdtemp`` never cleans up after itself.

    Deliberately ``atexit`` rather than a session-scoped fixture: a fixture
    only tears down when at least one test is collected, and this module is
    imported twice per run (pytest's own copy plus the plain ``conftest``
    that `tests/` modules import directly). Both copies register here, so
    both sets of directories are released.
    """
    for path in (_process_settings_dir, _process_runtime_dir, _process_log_dir):
        shutil.rmtree(path, ignore_errors=True)


# Create mock decky module before any imports of main
mock_decky = MagicMock()
mock_decky.DECKY_PLUGIN_DIR = _project_root
mock_decky.DECKY_PLUGIN_SETTINGS_DIR = _process_settings_dir
mock_decky.DECKY_PLUGIN_RUNTIME_DIR = _process_runtime_dir
mock_decky.DECKY_PLUGIN_LOG_DIR = _process_log_dir
mock_decky.DECKY_USER_HOME = os.path.expanduser("~")
mock_decky.logger = logging.getLogger("test_romm")
mock_decky.emit = AsyncMock()

sys.modules["decky"] = mock_decky


def _no_retry(fn, *a, **kw):
    """Pass-through Retry side_effect: invoke the wrapped callable once, no backoff."""
    return fn(*a, **kw)


def _make_retry():
    """Build a Retry ``MagicMock`` that runs ``with_retry`` callables exactly once
    and reports every exception as non-retryable. Used everywhere services
    take a ``Retry`` Protocol injection in tests."""
    retry = MagicMock()
    retry.with_retry.side_effect = _no_retry
    retry.is_retryable.return_value = False
    return retry


def _make_testable_plugin():
    """Return a TestablePlugin instance with test-only attributes declared.

    Pre-populates ``_migration_service`` with a non-pending MagicMock so the
    ``@migration_blocked`` decorator passes through (it requires the service and
    raises RuntimeError if it is unwired) in tests that don't otherwise wire
    migration state. Tests that exercise the block can override
    ``is_retrodeck_migration_pending`` per-test.

    Also pre-wires a no-op ``_debug_logger`` so any service that consumes
    ``Plugin._log_debug`` (which forwards through ``_debug_logger``) works
    out of the box. Tests that want to assert on debug-log behaviour can
    override ``_debug_logger`` after construction (e.g. with the real
    ``SettingsAwareDebugLogger`` bound to a settings dict they control).
    """
    # Import here to ensure decky mock is already installed
    from main import Plugin

    class TestablePlugin(Plugin):
        """Plugin subclass that declares test-only attributes for type safety.

        Genuinely test-fixture-only attributes live here: ``_fake_api``,
        ``_resolve_system``, ``_save_settings``, plus the Unit-of-Work
        handles tests seed and assert against (``_uow``, ``_uow_factory``)
        and the per-test ``_tmp_path`` scratch dir. Test-fixture handles
        shared with production wiring (``_state``, ``_http_adapter``, ...)
        are declared on ``Plugin`` itself as ``Any``-typed annotation slots
        so test-only construction paths type-check uniformly.
        ``_save_settings`` is a test-only handle for the settings dict tests
        thread into ``SaveService`` / ``PlaytimeService``; production threads
        its settings store as ``self.settings``, never under this name.
        """

        _fake_api: Any
        _resolve_system: Any
        _save_settings: Any
        _uow: Any
        _uow_factory: Any
        _tmp_path: Any
        _core_info: Any
        _platform_core_reader: Any
        _active_core: Any
        _m3u_supported: Any
        _renderer_rss: Any
        _renderer_gc: Any

    instance = TestablePlugin()
    instance._migration_service = MagicMock()
    instance._migration_service.is_retrodeck_migration_pending.return_value = False
    instance._debug_logger = lambda msg: None
    return instance


@pytest.fixture
def fake_romm_api():
    """Function-scoped ``FakeRommApi`` instance.

    Returns a fresh fake per test so seeded state never leaks across
    tests. Construct without args — tests seed ``platforms`` / ``roms``
    / ``firmware_files`` / etc. directly on the returned instance.
    """
    from fakes.fake_romm_api import FakeRommApi

    return FakeRommApi()


@pytest.fixture
def fake_steamgrid_db_api():
    """Function-scoped ``FakeSteamGridDbApi`` instance.

    Returns a fresh fake per test so seeded responses never leak
    across tests. Construct without args — tests seed responses via
    ``seed_igdb_lookup`` / ``seed_artwork`` / ``seed_raw_response`` /
    ``seed_image_bytes`` / ``seed_verify_response``.
    """
    from fakes.fake_steamgrid_db_api import FakeSteamGridDbApi

    return FakeSteamGridDbApi()


@pytest.fixture(autouse=True)
def _reset_decky_mock_paths():
    """Refresh per-test temp dirs on the mock decky module.

    Fresh ``DECKY_PLUGIN_SETTINGS_DIR`` and ``DECKY_PLUGIN_RUNTIME_DIR``
    per test prevents cross-test pollution from persistence-touching
    tests. Both are removed again on teardown — ``mkdtemp`` leaves the
    directory behind by design, and two leaked dirs per test across a
    suite this size exhaust the tmpfs inode table in days, not months.

    They deliberately do not live under ``tmp_path``: adapter tests assert
    on the exact contents of their ``tmp_path`` (``listdir(...) == []``),
    and ``tests/contract`` already owns ``tmp_path/settings`` and
    ``tmp_path/runtime``.
    """
    mock_decky.DECKY_USER_HOME = os.path.expanduser("~")
    mock_decky.DECKY_PLUGIN_DIR = _project_root
    settings_dir = tempfile.mkdtemp()
    runtime_dir = tempfile.mkdtemp()
    mock_decky.DECKY_PLUGIN_SETTINGS_DIR = settings_dir
    mock_decky.DECKY_PLUGIN_RUNTIME_DIR = runtime_dir
    yield
    shutil.rmtree(settings_dir, ignore_errors=True)
    shutil.rmtree(runtime_dir, ignore_errors=True)
