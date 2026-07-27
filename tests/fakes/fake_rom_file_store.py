"""In-memory ``RomFileStore`` implementation for service tests."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.prune import MutationOutcome, SourceClaim


class FakeRomFileStore:
    """In-memory ``RomFileStore`` for tests.

    Backed by a ``dict[str, bytes]`` for files and a ``set[str]`` for
    explicit directories so file ops are deterministic and free of
    filesystem side effects. ``remove_file`` is idempotent per the
    Protocol contract; ``remove_tree`` clears any entry whose path is
    *path* or lives under ``path + "/"``. ``is_dir`` reports True for
    any path in ``dirs`` or any path that is the parent of an entry,
    mirroring the loose "directory exists when it contains files"
    semantics tests need.

    Failure injection:
    - ``remove_file_failures`` — paths that raise ``OSError`` when
      passed to ``remove_file``.
    - ``remove_tree_failures`` — paths that raise ``OSError`` when
      passed to ``remove_tree``.

    Tests can pre-populate ``files`` directly to stage installed ROM
    state and inspect ``files`` / ``dirs`` after the act to assert
    on deletions.
    """

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        dirs: set[str] | None = None,
    ) -> None:
        self.files: dict[str, bytes] = dict(files) if files else {}
        self.dirs: set[str] = set(dirs) if dirs else set()
        self.remove_file_failures: set[str] = set()
        self.remove_tree_failures: set[str] = set()
        self.remove_file_calls: list[str] = []
        self.remove_tree_calls: list[str] = []

    def is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(stored.startswith(prefix) for stored in self.files)

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def remove_file(self, path: str) -> None:
        self.remove_file_calls.append(path)
        if path in self.remove_file_failures:
            raise OSError(f"simulated remove_file failure: {path}")
        self.files.pop(path, None)

    def remove_tree(self, path: str) -> None:
        self.remove_tree_calls.append(path)
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

    def claim_source(self, path: str, safe_root: str) -> SourceClaim:
        exists = self.exists(path)
        return {
            "source_path": path,
            "safe_root": safe_root,
            "source_identity": {
                "exists": exists,
                "mount_id": 1 if exists else 0,
                "device": 1 if exists else 0,
                "inode": 1 if exists else 0,
                "mode": 1 if exists else 0,
                "size": 0,
                "mtime_ns": 0,
                "ctime_ns": 0,
            },
            "sha256": hashlib.sha256(self.files[path]).hexdigest() if path in self.files else None,
            "entries": {},
        }

    def remove_claimed(self, path: str, safe_root: str, claim: SourceClaim) -> MutationOutcome:
        del safe_root, claim
        if self.is_dir(path):
            self.remove_tree(path)
            changed = True
        elif path in self.files:
            self.remove_file(path)
            changed = True
        else:
            changed = False
        return {"success": True, "changed": changed, "ambiguous": False, "message": "Source removed"}
