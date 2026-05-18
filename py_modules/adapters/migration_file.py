"""Filesystem adapter for RetroDECK path and save-sort migration I/O.

Owns the raw POSIX calls used by MigrationService to walk source
locations, create destination directories, and relocate files when the
RetroDECK home path changes or RetroArch save sorting flips. Path
construction, conflict policy, and state updates remain a service
concern; this adapter exposes only the I/O seams declared by
``services.protocols.MigrationFileStore``.
"""

from __future__ import annotations

import contextlib
import os
import shutil


class MigrationFileAdapter:
    """Synchronous filesystem operations for RetroDECK migration flows.

    Implements the ``MigrationFileStore`` Protocol. Methods are
    synchronous — services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        return os.path.isdir(path)

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        os.makedirs(path, exist_ok=True)

    def remove_file(self, path: str) -> None:
        """Delete the file at *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def remove_tree(self, path: str) -> None:
        """Recursively delete *path*. Idempotent: a missing directory is not an error."""
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(path)

    def move(self, src: str, dst: str) -> None:
        """Cross-filesystem-safe move from *src* to *dst*.

        Uses ``shutil.move`` so the operation degrades to copy+delete
        when *src* and *dst* live on different filesystems (``EXDEV``).
        """
        shutil.move(src, dst)

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*.

        Uses ``os.replace`` — same-filesystem only.
        """
        os.replace(src, dst)

    def get_mtime(self, path: str) -> float:
        """Return the mtime of *path* as a Unix timestamp."""
        return os.path.getmtime(path)

    def walk_files(self, base_dir: str) -> list[tuple[str, list[str], list[str]]]:
        """Return ``os.walk``-style triples for *base_dir*.

        Materialises the iterator to a list so callers receive a
        snapshot that is safe to mutate in place (the caller may prune
        ``dirnames`` to skip hidden directories).
        """
        return [(dirpath, list(dirnames), list(filenames)) for dirpath, dirnames, filenames in os.walk(base_dir)]
