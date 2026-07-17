"""In-memory ``CoverArtFileStore`` implementation for service tests."""

from __future__ import annotations


class FakeCoverArtFileStore:
    """In-memory ``CoverArtFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` is idempotent per
    the Protocol contract. ``listdir`` returns the first-level entries
    under *directory* — file names plus subdirectory names (inferred from
    deeper paths), mirroring ``os.listdir``. ``is_dir`` reports True for
    any path that is the parent of an entry, mirroring the loose
    "directory exists when it contains files" semantics tests need.

    Tests can pre-populate ``files`` directly to stage fixtures, and
    inspect it after the act to assert removals/renames/copies.
    ``isdir_paths`` can be set explicitly when a test needs to model an
    empty directory or override the path-based default; a directory
    created via ``make_dirs`` also reports as a directory. ``rename_failures``
    / ``copy_failures`` inject ``OSError`` on ``rename`` / ``copy_file`` for
    the listed source paths so tests can exercise the production
    error-handling branches without patching stdlib.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        # Explicit directory whitelist; when None, is_dir is inferred
        # from parent-of-files membership (plus any make_dirs targets).
        self.isdir_paths: set[str] | None = None
        # Directories created via make_dirs — so an empty created cache dir
        # still reports as a directory even before any file lands in it.
        self.made_dirs: set[str] = set()
        # Source paths that should raise OSError on rename. Mirrors the
        # Wave 3 fake-adapter failure-injection pattern (e.g.
        # FakeDownloadFileStore / FakeFirmwareFileStore) so tests drive
        # error paths through the Protocol instead of patching
        # ``os.replace`` globally.
        self.rename_failures: set[str] = set()
        # Source paths that should raise OSError on copy_file (same pattern).
        self.copy_failures: set[str] = set()

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def make_dirs(self, path: str) -> None:
        self.made_dirs.add(path)

    def remove_file(self, path: str) -> None:
        self.files.pop(path, None)

    def rename(self, src: str, dst: str) -> None:
        if src in self.rename_failures:
            raise OSError(f"rename failed for {src}")
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def copy_file(self, src: str, dst: str) -> None:
        if src in self.copy_failures:
            raise OSError(f"copy failed for {src}")
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files[src]

    def listdir(self, directory: str) -> list[str]:
        prefix = directory.rstrip("/") + "/"
        entries: set[str] = set()
        for path in list(self.files) + list(self.made_dirs):
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            if rest:
                entries.add(rest.split("/", 1)[0])
        return sorted(entries)

    def is_dir(self, path: str) -> bool:
        if self.isdir_paths is not None:
            return path in self.isdir_paths or path in self.made_dirs
        if path in self.made_dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def read_bytes(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_text_atomic(self, path: str, content: str) -> None:
        self.files[path] = content.encode("utf-8")
