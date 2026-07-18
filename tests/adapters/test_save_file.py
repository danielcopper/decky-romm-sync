"""Tests for SaveFileAdapter — raw filesystem ops for local save files."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
import zipfile
import zlib

import pytest

from adapters.save_file import SaveFileAdapter

# Golden value computed independently via RomM's _compute_zip_hash algorithm
# (sorted entries → "name:md5(bytes)" lines joined by "\n" → md5 of the UTF-8
# block) over a zip of {"a.srm": b"alpha", "b.srm": b"beta"}. Pins byte-exact
# parity with the 4.9.2 server; a separator/sort/encoding drift breaks it.
_GOLDEN_TWO_ENTRY_ZIP_HASH = "6e42de0bba44de86f213ca48f5c388dd"


@pytest.fixture
def save_files() -> SaveFileAdapter:
    return SaveFileAdapter(logger=logging.getLogger("test"))


class TestExists:
    def test_true_for_existing_file(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        assert save_files.exists(str(f)) is True

    def test_true_for_directory(self, save_files, tmp_path):
        assert save_files.exists(str(tmp_path)) is True

    def test_false_for_missing(self, save_files, tmp_path):
        assert save_files.exists(str(tmp_path / "missing.srm")) is False


class TestIsFile:
    def test_true_for_existing_file(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        assert save_files.is_file(str(f)) is True

    def test_false_for_directory(self, save_files, tmp_path):
        assert save_files.is_file(str(tmp_path)) is False

    def test_false_for_missing(self, save_files, tmp_path):
        assert save_files.is_file(str(tmp_path / "missing.srm")) is False


class TestIsDir:
    def test_true_for_directory(self, save_files, tmp_path):
        assert save_files.is_dir(str(tmp_path)) is True

    def test_false_for_file(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        assert save_files.is_dir(str(f)) is False

    def test_false_for_missing(self, save_files, tmp_path):
        assert save_files.is_dir(str(tmp_path / "missing")) is False


class TestMakeDirs:
    def test_creates_dir(self, save_files, tmp_path):
        target = tmp_path / "saves"
        save_files.make_dirs(str(target))
        assert target.is_dir()

    def test_creates_parents(self, save_files, tmp_path):
        target = tmp_path / "retrodeck" / "saves" / "gba"
        save_files.make_dirs(str(target))
        assert target.is_dir()

    def test_idempotent_when_dir_exists(self, save_files, tmp_path):
        target = tmp_path / "saves"
        target.mkdir()
        save_files.make_dirs(str(target))
        assert target.is_dir()


class TestRemove:
    def test_removes_existing(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        save_files.remove_file(str(f))
        assert not f.exists()

    def test_missing_is_noop(self, save_files, tmp_path):
        # Idempotent: must not raise.
        save_files.remove_file(str(tmp_path / "missing.srm"))

    def test_propagates_non_filenotfound_errors(self, save_files, tmp_path):
        # Removing a non-empty directory raises IsADirectoryError or OSError —
        # anything other than FileNotFoundError must surface.
        with pytest.raises(OSError):
            save_files.remove_file(str(tmp_path))


class TestRename:
    def test_renames_file(self, save_files, tmp_path):
        src = tmp_path / "a.tmp"
        dst = tmp_path / "a.srm"
        src.write_bytes(b"payload")
        save_files.rename(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"payload"

    def test_overwrites_existing_destination(self, save_files, tmp_path):
        src = tmp_path / "new.tmp"
        dst = tmp_path / "old.srm"
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        save_files.rename(str(src), str(dst))
        assert dst.read_bytes() == b"new"
        assert not src.exists()

    def test_missing_source_raises(self, save_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            save_files.rename(str(tmp_path / "missing"), str(tmp_path / "dst"))


class TestGetMtime:
    def test_returns_unix_timestamp(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        mtime = save_files.get_mtime(str(f))
        assert mtime == pytest.approx(f.stat().st_mtime)

    def test_missing_raises(self, save_files, tmp_path):
        with pytest.raises(OSError):
            save_files.get_mtime(str(tmp_path / "missing.srm"))


class TestGetSize:
    def test_returns_byte_count(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"abcdef")
        assert save_files.get_size(str(f)) == 6

    def test_zero_for_empty_file(self, save_files, tmp_path):
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")
        assert save_files.get_size(str(f)) == 0

    def test_missing_raises(self, save_files, tmp_path):
        with pytest.raises(OSError):
            save_files.get_size(str(tmp_path / "missing.srm"))


class TestChecksumMd5:
    def test_matches_hashlib(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        payload = b"save payload bytes"
        f.write_bytes(payload)
        expected = hashlib.md5(payload).hexdigest()
        assert save_files.checksum_md5(str(f)) == expected

    def test_returns_hex_digest_format(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"x")
        digest = save_files.checksum_md5(str(f))
        assert re.fullmatch(r"[0-9a-f]{32}", digest)

    def test_empty_file(self, save_files, tmp_path):
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")
        assert save_files.checksum_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_streams_large_file(self, save_files, tmp_path):
        """Files larger than one chunk are hashed correctly."""
        f = tmp_path / "large.srm"
        # 25 KiB — well above the 8 KiB chunk size.
        payload = b"A" * (25 * 1024)
        f.write_bytes(payload)
        assert save_files.checksum_md5(str(f)) == hashlib.md5(payload).hexdigest()

    def test_missing_file_raises(self, save_files, tmp_path):
        with pytest.raises(FileNotFoundError):
            save_files.checksum_md5(str(tmp_path / "missing.srm"))


def _write_zip(path, entries: list[tuple[str, bytes]]) -> None:
    """Write a zip at *path* with the given ``(name, bytes)`` members."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)


