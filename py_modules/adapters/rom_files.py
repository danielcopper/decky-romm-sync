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
from typing import TYPE_CHECKING

from adapters.descriptor_paths import claim_source, open_directory_fd, remove_claimed, staging_prefix

if TYPE_CHECKING:
    from collections.abc import Callable

    from models.prune import MutationOutcome, SourceClaim


def _outcome(*, changed: bool, message: str) -> MutationOutcome:
    return {"success": True, "changed": changed, "ambiguous": False, "message": message}


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

    def claim_source(self, path: str, safe_root: str, *, digest: bool = True) -> SourceClaim:
        return claim_source(path, safe_root, digest=digest)

    def remove_claimed(
        self,
        path: str,
        safe_root: str,
        claim: SourceClaim,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> MutationOutcome:
        return remove_claimed(path, safe_root, claim, on_progress)

    def reclaim_staged_source(self, path: str, safe_root: str) -> MutationOutcome:
        """Finish removing the staging entries an interrupted removal of *path* left behind.

        The staging name embeds the source's own inode and lives under the
        source's own parent, so this looks at exactly one directory and one
        name prefix. Each entry found is removed under a fresh identity-only
        claim: the interrupted run's claim was partially consumed and can no
        longer be revalidated, which is why the caller must have separate proof
        of ownership — a surviving install record — before asking for this.
        """
        parent = os.path.dirname(path)
        prefix = staging_prefix(os.path.basename(path))
        try:
            parent_fd = open_directory_fd(parent, safe_root)
        except FileNotFoundError:
            return _outcome(changed=False, message="Source was already absent")
        try:
            staged = sorted(name for name in os.listdir(parent_fd) if name.startswith(prefix))
        finally:
            os.close(parent_fd)
        changed = False
        for name in staged:
            staged_path = os.path.join(parent, name)
            outcome = remove_claimed(staged_path, safe_root, claim_source(staged_path, safe_root, digest=False))
            changed = changed or outcome["changed"]
            if not outcome["success"]:
                return {**outcome, "changed": changed}
        if not changed:
            return _outcome(changed=False, message="Source was already absent")
        return _outcome(changed=True, message="Interrupted removal was finished")
