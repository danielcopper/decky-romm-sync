"""In-memory ``SgdbArtworkCache`` implementation for service tests."""

from __future__ import annotations

import os


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
