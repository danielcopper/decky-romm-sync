"""In-memory ``PathExistsProbe`` implementation for service tests."""

from __future__ import annotations


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
