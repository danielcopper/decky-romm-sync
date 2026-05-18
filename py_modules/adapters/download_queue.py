"""Filesystem adapter for the launcher-script download request queue.

Owns the lock-and-poll round-trip used by DownloadService to consume
queued ROM-download requests written by the RetroDECK launcher script.
Path construction and request dispatch remain a service concern; this
adapter exposes only the read-and-clear seam declared by
``services.protocols.DownloadQueueStore``.
"""

from __future__ import annotations

import fcntl
import json


class DownloadQueueAdapter:
    """Synchronous lock-and-poll over the download request file.

    Implements the ``DownloadQueueStore`` Protocol. Methods are
    synchronous — services that call from an async context offload via
    ``loop.run_in_executor``.
    """

    def poll_and_clear(self, path: str) -> list[dict]:
        """Atomically read pending requests from *path* and clear the file.

        Acquires an exclusive ``fcntl`` lock around the read-and-truncate
        round-trip so concurrent writers cannot lose requests. Returns
        the list of request dicts that were in the file. Idempotent on
        a missing file (returns ``[]``); malformed JSON is treated as an
        empty queue and the file is still cleared.
        """
        try:
            with open(path, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    requests = json.load(f)
                except json.JSONDecodeError:
                    requests = []
                if not requests:
                    return []
                f.seek(0)
                f.truncate()
                json.dump([], f)
            return requests
        except FileNotFoundError:
            return []
