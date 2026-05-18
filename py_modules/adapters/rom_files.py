"""Filesystem adapter for installed ROM file removal.

Owns the raw POSIX calls used by RomRemovalService when physically
removing an installed ROM (single file or multi-file ROM directory).
Path construction, safety checks, and state mutation remain a service
or domain concern; this adapter exposes only the I/O seams declared by
``services.protocols.RomFileStore``.
"""

from __future__ import annotations

import contextlib
import os
import shutil


class RomFileAdapter:
    """Synchronous filesystem operations for installed ROM files.

    Implements the ``RomFileStore`` Protocol. Methods are
    synchronous — services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        return os.path.isdir(path)

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def remove_file(self, path: str) -> None:
        """Delete the file at *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def remove_tree(self, path: str) -> None:
        """Recursively delete *path* and all contents."""
        shutil.rmtree(path)
