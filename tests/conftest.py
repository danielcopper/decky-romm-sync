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


class _DeckyMock(MagicMock):
    """MagicMock that keeps ``domain.es_de_config`` in sync when
    DECKY_PLUGIN_DIR is reassigned in tests.

    Without this, tests that do ``decky.DECKY_PLUGIN_DIR = str(tmp_path)``
    would update the mock attribute but not the domain module's cached
    value, which is stored via ``configure()`` rather than read lazily.
    """

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "DECKY_PLUGIN_DIR":
            try:
                from domain import es_de_config

                logger = super().__getattribute__("logger")
                es_de_config.configure(plugin_dir=value, logger=logger)
            except Exception:
                pass


# Create mock decky module before any imports of main
mock_decky = _DeckyMock()
mock_decky.DECKY_PLUGIN_DIR = _project_root
mock_decky.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp()
mock_decky.DECKY_PLUGIN_RUNTIME_DIR = tempfile.mkdtemp()
mock_decky.DECKY_PLUGIN_LOG_DIR = tempfile.mkdtemp()
mock_decky.DECKY_USER_HOME = os.path.expanduser("~")
mock_decky.logger = logging.getLogger("test_romm")
mock_decky.emit = AsyncMock()

sys.modules["decky"] = mock_decky


def _make_testable_plugin():
    """Return a TestablePlugin instance with test-only attributes declared.

    Pre-populates ``_migration_service`` with a non-pending MagicMock so the
    ``@migration_blocked`` decorator does not raise AttributeError in tests
    that don't otherwise wire migration state. Tests that exercise the
    block can override ``is_retrodeck_migration_pending`` per-test.
    """
    # Import here to ensure decky mock is already installed
    from main import Plugin

    class TestablePlugin(Plugin):
        """Plugin subclass that declares test-only attributes for type safety."""

        _fake_api: Any
        _resolve_system: Any

    instance = TestablePlugin()
    instance._migration_service = MagicMock()
    instance._migration_service.is_retrodeck_migration_pending.return_value = False
    return instance


class FakeSaveSyncStatePersister:
    """In-memory ``SaveSyncStatePersister`` for tests.

    Keeps the most recently saved dict in ``self.last_saved`` and the
    canned payload returned by ``load`` in ``self.canned_load``. Tests
    that don't care about persistence can use the default (no canned
    payload, returns None) and rely on ``last_saved`` for assertions.
    """

    def __init__(self, *, canned_load: dict | None = None) -> None:
        self.canned_load = canned_load
        self.last_saved: dict | None = None
        self.save_count = 0
        self.load_count = 0

    def save(self, data: dict) -> None:
        self.save_count += 1
        # Snapshot a deep-ish copy so later in-memory mutations don't
        # silently change what the test inspects.
        import copy

        self.last_saved = copy.deepcopy(data)

    def load(self) -> dict | None:
        self.load_count += 1
        return self.canned_load


class FakeFirmwareCachePersister:
    """In-memory ``FirmwareCachePersister`` for tests.

    Keeps the most recently saved dict in ``self.last_saved`` and the
    canned payload returned by ``load`` in ``self.canned_load``. The
    persister contract returns ``dict`` (never ``None``) so the default
    canned load is an empty dict, mirroring the adapter's behaviour
    when no on-disk cache is present.
    """

    def __init__(self, *, canned_load: dict | None = None, load_side_effect: BaseException | None = None) -> None:
        self.canned_load: dict = canned_load if canned_load is not None else {}
        self.load_side_effect = load_side_effect
        self.last_saved: dict | None = None
        self.save_count = 0
        self.load_count = 0
        self.save_side_effect: BaseException | None = None

    def save(self, data: dict) -> None:
        self.save_count += 1
        if self.save_side_effect is not None:
            raise self.save_side_effect
        import copy

        self.last_saved = copy.deepcopy(data)

    def load(self) -> dict:
        self.load_count += 1
        if self.load_side_effect is not None:
            raise self.load_side_effect
        return self.canned_load


@pytest.fixture(autouse=True)
def _reset_es_de_config_user_home():
    """Reset ``es_de_config`` module-level state between every test.

    Calls ``configure()`` with the mock decky values so that services
    using this module work without explicit ``configure()`` calls in
    test bodies.
    """
    from domain import es_de_config

    # Fresh temp dirs per test — ensures no cross-test pollution
    mock_decky.DECKY_USER_HOME = os.path.expanduser("~")
    mock_decky.DECKY_PLUGIN_DIR = _project_root
    _fresh_settings = tempfile.mkdtemp()
    _fresh_runtime = tempfile.mkdtemp()
    mock_decky.DECKY_PLUGIN_SETTINGS_DIR = _fresh_settings
    mock_decky.DECKY_PLUGIN_RUNTIME_DIR = _fresh_runtime
    es_de_config.configure(plugin_dir=_project_root, logger=logging.getLogger("test_romm"))
    yield
