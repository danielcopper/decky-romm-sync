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


class FakeCoverArtFileStore:
    """In-memory ``CoverArtFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove`` is idempotent per the
    Protocol contract. ``listdir`` returns entries whose path's parent
    directory matches *directory* (no recursion). ``isdir`` reports True
    for any path that is the parent of an entry, mirroring the loose
    "directory exists when it contains files" semantics tests need.

    Tests can pre-populate ``files`` directly to stage fixtures, and
    inspect it after the act to assert removals/renames. ``isdir_paths``
    can be set explicitly when a test needs to model an empty directory
    or override the path-based default.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        # Explicit directory whitelist; when None, isdir is inferred from
        # parent-of-files membership.
        self.isdir_paths: set[str] | None = None

    def exists(self, path: str) -> bool:
        return path in self.files or self.isdir(path)

    def remove(self, path: str) -> None:
        self.files.pop(path, None)

    def rename(self, src: str, dst: str) -> None:
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def listdir(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        return [
            path[len(prefix) :] for path in self.files if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]

    def isdir(self, path: str) -> bool:
        if self.isdir_paths is not None:
            return path in self.isdir_paths
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeCoreInfoProvider:
    """In-memory CoreInfoProvider for tests.

    Returns the configured active_core and available_cores for any system.
    Both attributes are mutable so tests can set them directly on the
    instance before exercising the code under test.
    """

    def __init__(
        self,
        *,
        active_core: tuple[str | None, str | None] = (None, None),
        available_cores: list[dict] | None = None,
    ) -> None:
        self.active_core = active_core
        self.available_cores: list[dict] = available_cores if available_cores is not None else []

    def get_active_core(
        self,
        system_name: str,
        rom_filename: str | None = None,
    ) -> tuple[str | None, str | None]:
        return self.active_core

    def get_available_cores(self, system_name: str) -> list[dict]:
        return self.available_cores


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
