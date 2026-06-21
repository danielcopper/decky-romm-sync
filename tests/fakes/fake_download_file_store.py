"""In-memory ``DownloadFileStore`` implementation for service tests."""

from __future__ import annotations

import os
import urllib.parse
from typing import TYPE_CHECKING

from lib.path_safety import safe_path_component

if TYPE_CHECKING:
    from collections.abc import Callable


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
    - ``decode_calls`` / ``extract_calls`` / ``walk_calls`` — captured
      argument lists for tests that need to assert on adapter calls.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set()
        self.disk_free_bytes: int = 10 * 1024 * 1024 * 1024  # 10 GiB
        self.fail_on_atomic_write: bool = False
        self.tmp_files: set[str] = set()
        self.decode_calls: list[str] = []
        self.extract_calls: list[tuple[str, str, str]] = []
        self.walk_calls: list[tuple[str, tuple[str, ...]]] = []
        self.remove_failures: set[str] = set()
        self.remove_tree_failures: set[str] = set()

    def set_disk_free(self, bytes_free: int) -> None:
        self.disk_free_bytes = bytes_free

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def remove_file(self, path: str) -> None:
        if path in self.remove_failures:
            raise OSError(f"simulated remove failure: {path}")
        self.files.pop(path, None)

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

    def disk_free(self, path: str) -> int:
        return self.disk_free_bytes

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
