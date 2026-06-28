"""Tests for domain.save_hash.combine_zip_entry_hashes — RomM zip-hash parity."""

from __future__ import annotations

import hashlib

from domain.save_hash import combine_zip_entry_hashes


class TestCombineZipEntryHashes:
    def test_empty_hashes_the_empty_string(self):
        """No file entries → md5 of the empty string (matches RomM)."""
        assert combine_zip_entry_hashes([]) == hashlib.md5(b"").hexdigest()

    def test_single_entry_format(self):
        """One entry renders as ``name:hexdigest`` then is MD5'd — pins the format."""
        entry_md5 = hashlib.md5(b"hello").hexdigest()
        expected = hashlib.md5(f"save.srm:{entry_md5}".encode()).hexdigest()
        assert combine_zip_entry_hashes([("save.srm", entry_md5)]) == expected

    def test_two_entries_match_romm_golden(self):
        """Golden value computed independently via RomM's algorithm."""
        a = hashlib.md5(b"alpha").hexdigest()
        b = hashlib.md5(b"beta").hexdigest()
        assert combine_zip_entry_hashes([("a.srm", a), ("b.srm", b)]) == "6e42de0bba44de86f213ca48f5c388dd"

    def test_sorted_by_name_not_input_order(self):
        """Input order does not matter — entries are sorted by name before joining."""
        a = hashlib.md5(b"alpha").hexdigest()
        b = hashlib.md5(b"beta").hexdigest()
        forward = combine_zip_entry_hashes([("a.srm", a), ("b.srm", b)])
        reverse = combine_zip_entry_hashes([("b.srm", b), ("a.srm", a)])
        assert forward == reverse

    def test_distinct_entry_set_changes_hash(self):
        """A different entry payload (hence per-entry md5) yields a different combined hash."""
        a = hashlib.md5(b"alpha").hexdigest()
        b = hashlib.md5(b"beta").hexdigest()
        c = hashlib.md5(b"gamma").hexdigest()
        assert combine_zip_entry_hashes([("a.srm", a)]) != combine_zip_entry_hashes([("a.srm", c)])
        assert combine_zip_entry_hashes([("a.srm", a)]) != combine_zip_entry_hashes([("a.srm", a), ("b.srm", b)])
