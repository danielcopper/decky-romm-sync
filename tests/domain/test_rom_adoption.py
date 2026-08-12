"""Pure adoption judgements: the collision payload and the manifest comparison."""

from typing import ClassVar

from domain.rom_adoption import (
    DigestRequest,
    LocalFile,
    LocalMember,
    ServerFile,
    ServerMember,
    compare_manifest,
    digests_to_read,
    is_archive_name,
    occupied_target_refusal,
    server_manifest,
    sizes_agree,
    unpacked_member,
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
        # The CRC is carried alongside rather than dropped: inside an archive a
        # ZIP's central directory states the same number for free.
        assert manifest == (ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab", crc32="cd"),)

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
        assert isinstance(payload["message"], str)
        assert payload["message"]
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
        assert difference.detail == "missing"

    def test_a_size_difference_names_both_numbers(self):
        # Sizes stay in the message: unlike a digest, they are numbers a person
        # can act on.
        (difference,) = compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=99, digest="")})
        assert "10 bytes" in difference.detail
        assert "99" in difference.detail

    def test_a_size_difference_suppresses_the_digest_difference(self):
        # One finding per file: the size already proves it, and reporting the
        # hash of bytes we know are the wrong length adds nothing.
        differences = compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=99, digest="zz")})
        assert len(differences) == 1
        assert "bytes" in differences[0].detail

    def test_a_digest_difference_says_the_contents_differ_without_printing_them(self):
        # Two 32-character hex strings say no more than "these differ" and wrap
        # the line into a block nobody reads.
        (difference,) = compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=10, digest="cd")})
        assert difference.detail == "contents differ from the server's copy"

    def test_extra_files_on_disk_are_not_a_difference(self):
        # The plugin's own directories carry a generated .m3u and a healed
        # PS3_DISC.SFB the server never listed.
        local = {"a.bin": LocalFile(size_bytes=10, digest="ab"), "Game.m3u": LocalFile(size_bytes=44, digest="ff")}
        assert compare_manifest((self._ENTRY,), local) == ()

    def test_an_unverifiable_entry_is_still_size_checked(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="", digest="")
        (difference,) = compare_manifest((entry,), {"a.bin": LocalFile(size_bytes=11, digest="")})
        assert "bytes" in difference.detail

    def test_a_zero_server_size_skips_the_size_check(self):
        entry = ServerFile(name="a.bin", size_bytes=0, algorithm="md5", digest="ab")
        assert compare_manifest((entry,), {"a.bin": LocalFile(size_bytes=10, digest="ab")}) == ()

    def test_an_uncomputed_local_digest_is_not_a_difference(self):
        assert compare_manifest((self._ENTRY,), {"a.bin": LocalFile(size_bytes=10, digest="")}) == ()

    def test_a_located_entry_is_looked_up_by_its_relative_path(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab", rel_path="sub/a.bin")
        # Keyed by the bare name it is NOT found; keyed by where it belongs it is.
        assert compare_manifest((entry,), {"a.bin": LocalFile(size_bytes=10, digest="ab")})[0].detail == "missing"
        assert compare_manifest((entry,), {"sub/a.bin": LocalFile(size_bytes=10, digest="ab")}) == ()

    def test_a_located_entry_is_reported_under_its_relative_path(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab", rel_path="sub/a.bin")
        (difference,) = compare_manifest((entry,), {"sub/a.bin": LocalFile(size_bytes=10, digest="cd")})
        assert difference.name == "sub/a.bin"


def _archived(*members: ServerMember, name: str = "Game.zip", size: int = 4096) -> ServerFile:
    """An archive entry as RomM states it: a composite digest over its members."""
    return ServerFile(name=name, size_bytes=size, algorithm="md5", digest="composite", members=members)


_MEMBER = ServerMember(name="Game.gba", size_bytes=64, algorithm="md5", digest="ab", crc32="0000beef")

# What a real server sends for a zipped ROM: no ``archive_members`` at all, a
# size that is the container's, and a digest that is the content's. The values
# are the measured ones from a RomM 5.1.0 instance — its md5 and crc are the
# member's, not the archive's.
_WHOLE_ARCHIVE = ServerFile(
    name="Game.zip",
    size_bytes=12058408,
    algorithm="md5",
    digest="2e8814e664675572a43b01900bbbb16b",
    crc32="d56c2e54",
)


class TestServerManifestArchiveMembers:
    """``archive_members`` is what a zipped ROM's content can be held to."""

    def _detail(self, members):
        return {
            "files": [{"file_name": "Game.zip", "file_size_bytes": 4096, "md5_hash": "cd", "archive_members": members}]
        }

    def test_members_carry_their_name_uncompressed_size_and_digests(self):
        (entry,) = server_manifest(
            self._detail([{"name": "Game.gba", "size": 64, "crc_hash": "0000BEEF", "md5_hash": "AB"}])
        )
        assert entry.members == (
            ServerMember(name="Game.gba", size_bytes=64, algorithm="md5", digest="ab", crc32="0000beef"),
        )

    def test_an_archived_entry_is_verifiable_through_its_members(self):
        (entry,) = server_manifest(self._detail([{"name": "Game.gba", "size": 64, "md5_hash": "ab"}]))
        assert entry.archived is True
        assert entry.verifiable is True

    def test_an_entry_without_members_is_not_archived(self):
        (entry,) = server_manifest({"files": [{"file_name": "Game.sfc", "md5_hash": "ab"}]})
        assert entry.archived is False
        assert entry.members == ()

    def test_a_null_members_list_is_not_archived(self):
        # RomM sends null for every file it did not read as an archive, and for
        # an archive it could not read — where the file digest IS the container.
        (entry,) = server_manifest({"files": [{"file_name": "Game.zip", "md5_hash": "ab", "archive_members": None}]})
        assert entry.archived is False

    def test_members_without_a_name_are_dropped(self):
        (entry,) = server_manifest(self._detail([{"name": "", "size": 64, "md5_hash": "ab"}]))
        assert entry.members == ()

    def test_members_without_a_digest_leave_the_entry_unverifiable(self):
        (entry,) = server_manifest(self._detail([{"name": "Game.gba", "size": 64}]))
        assert entry.archived is True
        assert entry.verifiable is False


class TestCompareArchiveMembers:
    """A zipped ROM is held to what is inside it, never to the container."""

    def test_agreeing_members_yield_no_difference(self):
        local = LocalFile(size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000beef", "ab"),))
        assert compare_manifest((_archived(_MEMBER),), {"Game.zip": local}) == ()

    def test_a_container_of_a_different_size_is_not_a_difference(self):
        # Repacking the same content changes the archive's size and nothing else.
        local = LocalFile(size_bytes=9999, digest="", members=(LocalMember("Game.gba", 64, "0000beef", "ab"),))
        assert compare_manifest((_archived(_MEMBER),), {"Game.zip": local}) == ()

    def test_the_containers_own_digest_is_never_compared(self):
        # The file-level digest is a composite over the members; a whole-file
        # digest of the archive answers to nothing the server published.
        local = LocalFile(
            size_bytes=4096, digest="whole-archive", members=(LocalMember("Game.gba", 64, "0000beef", "ab"),)
        )
        assert compare_manifest((_archived(_MEMBER),), {"Game.zip": local}) == ()

    def test_a_missing_member_is_reported_inside_its_archive(self):
        local = LocalFile(size_bytes=4096, digest="", members=())
        (difference,) = compare_manifest((_archived(_MEMBER),), {"Game.zip": local})
        assert difference.name == "Game.zip/Game.gba"
        assert difference.detail == "missing from the archive"

    def test_a_member_size_difference_names_both_numbers(self):
        local = LocalFile(size_bytes=4096, digest="", members=(LocalMember("Game.gba", 99, "0000beef", ""),))
        (difference,) = compare_manifest((_archived(_MEMBER),), {"Game.zip": local})
        assert "64 bytes" in difference.detail
        assert "99" in difference.detail

    def test_a_crc_difference_is_reported_from_the_central_directory_alone(self):
        local = LocalFile(size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000dead", ""),))
        (difference,) = compare_manifest((_archived(_MEMBER),), {"Game.zip": local})
        assert difference.name == "Game.zip/Game.gba"
        assert difference.detail == "contents differ from the server's copy"

    def test_a_digest_difference_is_reported_when_the_crc_agreed(self):
        local = LocalFile(size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000beef", "cd"),))
        (difference,) = compare_manifest((_archived(_MEMBER),), {"Game.zip": local})
        assert difference.name == "Game.zip/Game.gba"
        assert difference.detail == "contents differ from the server's copy"

    def test_members_the_server_did_not_list_are_not_a_difference(self):
        # RomM drops excluded names and extensions from ``archive_members``, so
        # its own archive holds more than it listed.
        local = LocalFile(
            size_bytes=4096,
            digest="",
            members=(LocalMember("Game.gba", 64, "0000beef", "ab"), LocalMember("readme.txt", 9, "0000abcd", "")),
        )
        assert compare_manifest((_archived(_MEMBER),), {"Game.zip": local}) == ()

    def test_a_file_that_could_not_be_opened_is_held_to_the_single_member(self):
        # The user unpacked what the server keeps packed: the sizes agree, so the
        # loose bytes are that member's.
        assert compare_manifest((_archived(_MEMBER),), {"Game.zip": LocalFile(size_bytes=64, digest="ab")}) == ()

    def test_an_unpacked_file_that_differs_names_the_member(self):
        (difference,) = compare_manifest((_archived(_MEMBER),), {"Game.zip": LocalFile(size_bytes=64, digest="cd")})
        assert difference.name == "Game.zip/Game.gba"
        assert difference.detail == "contents differ from the server's copy"

    def test_a_container_of_another_format_is_not_accused_of_differing(self):
        # A .7z of the same ROM is smaller than the member it holds; hashing its
        # bytes against that member's digest would report content that is right.
        assert compare_manifest((_archived(_MEMBER),), {"Game.zip": LocalFile(size_bytes=40, digest="7z")}) == ()

    def test_a_composite_over_several_members_is_never_compared_with_one_file(self):
        entry = _archived(_MEMBER, ServerMember(name="disc2.bin", size_bytes=64, algorithm="md5", digest="cd"))
        assert compare_manifest((entry,), {"Game.zip": LocalFile(size_bytes=64, digest="ab")}) == ()


class TestCompareArchiveWithoutStatedMembers:
    """The file-level digest is the carrier; ``archive_members`` is an extra.

    A server that has not rescanned since RomM 4.9.0 states no members at all,
    and its digest for an archived ROM still describes the content inside — the
    composite over one member reduces to that member, and the older whole-file
    hasher took the largest member, which with one member is the same bytes.
    """

    def _opened(self, *members: LocalMember) -> LocalFile:
        return LocalFile(size_bytes=12058408, digest="", members=members, is_archive=True)

    def test_a_sole_member_that_agrees_yields_no_difference(self):
        local = self._opened(LocalMember("Game.gba", 16777216, "d56c2e54", "2e8814e664675572a43b01900bbbb16b"))
        assert compare_manifest((_WHOLE_ARCHIVE,), {"Game.zip": local}) == ()

    def test_a_sole_member_that_differs_is_reported_under_the_archive(self):
        local = self._opened(LocalMember("Game.gba", 16777216, "d56c2e54", "0" * 32))
        (difference,) = compare_manifest((_WHOLE_ARCHIVE,), {"Game.zip": local})
        assert difference.name == "Game.zip"
        assert difference.detail == "contents differ from the server's copy"

    def test_a_sole_member_is_disqualified_by_its_crc_alone(self):
        local = self._opened(LocalMember("Game.gba", 16777216, "0000dead", ""))
        (difference,) = compare_manifest((_WHOLE_ARCHIVE,), {"Game.zip": local})
        assert difference.detail == "contents differ from the server's copy"

    def test_the_containers_own_size_is_not_compared(self):
        local = LocalFile(
            size_bytes=999,
            digest="",
            members=(LocalMember("Game.gba", 16777216, "d56c2e54", "2e8814e664675572a43b01900bbbb16b"),),
            is_archive=True,
        )
        assert compare_manifest((_WHOLE_ARCHIVE,), {"Game.zip": local}) == ()

    def test_several_members_are_not_compared_at_all(self):
        # The number could be the composite over every member or the largest
        # member alone, and the payload does not say which — so a difference
        # would be a guess and a match would cover one member out of many.
        local = self._opened(LocalMember("disc1.bin", 64, "0000beef", ""), LocalMember("disc2.bin", 64, "0000f00d", ""))
        assert compare_manifest((_WHOLE_ARCHIVE,), {"Game.zip": local}) == ()

    def test_a_container_that_could_not_be_opened_is_not_compared(self):
        local = LocalFile(size_bytes=12058408, digest="", is_archive=True)
        assert compare_manifest((_WHOLE_ARCHIVE,), {"Game.zip": local}) == ()


class TestIsArchiveName:
    """Which names RomM would have hashed by their contents rather than bytes."""

    def test_recognises_every_format_romm_reads(self):
        for name in ("Game.zip", "Game.7z", "Game.rar", "Game.tar", "Game.tar.gz", "Game.tgz", "Game.gz", "Game.bz2"):
            assert is_archive_name(name) is True, name

    def test_is_case_insensitive(self):
        assert is_archive_name("GAME.ZIP") is True

    def test_a_plain_rom_is_not_an_archive(self):
        for name in ("Game.gba", "Game.iso", "Game.chd", "Game.zipper"):
            assert is_archive_name(name) is False, name


class TestUnpackedMember:
    def test_a_single_member_of_the_same_size_is_the_one_on_disk(self):
        assert unpacked_member(_archived(_MEMBER), 64) == _MEMBER

    def test_a_different_size_is_not_that_member(self):
        assert unpacked_member(_archived(_MEMBER), 63) is None

    def test_several_members_have_no_single_counterpart(self):
        entry = _archived(_MEMBER, ServerMember(name="disc2.bin", size_bytes=64, algorithm="md5", digest="cd"))
        assert unpacked_member(entry, 64) is None

    def test_a_member_without_a_stated_size_cannot_be_recognised(self):
        entry = _archived(ServerMember(name="Game.gba", size_bytes=0, algorithm="md5", digest="ab"))
        assert unpacked_member(entry, 0) is None


class TestDigestsToRead:
    """Cheap evidence decides what is worth decompressing; it never confirms."""

    _PLAIN = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab")

    def test_a_plain_file_is_read_whole(self):
        found = LocalFile(size_bytes=10, digest="")
        assert digests_to_read(self._PLAIN, found) == (DigestRequest(member="", algorithm="md5", size_bytes=10),)

    def test_a_plain_file_whose_size_disagrees_is_not_read(self):
        assert digests_to_read(self._PLAIN, LocalFile(size_bytes=99, digest="")) == ()

    def test_a_plain_file_without_a_digest_is_not_read(self):
        entry = ServerFile(name="a.bin", size_bytes=10, algorithm="", digest="")
        assert digests_to_read(entry, LocalFile(size_bytes=10, digest="")) == ()

    def test_an_agreeing_member_is_read_for_its_digest(self):
        found = LocalFile(
            size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000beef"),), is_archive=True
        )
        assert digests_to_read(_archived(_MEMBER), found) == (
            DigestRequest(member="Game.gba", algorithm="md5", size_bytes=64),
        )

    def test_a_member_whose_crc_already_disagrees_is_not_decompressed(self):
        found = LocalFile(
            size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000dead"),), is_archive=True
        )
        assert digests_to_read(_archived(_MEMBER), found) == ()

    def test_a_member_whose_size_already_disagrees_is_not_decompressed(self):
        found = LocalFile(
            size_bytes=4096, digest="", members=(LocalMember("Game.gba", 99, "0000beef"),), is_archive=True
        )
        assert digests_to_read(_archived(_MEMBER), found) == ()

    def test_a_member_the_archive_does_not_hold_is_not_read(self):
        assert (
            digests_to_read(_archived(_MEMBER), LocalFile(size_bytes=4096, digest="", members=(), is_archive=True))
            == ()
        )

    def test_a_member_the_server_stated_no_digest_for_is_not_read(self):
        entry = _archived(ServerMember(name="Game.gba", size_bytes=64, algorithm="", digest="", crc32="0000beef"))
        found = LocalFile(
            size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000beef"),), is_archive=True
        )
        assert digests_to_read(entry, found) == ()

    def test_an_unopenable_container_reads_the_file_only_for_a_single_member(self):
        found = LocalFile(size_bytes=64, digest="", is_archive=True)
        assert digests_to_read(_archived(_MEMBER), found) == (DigestRequest(member="", algorithm="md5", size_bytes=64),)

    def test_an_unopenable_container_of_the_wrong_size_is_not_read_at_all(self):
        assert digests_to_read(_archived(_MEMBER), LocalFile(size_bytes=40, digest="", is_archive=True)) == ()

    def test_an_unopenable_container_the_server_only_described_as_a_whole_is_not_read(self):
        # Its digest speaks for content this plugin cannot produce; hashing the
        # container would compare the wrong bytes and call it a difference.
        assert digests_to_read(_WHOLE_ARCHIVE, LocalFile(size_bytes=4096, digest="", is_archive=True)) == ()

    def test_a_sole_member_is_read_against_the_file_level_digest(self):
        found = LocalFile(
            size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "d56c2e54"),), is_archive=True
        )
        assert digests_to_read(_WHOLE_ARCHIVE, found) == (
            DigestRequest(member="Game.gba", algorithm="md5", size_bytes=64),
        )

    def test_a_sole_member_whose_crc_already_disagrees_is_not_decompressed(self):
        found = LocalFile(
            size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000dead"),), is_archive=True
        )
        assert digests_to_read(_WHOLE_ARCHIVE, found) == ()

    def test_several_members_the_server_only_described_as_a_whole_are_not_read(self):
        found = LocalFile(
            size_bytes=4096,
            digest="",
            members=(LocalMember("disc1.bin", 64, "0000beef"), LocalMember("disc2.bin", 64, "0000f00d")),
            is_archive=True,
        )
        assert digests_to_read(_WHOLE_ARCHIVE, found) == ()


class TestVerificationStatus:
    """``match`` is the strong claim, so it has to be earned by an actual read.

    A file that was never hashed produces no difference, exactly like a file that
    matched. Reading that silence as agreement is how a false ``match`` would
    authorise an adoption whose row carries deletion authority (ADR-0028).
    """

    _VERIFIABLE = ServerFile(name="a.bin", size_bytes=10, algorithm="md5", digest="ab")
    _BARE = ServerFile(name="a.bin", size_bytes=10, algorithm="", digest="")
    _NO_SIZE = ServerFile(name="b.bin", size_bytes=0, algorithm="md5", digest="cd")
    _CHECKED: ClassVar[dict[str, LocalFile]] = {"a.bin": LocalFile(size_bytes=10, digest="ab")}

    def test_no_digest_anywhere_is_unverifiable(self):
        assert verification_status((self._BARE,), {"a.bin": LocalFile(size_bytes=10, digest="")}, ()) == "unverifiable"

    def test_an_empty_manifest_is_unverifiable(self):
        assert verification_status((), {}, ()) == "unverifiable"

    def test_a_clean_comparison_matches(self):
        assert verification_status((self._VERIFIABLE,), self._CHECKED, ()) == "match"

    def test_any_difference_is_a_mismatch(self):
        differences = compare_manifest((self._VERIFIABLE,), {})
        assert verification_status((self._VERIFIABLE,), {}, differences) == "mismatch"

    def test_a_partly_hashed_manifest_is_still_verifiable(self):
        # A server that hashed some files can still confirm those. The bare entry
        # is exempt — there is nothing about it to confirm.
        local = {**self._CHECKED, "b.bin": LocalFile(size_bytes=10, digest="")}
        assert verification_status((self._VERIFIABLE, self._BARE), local, ()) == "match"

    def test_a_digest_that_was_never_read_is_not_a_match(self):
        # The finding: an entry the server put a digest on, whose local
        # counterpart was never hashed, produces no difference — and must not
        # therefore pass as confirmed.
        local = {"a.bin": LocalFile(size_bytes=10, digest="")}
        assert verification_status((self._VERIFIABLE,), local, ()) == "unverifiable"

    def test_one_unread_digest_downgrades_a_whole_clean_run(self):
        local = {**self._CHECKED, "b.bin": LocalFile(size_bytes=99, digest="")}
        assert verification_status((self._VERIFIABLE, self._NO_SIZE), local, ()) == "unverifiable"

    def test_a_verifiable_entry_absent_from_the_observations_is_not_a_match(self):
        assert verification_status((self._VERIFIABLE,), {}, ()) == "unverifiable"

    def test_an_archive_whose_members_were_all_read_matches(self):
        local = {
            "Game.zip": LocalFile(size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000beef", "ab"),))
        }
        assert verification_status((_archived(_MEMBER),), local, ()) == "match"

    def test_a_member_that_was_never_read_is_not_a_match(self):
        # The container could be opened and nothing disagreed, but the bytes the
        # digest speaks for were never read — a silence, not an agreement.
        local = {"Game.zip": LocalFile(size_bytes=4096, digest="", members=(LocalMember("Game.gba", 64, "0000beef"),))}
        assert verification_status((_archived(_MEMBER),), local, ()) == "unverifiable"

    def test_a_container_that_could_not_be_opened_is_not_a_match(self):
        local = {"Game.zip": LocalFile(size_bytes=4096, digest="")}
        assert verification_status((_archived(_MEMBER),), local, ()) == "unverifiable"

    def test_an_unpacked_member_that_was_read_matches(self):
        assert (
            verification_status((_archived(_MEMBER),), {"Game.zip": LocalFile(size_bytes=64, digest="ab")}, ())
            == "match"
        )

    def test_a_second_member_left_unread_downgrades_the_whole_archive(self):
        entry = _archived(_MEMBER, ServerMember(name="disc2.bin", size_bytes=64, algorithm="md5", digest="cd"))
        local = {
            "Game.zip": LocalFile(
                size_bytes=4096,
                digest="",
                members=(LocalMember("Game.gba", 64, "0000beef", "ab"), LocalMember("disc2.bin", 64, "0000f00d")),
            )
        }
        assert verification_status((entry,), local, ()) == "unverifiable"

    def test_a_sole_member_that_was_read_matches(self):
        local = {
            "Game.zip": LocalFile(
                size_bytes=12058408,
                digest="",
                members=(LocalMember("Game.gba", 16777216, "d56c2e54", "2e8814e664675572a43b01900bbbb16b"),),
                is_archive=True,
            )
        }
        assert verification_status((_WHOLE_ARCHIVE,), local, ()) == "match"

    def test_a_sole_member_that_was_never_read_is_not_a_match(self):
        local = {
            "Game.zip": LocalFile(
                size_bytes=12058408,
                digest="",
                members=(LocalMember("Game.gba", 16777216, "d56c2e54"),),
                is_archive=True,
            )
        }
        assert verification_status((_WHOLE_ARCHIVE,), local, ()) == "unverifiable"

    def test_several_members_the_server_only_described_as_a_whole_cannot_match(self):
        local = {
            "Game.zip": LocalFile(
                size_bytes=12058408,
                digest="",
                members=(
                    LocalMember("disc1.bin", 64, "0000beef", "ab"),
                    LocalMember("disc2.bin", 64, "0000f00d", "cd"),
                ),
                is_archive=True,
            )
        }
        assert verification_status((_WHOLE_ARCHIVE,), local, ()) == "unverifiable"

    def test_an_archive_that_could_not_be_opened_cannot_match(self):
        local = {"Game.zip": LocalFile(size_bytes=12058408, digest="anything", is_archive=True)}
        assert verification_status((_WHOLE_ARCHIVE,), local, ()) == "unverifiable"

    def test_a_member_the_server_put_no_digest_on_is_exempt(self):
        entry = _archived(_MEMBER, ServerMember(name="notes.txt", size_bytes=9, algorithm="", digest=""))
        local = {
            "Game.zip": LocalFile(
                size_bytes=4096,
                digest="",
                members=(LocalMember("Game.gba", 64, "0000beef", "ab"), LocalMember("notes.txt", 9, "0000abcd")),
            )
        }
        assert verification_status((entry,), local, ()) == "match"
