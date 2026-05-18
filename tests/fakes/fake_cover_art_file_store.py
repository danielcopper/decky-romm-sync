"""In-memory ``CoverArtFileStore`` implementation for service tests."""

from __future__ import annotations


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
