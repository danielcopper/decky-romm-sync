"""In-memory ``SaveFileStore`` implementation for service tests."""

from __future__ import annotations

import hashlib


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
