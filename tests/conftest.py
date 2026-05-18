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

        Only the genuinely test-fixture-only attributes (``_fake_api``,
        ``_resolve_system``) live here. Test-fixture handles shared with
        production wiring (``_state``, ``_http_adapter``, ...) are
        declared on ``Plugin`` itself as ``Any``-typed annotation slots
        so test-only construction paths type-check uniformly.
        """

        _fake_api: Any
        _resolve_system: Any

    instance = TestablePlugin()
    instance._migration_service = MagicMock()
    instance._migration_service.is_retrodeck_migration_pending.return_value = False
    instance._debug_logger = lambda msg: None
    return instance


class FakeStatePersister:
    """In-memory ``StatePersister`` for tests.

    Counts how many times ``save_state()`` was invoked. Tests use
    ``save_count`` to assert the persister was triggered without
    standing up a real on-disk write.
    """

    def __init__(self) -> None:
        self.save_count = 0

    def save_state(self) -> None:
        self.save_count += 1


class FakeSettingsPersister:
    """In-memory ``SettingsPersister`` for tests.

    Counts how many times ``save_settings()`` was invoked. Tests use
    ``save_count`` to assert the persister was triggered without
    standing up a real on-disk write.
    """

    def __init__(self) -> None:
        self.save_count = 0

    def save_settings(self) -> None:
        self.save_count += 1


class FakeMetadataCachePersister:
    """In-memory ``MetadataCachePersister`` for tests.

    Counts how many times ``save_metadata()`` was invoked. Tests use
    ``save_count`` to assert the persister was triggered without
    standing up a real on-disk write.
    """

    def __init__(self) -> None:
        self.save_count = 0

    def save_metadata(self) -> None:
        self.save_count += 1


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


class FakePluginMetadataReader:
    """In-memory ``PluginMetadataReader`` for tests.

    Returns the version string configured at construction. Tests that
    don't care about the value can rely on the ``"0.0.0"`` default —
    matches the production adapter's fallback when ``package.json`` is
    unreadable. ``read_version`` records the last ``plugin_dir`` it was
    called with so tests can assert wiring.
    """

    def __init__(self, version: str = "0.0.0") -> None:
        self.version = version
        self.last_plugin_dir: str | None = None
        self.read_count = 0

    def read_version(self, plugin_dir: str) -> str:
        self.last_plugin_dir = plugin_dir
        self.read_count += 1
        return self.version


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
    free of filesystem side effects. ``remove_file`` is idempotent per
    the Protocol contract. ``listdir`` returns entries whose path's
    parent directory matches *directory* (no recursion). ``is_dir``
    reports True for any path that is the parent of an entry, mirroring
    the loose "directory exists when it contains files" semantics tests
    need.

    Tests can pre-populate ``files`` directly to stage fixtures, and
    inspect it after the act to assert removals/renames. ``isdir_paths``
    can be set explicitly when a test needs to model an empty directory
    or override the path-based default. ``rename_failures`` injects
    ``OSError`` on ``rename`` for the listed source paths so tests can
    exercise the production error-handling branches without patching
    stdlib.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        # Explicit directory whitelist; when None, is_dir is inferred
        # from parent-of-files membership.
        self.isdir_paths: set[str] | None = None
        # Source paths that should raise OSError on rename. Mirrors the
        # Wave 3 fake-adapter failure-injection pattern (e.g.
        # FakeDownloadFileStore / FakeFirmwareFileStore) so tests drive
        # error paths through the Protocol instead of patching
        # ``os.replace`` globally.
        self.rename_failures: set[str] = set()

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def remove_file(self, path: str) -> None:
        self.files.pop(path, None)

    def rename(self, src: str, dst: str) -> None:
        if src in self.rename_failures:
            raise OSError(f"rename failed for {src}")
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def listdir(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        return [
            path[len(prefix) :] for path in self.files if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]

    def is_dir(self, path: str) -> bool:
        if self.isdir_paths is not None:
            return path in self.isdir_paths
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeSgdbArtworkCache:
    """In-memory ``SgdbArtworkCache`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` is idempotent per
    the Protocol contract. ``cache_dir`` returns the canonical
    ``{cache_root}/artwork`` path; ``is_dir`` reports True for any path
    that is the parent of an entry or matches ``cache_dir``, mirroring
    the loose "directory exists when it contains files" semantics tests
    need.

    Tests can pre-populate ``files`` directly to stage cached artwork.
    ``isdir_paths`` can be set explicitly when a test needs to model an
    empty cache directory.
    """

    def __init__(self, cache_root: str = "/runtime", files: dict[str, bytes] | None = None) -> None:
        self._cache_dir = os.path.join(cache_root, "artwork")
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.isdir_paths: set[str] | None = None
        self.cache_dir_call_count = 0

    def cache_dir(self) -> str:
        self.cache_dir_call_count += 1
        return self._cache_dir

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def remove_file(self, path: str) -> None:
        self.files.pop(path, None)

    def listdir(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        return [
            path[len(prefix) :] for path in self.files if path.startswith(prefix) and "/" not in path[len(prefix) :]
        ]

    def is_dir(self, path: str) -> bool:
        if self.isdir_paths is not None:
            return path in self.isdir_paths
        if path == self._cache_dir:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeDownloadFileStore:
    """In-memory ``DownloadFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` / ``remove_tree``
    are idempotent per the Protocol contract. ``is_dir`` reports True
    for any path that is the parent of an entry or matches a directory
    created via ``make_dirs``.

    The fake captures enough state to model the download flow:
    - ``files`` — ``{path: bytes}`` snapshot of the virtual filesystem.
    - ``dirs`` — explicit set of directory paths (populated by
      ``make_dirs`` and ``extract_zip``).
    - ``disk_free_bytes`` — value returned by ``disk_free`` (default
      large, override via ``set_disk_free``).
    - ``fail_on_atomic_write`` — when True, ``write_text_atomic`` cleans
      up the tmp file and raises ``OSError`` to mirror the real adapter
      behaviour.
    - ``remove_failures`` / ``remove_tree_failures`` — sets of paths that
      raise ``OSError`` on the respective operation; used by partial-
      failure tests in ``cleanup_leftover_tmp_files`` and
      ``_cleanup_partial_download``.
    - ``decode_calls`` / ``extract_calls`` / ``walk_calls`` — captured
      argument lists for tests that need to assert on adapter calls.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set()
        self.disk_free_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB
        self.fail_on_atomic_write: bool = False
        self.tmp_files: set[str] = set()
        self.decode_calls: list[str] = []
        self.extract_calls: list[tuple[str, str, str]] = []
        self.walk_calls: list[tuple[str, tuple[str, ...]]] = []
        self.remove_failures: set[str] = set()
        self.remove_tree_failures: set[str] = set()

    def set_disk_free(self, bytes_free: int) -> None:
        self.disk_free_bytes = bytes_free

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def remove_file(self, path: str) -> None:
        if path in self.remove_failures:
            raise OSError(f"simulated remove failure: {path}")
        self.files.pop(path, None)

    def remove_tree(self, path: str) -> None:
        if path in self.remove_tree_failures:
            raise OSError(f"simulated remove_tree failure: {path}")
        prefix = path.rstrip("/") + "/"
        for stored in list(self.files):
            if stored == path or stored.startswith(prefix):
                del self.files[stored]
        self.dirs.discard(path)
        for d in list(self.dirs):
            if d.startswith(prefix):
                self.dirs.discard(d)

    def make_dirs(self, path: str) -> None:
        self.dirs.add(path)

    def rename(self, src: str, dst: str) -> None:
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def disk_free(self, path: str) -> int:
        return self.disk_free_bytes

    def is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def walk_files_matching_suffixes(self, base_dir: str, suffixes: tuple[str, ...]) -> list[str]:
        self.walk_calls.append((base_dir, suffixes))
        if not self.is_dir(base_dir):
            return []
        prefix = base_dir.rstrip("/") + "/"
        matches: list[str] = []
        for stored in self.files:
            if not (stored == base_dir or stored.startswith(prefix)):
                continue
            if stored.endswith(suffixes):
                matches.append(stored)
        return matches

    def extract_zip(self, archive_path: str, dest_dir: str, safe_root: str) -> None:
        self.extract_calls.append((archive_path, dest_dir, safe_root))
        if archive_path not in self.files:
            raise FileNotFoundError(archive_path)
        # Model the slip-protection: dest_dir must live under safe_root
        if not (dest_dir == safe_root or dest_dir.startswith(safe_root.rstrip("/") + "/")):
            raise ValueError(f"Extract directory would be outside safe root: {dest_dir}")
        # Fake-mode: derive extracted entries from a paired dict the test set.
        members = getattr(self, "_zip_members", {}).get(archive_path, {})
        self.make_dirs(dest_dir)
        for name, data in members.items():
            full = os.path.join(dest_dir, name)
            self.files[full] = data

    def set_zip_members(self, archive_path: str, members: dict[str, bytes]) -> None:
        if not hasattr(self, "_zip_members"):
            self._zip_members: dict[str, dict[str, bytes]] = {}
        self._zip_members[archive_path] = members

    def decode_url_encoded_names(self, directory: str) -> None:
        import urllib.parse

        self.decode_calls.append(directory)
        prefix = directory.rstrip("/") + "/"
        for stored in list(self.files):
            if not stored.startswith(prefix):
                continue
            rel = stored[len(prefix) :]
            decoded = urllib.parse.unquote(rel)
            if decoded != rel:
                new_path = prefix + decoded
                self.files[new_path] = self.files.pop(stored)

    def scan_files_with_sizes(self, directory: str) -> list[tuple[str, int]]:
        prefix = directory.rstrip("/") + "/"
        out: list[tuple[str, int]] = []
        for stored, data in self.files.items():
            if stored == directory or stored.startswith(prefix):
                out.append((stored, len(data)))
        return out

    def write_text_atomic(self, path: str, content: str) -> None:
        tmp_path = path + ".tmp"
        self.tmp_files.add(tmp_path)
        if self.fail_on_atomic_write:
            self.tmp_files.discard(tmp_path)
            raise OSError("simulated atomic-write failure")
        self.files[path] = content.encode("utf-8")
        self.tmp_files.discard(tmp_path)


class FakeMigrationFileStore:
    """In-memory ``MigrationFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` / ``remove_tree``
    are idempotent per the Protocol contract. ``is_dir`` reports True
    for any path that is the parent of an entry or matches a directory
    created via ``make_dirs``.

    Failure-injection seams support partial-failure tests:
    - ``move_failures``, ``rename_failures``, ``remove_failures``,
      ``get_mtime_failures`` — sets of paths that should raise
      ``OSError`` on the respective operation even when the path is
      otherwise present in ``files``.
    - ``mtimes`` — explicit ``{path: mtime}`` overrides for
      ``get_mtime``; missing entries fall back to the order they were
      added (monotonically increasing).
    - ``walk_returns`` — explicit ``{base_dir: triples}`` override for
      ``walk_files``; when absent, triples are synthesised from the
      ``files`` and ``dirs`` snapshot.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set()
        self.move_failures: set[str] = set()
        self.rename_failures: set[str] = set()
        self.remove_failures: set[str] = set()
        self.get_mtime_failures: set[str] = set()
        self.mtimes: dict[str, float] = {}
        self.walk_returns: dict[str, list[tuple[str, list[str], list[str]]]] | None = None
        self.move_calls: list[tuple[str, str]] = []
        self.rename_calls: list[tuple[str, str]] = []

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def make_dirs(self, path: str) -> None:
        self.dirs.add(path)

    def remove_file(self, path: str) -> None:
        if path in self.remove_failures:
            raise OSError(f"simulated remove failure: {path}")
        self.files.pop(path, None)

    def remove_tree(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for stored in list(self.files):
            if stored == path or stored.startswith(prefix):
                del self.files[stored]
        self.dirs.discard(path)
        for d in list(self.dirs):
            if d.startswith(prefix):
                self.dirs.discard(d)

    def move(self, src: str, dst: str) -> None:
        self.move_calls.append((src, dst))
        if src in self.move_failures:
            raise OSError(f"simulated move failure: {src}")
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def rename(self, src: str, dst: str) -> None:
        self.rename_calls.append((src, dst))
        if src in self.rename_failures:
            raise OSError(f"simulated rename failure: {src}")
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def get_mtime(self, path: str) -> float:
        if path in self.get_mtime_failures:
            raise OSError(f"simulated get_mtime failure: {path}")
        if path in self.mtimes:
            return self.mtimes[path]
        if path not in self.files:
            raise OSError(f"no such file: {path}")
        # Stable fallback derived from insertion order so callers can
        # reason about relative ordering without setting mtimes
        # explicitly.
        return float(list(self.files).index(path))

    def walk_files(self, base_dir: str) -> list[tuple[str, list[str], list[str]]]:
        if self.walk_returns is not None and base_dir in self.walk_returns:
            return [(dp, list(dn), list(fn)) for dp, dn, fn in self.walk_returns[base_dir]]
        if not self.is_dir(base_dir):
            return []
        prefix = base_dir.rstrip("/") + "/"
        # Build per-dir filename lists from the flat snapshot.
        per_dir_files: dict[str, list[str]] = {}
        per_dir_subdirs: dict[str, set[str]] = {}
        for stored in self.files:
            if not stored.startswith(prefix):
                continue
            rel = stored[len(prefix) :]
            dirname, _, filename = rel.rpartition("/")
            dir_abs = base_dir if not dirname else os.path.join(base_dir, dirname)
            per_dir_files.setdefault(dir_abs, []).append(filename)
            # Build the dir chain so subdir names propagate to parents.
            current = base_dir
            for part in dirname.split("/") if dirname else []:
                per_dir_subdirs.setdefault(current, set()).add(part)
                current = os.path.join(current, part)
        triples: list[tuple[str, list[str], list[str]]] = []
        all_dirs = sorted(set(per_dir_files) | set(per_dir_subdirs) | {base_dir})
        for d in all_dirs:
            triples.append(
                (
                    d,
                    sorted(per_dir_subdirs.get(d, set())),
                    sorted(per_dir_files.get(d, [])),
                )
            )
        return triples


class FakeDownloadQueueCleanup:
    """In-memory ``DownloadQueueCleanup`` for tests.

    Records ``evict`` calls in ``evicted`` (a list of rom_ids in call
    order) and counts ``clear()`` invocations in ``cleared``. Tests
    inspect either attribute to assert eviction behaviour without
    standing up a full DownloadService.
    """

    def __init__(self) -> None:
        self.evicted: list[int] = []
        self.cleared: int = 0

    def evict(self, rom_id: int) -> None:
        self.evicted.append(int(rom_id))

    def clear(self) -> None:
        self.cleared += 1


class FakeDownloadQueueAdapter:
    """In-memory ``DownloadQueueAdapter`` for tests.

    Backed by a single ``entries`` list so ``poll_and_clear`` is
    deterministic. Tests pre-populate ``entries`` to stage queued
    requests and inspect ``poll_count`` / ``last_path`` for behaviour
    assertions. ``set_missing(True)`` makes the next ``poll_and_clear``
    behave as if the file were missing (returns ``[]`` without clearing).
    """

    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries: list[dict] = list(entries) if entries else []
        self.poll_count: int = 0
        self.last_path: str | None = None
        self.missing: bool = False

    def set_missing(self, missing: bool) -> None:
        self.missing = missing

    def poll_and_clear(self, path: str) -> list[dict]:
        self.poll_count += 1
        self.last_path = path
        if self.missing:
            return []
        out = list(self.entries)
        self.entries.clear()
        return out


class FakeFirmwareFileStore:
    """In-memory ``FirmwareFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` is idempotent per
    the Protocol contract. ``exists`` reports True for any stored file
    or any path explicitly registered as a directory (via ``make_dirs``).

    Failure injection:
    - ``remove_failures`` — paths that raise ``OSError`` when removed,
      letting tests assert error handling without a real filesystem.
    - ``checksum_overrides`` — pinned hex digests returned by
      ``checksum_md5`` for specific paths, sidestepping the in-memory
      ``hashlib`` call when tests want a deterministic mismatch.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set()
        self.remove_failures: set[str] = set()
        self.checksum_overrides: dict[str, str] = {}

    def exists(self, path: str) -> bool:
        if path in self.files or path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def remove_file(self, path: str) -> None:
        if path in self.remove_failures:
            raise OSError(f"simulated remove failure: {path}")
        self.files.pop(path, None)

    def rename(self, src: str, dst: str) -> None:
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def make_dirs(self, path: str) -> None:
        self.dirs.add(path)

    def checksum_md5(self, path: str) -> str:
        if path in self.checksum_overrides:
            return self.checksum_overrides[path]
        if path not in self.files:
            raise FileNotFoundError(path)
        import hashlib

        return hashlib.md5(self.files[path]).hexdigest()

    def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakeRomFileStore:
    """In-memory ``RomFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` for files and a ``set[str]`` for
    explicit directories so file ops are deterministic and free of
    filesystem side effects. ``remove_file`` is idempotent per the
    Protocol contract; ``remove_tree`` clears any entry whose path is
    *path* or lives under ``path + "/"``. ``is_dir`` reports True for
    any path in ``dirs`` or any path that is the parent of an entry,
    mirroring the loose "directory exists when it contains files"
    semantics tests need.

    Failure injection:
    - ``remove_file_failures`` — paths that raise ``OSError`` when
      passed to ``remove_file``.
    - ``remove_tree_failures`` — paths that raise ``OSError`` when
      passed to ``remove_tree``.

    Tests can pre-populate ``files`` directly to stage installed ROM
    state and inspect ``files`` / ``dirs`` after the act to assert
    on deletions.
    """

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        dirs: set[str] | None = None,
    ) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set(dirs) if dirs else set()
        self.remove_file_failures: set[str] = set()
        self.remove_tree_failures: set[str] = set()
        self.remove_file_calls: list[str] = []
        self.remove_tree_calls: list[str] = []

    def is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def remove_file(self, path: str) -> None:
        self.remove_file_calls.append(path)
        if path in self.remove_file_failures:
            raise OSError(f"simulated remove_file failure: {path}")
        self.files.pop(path, None)

    def remove_tree(self, path: str) -> None:
        self.remove_tree_calls.append(path)
        if path in self.remove_tree_failures:
            raise OSError(f"simulated remove_tree failure: {path}")
        prefix = path.rstrip("/") + "/"
        for stored in list(self.files):
            if stored == path or stored.startswith(prefix):
                del self.files[stored]
        self.dirs.discard(path)
        for d in list(self.dirs):
            if d.startswith(prefix):
                self.dirs.discard(d)


class FakeSaveFileStore:
    """In-memory ``SaveFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` is idempotent per
    the Protocol contract. ``is_dir`` reports True for any path
    explicitly registered as a directory (via ``make_dirs``) or any
    path that is the parent of a stored file. ``make_temp_path`` returns
    a monotonically incrementing path under ``/tmp`` and registers it as
    an empty file so subsequent ``remove_file`` calls behave like the
    real adapter.

    Mtime/size behaviour: ``get_mtime`` returns the value set via
    ``set_mtime`` (or the monotonically-incrementing default assigned
    on first write), and ``get_size`` returns ``len(files[path])``.

    Failure injection:
    - ``remove_failures`` — paths that raise ``OSError`` when removed.
    - ``checksum_overrides`` — pinned hex digests returned by
      ``checksum_md5`` for specific paths, sidestepping the in-memory
      ``hashlib`` call when tests want a deterministic mismatch.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set()
        self.mtimes: dict[str, float] = {}
        self.remove_failures: set[str] = set()
        self.checksum_overrides: dict[str, str] = {}
        self.remove_calls: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.temp_counter: int = 0
        self._next_mtime: float = 1_000_000.0

    def _ensure_mtime(self, path: str) -> None:
        if path not in self.mtimes:
            self.mtimes[path] = self._next_mtime
            self._next_mtime += 1.0

    def set_mtime(self, path: str, mtime: float) -> None:
        self.mtimes[path] = mtime

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def is_file(self, path: str) -> bool:
        return path in self.files

    def is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def make_dirs(self, path: str) -> None:
        self.dirs.add(path)

    def remove_file(self, path: str) -> None:
        self.remove_calls.append(path)
        if path in self.remove_failures:
            raise OSError(f"simulated remove failure: {path}")
        self.files.pop(path, None)
        self.mtimes.pop(path, None)

    def rename(self, src: str, dst: str) -> None:
        self.rename_calls.append((src, dst))
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)
        if src in self.mtimes:
            self.mtimes[dst] = self.mtimes.pop(src)
        else:
            self._ensure_mtime(dst)

    def get_mtime(self, path: str) -> float:
        if path not in self.files:
            raise FileNotFoundError(path)
        self._ensure_mtime(path)
        return self.mtimes[path]

    def get_size(self, path: str) -> int:
        if path not in self.files:
            raise FileNotFoundError(path)
        return len(self.files[path])

    def checksum_md5(self, path: str) -> str:
        if path in self.checksum_overrides:
            return self.checksum_overrides[path]
        if path not in self.files:
            raise FileNotFoundError(path)
        import hashlib

        return hashlib.md5(self.files[path]).hexdigest()

    def make_temp_path(self, suffix: str = "") -> str:
        self.temp_counter += 1
        path = f"/tmp/fake-save-{self.temp_counter}{suffix}"
        self.files[path] = b""
        self._ensure_mtime(path)
        return path

    def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class FakePathProbe:
    """In-memory ``PathExistsProbe`` for tests.

    Backed by a ``set[str]`` of paths that report as existing. Tests
    pre-populate ``paths`` directly to stage what the probe should
    treat as present on disk. Lookup is exact: ``exists("/a/b")`` is
    True iff ``"/a/b"`` is in the set.
    """

    def __init__(self, paths: set[str] | None = None) -> None:
        self.paths: set[str] = set(paths) if paths else set()

    def exists(self, path: str) -> bool:
        return path in self.paths


