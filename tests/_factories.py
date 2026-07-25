"""Construction helpers shared across the suite.

These live outside ``conftest.py`` on purpose. A conftest is imported by
pytest under its own module name; importing it a second time by plain name
(`from conftest import ...`) creates a *separate* module object that re-runs
the module body — including ``sys.modules["decky"] = mock_decky``, which
would then shadow the mock whose paths the autouse fixtures refresh.
Anything a test module needs to import belongs here instead.

Import as ``from _factories import _make_retry`` — ``tests/`` is on the path
via the root conftest, the same way ``fakes/`` is reached.
"""

from typing import Any
from unittest.mock import MagicMock


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
    instance._prune_service = MagicMock()
    instance._prune_service.is_active.return_value = False
    instance._debug_logger = lambda msg: None
    return instance
