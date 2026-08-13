"""In-memory ``AdoptionMoveStore`` implementation for service tests.

Backed by the **same** ``FakeDownloadFileStore`` the service reads its targets
through, so a rename is visible to the stat that follows it and a test asserts on
where the files ended up rather than on which calls were made. Two separate
virtual filesystems would let a move "succeed" into a place the next step cannot
see, which is a green test for a broken flow.

The link-then-unlink and rollback semantics are the adapter's, and are pinned
against a real filesystem in ``tests/adapters/test_adoption_move.py``. What a
staged ``outcome`` here buys is the service's reaction to an outcome the adapter
can genuinely produce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.adoption import MoveOutcome

    from .fake_download_file_store import FakeDownloadFileStore


class FakeAdoptionMoveStore:
    """In-memory ``AdoptionMoveStore`` sharing one virtual filesystem with the download store.

    ``moves`` records what was asked for. ``outcome`` stages the
    result of the next :meth:`move_pairs` so a service test can drive the
    partial-failure branches — and the filesystem is then left in the state that
    outcome *describes*: a moved pair arrives, a stranded one arrives and keeps
    its old name too, an unmoved one does not budge. Reporting a partial failure
    over an untouched tree would be a state the adapter cannot produce.
    """

    def __init__(self, store: FakeDownloadFileStore) -> None:
        self._store = store
        self.moves: list[tuple[str, str]] = []
        self.outcome: MoveOutcome | None = None

    def list_names(self, directory: str) -> tuple[str, ...]:
        prefix = directory.rstrip("/") + "/"
        return tuple(
            sorted(
                path[len(prefix) :]
                for path in self._store.files
                if path.startswith(prefix) and "/" not in path[len(prefix) :]
            )
        )

    def exists(self, path: str) -> bool:
        return self._store.exists(path)

    def is_file(self, path: str) -> bool:
        return path in self._store.files

    def move_pairs(self, pairs: tuple[tuple[str, str], ...]) -> MoveOutcome:
        self.moves.extend(pairs)
        if self.outcome is None:
            for source, target in pairs:
                self._relocate(source, target)
            return {"moved": [target for _source, target in pairs], "stranded": [], "unmoved": [], "error": ""}
        for source, target in pairs:
            if target in self.outcome["moved"]:
                self._relocate(source, target)
            elif source in self.outcome["stranded"]:
                self._relocate(source, target)
                self._store.files[source] = self._store.files[target]
        return self.outcome

    def _relocate(self, source: str, target: str) -> None:
        """Move one file, or a whole subtree when *source* names a directory."""
        if source in self._store.files:
            self._store.files[target] = self._store.files.pop(source)
            return
        prefix = source.rstrip("/") + "/"
        for path in [path for path in self._store.files if path.startswith(prefix)]:
            self._store.files[target.rstrip("/") + "/" + path[len(prefix) :]] = self._store.files.pop(path)
        self._store.dirs.discard(source)
        self._store.dirs.add(target)
