"""Filesystem adapter for ROM download target operations.

Owns the raw POSIX calls used by DownloadService to manage downloaded
ROM files under the RetroDECK roms/bios directories. Path construction,
queue policy, and progress callbacks remain a service concern; this
adapter exposes only the I/O seams declared by
``services.protocols.DownloadFileStore``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import urllib.parse
import zipfile


class DownloadFileAdapter:
    """Synchronous filesystem operations for ROM download files.

    Implements the ``DownloadFileStore`` Protocol. Methods are
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

    def remove_tree(self, path: str) -> None:
        """Recursively delete *path*. Idempotent: a missing directory is not an error."""
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(path)

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        os.makedirs(path, exist_ok=True)

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        os.replace(src, dst)

    def disk_free(self, path: str) -> int:
        """Return the free space in bytes for the filesystem hosting *path*."""
        return shutil.disk_usage(path).free

    def walk_files_matching_suffixes(self, base_dir: str, suffixes: tuple[str, ...]) -> list[str]:
        """Recursively list files under *base_dir* matching any of *suffixes*.

        Returns absolute paths. Idempotent on missing *base_dir*
        (returns ``[]``). Pure listing — does not mutate the filesystem.
        """
        if not os.path.isdir(base_dir):
            return []
        matches: list[str] = []
        for root, _dirs, files in os.walk(base_dir):
            for filename in files:
                if filename.endswith(suffixes):
                    matches.append(os.path.join(root, filename))
        return matches

    def extract_zip(self, archive_path: str, dest_dir: str, safe_root: str) -> None:
        """Extract *archive_path* into *dest_dir* with ZIP-slip protection.

        Resolves both *dest_dir* and *safe_root* via ``os.path.realpath``
        and verifies that every ZIP member resolves within both before
        extracting. Raises ``ValueError`` on any escape attempt.
        """
        real_dest = os.path.realpath(dest_dir)
        real_safe = os.path.realpath(safe_root)
        if not (real_dest == real_safe or real_dest.startswith(real_safe + os.sep)):
            raise ValueError(f"Extract directory would be outside safe root: {dest_dir}")
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                member_path = os.path.realpath(os.path.join(real_dest, member))
                if not (member_path == real_dest or member_path.startswith(real_dest + os.sep)):
                    raise ValueError(f"ZIP member {member} would extract outside target directory")
            zf.extractall(real_dest)

    def decode_url_encoded_names(self, directory: str) -> None:
        """Rename URL-encoded files and directories under *directory* in place.

        Walks bottom-up so nested encoded directories are handled
        correctly. A no-op when the decoded name equals the original.
        """
        for root, dirs, files in os.walk(directory, topdown=False):
            for fname in files:
                decoded = urllib.parse.unquote(fname)
                if decoded != fname:
                    os.replace(os.path.join(root, fname), os.path.join(root, decoded))
            for dname in dirs:
                decoded = urllib.parse.unquote(dname)
                if decoded != dname:
                    os.replace(os.path.join(root, dname), os.path.join(root, decoded))

    def scan_files_with_sizes(self, directory: str) -> list[tuple[str, int]]:
        """Recursively return ``(absolute_path, size_bytes)`` tuples for every file under *directory*.

        Files whose size cannot be read report size ``0`` rather than
        raising so callers can still reason over the list.
        """
        out: list[tuple[str, int]] = []
        for root, _dirs, files in os.walk(directory):
            for f in files:
                path = os.path.join(root, f)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                out.append((path, size))
        return out

    def write_text_atomic(self, path: str, content: str) -> None:
        """Atomically write *content* to *path* as UTF-8 text.

        Writes to ``path + ".tmp"`` first, then ``os.replace``s into
        place. The temp file is removed on any failure so the caller is
        free to retry without an orphan lingering.
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
