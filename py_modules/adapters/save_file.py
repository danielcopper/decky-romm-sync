"""Filesystem adapter for local save file operations.

Owns the raw POSIX, ``open()``, ``tempfile``, and ``hashlib``-on-file
calls used by SaveService and its sub-services when reading, writing,
backing up, hashing, and removing local save files under the RetroDECK
saves directory. Path construction, platform-specific extension lookup,
and slot/sync policy remain a service or domain concern; this adapter
exposes only the I/O seams declared by
``services.protocols.SaveFileAdapter``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile

_MD5_CHUNK_SIZE = 8192


class SaveFileAdapter:
    """Synchronous filesystem operations for local save files.

    Implements the ``SaveFileAdapter`` Protocol. Methods are
    synchronous — services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def is_file(self, path: str) -> bool:
        """Return True when *path* exists and is a regular file."""
        return os.path.isfile(path)

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        return os.path.isdir(path)

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        os.makedirs(path, exist_ok=True)

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        os.replace(src, dst)

    def get_mtime(self, path: str) -> float:
        """Return the mtime of *path* as a Unix timestamp."""
        return os.path.getmtime(path)

    def get_size(self, path: str) -> int:
        """Return the size of *path* in bytes."""
        return os.path.getsize(path)

    def checksum_md5(self, path: str) -> str:
        """Return the hex-encoded MD5 digest of *path*'s contents.

        Streams the file in fixed-size chunks so memory use stays
        bounded for large save files. Non-security use: MD5 here is the
        drift baseline that ``compute_sync_action`` compares against
        ``last_sync_hash`` to decide whether a local save changed since
        the last sync. ``usedforsecurity=False`` documents this and
        silences Sonar S4790.
        """
        h = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_MD5_CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()

    def make_temp_path(self, suffix: str = "") -> str:
        """Return a fresh, unique path safe to write to.

        Backed by ``tempfile.mkstemp`` so the file is created atomically
        (``O_EXCL``) before the fd is closed. The caller owns the file
        and is responsible for removing it.
        """
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        return path
