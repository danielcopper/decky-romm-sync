"""Tests for the concrete ``PathProbeAdapter``."""

from __future__ import annotations

from pathlib import Path

from adapters.path_probe import PathProbeAdapter


class TestExists:
    def test_returns_true_for_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "present"
        f.write_text("x")
        assert PathProbeAdapter().exists(str(f)) is True

    def test_returns_true_for_existing_directory(self, tmp_path: Path) -> None:
        assert PathProbeAdapter().exists(str(tmp_path)) is True

    def test_returns_false_for_missing_path(self, tmp_path: Path) -> None:
        assert PathProbeAdapter().exists(str(tmp_path / "nope")) is False

    def test_returns_false_for_empty_path(self) -> None:
        assert PathProbeAdapter().exists("") is False
