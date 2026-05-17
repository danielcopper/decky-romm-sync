"""Tests for the HostnameAdapter — wraps socket.gethostname."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from adapters.hostname import HostnameAdapter


class TestHostnameAdapter:
    def test_get_returns_socket_gethostname_value(self):
        adapter = HostnameAdapter()
        with patch("adapters.hostname.socket.gethostname", return_value="steamdeck"):
            assert adapter.get() == "steamdeck"

    def test_get_propagates_oserror_from_socket(self):
        adapter = HostnameAdapter()
        with (
            patch("adapters.hostname.socket.gethostname", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            adapter.get()
