"""Filesystem seam Protocols for service file I/O.

Each Protocol owns the raw POSIX-style file operations one service
needs against one logical subtree (cover art, ROM downloads, the
launcher download queue, firmware/BIOS files, RetroDECK migration
flows, installed ROMs, save files, SteamGridDB artwork cache).
Implementations live in adapters; services see only the I/O seams.

Implementations are synchronous — services that call from an async
context offload via ``loop.run_in_executor``.
"""

from __future__ import annotations

from typing import Protocol


class CoverArtFileStore(Protocol):
    """Filesystem seam for cover-art file operations.

    Owns the raw POSIX calls (``exists``, ``remove``, atomic ``rename``,
    ``listdir``, ``isdir``, ``read_bytes``) ArtworkService uses to manage
    cover art under the Steam grid directory. Path construction, registry
    lookups, and orphan detection remain a service concern; this Protocol
    exposes only the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        ...

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        ...

    def isdir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        ...


class DownloadFileAdapter(Protocol):
    """Filesystem seam for ROM download target operations.

    Owns the raw POSIX calls DownloadService uses to manage downloaded
    ROM files: temp-file lifecycle, atomic renames, disk-space probes,
    ZIP extraction with ZIP-slip protection, post-extract URL-decoding,
    and file-size scans for launch-file detection. Path construction,
    queue management, and progress callbacks remain a service concern;
    this Protocol exposes only the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def remove_tree(self, path: str) -> None:
        """Recursively delete *path*. Idempotent: a missing directory is not an error."""
        ...

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        ...

    def disk_free(self, path: str) -> int:
        """Return the free space in bytes for the filesystem hosting *path*."""
        ...

    def walk_files_matching_suffixes(self, base_dir: str, suffixes: tuple[str, ...]) -> list[str]:
        """Recursively list files under *base_dir* whose name ends with any of *suffixes*.

        Returns absolute paths. Idempotent on missing *base_dir*
        (returns ``[]``). Pure listing — does not mutate the filesystem;
        callers own the removal loop and any per-file error handling.
        """
        ...

    def extract_zip(self, archive_path: str, dest_dir: str, safe_root: str) -> None:
        """Extract *archive_path* into *dest_dir* with ZIP-slip protection.

        *safe_root* is the boundary outside of which extraction is
        rejected. Implementations resolve both *dest_dir* and *safe_root*
        via ``os.path.realpath`` and verify that every member resolves
        within *safe_root* before extracting.
        """
        ...

    def decode_url_encoded_names(self, directory: str) -> None:
        """Recursively rename URL-encoded entries under *directory*.

        Files and subdirectories whose names contain ``%XX`` escapes are
        renamed in place to their decoded form. Walks bottom-up so
        nested encoded directories are handled correctly.
        """
        ...

    def scan_files_with_sizes(self, directory: str) -> list[tuple[str, int]]:
        """Recursively list files under *directory* with their sizes.

        Returns a list of ``(absolute_path, size_bytes)`` tuples. Files
        whose size cannot be read report size ``0`` so callers can still
        reason over the list.
        """
        ...

    def write_text_atomic(self, path: str, content: str) -> None:
        """Atomically write *content* to *path* as UTF-8 text.

        Writes to a temp file beside *path* and ``os.replace``s it to
        the final destination. The temp file is removed on any failure.
        """
        ...


