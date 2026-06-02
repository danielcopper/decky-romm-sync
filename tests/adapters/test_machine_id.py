"""Tests for the MachineIdAdapter — reads /etc/machine-id."""

from __future__ import annotations

from unittest.mock import mock_open, patch

from adapters.machine_id import MachineIdAdapter


class TestMachineIdAdapter:
    def test_get_returns_stripped_machine_id(self):
        adapter = MachineIdAdapter()
        with patch("adapters.machine_id.open", mock_open(read_data="  abc123def456\n")):
            assert adapter.get() == "abc123def456"

    def test_get_returns_none_when_file_missing(self):
        adapter = MachineIdAdapter()
        with patch("adapters.machine_id.open", side_effect=FileNotFoundError("no such file")):
            assert adapter.get() is None

    def test_get_returns_none_on_oserror(self):
        adapter = MachineIdAdapter()
        with patch("adapters.machine_id.open", side_effect=OSError("boom")):
            assert adapter.get() is None

    def test_get_returns_none_when_file_empty(self):
        adapter = MachineIdAdapter()
        with patch("adapters.machine_id.open", mock_open(read_data="")):
            assert adapter.get() is None

    def test_get_returns_none_when_file_whitespace_only(self):
        adapter = MachineIdAdapter()
        with patch("adapters.machine_id.open", mock_open(read_data="   \n\t ")):
            assert adapter.get() is None
