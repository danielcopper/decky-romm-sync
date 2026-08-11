"""Pure adoption judgements: the collision payload and the manifest comparison."""

from domain.rom_adoption import (
    LocalFile,
    ServerFile,
    compare_manifest,
    occupied_target_refusal,
    server_manifest,
    sizes_agree,
    verification_status,
)


def _refusal(**overrides):
    payload = {
        "path": "/roms/snes/Game.sfc",
        "is_dir": False,
        "size_bytes": 1024,
        "modified_at": 1_700_000_000.0,
        "incoming_name": "Game.sfc",
        "incoming_size": 1024,
        "adoptable": True,
    }
    payload.update(overrides)
    return occupied_target_refusal(**payload)


class TestServerManifest:
    def test_prefers_md5_over_crc(self):
        manifest = server_manifest(
            {"files": [{"file_name": "a.bin", "file_size_bytes": 10, "md5_hash": "AB", "crc_hash": "CD"}]}
        )
        assert manifest == (ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab"),)

    def test_falls_back_to_crc_when_md5_is_absent(self):
        manifest = server_manifest({"files": [{"file_name": "a.bin", "file_size_bytes": 10, "crc_hash": "1A2B3C4D"}]})
        assert manifest[0].algorithm == "crc32"
        assert manifest[0].digest == "1a2b3c4d"

    def test_empty_digest_strings_are_not_a_digest(self):
        manifest = server_manifest(
            {"files": [{"file_name": "a.bin", "file_size_bytes": 10, "md5_hash": "  ", "crc_hash": ""}]}
        )
        assert manifest[0].verifiable is False
        assert manifest[0].algorithm == ""

    def test_null_digests_are_not_a_digest(self):
        # ``skip_hash_calculation`` leaves every digest field null, not absent.
        manifest = server_manifest(
            {"files": [{"file_name": "a.bin", "file_size_bytes": 10, "md5_hash": None, "crc_hash": None}]}
        )
        assert manifest[0].verifiable is False

    def test_entries_without_a_name_are_dropped(self):
        # A nameless entry cannot be located on disk, so keeping it would report
        # a false "missing" for every comparison.
        manifest = server_manifest({"files": [{"file_name": "", "md5_hash": "ab"}, {"file_name": "b.bin"}]})
        assert [entry.name for entry in manifest] == ["b.bin"]

    def test_missing_files_key_yields_an_empty_manifest(self):
        assert server_manifest({}) == ()
        assert server_manifest({"files": None}) == ()

    def test_missing_size_reads_as_zero(self):
        manifest = server_manifest({"files": [{"file_name": "a.bin"}]})
        assert manifest[0].size_bytes == 0


class TestManifestRelativePaths:
    """Where the server says each file belongs inside the ROM's own directory.

    ``RomFile.is_top_level`` compares ``rom.full_path`` against ``file_path``, so
    the two are one coordinate system and the ROM-relative path is a subtraction.
    Everything the payload does not state falls back to the bare filename rather
    than to a guessed prefix.
    """

    def _manifest(self, *, full_path=None, file_path=None, name="a.bin"):
        detail = {"files": [{"file_name": name}]}
        if file_path is not None:
            detail["files"][0]["file_path"] = file_path
        if full_path is not None:
            detail["full_path"] = full_path
        return server_manifest(detail)[0]

    def test_a_top_level_file_sits_at_the_rom_root(self):
        entry = self._manifest(full_path="roms/psx/Game", file_path="roms/psx/Game")
        assert entry.rel_path == "a.bin"
        assert entry.lookup_key == "a.bin"

    def test_a_nested_file_keeps_its_subdirectory(self):
        entry = self._manifest(full_path="roms/psx/Game", file_path="roms/psx/Game/PS3_GAME/USRDIR", name="EBOOT.BIN")
        assert entry.rel_path == "PS3_GAME/USRDIR/EBOOT.BIN"

    def test_untidy_separators_and_slashes_are_normalized(self):
        entry = self._manifest(full_path="/roms/psx/Game/", file_path="roms//psx/Game/sub")
        assert entry.rel_path == "sub/a.bin"

    def test_backslash_separators_are_normalized(self):
        entry = self._manifest(full_path="roms/psx/Game", file_path="roms\\psx\\Game\\sub")
        assert entry.rel_path == "sub/a.bin"

    def test_a_missing_rom_full_path_falls_back_to_the_name(self):
        entry = self._manifest(file_path="roms/psx/Game/sub")
        assert entry.rel_path == ""
        assert entry.lookup_key == "a.bin"

    def test_a_missing_file_path_falls_back_to_the_name(self):
        entry = self._manifest(full_path="roms/psx/Game")
        assert entry.rel_path == ""
        assert entry.lookup_key == "a.bin"

    def test_a_non_nesting_file_path_falls_back_rather_than_guessing(self):
        # "roms/psx/Other" is not under "roms/psx/Game" — there is nothing to
        # subtract, and a guessed prefix would assert a location confidently and
        # wrongly.
        entry = self._manifest(full_path="roms/psx/Game", file_path="roms/psx/Other")
        assert entry.rel_path == ""

    def test_a_sibling_directory_sharing_a_name_prefix_does_not_nest(self):
        # "Game 2" starts with "Game" textually but is not inside it; the
        # separator is what makes the prefix a containment.
        entry = self._manifest(full_path="roms/psx/Game", file_path="roms/psx/Game 2")
        assert entry.rel_path == ""

    def test_a_non_string_full_path_is_ignored(self):
        entry = self._manifest(full_path=42, file_path="roms/psx/Game")
        assert entry.rel_path == ""


class TestSizesAgree:
    def test_equal_sizes_agree(self):
        assert sizes_agree(1024, 1024) is True

    def test_different_sizes_disagree(self):
        assert sizes_agree(1024, 2048) is False

    def test_absent_server_size_is_not_a_verdict(self):
        # None, never False: "the server said nothing" is not evidence of a
        # difference.
        assert sizes_agree(1024, 0) is None


class TestOccupiedTargetRefusal:
    def test_carries_the_canonical_failure_shape(self):
        payload = _refusal()
        assert payload["success"] is False
        assert payload["reason"] == "target_occupied"
        assert isinstance(payload["message"], str) and payload["message"]
        assert "error" not in payload
        assert "error_code" not in payload

    def test_carries_both_sides_and_the_size_verdict(self):
        payload = _refusal(size_bytes=2048, incoming_size=1024)
        assert payload["existing"] == {
            "name": "Game.sfc",
            "path": "/roms/snes/Game.sfc",
            "is_dir": False,
            "size_bytes": 2048,
            "modified_at": 1_700_000_000.0,
        }
        assert payload["incoming"] == {"name": "Game.sfc", "size_bytes": 1024}
        assert payload["sizes_match"] is False

    def test_names_the_kind_in_the_message(self):
        assert "folder" in _refusal(is_dir=True, path="/roms/psx/Game")["message"]
        assert "file" in _refusal(is_dir=False)["message"]

    def test_adoptable_is_carried_through(self):
        assert _refusal(adoptable=False)["adoptable"] is False


class TestCompareManifest:
    _ENTRY = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab")

    def test_matching_size_and_digest_yields_no_difference(self):
        assert compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=10, digest="ab")}) == ()

    def test_a_missing_file_is_reported_by_name(self):
        (difference,) = compare_manifest((self._ENTRY,), {})
        assert difference.name == "a.bin"
        assert difference.actual == "missing"

    def test_a_size_difference_names_both_numbers(self):
        (difference,) = compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=99, digest="")})
        assert "10 bytes" in difference.expected
        assert "99 bytes" in difference.actual

    def test_a_size_difference_suppresses_the_digest_difference(self):
        # One finding per file: the size already proves it, and reporting the
        # hash of bytes we know are the wrong length adds nothing.
        differences = compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=99, digest="zz")})
        assert len(differences) == 1
        assert "bytes" in differences[0].actual

    def test_a_digest_difference_names_the_algorithm(self):
        (difference,) = compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=10, digest="cd")})
        assert difference.expected == "md5 ab"
        assert difference.actual == "md5 cd"

    def test_extra_files_on_disk_are_not_a_difference(self):
        # The plugin's own directories carry a generated .m3u and a healed
        # PS3_DISC.SFB the server never listed.
        local = {"a.bin": LocalFile(size_bytes=10, digest="ab"), "Game.m3u": LocalFile(size_bytes=44, digest="ff")}
        assert compare_manifest((self._ENTRY,), local) == ()

    def test_an_unverifiable_entry_is_still_size_checked(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="", digest="")
        (difference,) = compare_manifest((entry,), {"a.bin": LocalFile(size_bytes=11, digest="")})
        assert "bytes" in difference.actual

    def test_a_zero_server_size_skips_the_size_check(self):
        entry = ServerFile(name="a.bin", size_bytes=0, algorithm="md5", digest="ab")
        assert compare_manifest((entry,), {"a.bin": LocalFile(size_bytes=10, digest="ab")}) == ()

    def test_an_uncomputed_local_digest_is_not_a_difference(self):
        assert compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=10, digest="")}) == ()

    def test_a_located_entry_is_looked_up_by_its_relative_path(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab", rel_path="sub/a.bin")
        # Keyed by the bare name it is NOT found; keyed by where it belongs it is.
        assert compare_manifest((entry,), {"a.bin": LocalFile(size_bytes=10, digest="ab")})[0].actual == "missing"
        assert compare_manifest((entry,), {"sub/a.bin": LocalFile(size_bytes=10, digest="ab")}) == ()

    def test_a_located_entry_is_reported_under_its_relative_path(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab", rel_path="sub/a.bin")
        (difference,) = compare_manifest((entry,), {"sub/a.bin": LocalFile(size_bytes=10, digest="cd")})
        assert difference.name == "sub/a.bin"


class TestVerificationStatus:
    _VERIFIABLE = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab")
    _BARE = ServerFile(name="a.bin", size_bytes=10, algorithm="", digest="")

    def test_no_digest_anywhere_is_unverifiable(self):
        assert verification_status((self._BARE,), ()) == "unverifiable"

    def test_an_empty_manifest_is_unverifiable(self):
        assert verification_status((), ()) == "unverifiable"

    def test_a_clean_comparison_matches(self):
        assert verification_status((self._VERIFIABLE,), ()) == "match"

    def test_any_difference_is_a_mismatch(self):
        differences = compare_manifest((self._VERIFIABLE,), {})
        assert verification_status((self._VERIFIABLE,), differences) == "mismatch"

    def test_a_partly_hashed_manifest_is_still_verifiable(self):
        # A server that hashed some files can still confirm those; only "no
        # digest for any file" is the unverifiable outcome.
        assert verification_status((self._VERIFIABLE, self._BARE), ()) == "match"
