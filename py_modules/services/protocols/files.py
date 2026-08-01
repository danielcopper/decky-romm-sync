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

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from models.prune import (
        MutationOutcome,
        RecoveryArtifact,
        SealedSourceClaims,
        SourceClaim,
        SteamRecoverySnapshot,
    )

    from domain.prune import BundleReadmeContext


class DirectoryFileListerFn(Protocol):
    """Recursively list the absolute paths of every file under a directory.

    The narrow read seam the disc resolver needs to enumerate a multi-file
    ROM's install directory: it cares only about which files are present, not
    their sizes. Returns absolute paths; idempotent on a missing directory
    (returns ``[]``). Backed by the same recursive walk the download file store
    uses, exposed as a call-shaped Protocol so the resolver never depends on the
    whole ``DownloadFileStore`` surface.
    """

    def __call__(self, directory: str) -> list[str]: ...


class CoverArtFileStore(Protocol):
    """Filesystem seam for cover-art file operations.

    Owns the raw POSIX calls ArtworkService uses to manage cover art across the
    plugin-owned per-ROM cover cache and the shared Steam grid directory: the
    per-ROM cache is downloaded/seeded into, ``copy_file`` publishes the active
    version's cache cover onto the Steam grid as ``{app_id}p.png``, and the read
    seams back the base64 queries and orphan pruning. Path construction,
    registry lookups, and orphan detection remain a service concern; this
    Protocol exposes only the I/O seams.

    Implementations are synchronous — services that call from an async
    context offload via ``loop.run_in_executor``.
    """

    def exists(self, path: str) -> bool:
        """Return True when *path* refers to an existing file or directory."""
        ...

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        ...

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*."""
        ...

    def copy_file(self, src: str, dst: str) -> None:
        """Copy the file *src* to *dst*, leaving *src* in place.

        Publishes a per-ROM cache cover onto the Steam grid (or seeds the cache
        from an existing grid cover) without consuming the source, so every
        sibling version keeps its own cache file. The destination's parent
        directory must already exist; callers create it via :meth:`make_dirs`.
        """
        ...

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        ...

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        ...

    def write_text_atomic(self, path: str, content: str) -> None:
        """Atomically write *content* to *path* as UTF-8 text.

        Writes to a temp file beside *path* and ``os.replace``s it into place;
        the temp file is removed on any failure. Backs the per-ROM cover-validator
        sidecar (#1454).
        """
        ...


class DownloadFileStore(Protocol):
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

    def remove_file(self, path: str) -> None:
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

    def move_dir(self, src: str, dst: str) -> None:
        """Atomically move the whole directory *src* to *dst*.

        Moves the entire subtree (never a single file inside it), so the
        ES-DE directory-collapse rename can never split a multi-file ROM
        (ADR-0008). *dst* must not already exist — callers probe with
        ``exists`` first and skip the move on collision. Same-filesystem
        only (``os.replace`` semantics); the extract dir and its rename
        target are siblings under the platform folder.
        """
        ...

    def copy_file(self, src: str, dst: str) -> None:
        """Copy the file *src* to *dst*, leaving *src* in place.

        Used to heal a mis-suffixed dump file (``PS3_DISC.SFB.txt`` →
        ``PS3_DISC.SFB``) — a correctly-named copy is written while the
        original is preserved. Callers probe with ``exists`` first.
        """
        ...

    def disk_free(self, path: str) -> int:
        """Return the free space in bytes for the filesystem hosting *path*."""
        ...

    def file_size(self, path: str) -> int:
        """Return the size in bytes of the file at *path*, or 0 if it's missing.

        Used by the resume pre-flight to discount the bytes already held by a
        partial ``.tmp``. A missing path reports 0 (no partial to discount).
        """
        ...

    def walk_files_matching_suffixes(self, base_dir: str, suffixes: tuple[str, ...]) -> list[str]:
        """Recursively list files under *base_dir* whose name ends with any of *suffixes*.

        Returns absolute paths. Idempotent on missing *base_dir*
        (returns ``[]``). Pure listing — does not mutate the filesystem;
        callers own the removal loop and any per-file error handling.
        """
        ...

    def extract_zip(
        self,
        archive_path: str,
        dest_dir: str,
        safe_root: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        """Extract *archive_path* into *dest_dir* with ZIP-slip protection.

        *safe_root* is the boundary outside of which extraction is
        rejected. Implementations resolve both *dest_dir* and *safe_root*
        via ``os.path.realpath`` and verify that every member resolves
        within *safe_root* before extracting.

        When *progress_callback* is supplied it is invoked with
        ``(extracted, total)`` uncompressed byte counts as members stream
        out; with it left ``None`` the extraction is silent and produces
        byte-identical output.
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


class FirmwareFileStore(Protocol):
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

    def remove_file(self, path: str) -> None:
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


class MigrationFileStore(Protocol):
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

    def remove_file(self, path: str) -> None:
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

    def get_mtime(self, path: str) -> float:
        """Return the mtime of *path* as a Unix timestamp."""
        ...

    def walk_files(self, base_dir: str) -> list[tuple[str, list[str], list[str]]]:
        """Return ``os.walk``-style ``(dirpath, dirnames, filenames)`` triples for *base_dir*.

        Mirrors ``os.walk`` exactly: returns raw triples so callers
        retain control over directory pruning (e.g., skipping hidden
        directories).
        """
        ...


class RomFileStore(Protocol):
    """Filesystem seam for installed ROM file operations.

    Owns the raw POSIX calls RomRemovalService uses when physically
    removing an installed ROM (single file or multi-file ROM directory).
    Path-safety checks live in ``lib.path_safety``; this Protocol
    exposes only the I/O seams.

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

    def claim_source(self, path: str, safe_root: str) -> SourceClaim:
        """Capture the current no-follow source and complete subtree identity."""
        ...

    def remove_claimed(self, path: str, safe_root: str, claim: SourceClaim) -> MutationOutcome:
        """Remove only the complete claimed source tree and report durable progress."""
        ...


class SaveFileStore(Protocol):
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

    def is_symlink(self, path: str) -> bool:
        """Return True when *path* is a symbolic link, including dangling links."""
        ...

    def canonical_path(self, path: str) -> str:
        """Return the canonical real path used for exact ownership comparison."""
        ...

    def is_within(self, path: str, root: str) -> bool:
        """Return whether canonical *path* is contained by canonical *root*."""
        ...

    def make_dirs(self, path: str) -> None:
        """Create *path* and any missing parents. Idempotent."""
        ...

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def listdir(self, directory: str) -> list[str]:
        """Return the entry names in *directory*; empty list if it does not exist."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Atomically rename *src* to *dst*, replacing any existing file at *dst*.

        Uses ``os.replace`` semantics — same-filesystem only.
        """
        ...

    def claim_source(self, path: str, safe_root: str) -> SourceClaim:
        """Capture the current no-follow source and complete subtree identity."""
        ...

    def ensure_directory(self, path: str, safe_root: str) -> None:
        """Create a directory through anchored no-follow parents and fsync each creation."""
        ...

    def rename_claimed(self, src: str, dst: str, safe_root: str, claim: SourceClaim) -> MutationOutcome:
        """Rename only the complete claimed source and report durable progress."""
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

    def content_hash(self, path: str) -> str:
        """Return RomM's zip-aware content hash for *path*.

        Identical to :meth:`checksum_md5` for a plain file; for a zip archive
        (a multi-file save) the per-entry combined hash RomM computes, so a
        zipped save converges on its content rather than mismatching on the
        archive container's framing. A file that sniffs as a zip but cannot be
        read as one falls back to the plain MD5 rather than raising, so one
        unreadable save never aborts a sync sweep. Non-security use, like
        ``checksum_md5``.

        Inside a :meth:`hash_memo_scope` the result is memoized per file; see
        that method.
        """
        ...

    def hash_memo_scope(self) -> AbstractContextManager[None]:
        """Bound a per-run :meth:`content_hash` memo to a single sync run.

        Within the returned scope, ``content_hash`` caches each save's digest
        keyed by ``(path, mtime_ns, size)`` so one sync run's repeated hashings
        of the same file (negotiate inventory, the newest-wins matrix, the
        post-op baseline write) read it once. The memo is discarded on scope exit
        — never a process-lifetime cache — and a file overwritten mid-run
        re-hashes on its new stat. Reentrant: nested scopes share one memo. The
        engine opens exactly one outermost scope per device-gated save-sync run.
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

    def remove_file(self, path: str) -> None:
        """Delete *path*. Idempotent: a missing file is not an error."""
        ...

    def listdir(self, directory: str) -> list[str]:
        """Return the entries in *directory*."""
        ...

    def is_dir(self, path: str) -> bool:
        """Return True when *path* exists and is a directory."""
        ...

    def read_bytes(self, path: str) -> bytes:
        """Return the contents of *path* as raw bytes."""
        ...


class RecoveryBundleStore(Protocol):
    """Build and seal verified recovery bundles under the plugin recovery root."""

    def root(self) -> str: ...
    def free_bytes(self) -> int: ...
    def measure_path(self, path: str, safe_root: str) -> int: ...
    def validate_sources(self, bundle_path: str, bundle_digest: str | None = None) -> bool: ...
    def source_claims(self, bundle_path: str) -> SealedSourceClaims: ...
    def seal_bundle(
        self,
        bundle_id: str,
        snapshot: dict[str, object],
        artifacts: list[RecoveryArtifact],
        readme_context: BundleReadmeContext,
        playtime_text: str,
        should_abort: Callable[[], bool] | None = None,
    ) -> str: ...


class PruneArtifactStore(Protocol):
    """Own per-ROM cover and SteamGridDB cache artifacts during cleanup."""

    def recovery_artifacts(self, rom_ids: list[int]) -> list[RecoveryArtifact]: ...
    def remove(self, rom_ids: list[int], claims: dict[str, SourceClaim] | None = None) -> MutationOutcome: ...


class SteamRecoveryStore(Protocol):
    """Own per-shortcut Steam Input/grid recovery files and controller state."""

    def snapshot(self, app_id: int) -> SteamRecoverySnapshot: ...
    def validate_state(self, app_id: int, snapshot: SteamRecoverySnapshot) -> bool: ...
    def remove_state(
        self,
        app_id: int,
        snapshot: SteamRecoverySnapshot,
        claims: dict[str, SourceClaim],
    ) -> MutationOutcome: ...