def _write_corrupt_central_dir_zip(path) -> None:
    """Write a real zip, then clobber its central-directory signature.

    The End-Of-Central-Directory record stays intact, so ``is_zipfile`` still
    sniffs it as a zip — but ``ZipFile(path)`` raises ``BadZipFile`` on open. The
    dominant real-world poison: a corrupt / truncated archive.
    """
    _write_zip(path, [("battery.srm", b"battery-bytes"), ("rtc.bin", b"rtc-bytes")])
    data = bytearray(path.read_bytes())
    cd_offset = struct.unpack("<I", data[-22:][16:20])[0]  # EOCD → central-dir offset
    data[cd_offset : cd_offset + 4] = b"\x00\x00\x00\x00"  # kill the PK\x01\x02 magic
    path.write_bytes(bytes(data))


def _write_unknown_compression_zip(path) -> None:
    """Write a stored zip, then patch both compression-method fields to a value
    this runtime cannot decode → ``NotImplementedError`` (a ``RuntimeError``) on
    read. Models a save compressed with a method the stdlib lacks (e.g. zstd,
    which ``zipfile`` only learns to read in 3.14)."""
    _write_zip(path, [("a.srm", b"hello world")])
    data = bytearray(path.read_bytes())
    data[8:10] = struct.pack("<H", 99)  # local file header compression method
    cd = data.find(b"PK\x01\x02")
    data[cd + 10 : cd + 12] = struct.pack("<H", 99)  # central directory method
    path.write_bytes(bytes(data))


