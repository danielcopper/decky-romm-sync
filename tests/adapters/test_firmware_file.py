"""Tests for FirmwareFileAdapter — raw filesystem ops for firmware/BIOS files."""

from __future__ import annotations

import hashlib
import re

import pytest

from adapters.firmware_file import FirmwareFileAdapter


@pytest.fixture
def fw_files() -> FirmwareFileAdapter:
    return FirmwareFileAdapter()


class TestExists:
    def test_true_for_existing_file(self, fw_files, tmp_path):
        f = tmp_path / "bios.bin"
        f.write_bytes(b"x")
        assert fw_files.exists(str(f)) is True

    def test_true_for_directory(self, fw_files, tmp_path):
        assert fw_files.exists(str(tmp_path)) is True

    def test_false_for_missing(self, fw_files, tmp_path):
        assert fw_files.exists(str(tmp_path / "missing.bin")) is False


class TestRemove:
    def test_removes_existing(self, fw_files, tmp_path):
        f = tmp_path / "bios.bin"
        f.write_bytes(b"x")
        fw_files.remove_file(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, fw_files, tmp_path):
        # idempotent: must not raise
        fw_files.remove_file(str(tmp_path / "missing.bin"))

    def test_propagates_non_filenotfound_errors(self, fw_files, tmp_path):
        # Removing a non-empty directory raises IsADirectoryError or OSError —
        # anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            fw_files.remove_file(str(tmp_path))


class TestRename:
    def test_renames_file(self, fw_files, tmp_path):
        src = tmp_path / "a.bin.tmp"
        dst = tmp_path / "a.bin"
        src.write_bytes(b"payload")
        fw_files.rename(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"payload"

    def test_overwrites_existing_destination(self, fw_files, tmp_path):
        src = tmp_path / "new.tmp"
        dst = tmp_path / "old.bin"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        fw_files.rename(str(src), str(dst))
        assert dst.read_bytes() == b"new"
        assert not src.exists()

    def test_missing_source_raises(self, fw_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            fw_files.rename(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestMakeDirs:
    def test_creates_dir(self, fw_files, tmp_path):
        target = tmp_path / "bios"
        fw_files.make_dirs(str(target))
        assert target.is_dir()

    def test_creates_parents(self, fw_files, tmp_path):
        target = tmp_path / "retrodeck" / "bios" / "dc"
        fw_files.make_dirs(str(target))
        assert target.is_dir()

    def test_idempotent_when_dir_exists(self, fw_files, tmp_path):
        target = tmp_path / "bios"
        target.mkdir()
        # Must not raise
        fw_files.make_dirs(str(target))
        assert target.is_dir()


class TestChecksumMd5:
    def test_matches_hashlib(self, fw_files, tmp_path):
        f = tmp_path / "bios.bin"
        payload = b"firmware payload bytes"
        f.write_bytes(payload)
        expected = hashlib.md5(payload).hexdigest()
        assert fw_files.checksum_md5(str(f)) == expected

    def test_returns_hex_digest_format(self, fw_files, tmp_path):
        f = tmp_path / "bios.bin"
        f.write_bytes(b"x")
        digest = fw_files.checksum_md5(str(f))
        # 32-char lowercase hex string
        assert re.fullmatch(r"[0-9a-f]{32}", digest)

    def test_empty_file(self, fw_files, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert fw_files.checksum_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_streams_large_file(self, fw_files, tmp_path):
        """Files larger than one chunk are hashed correctly."""
        f = tmp_path / "large.bin"
        # 25 KiB — well above the 8 KiB chunk size
        payload = b"A" * (25 * 1024)
        f.write_bytes(payload)
        assert fw_files.checksum_md5(str(f)) == hashlib.md5(payload).hexdigest()

    def test_missing_file_raises(self, fw_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            fw_files.checksum_md5(str(tmp_path / "missing.bin"))


class TestReadBytes:
    def test_roundtrip(self, fw_files, tmp_path):
        f = tmp_path / "bios.bin"
        f.write_bytes(b"\x00\x01\x02\x03")
        assert fw_files.read_bytes(str(f)) == b"\x00\x01\x02\x03"

    def test_empty_file(self, fw_files, tmp_path):
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert fw_files.read_bytes(str(f)) == b""

    def test_missing_raises(self, fw_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            fw_files.read_bytes(str(tmp_path / "missing.bin"))