class DownloadQueueAdapter(Protocol):
    """Filesystem seam for the launcher-script download request queue.

    Owns the lock-and-poll round-trip DownloadService uses to consume
    queued ROM-download requests written by the RetroDECK launcher
    script. Path construction and request dispatch remain a service
    concern; this Protocol exposes only the read-and-clear seam.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def poll_and_clear(self, path: str) -> list[dict]:
        """Atomically read all pending requests from *path* and clear the file.

        Acquires an exclusive ``fcntl`` lock for the read-and-truncate
        round-trip so concurrent writers cannot lose requests. Returns
        the list of request dicts that were in the file. Idempotent on
        missing or malformed files: returns ``[]``.
        """
        ...


class FirmwareFileAdapter(Protocol):
    """Filesystem seam for firmware/BIOS file operations.

    Owns the raw POSIX calls FirmwareService uses to manage firmware
    downloads under the RetroDECK BIOS directory: existence probes,
    atomic temp-file lifecycle, parent-directory creation, MD5 hashing
    of downloaded payloads, and BIOS registry JSON reads. Path
    construction, registry lookups, and download orchestration remain a
    service concern; this Protocol exposes only the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        ...

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        ...

    def checksum_md5(self, path: str) -> str:
        """Return the hex-encoded MD5 digest of *path*'s contents."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        ...


class MigrationFileAdapter(Protocol):
    """Filesystem seam for RetroDECK path and save-sort migration I/O.

    Owns the raw POSIX calls MigrationService uses to walk source
    locations, create destination directories, and relocate files when
    the RetroDECK home path changes or RetroArch save sorting flips.
    Path construction, conflict policy, and state updates remain a
    service concern; this Protocol exposes only the I/O seams.

    The Protocol distinguishes ``move`` from ``rename`` because the two
    migration flows have different filesystem semantics. ``move`` is
    the cross-device-safe shutil-style relocation used for RetroDECK
    home changes (e.g., internal SSD to SD card); it falls back to
    copy+delete on ``EXDEV``. ``rename`` is the same-filesystem atomic
    ``os.replace`` used inside the saves tree where source and
    destination are guaranteed to share a filesystem.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        ...

    def remove(self, path: str) -> None:
        """Delete the file at *path*. Idempotent: a missing file is not an error."""
        ...

    def remove_tree(self, path: str) -> None:
        """Recursively delete *path*. Idempotent: a missing directory is not an error."""
        ...

    def move(self, src: str, dst: str) -> None:
        """Cross-filesystem-safe move from *src* to *dst*.

        Uses ``shutil.move`` semantics: a same-filesystem rename when
        possible, falling back to copy+delete on ``EXDEV``. Use this
        for RetroDECK home migrations where source and destination may
        live on different filesystems.
        """
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*.

        Uses ``os.replace`` semantics — same-filesystem only. Use this
        for save-sort migrations inside the saves tree where source
        and destination are guaranteed to share a filesystem.
        """
        ...

    def getmtime(self, path: str) -> float:
        """Return the mtime of *path* as a Unix timestamp."""
        ...

    def walk_files(self, base_dir: str) -> list[tuple[str, list[str], list[str]]]:
        """Return ``os.walk``-style ``(dirpath, dirnames, filenames)`` triples for *base_dir*.

        Mirrors ``os.walk`` exactly: returns raw triples so callers
        retain control over directory pruning (e.g., skipping hidden
        directories).
        """
        ...


class RomFileAdapter(Protocol):
    """Filesystem seam for installed ROM file operations.

    Owns the raw POSIX calls RomRemovalService uses when physically
    removing an installed ROM (single file or multi-file ROM directory).
    Path-safety checks remain a domain concern (``domain.path_safety``);
    this Protocol exposes only the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def remove_tree(self, path: str) -> None:
        """Recursively delete *path* and all contents."""
        ...


class SaveFileAdapter(Protocol):
    """Filesystem seam for local save file operations.

    Owns the raw POSIX, ``open()``, ``tempfile``, and ``hashlib``-on-file
    calls SaveService and its sub-services use when reading, writing,
    backing up, hashing, and removing local save files under the
    RetroDECK saves directory. Path construction and platform-specific
    extension lookup remain a domain concern; this Protocol exposes only
    the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def is_file(self, path: str) -> bool:
        """Return True when *path* exists and is a regular file."""
        ...

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        ...

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*.

        Uses ``os.replace`` semantics — same-filesystem only.
        """
        ...

    def get_mtime(self, path: str) -> float:
        """Return the mtime of *path* as a Unix timestamp."""
        ...

    def get_size(self, path: str) -> int:
        """Return the size of *path* in bytes."""
        ...

    def checksum_md5(self, path: str) -> str:
        """Return the hex-encoded MD5 digest of *path*'s contents.

        Non-security use: drift detection between the local file and the
        recorded ``last_sync_hash`` baseline. A collision here would mean
        two different save files treated as identical — "sync misses an
        update", not a security breach.
        """
        ...

    def make_temp_path(self, suffix: str = "") -> str:
        """Return a fresh, unique path safe to write to.

        Backed by ``tempfile.mkstemp`` so the file is created atomically
        (``O_EXCL``) before the fd is closed. The caller owns the file
        and is responsible for removing it.
        """
        ...

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        ...

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path*, overwriting any existing contents."""
        ...


class SgdbArtworkCache(Protocol):
    """Filesystem seam for the SteamGridDB artwork cache directory.

    Owns the raw POSIX calls SteamGridService uses to manage cached
    SGDB artwork (heroes, logos, grids, icons) under the plugin runtime
    directory. Path construction and pruning policy remain a service
    concern; this Protocol exposes only the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def cache_dir(self) -> str:
        """Return the absolute path to the SGDB artwork cache directory.

        Idempotently ensures the directory exists before returning.
        """
        ...

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def remove(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        ...

    def isdir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        ...

    def write_bytes_atomic(self, path: str, data: bytes) -> None:
        """Atomically write *data* to *path*.

        Writes to a temp file beside *path* and ``os.replace``s it to the
        final destination. The temp file is removed on any failure.
        """
        ...