def _write_corrupt_deflate_zip(path) -> None:
    """Write a deflate zip with an intact directory, then scramble the compressed
    payload bytes → ``zlib.error`` (a decompression failure) on read."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.srm", b"A" * 5000)
    data = bytearray(path.read_bytes())
    cd = data.find(b"PK\x01\x02")
    for i in range(40, min(80, cd)):
        data[i] ^= 0xFF
    path.write_bytes(bytes(data))


class TestContentHash:
    """``content_hash`` mirrors RomM's ``compute_content_hash`` (zip-aware MD5)."""

    def test_plain_file_matches_checksum_md5(self, save_files, tmp_path):
        """A non-zip file hashes identically to the streamed single-file MD5."""
        f = tmp_path / "game.srm"
        payload = b"save payload bytes"
        f.write_bytes(payload)
        assert save_files.content_hash(str(f)) == hashlib.md5(payload).hexdigest()
        assert save_files.content_hash(str(f)) == save_files.checksum_md5(str(f))

    def test_zip_matches_romm_golden(self, save_files, tmp_path):
        """A multi-file (zip) save converges on RomM's per-entry combined hash."""
        z = tmp_path / "multi.zip"
        _write_zip(z, [("a.srm", b"alpha"), ("b.srm", b"beta")])
        assert save_files.content_hash(str(z)) == _GOLDEN_TWO_ENTRY_ZIP_HASH

    def test_dispatch_is_by_content_not_extension(self, save_files, tmp_path):
        """A zip archive named ``.srm`` still takes the zip path (content sniff)."""
        z = tmp_path / "looks_single.srm"
        _write_zip(z, [("a.srm", b"alpha"), ("b.srm", b"beta")])
        # Zip path → the combined hash, NOT a raw-bytes MD5 of the archive.
        assert save_files.content_hash(str(z)) == _GOLDEN_TWO_ENTRY_ZIP_HASH
        assert save_files.content_hash(str(z)) != save_files.checksum_md5(str(z))

    def test_member_order_independent(self, save_files, tmp_path):
        """Reversing the archive write order yields the same hash (RomM sorts)."""
        forward = tmp_path / "forward.zip"
        reverse = tmp_path / "reverse.zip"
        _write_zip(forward, [("a.srm", b"alpha"), ("b.srm", b"beta")])
        _write_zip(reverse, [("b.srm", b"beta"), ("a.srm", b"alpha")])
        assert save_files.content_hash(str(forward)) == save_files.content_hash(str(reverse))

    def test_directory_entries_are_skipped(self, save_files, tmp_path):
        """A directory member does not change the hash — only file entries count."""
        plain = tmp_path / "plain.zip"
        withdir = tmp_path / "withdir.zip"
        _write_zip(plain, [("a.srm", b"alpha"), ("b.srm", b"beta")])
        _write_zip(withdir, [("sub/", b""), ("a.srm", b"alpha"), ("b.srm", b"beta")])
        assert save_files.content_hash(str(withdir)) == save_files.content_hash(str(plain))
        assert save_files.content_hash(str(withdir)) == _GOLDEN_TWO_ENTRY_ZIP_HASH

    def test_empty_zip_hashes_empty_string(self, save_files, tmp_path):
        """A zip with no file entries hashes the empty string (md5 of '')."""
        z = tmp_path / "empty.zip"
        _write_zip(z, [])
        assert save_files.content_hash(str(z)) == hashlib.md5(b"").hexdigest()

    def test_missing_file_raises(self, save_files, tmp_path):
        with pytest.raises(OSError):
            save_files.content_hash(str(tmp_path / "missing.srm"))


class TestContentHashUnreadableZipFallback:
    """#1470 — a file that sniffs as a zip but cannot be read as one falls back to
    the plain ``checksum_md5`` instead of raising, so one poison save can never
    abort the whole sync sweep. RomM's server degrades the same file to
    ``content_hash=None``; the plain MD5 keeps the local drift baseline working
    and the kernel's truthiness guards reject the ``None``-side identity match.
    """

    _POISON_BUILDERS = (_write_corrupt_central_dir_zip, _write_unknown_compression_zip, _write_corrupt_deflate_zip)

    @pytest.mark.parametrize("build", _POISON_BUILDERS)
    def test_fixture_sniffs_as_zip_but_is_unreadable(self, build, tmp_path):
        """Guard against a vacuous fixture: each poison must genuinely pass the
        ``is_zipfile`` sniff yet raise when actually read — otherwise the fallback
        tests would go green without exercising the fallback at all."""
        f = tmp_path / "poison.srm"
        build(f)
        assert zipfile.is_zipfile(str(f)) is True
        with pytest.raises((zipfile.BadZipFile, zlib.error, RuntimeError)), zipfile.ZipFile(str(f), "r") as zf:
            for name in zf.namelist():
                zf.read(name)

    @pytest.mark.parametrize("build", _POISON_BUILDERS)
    def test_falls_back_to_checksum_md5(self, build, save_files, tmp_path):
        """No raise, and the digest is the whole-file MD5 (never a partial zip
        per-entry hash) — the same value RomM's local drift baseline expects."""
        f = tmp_path / "poison.srm"
        build(f)
        assert save_files.content_hash(str(f)) == save_files.checksum_md5(str(f))

    def test_fallback_is_debug_logged(self, save_files, tmp_path, caplog):
        f = tmp_path / "poison.srm"
        _write_corrupt_central_dir_zip(f)
        with caplog.at_level(logging.DEBUG, logger="test"):
            save_files.content_hash(str(f))
        messages = [r.getMessage() for r in caplog.records]
        assert any("unreadable" in m and str(f) in m for m in messages)


