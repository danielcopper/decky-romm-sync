"""Filesystem adapter for ROM download target operations.

Owns the raw POSIX calls used by DownloadService to manage downloaded
ROM files under the RetroDECK roms/bios directories. Path construction,
queue policy, and progress callbacks remain a service concern; this
adapter exposes only the I/O seams declared by
``services.protocols.DownloadFileStore``.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import urllib.parse
import zipfile
import zlib
from typing import TYPE_CHECKING

from lib.path_safety import safe_path_component

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import IO

    from models.adoption import ArchiveMemberInfo, ExistingContent

_EXTRACT_CHUNK = 1024 * 1024
_HASH_CHUNK = 1024 * 1024


class DownloadFileAdapter:
    """Synchronous filesystem operations for ROM download files.

    Implements the ``DownloadFileStore`` Protocol. Methods are
    synchronous — services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        return os.path.exists(path)

    def describe_path(self, path: str) -> ExistingContent | None:
        """Describe whatever occupies *path*, or ``None`` when nothing does.

        A directory reports the recursive total of its contents so the number is
        comparable with the server's ``fs_size_bytes`` for a multi-file ROM; the
        walk accumulates sizes rather than collecting paths, because a single
        multi-file install can hold tens of thousands of files. A file whose size
        cannot be read contributes 0 instead of aborting the description — the
        caller is deciding whether to *ask* the user, and a partial total is a
        better answer than none.
        """
        try:
            stat = os.stat(path)
        except OSError:
            return None
        is_dir = os.path.isdir(path)
        return {
            "path": path,
            "is_dir": is_dir,
            "size_bytes": self._tree_size(path) if is_dir else stat.st_size,
            "modified_at": stat.st_mtime,
        }

    @staticmethod
    def _tree_size(directory: str) -> int:
        """Sum the sizes of every file under *directory*, unreadable entries as 0."""
        total = 0
        for root, _dirs, files in os.walk(directory):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
        return total

    def checksum(self, path: str, algorithm: str, progress_callback: Callable[[int], None] | None = None) -> str:
        """Return *path*'s hex digest under *algorithm* (``"md5"`` or ``"crc32"``).

        One read pass, fixed-size chunks, so memory stays bounded on a
        multi-gigabyte disc image. *progress_callback* receives the number of
        bytes consumed by each chunk — a delta, not a running total, so a caller
        hashing a whole directory can accumulate across files without tracking
        per-file offsets. Non-security use: both digests are matched against the
        value RomM published for the same file to decide whether the content on
        disk is that file.
        """
        with open(path, "rb") as f:
            return self._digest_stream(f, algorithm, progress_callback)

    def list_archive_members(self, path: str) -> tuple[ArchiveMemberInfo, ...] | None:
        """Describe what sits inside the archive at *path*, or ``None``.

        The central directory alone answers this: it states every member's
        internal name, uncompressed size and CRC32, so nothing is decompressed.
        ``None`` is returned for anything this adapter cannot open as a ZIP,
        which the caller reads as "could not look inside" rather than as a
        statement about the content.
        """
        members: list[ArchiveMemberInfo] = []
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    members.append(
                        {
                            "name": info.filename,
                            "size_bytes": info.file_size,
                            "crc32": f"{info.CRC & 0xFFFFFFFF:08x}",
                        }
                    )
        except (OSError, zipfile.BadZipFile):
            return None
        return tuple(members)

    def checksum_archive_member(
        self,
        path: str,
        member_name: str,
        algorithm: str,
        progress_callback: Callable[[int], None] | None = None,
    ) -> str:
        """Return the hex digest of *member_name*'s decompressed bytes.

        Streams the member out of the archive, so peak memory stays at one chunk
        however large the member is. Failures are not caught: an unsupported
        compression method (``NotImplementedError``), an encrypted member
        (``RuntimeError``) or a container damaged since it was listed all mean
        the bytes were never read, which is the caller's to report.
        """
        with zipfile.ZipFile(path) as archive, archive.open(member_name) as member:
            return self._digest_stream(member, algorithm, progress_callback)

    @classmethod
    def _digest_stream(cls, stream: IO[bytes], algorithm: str, progress_callback: Callable[[int], None] | None) -> str:
        """Hex-digest *stream* under *algorithm*, reporting each chunk's length."""
        if algorithm == "crc32":
            crc = 0
            for chunk in cls._read_chunks(stream, progress_callback):
                crc = zlib.crc32(chunk, crc)
            return f"{crc & 0xFFFFFFFF:08x}"
        if algorithm != "md5":
            raise ValueError(f"unsupported checksum algorithm: {algorithm!r}")
        digest = hashlib.md5(usedforsecurity=False)
        for chunk in cls._read_chunks(stream, progress_callback):
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _read_chunks(stream: IO[bytes], progress_callback: Callable[[int], None] | None):
        """Yield *stream* in fixed-size chunks, reporting each chunk's length."""
        while chunk := stream.read(_HASH_CHUNK):
            if progress_callback is not None:
                progress_callback(len(chunk))
            yield chunk

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

    def move_dir(self, src: str, dst: str) -> None:
        """Atomically move the whole directory *src* to *dst* via ``os.replace``.

        Same-filesystem only — *src* and *dst* are siblings under the
        platform roms folder. Moves the entire subtree in one syscall so
        a multi-file ROM is never split.
        """
        os.replace(src, dst)

    def copy_file(self, src: str, dst: str) -> None:
        """Copy the file *src* to *dst*, preserving *src* and its metadata.

        Used to heal a mis-suffixed dump file (``PS3_DISC.SFB.txt`` →
        ``PS3_DISC.SFB``) by writing a correctly-named copy while leaving the
        original in place. ``shutil.copy2`` preserves mode + timestamps.
        """
        shutil.copy2(src, dst)

    def disk_free(self, path: str) -> int:
        """Return the free space in bytes for the filesystem hosting *path*."""
        return shutil.disk_usage(path).free

    def file_size(self, path: str) -> int:
        """Return the size in bytes of the file at *path*, or 0 if it's missing."""
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def walk_files_matching_suffixes(self, base_dir: str, suffixes: tuple[str, ...]) -> list[str]:
        """Recursively list files under *base_dir* matching any of *suffixes*.

        Returns absolute paths. Idempotent on missing *base_dir*
        (returns ``[]``). Pure listing — does not mutate the filesystem.
        """
        if not os.path.isdir(base_dir):
            return []
        matches: list[str] = []
        for root, _dirs, files in os.walk(base_dir):
            matches.extend(os.path.join(root, filename) for filename in files if filename.endswith(suffixes))
        return matches

    def extract_zip(
        self,
        archive_path: str,
        dest_dir: str,
        safe_root: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Extract *archive_path* into *dest_dir* with ZIP-slip protection.

        Resolves both *dest_dir* and *safe_root* via ``os.path.realpath``
        and verifies that every ZIP member resolves within both before
        writing any byte. Raises ``ValueError`` on any escape attempt.

        When *progress_callback* is supplied it is invoked with
        ``(extracted, total)`` uncompressed byte counts after each chunk
        write — ``total`` is the sum of every member's uncompressed size,
        ``extracted`` is the running total written so far. With
        *progress_callback* left ``None`` the extraction is silent and the
        output files are byte-identical to a plain ``extractall``.
        """
        real_dest = os.path.realpath(dest_dir)
        real_safe = os.path.realpath(safe_root)
        if not (real_dest == real_safe or real_dest.startswith(real_safe + os.sep)):
            raise ValueError(f"Extract directory would be outside safe root: {dest_dir}")
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.infolist()
            for member in members:
                member_path = os.path.realpath(os.path.join(real_dest, member.filename))
                if not (member_path == real_dest or member_path.startswith(real_dest + os.sep)):
                    raise ValueError(f"ZIP member {member.filename} would extract outside target directory")
            total = sum(member.file_size for member in members)
            extracted = 0
            for member in members:
                target = os.path.join(real_dest, member.filename)
                if member.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    while chunk := src.read(_EXTRACT_CHUNK):
                        dst.write(chunk)
                        extracted += len(chunk)
                        if progress_callback is not None:
                            progress_callback(extracted, total)

    def decode_url_encoded_names(self, directory: str) -> None:
        """Rename URL-encoded files and directories under *directory* in place.

        Walks bottom-up so nested encoded directories are handled
        correctly. A no-op when the decoded name equals the original.

        ``os.walk`` yields each name as a single basename, so a clean
        decode always stays a single component (e.g. ``Game%20Title.cue``
        → ``Game Title.cue``). A crafted member like ``%2e%2e%2fevil.sh``
        passes the pre-decode ZIP-slip check as one safe component, then
        decodes to ``../evil.sh`` — a traversal. ``safe_path_component``
        catches that: a decoded name that is not a single safe component
        raises :class:`PathTraversalError` and the whole extraction
        aborts (fail-stop) rather than ``os.replace``-ing the file outside
        the extraction directory.
        """
        for root, dirs, files in os.walk(directory, topdown=False):
            for fname in files:
                decoded = urllib.parse.unquote(fname)
                if decoded != fname:
                    safe_path_component(decoded)
                    os.replace(os.path.join(root, fname), os.path.join(root, decoded))
            for dname in dirs:
                decoded = urllib.parse.unquote(dname)
                if decoded != dname:
                    safe_path_component(decoded)
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

    def list_files(self, directory: str) -> list[str]:
        """Recursively return the absolute path of every file under *directory*.

        Satisfies the ``DirectoryFileListerFn`` Protocol — the disc resolver
        needs only which files are present, not their sizes. Idempotent on a
        missing directory: ``os.walk`` yields nothing, so the result is ``[]``.
        """
        out: list[str] = []
        for root, _dirs, files in os.walk(directory):
            out.extend(os.path.join(root, f) for f in files)
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
