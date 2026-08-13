"""In-memory ``SaveQuarantineFn`` for service tests.

Models what the real funnel *does* — the file is moved into
``<dir>/.romm-backup/`` and still exists afterwards — rather than recording that
it was called. A test that only asserted the call could not tell a quarantine
from a delete, which is the whole distinction the funnel exists to make.

The naming here is deliberately simpler than ``domain.save_backup.backup_name``:
what a service test needs to know is that the bytes survived somewhere
recoverable, and pinning the real timestamped name is the matrix executor's own
tier.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from domain.save_backup import BACKUP_DIR_NAME

if TYPE_CHECKING:
    from .fake_download_file_store import FakeDownloadFileStore


class FakeSaveQuarantine:
    """Move a save aside inside the same virtual filesystem the stores share.

    ``failures`` names paths whose quarantine raises, which is how a test drives
    the Overwrite refusal. ``missing`` names paths that vanished between the
    caller's ``exists`` probe and this call: the funnel finds nothing and reports
    ``False``, and the file leaves the shared filesystem here rather than
    lingering in a state the real race could not produce. ``quarantined`` records
    the source paths that actually went, in order.
    """

    def __init__(self, store: FakeDownloadFileStore) -> None:
        self._store = store
        self.quarantined: list[str] = []
        self.failures: set[str] = set()
        self.missing: set[str] = set()

    def __call__(self, saves_dir: str, filename: str) -> bool:
        path = os.path.join(saves_dir, filename)
        if path in self.failures:
            raise OSError(f"staged quarantine failure for {filename}")
        if path in self.missing:
            self._store.files.pop(path, None)
            return False
        if path not in self._store.files:
            return False
        self._store.files[os.path.join(saves_dir, BACKUP_DIR_NAME, filename)] = self._store.files.pop(path)
        self.quarantined.append(path)
        return True
