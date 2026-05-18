"""Filesystem adapter for firmware/BIOS file operations.

Owns the raw POSIX calls used by FirmwareService to manage firmware
downloads under the RetroDECK BIOS directory. Path construction,
registry lookups, and download orchestration remain a service concern;
this adapter exposes only the I/O seams declared by
``services.protocols.FirmwareFileStore``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib

_MD5_CHUNK_SIZE = 8192


class FirmwareFileAdapter:
    """Synchronous filesystem operations for firmware/BIOS files.

    Implements the ``FirmwareFileStore`` Protocol. Methods are
    synchronous — services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        os.replace(src, dst)

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        os.makedirs(path, exist_ok=True)

    def checksum_md5(self, path: str) -> str:
        """Return the hex-encoded MD5 digest of *path*'s contents.

        Streams the file in fixed-size chunks so memory use stays
        bounded for large firmware blobs. Non-security use: MD5 here
        matches the digest published alongside the firmware download
        (RomM server / BIOS registry) to verify the file arrived intact.
        """
        h = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_MD5_CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        return pathlib.Path(path).read_bytes()