class FakeHostnameProvider:
    """In-memory ``HostnameProvider`` for tests.

    Returns the ``hostname`` value configured at construction. Tests
    that need to assert on the registered device name read the same
    string back through the service.
    """

    def __init__(self, hostname: str = "test-host") -> None:
        self.hostname = hostname

    def get(self) -> str:
        return self.hostname


class FakeCoreInfoProvider:
    """In-memory CoreInfoProvider for tests.

    Returns the configured active_core and available_cores for any system.
    Both attributes are mutable so tests can set them directly on the
    instance before exercising the code under test. ``reset_cache``
    increments ``reset_cache_count`` so writers can assert the cache
    was invalidated after a write.
    """

    def __init__(
        self,
        *,
        active_core: tuple[str | None, str | None] = (None, None),
        available_cores: list[dict] | None = None,
    ) -> None:
        self.active_core = active_core
        self.available_cores: list[dict] = available_cores if available_cores is not None else []
        self.reset_cache_count = 0

    def get_active_core(
        self,
        system_name: str,
        rom_filename: str | None = None,
    ) -> tuple[str | None, str | None]:
        return self.active_core

    def get_available_cores(self, system_name: str) -> list[dict]:
        return self.available_cores

    def reset_cache(self) -> None:
        self.reset_cache_count += 1


class FakeRetroDeckPaths:
    """In-memory ``RetroDeckPaths`` for tests.

    Each path is a mutable attribute so tests can flip individual
    directories without rebuilding the whole bundle. Defaults to empty
    strings, matching the production fallback when ``retrodeck.json``
    is absent.
    """

    def __init__(
        self,
        *,
        saves: str = "",
        roms: str = "",
        bios: str = "",
        home: str = "",
    ) -> None:
        self.saves = saves
        self.roms = roms
        self.bios = bios
        self.home = home

    def saves_path(self) -> str:
        return self.saves

    def roms_path(self) -> str:
        return self.roms

    def bios_path(self) -> str:
        return self.bios

    def retrodeck_home(self) -> str:
        return self.home


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