class TestHashMemoScope:
    """``hash_memo_scope`` bounds a per-run ``content_hash`` memo to one sync run.

    Keyed ``(path, mtime_ns, size)``: a repeat hash of an unchanged file inside a
    scope is served from the memo (no re-read), an mtime or size change misses,
    and the memo is discarded on scope exit so it never grows across runs.
    """

    @staticmethod
    def _rewrite_same_stat(path, data: bytes) -> None:
        """Overwrite *path* with *data*, restoring the original mtime_ns.

        Lets a test change a file's *content* while keeping ``(mtime_ns, size)``
        identical, so a memo hit returns the pre-change (stale) digest — the only
        observable proof the file was not re-read.
        """
        before = os.stat(path)
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_memo_hit_serves_stale_digest_without_reread(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"A" * 16)
        first = hashlib.md5(b"A" * 16).hexdigest()

        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == first
            # Change the bytes but keep (mtime_ns, size) → same memo key.
            self._rewrite_same_stat(f, b"B" * 16)
            # Memo hit: returns the pre-change digest, proving no re-read.
            assert save_files.content_hash(str(f)) == first
            assert save_files.content_hash(str(f)) != hashlib.md5(b"B" * 16).hexdigest()

    def test_memo_misses_on_mtime_change(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"A" * 16)
        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == hashlib.md5(b"A" * 16).hexdigest()
            # Same content + size, but bump mtime_ns → new key → recompute.
            f.write_bytes(b"C" * 16)
            os.utime(f, ns=(os.stat(f).st_atime_ns, os.stat(f).st_mtime_ns + 1000))
            assert save_files.content_hash(str(f)) == hashlib.md5(b"C" * 16).hexdigest()

    def test_memo_misses_on_size_change(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"A" * 16)
        with save_files.hash_memo_scope():
            first = save_files.content_hash(str(f))
            # Different size (even if mtime were reused) → new key → recompute.
            self._rewrite_same_stat(f, b"A" * 32)
            assert save_files.content_hash(str(f)) == hashlib.md5(b"A" * 32).hexdigest()
            assert save_files.content_hash(str(f)) != first

    def test_memo_reset_between_scopes(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"A" * 16)
        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == hashlib.md5(b"A" * 16).hexdigest()
        # New content, identical (mtime_ns, size): a leaked memo would return the
        # stale digest. A fresh scope must recompute — the memo was discarded.
        self._rewrite_same_stat(f, b"B" * 16)
        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == hashlib.md5(b"B" * 16).hexdigest()

    def test_no_memo_outside_scope(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"A" * 16)
        assert save_files.content_hash(str(f)) == hashlib.md5(b"A" * 16).hexdigest()
        # No scope open → every call reads the file, so a same-stat rewrite is
        # observed (no process-lifetime cache).
        self._rewrite_same_stat(f, b"B" * 16)
        assert save_files.content_hash(str(f)) == hashlib.md5(b"B" * 16).hexdigest()

    def test_nested_scopes_share_one_memo(self, save_files, tmp_path):
        f = tmp_path / "game.srm"
        f.write_bytes(b"A" * 16)
        first = hashlib.md5(b"A" * 16).hexdigest()
        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == first
            with save_files.hash_memo_scope():
                # Inner scope shares the outer memo → still a hit on stale bytes.
                self._rewrite_same_stat(f, b"B" * 16)
                assert save_files.content_hash(str(f)) == first
            # Inner exit must NOT clear the memo the outer scope still owns.
            assert save_files.content_hash(str(f)) == first
        # Only the outermost exit clears it: a fresh scope recomputes.
        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == hashlib.md5(b"B" * 16).hexdigest()

    def test_zip_content_hash_is_memoized(self, save_files, tmp_path):
        """The zip path is memoized too — the dedup covers multi-file saves."""
        z = tmp_path / "multi.zip"
        _write_zip(z, [("a.srm", b"alpha"), ("b.srm", b"beta")])
        with save_files.hash_memo_scope():
            first = save_files.content_hash(str(z))
            assert first == _GOLDEN_TWO_ENTRY_ZIP_HASH
            # Repack a different entry payload of identical length, restoring
            # (mtime_ns, size). ZIP_STORED keeps the archive byte-size stable, so
            # the memo key is unchanged and a hit serves the pre-change digest —
            # not the digest of the new bytes.
            before = os.stat(z)
            _write_zip(z, [("a.srm", b"AAAAA"), ("b.srm", b"beta")])
            os.utime(z, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert os.stat(z).st_size == before.st_size  # guards against a vacuous pass
            assert save_files.content_hash(str(z)) == first
        # Scope closed → memo discarded → recompute now reflects the new bytes,
        # confirming the in-scope value was served from the memo, not recomputed.
        assert save_files.content_hash(str(z)) != first

    def test_unreadable_zip_fallback_is_memoized(self, save_files, tmp_path):
        """#1470 — the fallback digest populates the memo under the file's stat key
        (it does not bypass it): a same-stat rewrite to readable content still
        serves the fallback digest, proving the in-scope value came from the memo."""
        f = tmp_path / "poison.srm"
        _write_corrupt_central_dir_zip(f)
        fallback = hashlib.md5(f.read_bytes()).hexdigest()
        with save_files.hash_memo_scope():
            assert save_files.content_hash(str(f)) == fallback
            # Overwrite with a readable single-file save of identical (mtime_ns,
            # size) → same memo key → a hit must still return the fallback digest.
            before = os.stat(f)
            new = b"Z" * before.st_size
            f.write_bytes(new)
            os.utime(f, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert os.stat(f).st_size == before.st_size  # guards against a vacuous pass
            assert save_files.content_hash(str(f)) == fallback
            assert save_files.content_hash(str(f)) != hashlib.md5(new).hexdigest()

    def test_unreadable_zip_fallback_memo_invalidated_on_change(self, save_files, tmp_path):
        """#1470 — a stat change to the poison file misses the memo and re-hashes,
        so the fallback digest is bound to ``(path, mtime_ns, size)``, not pinned
        across a real edit."""
        f = tmp_path / "poison.srm"
        _write_corrupt_central_dir_zip(f)
        with save_files.hash_memo_scope():
            first = save_files.content_hash(str(f))
            assert first == hashlib.md5(f.read_bytes()).hexdigest()
            # Grow the file → different size → new memo key → recompute reflects the
            # new bytes (still whole-file MD5: garbage can't become a readable zip).
            f.write_bytes(f.read_bytes() + b"tail-bytes")
            recomputed = save_files.content_hash(str(f))
            assert recomputed == hashlib.md5(f.read_bytes()).hexdigest()
            assert recomputed != first


class TestMakeTempPath:
    def test_returns_existing_empty_file(self, save_files):
        path = save_files.make_temp_path()
        try:
            assert os.path.isfile(path)
            assert os.path.getsize(path) == 0
        finally:
            os.remove(path)

    def test_unique_paths_each_call(self, save_files):
        paths = [save_files.make_temp_path() for _ in range(3)]
        try:
            assert len(set(paths)) == 3
        finally:
            for p in paths:
                os.remove(p)

    def test_respects_suffix(self, save_files):
        path = save_files.make_temp_path(suffix=".srm.tmp")
        try:
            assert path.endswith(".srm.tmp")
        finally:
            os.remove(path)

    def test_no_suffix_default(self, save_files):
        path = save_files.make_temp_path()
        try:
            assert os.path.isfile(path)
        finally:
            os.remove(path)
