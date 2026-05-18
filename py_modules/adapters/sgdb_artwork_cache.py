"""Filesystem adapter for the SteamGridDB artwork cache.

Owns the raw POSIX calls used by SteamGridService to manage cached SGDB
artwork (heroes, logos, grids, icons) under the plugin runtime directory.
Path construction and pruning policy remain a service concern; this
adapter exposes only the I/O seams declared by
``services.protocols.SgdbArtworkCache``.
"""

from __future__ import annotations

import contextlib
import os
import pathlib


class SgdbArtworkCacheAdapter:
    """Synchronous filesystem operations for the SGDB artwork cache.

    Implements the ``SgdbArtworkCache`` Protocol. Methods are synchronous —
    services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def __init__(self, *, runtime_dir: str) -> None:
        self._runtime_dir = runtime_dir

    def cache_dir(self) -> str:
        """Return the SGDB artwork cache directory, creating it if missing."""
        art_dir = os.path.join(self._runtime_dir, "artwork")
        os.makedirs(art_dir, exist_ok=True)
        return art_dir

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        return os.listdir(directory)

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        return os.path.isdir(path)

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        return pathlib.Path(path).read_bytes()
