"""In-memory ``RomFileStore`` implementation for service tests."""

from __future__ import annotations


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
