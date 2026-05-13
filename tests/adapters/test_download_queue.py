"""Tests for DownloadQueueAdapter — lock-and-poll on download_requests.json."""

from __future__ import annotations

import fcntl
import json
import threading
import time

import pytest

from adapters.download_queue import DownloadQueueAdapter


@pytest.fixture
def adapter() -> DownloadQueueAdapter:
    return DownloadQueueAdapter()


class TestPollAndClear:
    def test_returns_list_and_clears(self, adapter, tmp_path):
        path = tmp_path / "requests.json"
        path.write_text(json.dumps([{"rom_id": 1}, {"rom_id": 2}]))
        result = adapter.poll_and_clear(str(path))
        assert result == [{"rom_id": 1}, {"rom_id": 2}]
        # File now holds an empty list
        assert json.loads(path.read_text()) == []

    def test_returns_empty_on_missing_file(self, adapter, tmp_path):
        assert adapter.poll_and_clear(str(tmp_path / "missing.json")) == []

    def test_returns_empty_on_empty_list_payload(self, adapter, tmp_path):
        path = tmp_path / "requests.json"
        path.write_text("[]")
        assert adapter.poll_and_clear(str(path)) == []
        # File is left as-is (already empty)
        assert json.loads(path.read_text()) == []

    def test_returns_empty_on_malformed_json(self, adapter, tmp_path):
        path = tmp_path / "requests.json"
        path.write_text("not json")
        assert adapter.poll_and_clear(str(path)) == []

    def test_acquires_exclusive_lock(self, adapter, tmp_path):
        """Concurrent writers must wait for poll_and_clear to release the lock.

        Reads the request file while another thread holds an LOCK_EX —
        poll_and_clear should block until the lock is released, then
        successfully consume the entry.
        """
        path = tmp_path / "requests.json"
        path.write_text(json.dumps([{"rom_id": 42}]))

        holder_acquired = threading.Event()
        holder_release = threading.Event()
        poll_done = threading.Event()
        result_holder: list = []

        def hold_lock() -> None:
            with open(path, "r+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                holder_acquired.set()
                holder_release.wait(timeout=5)
                fcntl.flock(f, fcntl.LOCK_UN)

        def do_poll() -> None:
            result_holder.append(adapter.poll_and_clear(str(path)))
            poll_done.set()

        holder = threading.Thread(target=hold_lock)
        poller = threading.Thread(target=do_poll)
        holder.start()
        assert holder_acquired.wait(timeout=5)
        poller.start()
        # Give poller a moment to attempt the lock and block.
        time.sleep(0.1)
        assert not poll_done.is_set(), "poll_and_clear should block while LOCK_EX is held"
        holder_release.set()
        assert poll_done.wait(timeout=5)
        holder.join()
        poller.join()
        assert result_holder[0] == [{"rom_id": 42}]
        # File is truncated
        assert json.loads(path.read_text()) == []

    def test_handles_unicode_payload(self, adapter, tmp_path):
        path = tmp_path / "requests.json"
        path.write_text(json.dumps([{"rom_id": 1, "note": "français"}]))
        result = adapter.poll_and_clear(str(path))
        assert result == [{"rom_id": 1, "note": "français"}]
