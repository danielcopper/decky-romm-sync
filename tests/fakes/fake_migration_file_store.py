"""In-memory ``MigrationFileStore`` implementation for service tests."""

from __future__ import annotations

import os


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
