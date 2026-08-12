"""In-memory ``AdoptionMoveStore`` implementation for service tests.

Backed by a flat set of paths that exist, so a test stages the tree the rename
will find and then asserts on where the files ended up — not on which calls were
made. The link-then-unlink and rollback semantics themselves are the adapter's,
and are pinned against a real filesystem in ``tests/adapters/test_adoption_move.py``;
what a staged ``outcome`` here buys is the service's reaction to an outcome the
adapter can produce.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.adoption import MoveOutcome


class FakeAdoptionMoveStore:
    """In-memory ``AdoptionMoveStore`` for tests.

    ``paths`` is every file that exists, by absolute path. ``outcome`` overrides
    the result of the next :meth:`move_pairs` (and suppresses its effect on
    ``paths``) so a service test can drive the partial-failure branches.
    ``remove_failures`` names paths whose removal raises the Overwrite refusal.
    """

    def __init__(self, paths: set[str] | None = None) -> None:
        self.paths: set[str] = set(paths or ())
        self.moves: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.remove_failures: set[str] = set()
        self.outcome: MoveOutcome | None = None

    def list_names(self, directory: str) -> tuple[str, ...]:
        prefix = directory.rstrip("/") + "/"
        return tuple(
            sorted(
                path[len(prefix) :] for path in self.paths if path.startswith(prefix) and "/" not in path[len(prefix) :]
            )
        )

    def exists(self, path: str) -> bool:
        return path in self.paths

    def remove_targets(self, paths: tuple[str, ...]) -> tuple[list[str], str]:
        removed: list[str] = []
        for path in paths:
            if path in self.remove_failures:
                return (removed, f"could not replace {os.path.basename(path)}: staged failure")
            self.paths.discard(path)
            self.removed.append(path)
            removed.append(path)
        return (removed, "")

    def move_pairs(self, pairs: tuple[tuple[str, str], ...]) -> MoveOutcome:
        self.moves.extend(pairs)
        if self.outcome is not None:
            return self.outcome
        for source, target in pairs:
            self.paths.discard(source)
            self.paths.add(target)
        return {"moved": [target for _source, target in pairs], "stranded": [], "unmoved": [], "error": ""}
