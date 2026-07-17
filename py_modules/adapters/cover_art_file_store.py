"""Filesystem adapter for cover-art file operations.

Owns the raw POSIX calls used by ArtworkService to manage cover art across the
plugin-owned per-ROM cover cache and the Steam grid directory. Path
construction, registry lookups, and orphan detection remain a service concern;
this adapter exposes only the I/O seams declared by
``services.protocols.CoverArtFileStore``.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import shutil


class CoverArtFileStoreAdapter:
    """Synchronous filesystem operations for cover-art files.

    Implements the ``CoverArtFileStore`` Protocol. Methods are synchronous —
    services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        os.makedirs(path, exist_ok=True)

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        os.replace(src, dst)

    def copy_file(self, src: str, dst: str) -> None:
        """Copy the file *src* to *dst*, leaving *src* in place."""
        shutil.copyfile(src, dst)

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        return os.listdir(directory)

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        return os.path.isdir(path)

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        return pathlib.Path(path).read_bytes()

    def write_text_atomic(self, path: str, content: str) -> None:
        """Atomically write *content* to *path* as UTF-8 text.

        Writes to ``path + ".tmp"`` first, then ``os.replace``s into place. The
        temp file is removed on any failure so a broken write leaves no orphan.
        """
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise
