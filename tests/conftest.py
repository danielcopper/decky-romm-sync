import logging
import os
import sys
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Mirror Decky's sys.path setup: add py_modules/ so `from lib.xxx import` works
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tests_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_project_root, "py_modules"))
# Add tests/ root so subdirectory tests can still import from fakes/ and conftest
sys.path.insert(0, _tests_root)


# Create mock decky module before any imports of main
mock_decky = MagicMock()
mock_decky.DECKY_PLUGIN_DIR = _project_root
mock_decky.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()
mock_decky.DECKY_PLUGIN_RUNTIME_DIR = tempfile.mkdtemp()
mock_decky.DECKY_PLUGIN_LOG_DIR = tempfile.mkdtemp()
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
    ``@migration_blocked`` decorator does not raise AttributeError in tests
    that don't otherwise wire migration state. Tests that exercise the
    block can override ``is_retrodeck_migration_pending`` per-test.

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
    tests.
    """
    mock_decky.DECKY_USER_HOME = os.path.expanduser("~")
    mock_decky.DECKY_PLUGIN_DIR = _project_root
    mock_decky.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()
    mock_decky.DECKY_PLUGIN_RUNTIME_DIR = tempfile.mkdtemp()
    yield
