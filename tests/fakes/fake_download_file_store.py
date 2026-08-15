"""In-memory ``DownloadFileStore`` implementation for service tests."""

from __future__ import annotations

import hashlib
import io
import os
import urllib.parse
import zipfile
import zlib
from typing import TYPE_CHECKING

from domain.rom_candidates import DIR, FILE, LINK, Kind
from lib.path_safety import safe_path_component

if TYPE_CHECKING:
    from collections.abc import Callable

    from models.adoption import ArchiveMemberInfo, ExistingContent, TopLevelEntry, TopLevelName


class FakeDownloadFileStore:
    """In-memory ``DownloadFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` so file ops are deterministic and
    free of filesystem side effects. ``remove_file`` / ``remove_tree``
    are idempotent per the Protocol contract. ``is_dir`` reports True
    for any path that is the parent of an entry or matches a directory
    created via ``make_dirs``.

    The fake captures enough state to model the download flow:
    - ``files`` — ``{path: bytes}`` snapshot of the virtual filesystem.
    - ``dirs`` — explicit set of directory paths (populated by
      ``make_dirs`` and ``extract_zip``).
    - ``disk_free_bytes`` — value returned by ``disk_free`` (default
      large, override via ``set_disk_free``).
    - ``fail_on_atomic_write`` — when True, ``write_text_atomic`` cleans
      up the tmp file and raises ``OSError`` to mirror the real adapter
      behaviour.
    - ``remove_failures`` / ``remove_tree_failures`` — sets of paths that
      raise ``OSError`` on the respective operation; used by partial-
      failure tests in ``cleanup_leftover_tmp_files`` and
      ``_cleanup_partial_download``.
    - ``decode_calls`` / ``extract_calls`` / ``walk_calls`` /
      ``member_checksum_calls`` — captured argument lists for tests that
      need to assert on adapter calls.
    - ``links`` — ``{path: target}`` for every entry the directory read reports
      as a symlink. Listed by both listings with ``kind="link"``, which is never
      adoptable. The target is stored rather than a bare set of paths because a
      link's size is the length of the path it holds, and a fake that answered 0
      would state a number the real adapter never returns.
    - ``other_kinds`` — paths a filesystem holds that are neither file,
      directory nor link (a FIFO, a socket, a device node). Staged so a
      test can prove they are not listed at all.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.mtimes: dict[str, float] = {}
        self.dirs: set[str] = set()
        self.links: dict[str, str] = {}
        self.other_kinds: set[str] = set()
        self.disk_free_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB
        self.fail_on_atomic_write: bool = False
        self.tmp_files: set[str] = set()
        self.decode_calls: list[str] = []
        self.extract_calls: list[tuple[str, str, str]] = []
        self.walk_calls: list[tuple[str, tuple[str, ...]]] = []
        self.remove_failures: set[str] = set()
        self.remove_tree_failures: set[str] = set()
        self.member_checksum_calls: list[tuple[str, str, str]] = []

    def set_disk_free(self, bytes_free: int) -> None:
        self.disk_free_bytes = bytes_free

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def describe_path(self, path: str) -> ExistingContent | None:
        """Describe *path*, summing a directory's contents like the real adapter.

        A staged link or a staged kindless entry occupies its path whether or not
        anything is behind it, so both are described rather than reported as
        absent — the real adapter's ``lstat`` answers the same way, and the
        kindless one comes back with no ``kind`` rather than as a file.

        A link's ``size_bytes`` is the length of the path it stores, which is
        what ``lstat`` reports for one; ``links`` therefore holds a target path
        per link rather than a bare set, so the fake states that number instead
        of a zero the real adapter never returns. ``mtimes`` is an explicit
        per-path override; anything unset reports 0.0 so a test that does not
        care about the timestamp does not have to stage one.
        """
        if path in self.links:
            return {
                "path": path,
                "kind": LINK,
                "size_bytes": len(self.links[path]),
                "modified_at": self.mtimes.get(path, 0.0),
            }
        if path in self.other_kinds:
            return {"path": path, "kind": None, "size_bytes": 0, "modified_at": self.mtimes.get(path, 0.0)}
        if not self.exists(path):
            return None
        is_dir = self.is_dir(path)
        size = sum(size for _p, size in self.scan_files_with_sizes(path)) if is_dir else len(self.files.get(path, b""))
        return {
            "path": path,
            "kind": DIR if is_dir else FILE,
            "size_bytes": size,
            "modified_at": self.mtimes.get(path, 0.0),
        }

    def _top_level(self, directory: str) -> tuple[tuple[str, str, Kind], ...]:
        """``(name, path, kind)`` for everything directly inside *directory*.

        Derived from the stored paths rather than scripted, so a fixture cannot
        claim a listing its virtual filesystem does not have. Both listings read
        this and neither filters it further, exactly as the real adapter's two
        both ask ``_entry_kind`` and nothing else — including its rule that a
        kind outside file/dir/link is not listed at all.
        """
        prefix = directory.rstrip("/") + "/"
        kinds: dict[str, Kind] = {}
        for path in list(self.files) + list(self.dirs) + list(self.links) + list(self.other_kinds):
            if not path.startswith(prefix):
                continue
            head, _sep, tail = path[len(prefix) :].partition("/")
            if not head:
                continue
            entry = prefix + head
            if entry in self.other_kinds:
                continue
            if entry in self.links:
                kinds[head] = LINK
            elif tail or entry in self.dirs:
                kinds[head] = DIR
            else:
                kinds.setdefault(head, FILE)
        return tuple((name, prefix + name, kind) for name, kind in sorted(kinds.items()))

    def list_top_level_entries(self, directory: str) -> tuple[TopLevelEntry, ...]:
        """Describe what sits directly inside *directory*, without descending.

        A directory reports size 0, exactly as the real adapter does; a link
        reports the length of the path it stores rather than its target's size,
        because nothing here follows one and that is the number ``lstat`` gives.
        """
        return tuple(
            {
                "name": name,
                "path": path,
                "kind": kind,
                "size_bytes": self._entry_size(path, kind),
                "modified_at": self.mtimes.get(path, 0.0),
            }
            for name, path, kind in self._top_level(directory)
        )

    def _entry_size(self, path: str, kind: Kind) -> int:
        """What a per-entry ``stat`` reports for this kind, the adapter's way."""
        if kind == DIR:
            return 0
        return len(self.links[path]) if kind == LINK else len(self.files.get(path, b""))

    def list_top_level_names(self, directory: str) -> tuple[TopLevelName, ...]:
        """Name and kind only — the same set, read without the per-entry ``stat``.

        Built from the same source as :meth:`list_top_level_entries` and **not**
        projected from it: the two must agree because they apply the same rule,
        not because one is derived from the other. A fake that projects cannot
        exhibit a disagreement, which is how a real one went unnoticed.
        """
        return tuple({"name": name, "path": path, "kind": kind} for name, path, kind in self._top_level(directory))

    def checksum(self, path: str, algorithm: str, progress_callback: Callable[[int], None] | None = None) -> str:
        """Hash the stored bytes, reporting the whole file as one progress chunk."""
        if path not in self.files:
            raise FileNotFoundError(path)
        data = self.files[path]
        if progress_callback is not None:
            progress_callback(len(data))
        if algorithm == "crc32":
            return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
        if algorithm != "md5":
            raise ValueError(f"unsupported checksum algorithm: {algorithm!r}")
        return hashlib.md5(data, usedforsecurity=False).hexdigest()

    def list_archive_members(self, path: str) -> tuple[ArchiveMemberInfo, ...] | None:
        """Read the stored bytes as a real ZIP central directory, or ``None``.

        Deliberately not a scripted answer: the tests stage genuine archive bytes
        and this reads them exactly as the adapter does, so a fixture cannot
        claim a member layout the bytes do not have.
        """
        data = self.files.get(path)
        if data is None:
            return None
        members: list[ArchiveMemberInfo] = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
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
        """Hash one member's decompressed bytes, reporting them as one progress chunk."""
        self.member_checksum_calls.append((path, member_name, algorithm))
        data = self.files.get(path)
        if data is None:
            raise FileNotFoundError(path)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member_bytes = archive.read(member_name)
        if progress_callback is not None:
            progress_callback(len(member_bytes))
        if algorithm == "crc32":
            return f"{zlib.crc32(member_bytes) & 0xFFFFFFFF:08x}"
        if algorithm != "md5":
            raise ValueError(f"unsupported checksum algorithm: {algorithm!r}")
        return hashlib.md5(member_bytes, usedforsecurity=False).hexdigest()

    def remove_file(self, path: str) -> None:
        if path in self.remove_failures:
            raise OSError(f"simulated remove failure: {path}")
        self.files.pop(path, None)
        # An unlink takes the entry out of the listing whatever kind it was —
        # unlinking a link does not need its target.
        self.links.pop(path, None)
        self.other_kinds.discard(path)

    def remove_tree(self, path: str) -> None:
        if path in self.remove_tree_failures:
            raise OSError(f"simulated remove_tree failure: {path}")
        prefix = path.rstrip("/") + "/"
        for stored in list(self.files):
            if stored == path or stored.startswith(prefix):
                del self.files[stored]
        self.dirs.discard(path)
        for d in list(self.dirs):
            if d.startswith(prefix):
                self.dirs.discard(d)

    def make_dirs(self, path: str) -> None:
        self.dirs.add(path)

    def rename(self, src: str, dst: str) -> None:
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files.pop(src)

    def move_dir(self, src: str, dst: str) -> None:
        """Re-key every entry under *src* to *dst*, modelling ``os.replace`` on a dir.

        Raises ``FileNotFoundError`` when *src* is not a known directory.
        """
        if not self.is_dir(src):
            raise FileNotFoundError(src)
        src_prefix = src.rstrip("/") + "/"
        dst_prefix = dst.rstrip("/") + "/"
        for stored in list(self.files):
            if stored == src:
                self.files[dst] = self.files.pop(src)
            elif stored.startswith(src_prefix):
                self.files[dst_prefix + stored[len(src_prefix) :]] = self.files.pop(stored)
        for d in list(self.dirs):
            if d == src:
                self.dirs.discard(d)
                self.dirs.add(dst)
            elif d.startswith(src_prefix):
                self.dirs.discard(d)
                self.dirs.add(dst_prefix + d[len(src_prefix) :])

    def copy_file(self, src: str, dst: str) -> None:
        """Copy the bytes at *src* to *dst*, keeping *src* (models ``shutil.copy2``)."""
        if src not in self.files:
            raise FileNotFoundError(src)
        self.files[dst] = self.files[src]

    def disk_free(self, path: str) -> int:
        return self.disk_free_bytes

    def file_size(self, path: str) -> int:
        """Size of the stored bytes, or 0 for a path the store does not hold."""
        return len(self.files.get(path, b""))

    def is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def walk_files_matching_suffixes(self, base_dir: str, suffixes: tuple[str, ...]) -> list[str]:
        self.walk_calls.append((base_dir, suffixes))
        if not self.is_dir(base_dir):
            return []
        prefix = base_dir.rstrip("/") + "/"
        matches: list[str] = []
        for stored in self.files:
            if not (stored == base_dir or stored.startswith(prefix)):
                continue
            if stored.endswith(suffixes):
                matches.append(stored)
        return matches

    def extract_zip(
        self,
        archive_path: str,
        dest_dir: str,
        safe_root: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        self.extract_calls.append((archive_path, dest_dir, safe_root))
        if archive_path not in self.files:
            raise FileNotFoundError(archive_path)
        # Model the slip-protection: dest_dir must live under safe_root
        if not (dest_dir == safe_root or dest_dir.startswith(safe_root.rstrip("/") + "/")):
            raise ValueError(f"Extract directory would be outside safe root: {dest_dir}")
        # Fake-mode: derive extracted entries from a paired dict the test set.
        members = getattr(self, "_zip_members", {}).get(archive_path, {})
        self.make_dirs(dest_dir)
        # Drive the progress callback with running (extracted, total) byte
        # counts so service tests can observe the "extracting" frames the
        # real adapter emits per member chunk.
        total = sum(len(data) for data in members.values())
        extracted = 0
        for name, data in members.items():
            full = os.path.join(dest_dir, name)
            self.files[full] = data
            extracted += len(data)
            if progress_callback is not None:
                progress_callback(extracted, total)

    def set_zip_members(self, archive_path: str, members: dict[str, bytes]) -> None:
        if not hasattr(self, "_zip_members"):
            self._zip_members: dict[str, dict[str, bytes]] = {}
        self._zip_members[archive_path] = members

    def decode_url_encoded_names(self, directory: str) -> None:
        self.decode_calls.append(directory)
        prefix = directory.rstrip("/") + "/"
        for stored in list(self.files):
            if not stored.startswith(prefix):
                continue
            rel = stored[len(prefix) :]
            # The real adapter walks the tree and decodes each name as a single
            # basename, so a legitimate multi-component ``rel`` (e.g.
            # ``update/Game%20.bin``) is decoded segment-by-segment. Mirror that
            # here — decode + safe-check each component — so the fake is not
            # spuriously stricter than the adapter on legit nested layouts,
            # while still failing-stop on a ``%2e%2e%2f`` → ``..`` segment.
            segments = rel.split("/")
            decoded_segments = [urllib.parse.unquote(seg) for seg in segments]
            if decoded_segments == segments:
                continue
            for seg, decoded_seg in zip(segments, decoded_segments, strict=True):
                if decoded_seg != seg:
                    safe_path_component(decoded_seg)
            new_path = prefix + "/".join(decoded_segments)
            self.files[new_path] = self.files.pop(stored)

    def scan_files_with_sizes(self, directory: str) -> list[tuple[str, int]]:
        prefix = directory.rstrip("/") + "/"
        out: list[tuple[str, int]] = []
        for stored, data in self.files.items():
            if stored == directory or stored.startswith(prefix):
                out.append((stored, len(data)))
        return out

    def write_text_atomic(self, path: str, content: str) -> None:
        tmp_path = path + ".tmp"
        self.tmp_files.add(tmp_path)
        if self.fail_on_atomic_write:
            self.tmp_files.discard(tmp_path)
            raise OSError("simulated atomic-write failure")
        self.files[path] = content.encode("utf-8")
        self.tmp_files.discard(tmp_path)
