"""Filesystem adapter for cover-art file operations.

Owns the raw POSIX calls used by ArtworkService to manage cover art under
the Steam grid directory. Path construction, registry lookups, and orphan
detection remain a service concern; this adapter exposes only the I/O
seams declared by ``services.protocols.CoverArtFileStore``.
"""

from __future__ import annotations

import contextlib
import os
import pathlib


class CoverArtFileStoreAdapter:
    """Synchronous filesystem operations for cover-art files.

    Implements the ``CoverArtFileStore`` Protocol. Methods are synchronous —
    services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        os.replace(src, dst)

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        return os.listdir(directory)

    def isdir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        return os.path.isdir(path)

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        return pathlib.Path(path).read_bytes()
