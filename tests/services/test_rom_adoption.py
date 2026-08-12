"""RomAdoptionService — the download gate, adoption, and the content check.

Every case here is about content the plugin did not put on disk, so the
destructive assertions are the load-bearing ones: what survives a refusal, what
a replace is allowed to remove, and that a failed adoption never deletes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import zipfile
import zlib
from types import SimpleNamespace
from typing import Any

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_download_file_store import FakeDownloadFileStore
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_romm_api import FakeRommApi
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.rom import Rom
from domain.rom_install import RomInstall
from services.rom_adoption import RomAdoptionService, RomAdoptionServiceConfig
from services.rom_install_recorder import RomInstallRecorder, RomInstallRecorderConfig

_ROMS = "/roms"
_ROM_ID = 42


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _single_file_detail(
    *, name: str = "Game.sfc", size: int = 10, files: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "id": _ROM_ID,
        "name": "Game",
        "platform_slug": "snes",
        "fs_name": name,
        "fs_size_bytes": size,
    }
    if files is not None:
        detail["files"] = files
    return detail


def _multi_file_detail(
    *,
    dir_name: str = "Game",
    size: int = 20,
    files: list[dict[str, Any]] | None = None,
    full_path: str | None = None,
) -> dict[str, Any]:
    """A directory ROM. *full_path* is RomM's own path for the ROM directory.

    Passing it turns on exact relative-path matching for the manifest entries
    that also carry ``file_path``; leaving it out exercises the by-name fallback.
    """
    detail: dict[str, Any] = {
        "id": _ROM_ID,
        "name": "Game",
        "platform_slug": "psx",
        "fs_name": f"{dir_name}.zip",
        "fs_name_no_ext": dir_name,
        "fs_size_bytes": size,
        "has_multiple_files": True,
        "files": files if files is not None else [{"file_name": "a.bin"}, {"file_name": "b.bin"}],
    }
    if full_path is not None:
        detail["full_path"] = full_path
    return detail


def _zip_bytes(members: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """A real ZIP holding *members*, so the central directory is genuine."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _archived_detail(
    *, archive: bytes, members: dict[str, bytes], name: str = "Game.zip", state_members: bool = False
) -> dict[str, Any]:
    """A single-file ROM RomM serves as an archive, shaped as its scanner writes it.

    ``file_size_bytes`` is the container on disk while the file-level digest is
    the composite RomM accumulates over every member's decompressed bytes in
    ASCII name order — the two describe different things, which is exactly why a
    whole-file comparison reports a mismatch on a byte-perfect copy.

    ``archive_members`` is **off by default**, because that is what a real server
    sends: the column arrived in RomM 4.9.0 and stays null on every library not
    rescanned since. *state_members* turns on the richer shape a rescanned
    library carries.
    """
    composite = hashlib.md5(usedforsecurity=False)
    composite_crc = 0
    for member_name in sorted(members):
        composite.update(members[member_name])
        composite_crc = zlib.crc32(members[member_name], composite_crc)
    file_entry: dict[str, Any] = {
        "file_name": name,
        "file_size_bytes": len(archive),
        "md5_hash": composite.hexdigest(),
        "crc_hash": f"{composite_crc & 0xFFFFFFFF:08x}",
    }
    if state_members:
        file_entry["archive_members"] = [
            {
                "name": member_name,
                "size": len(data),
                "crc_hash": f"{zlib.crc32(data) & 0xFFFFFFFF:08x}",
                "md5_hash": _md5(data),
                "sha1_hash": hashlib.sha1(data, usedforsecurity=False).hexdigest(),
            }
            for member_name, data in sorted(members.items())
        ]
    return _single_file_detail(name=name, size=len(archive), files=[file_entry])


_ROM_FULL_PATH = "roms/psx/Game"


def _located(name: str, *, size: int, digest: str, in_dir: str = "") -> dict[str, Any]:
    """A manifest entry RomM located: ``file_path`` is the ROM root plus *in_dir*."""
    file_path = f"{_ROM_FULL_PATH}/{in_dir}" if in_dir else _ROM_FULL_PATH
    return {
        "file_name": name,
        "file_path": file_path,
        "file_size_bytes": size,
        "md5_hash": digest,
    }


class Harness:
    """The service under test plus the state its assertions read back."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.store = FakeDownloadFileStore()
        self.romm_api = FakeRommApi()
        self.uow = FakeUnitOfWork()
        self.events: list[tuple[str, object]] = []
        self.clock = FakeClock()
        self.m3u_supported = True
        self.system_extensions: dict[str, frozenset[str]] = {}
        self.recorder = RomInstallRecorder(
            config=RomInstallRecorderConfig(
                logger=logging.getLogger("test_rom_adoption"),
                clock=self.clock,
                uow_factory=FakeUnitOfWorkFactory(self.uow),
                system_extensions=lambda system_name: self.system_extensions.get(system_name, frozenset()),
                active_core=FakeActiveCoreResolver(default=(None, None)),
                disc_resolver=FakeDiscResolver(),
            ),
        )
        self.paths = FakeRetroDeckPaths(roms=_ROMS)
        # Records every rom_id the supersede was asked about, and answers with
        # whatever a test has staged. Default: nothing to supersede.
        self.superseded: list[int] = []
        self.supersede_result: dict[str, Any] | None = None
        self.service = RomAdoptionService(
            config=RomAdoptionServiceConfig(
                romm_api=self.romm_api,
                download_file_store=self.store,
                resolve_system=lambda platform_slug, platform_fs_slug=None: platform_fs_slug or platform_slug,
                retrodeck_paths=self.paths,
                install_recorder=self.recorder,
                m3u_support=lambda system_name: self.m3u_supported,
                sibling_supersede=lambda: self._supersede,
                uow_factory=FakeUnitOfWorkFactory(self.uow),
                loop=loop,
                logger=logging.getLogger("test_rom_adoption"),
                emit=self._emit,
                clock=self.clock,
            ),
        )

    async def _emit(self, event: str, /, *args: object) -> None:
        self.events.append((event, args[0] if args else None))

    async def _supersede(self, rom_id: int) -> dict[str, Any] | None:
        self.superseded.append(rom_id)
        return self.supersede_result

    def seed_rom(self, *, app_id: int | None = 1042) -> None:
        with self.uow:
            self.uow.roms.save(
                Rom.synced(
                    rom_id=_ROM_ID,
                    platform_slug="snes",
                    name="Game",
                    fs_name="Game.sfc",
                    shortcut_app_id=app_id,
                    synced_at="2026-01-01T00:00:00+00:00",
                )
            )

    def seed_install(self, *, rom_id: int = _ROM_ID, file_path: str, rom_dir: str | None = None) -> None:
        """Record an install for *rom_id* (seeding its FK parent when it is a sibling)."""
        with self.uow:
            if rom_id != _ROM_ID:
                self.uow.roms.save(
                    Rom.synced(
                        rom_id=rom_id,
                        platform_slug="snes",
                        name="Other",
                        fs_name="Other.sfc",
                        shortcut_app_id=None,
                        synced_at="2026-01-01T00:00:00+00:00",
                    )
                )
            self.uow.rom_installs.save(
                RomInstall.mark_installed(
                    rom_id=rom_id,
                    file_path=file_path,
                    rom_dir=rom_dir,
                    platform_slug="snes",
                    system="snes",
                    installed_at="2026-01-01T00:00:00+00:00",
                )
            )

    def stage_detail(self, detail: dict[str, Any]) -> None:
        self.romm_api.roms[_ROM_ID] = detail


@pytest.fixture
async def h():
    return Harness(asyncio.get_event_loop())


# ── the download gate ────────────────────────────────────────────────────


class TestCheckDownloadTarget:
    async def test_a_free_path_lets_the_download_proceed(self, h):
        assert (
            await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False) is None
        )

    async def test_an_occupied_path_refuses_with_both_sides(self, h):
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 25
        h.store.mtimes["/roms/snes/Game.sfc"] = 1_700_000_000.0

        result = await h.service.check_download_target(
            _single_file_detail(size=10), "/roms/snes/Game.sfc", replace=False
        )

        assert result is not None
        assert result["success"] is False
        assert result["reason"] == "target_occupied"
        assert result["existing"]["size_bytes"] == 25
        assert result["existing"]["is_dir"] is False
        assert result["existing"]["modified_at"] == 1_700_000_000.0
        assert result["incoming"] == {"name": "Game.sfc", "size_bytes": 10}
        assert result["sizes_match"] is False

    async def test_a_refusal_leaves_the_content_untouched(self, h):
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False)

        assert h.store.files["/roms/snes/Game.sfc"] == b"mine"

    async def test_a_directory_in_a_single_file_ROM_s_way_is_not_adoptable(self, h):
        h.store.files["/roms/snes/Game.sfc/inner.bin"] = b"x"

        result = await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False)

        assert result is not None
        assert result["existing"]["is_dir"] is True
        assert result["adoptable"] is False

    async def test_a_directory_in_a_multi_file_ROM_s_way_is_adoptable(self, h):
        h.store.files["/roms/psx/Game/a.bin"] = b"x"

        result = await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=False)

        assert result is not None
        assert result["adoptable"] is True

    async def test_a_file_in_a_multi_file_ROM_s_way_is_not_adoptable(self, h):
        h.store.files["/roms/psx/Game"] = b"x"

        result = await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=False)

        assert result is not None
        assert result["adoptable"] is False

    async def test_a_rom_s_own_recorded_install_is_not_asked_about(self, h):
        # A re-download finds its own files in the way. The install record is the
        # plugin's claim on them, so it replaces them as it always has.
        h.seed_rom()
        h.seed_install(file_path="/roms/snes/Game.sfc")
        h.store.files["/roms/snes/Game.sfc"] = b"ours"

        assert (
            await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False) is None
        )

    async def test_a_multi_file_rom_s_own_directory_is_not_asked_about(self, h):
        h.seed_rom()
        h.seed_install(file_path="/roms/psx/Game/Game.cue", rom_dir="/roms/psx/Game")
        h.store.files["/roms/psx/Game/Game.cue"] = b"ours"

        assert await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=False) is None

    async def test_another_rom_s_install_at_this_path_is_still_asked_about(self, h):
        # The record has to belong to THIS rom. A row for a different rom_id is
        # not this download's claim on the bytes.
        h.seed_rom()
        h.seed_install(rom_id=_ROM_ID + 1, file_path="/roms/snes/Game.sfc")
        h.store.files["/roms/snes/Game.sfc"] = b"someone else's"

        result = await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False)

        assert result is not None
        assert result["reason"] == "target_occupied"


class TestReplace:
    async def test_replace_removes_the_existing_directory_whole(self, h):
        # The merge is the bug: extraction into an existing dir leaves a hybrid
        # that a later uninstall deletes whole, taking the user's other files.
        h.store.files["/roms/psx/Game/a.bin"] = b"x"
        h.store.files["/roms/psx/Game/unrelated.txt"] = b"y"

        assert await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=True) is None

        assert "/roms/psx/Game/a.bin" not in h.store.files
        assert "/roms/psx/Game/unrelated.txt" not in h.store.files

    async def test_replace_leaves_a_single_file_for_the_atomic_rename(self, h):
        # os.replace swaps the new bytes in; deleting first would leave the user
        # with neither copy if the transfer then failed.
        h.store.files["/roms/snes/Game.sfc"] = b"old"

        assert await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=True) is None

        assert h.store.files["/roms/snes/Game.sfc"] == b"old"

    async def test_replace_removes_a_file_blocking_a_multi_file_directory(self, h):
        h.store.files["/roms/psx/Game"] = b"blocker"

        assert await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=True) is None

        assert "/roms/psx/Game" not in h.store.files

    async def test_replace_refuses_outside_the_roms_tree(self, h):
        h.store.files["/elsewhere/Game.sfc"] = b"precious"

        result = await h.service.check_download_target(_single_file_detail(), "/elsewhere/Game.sfc", replace=True)

        assert result is not None
        assert result["reason"] == "unsafe_replace_target"
        assert h.store.files["/elsewhere/Game.sfc"] == b"precious"

    async def test_replace_refuses_a_bare_platform_directory(self, h):
        # is_safe_rom_path demands two segments below the base, so the shared
        # platform folder can never be the thing a replace removes.
        h.store.files["/roms/psx/Game/a.bin"] = b"x"

        result = await h.service.check_download_target(_multi_file_detail(), "/roms/psx", replace=True)

        assert result is not None
        assert result["reason"] == "unsafe_replace_target"
        assert h.store.files["/roms/psx/Game/a.bin"] == b"x"

    async def test_replace_refuses_when_the_roms_path_is_unknown(self, h):
        h.paths.roms = ""
        h.store.files["/roms/psx/Game/a.bin"] = b"x"

        result = await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=True)

        assert result is not None
        assert result["reason"] == "unsafe_replace_target"

    async def test_a_failed_removal_aborts_the_download(self, h):
        h.store.files["/roms/psx/Game/a.bin"] = b"x"
        h.store.remove_tree_failures.add("/roms/psx/Game")

        result = await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=True)

        assert result is not None
        assert result["reason"] == "replace_failed"
        assert result["success"] is False

    async def test_replace_on_a_free_path_is_a_no_op(self, h):
        assert await h.service.check_download_target(_multi_file_detail(), "/roms/psx/Game", replace=True) is None


# ── adopt ────────────────────────────────────────────────────────────────


class TestAdopt:
    async def test_single_file_records_the_file_with_no_rom_dir(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is True
        assert result["file_path"] == "/roms/snes/Game.sfc"
        assert result["rom_dir"] is None
        install = h.uow.rom_installs.get(_ROM_ID)
        assert install is not None
        assert install.file_path == "/roms/snes/Game.sfc"
        assert install.rom_dir is None
        assert install.system == "snes"

    async def test_multi_file_records_the_directory_and_detects_the_launch_file(self, h):
        h.seed_rom()
        h.stage_detail(_multi_file_detail())
        h.store.files["/roms/psx/Game/Game.cue"] = b"c" * 4
        h.store.files["/roms/psx/Game/Game.bin"] = b"b" * 400

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is True
        assert result["rom_dir"] == "/roms/psx/Game"
        # The .cue wins over the larger .bin — the download path's own rule.
        assert result["file_path"] == "/roms/psx/Game/Game.cue"

    async def test_adoption_generates_no_m3u_and_renames_nothing(self, h):
        # Adoption records what is there (ADR-0028): no generated playlist, no
        # ES-DE collapse rename.
        h.seed_rom()
        h.stage_detail(_multi_file_detail())
        h.store.files["/roms/psx/Game/Disc 1.cue"] = b"1"
        h.store.files["/roms/psx/Game/Disc 2.cue"] = b"2"
        before = set(h.store.files)

        await h.service.adopt_existing_rom(_ROM_ID)

        assert set(h.store.files) == before

    async def test_the_launchable_verdict_uses_the_live_accept_list(self, h):
        h.seed_rom()
        h.system_extensions = {"snes": frozenset({".sfc", ".smc"})}
        h.stage_detail(_single_file_detail(name="Game.pkg"))
        h.store.files["/roms/snes/Game.pkg"] = b"x"

        await h.service.adopt_existing_rom(_ROM_ID)

        install = h.uow.rom_installs.get(_ROM_ID)
        assert install is not None
        assert install.launchable is False

    async def test_a_launchable_adopted_install_bakes_the_launch_command(self, h):
        h.seed_rom(app_id=1042)
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"x"

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["app_id"] == 1042
        assert result["launch_options"].endswith('"/roms/snes/Game.sfc"')
        rom = h.uow.roms.get(_ROM_ID)
        assert rom is not None
        assert rom.applied_launch_options == result["launch_options"]

    async def test_an_unbound_rom_records_no_applied_launch_options(self, h):
        h.seed_rom(app_id=None)
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"x"

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["app_id"] is None
        rom = h.uow.roms.get(_ROM_ID)
        assert rom is not None
        assert rom.applied_launch_options is None

    async def test_a_vanished_path_is_refused_and_writes_no_row(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "nothing_to_adopt"
        assert h.uow.rom_installs.get(_ROM_ID) is None

    async def test_a_directory_where_a_file_belongs_is_refused(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc/inner.bin"] = b"x"

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "unexpected_content_kind"
        assert h.uow.rom_installs.get(_ROM_ID) is None

    async def test_a_file_where_a_directory_belongs_is_refused(self, h):
        h.seed_rom()
        h.stage_detail(_multi_file_detail())
        h.store.files["/roms/psx/Game"] = b"x"

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "unexpected_content_kind"

    async def test_a_rejected_install_never_deletes_the_content(self, h):
        # A ROM with no `roms` row cannot carry an install. Refused up front —
        # before the supersede — so neither the user's bytes nor a sibling's are
        # touched by an adoption that was never going to be recorded.
        h.romm_api.roms[0] = {**_single_file_detail(), "id": 0}
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        result = await h.service.adopt_existing_rom(0)

        assert result["success"] is False
        assert result["reason"] == "invalid_install"
        assert h.store.files["/roms/snes/Game.sfc"] == b"mine"
        assert h.superseded == []

    async def test_adopting_supersedes_the_group_s_other_install(self, h):
        # One installed version per shortcut binding (#1298), whichever route
        # produced it — an adopted install is an install (ADR-0028).
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is True
        assert h.superseded == [_ROM_ID]

    async def test_a_failed_supersede_aborts_the_adoption_with_nothing_written(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10
        h.supersede_result = {"success": False, "reason": "in_progress", "message": "boom"}

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result == {"success": False, "reason": "in_progress", "message": "boom"}
        assert h.uow.rom_installs.get(_ROM_ID) is None
        assert h.store.files["/roms/snes/Game.sfc"] == b"x" * 10

    async def test_the_supersede_runs_after_validation_and_before_the_row(self, h):
        # The ordering the whole design turns on, pinned so a refactor cannot
        # invert it: nothing is deleted until every refusal has been ruled out,
        # and the row is not written until the deletion has succeeded.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10
        order: list[str] = []

        async def _record_supersede(rom_id: int) -> None:
            order.append(f"supersede:{rom_id}")
            return

        recorder_record = h.recorder.do_record_install

        def _record_install(**kwargs):
            order.append("record")
            return recorder_record(**kwargs)

        h.service._sibling_supersede = lambda: _record_supersede
        h.service._install_recorder = SimpleNamespace(
            do_record_install=_record_install,
            do_resolve_launch_bake=h.recorder.do_resolve_launch_bake,
            do_record_applied_launch_options=h.recorder.do_record_applied_launch_options,
        )

        assert (await h.service.adopt_existing_rom(_ROM_ID))["success"] is True

        assert order == [f"supersede:{_ROM_ID}", "record"]

    async def test_a_vanished_path_is_refused_before_anything_is_superseded(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["reason"] == "nothing_to_adopt"
        assert h.superseded == []

    async def test_a_server_failure_surfaces_the_canonical_shape(self, h):
        h.romm_api.fail_on_next(OSError("boom"))

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert isinstance(result["reason"], str)
        assert isinstance(result["message"], str)

    async def test_an_unsafe_platform_slug_is_refused(self, h):
        h.stage_detail({**_single_file_detail(), "platform_slug": "../../etc"})
        result = await h.service.adopt_existing_rom(_ROM_ID)
        assert result["success"] is False
        assert result["reason"] == "path_traversal"


# ── verify ───────────────────────────────────────────────────────────────


class TestVerify:
    async def test_a_matching_single_file_reports_match(self, h):
        payload = b"x" * 10
        h.stage_detail(
            _single_file_detail(files=[{"file_name": "Game.sfc", "file_size_bytes": 10, "md5_hash": _md5(payload)}])
        )
        h.store.files["/roms/snes/Game.sfc"] = payload

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"
        assert result["differences"] == []

    async def test_the_manifest_name_is_used_even_when_the_local_name_differs(self, h):
        # The on-disk name comes from fs_name; the manifest states the server's.
        payload = b"x" * 10
        detail = _single_file_detail(
            name="Local Name.sfc",
            files=[{"file_name": "Server Name.sfc", "file_size_bytes": 10, "md5_hash": _md5(payload)}],
        )
        h.stage_detail(detail)
        h.store.files["/roms/snes/Local Name.sfc"] = payload

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_wrong_digest_names_the_file_and_both_values(self, h):
        h.stage_detail(
            _single_file_detail(files=[{"file_name": "Game.sfc", "file_size_bytes": 10, "md5_hash": "deadbeef"}])
        )
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"] == [{"name": "Game.sfc", "detail": "contents differ from the server's copy"}]
        # The two digests are deliberately absent: 32 hex characters each said no
        # more than "these differ" and wrapped the line into an unreadable block.
        assert "deadbeef" not in result["differences"][0]["detail"]

    async def test_a_wrong_size_is_reported_without_hashing(self, h):
        h.stage_detail(
            _single_file_detail(files=[{"file_name": "Game.sfc", "file_size_bytes": 99, "md5_hash": "deadbeef"}])
        )
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        # Sizes stay: unlike a digest, they are numbers a person can act on.
        assert result["differences"] == [{"name": "Game.sfc", "detail": "expected 99 bytes, found 10"}]

    async def test_a_crc_only_server_is_still_verifiable(self, h):
        import zlib

        payload = b"x" * 10
        crc = f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"
        h.stage_detail(
            _single_file_detail(files=[{"file_name": "Game.sfc", "file_size_bytes": 10, "crc_hash": crc.upper()}])
        )
        h.store.files["/roms/snes/Game.sfc"] = payload

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_server_without_checksums_is_unverifiable(self, h):
        h.stage_detail(_single_file_detail(files=[{"file_name": "Game.sfc", "file_size_bytes": 10}]))
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"
        assert result["differences"] == []
        assert "publishes no checksums" in result["message"]

    async def test_an_entry_with_a_digest_but_no_size_is_still_hashed(self, h):
        # A stated size that is absent means "cannot compare on size", never
        # "sizes agree" — so the digest is what decides, and it must be read.
        payload = b"x" * 10
        h.stage_detail(_single_file_detail(files=[{"file_name": "Game.sfc", "md5_hash": _md5(payload)}]))
        h.store.files["/roms/snes/Game.sfc"] = payload

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_an_entry_with_a_digest_but_no_size_still_catches_a_mismatch(self, h):
        # The failure direction that matters: without the size gate skipping the
        # hash, wrong bytes here used to sail through as a clean "match".
        h.stage_detail(_single_file_detail(files=[{"file_name": "Game.sfc", "md5_hash": "0" * 32}]))
        h.store.files["/roms/snes/Game.sfc"] = b"different bytes"

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"][0]["name"] == "Game.sfc"

    async def test_an_entry_with_neither_size_nor_digest_is_unverifiable(self, h):
        h.stage_detail(_single_file_detail(files=[{"file_name": "Game.sfc"}]))
        h.store.files["/roms/snes/Game.sfc"] = b"x" * 10

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"

    async def test_nothing_on_disk_reports_missing(self, h):
        h.stage_detail(_single_file_detail(files=[{"file_name": "Game.sfc", "md5_hash": "ab"}]))

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "missing"

    async def test_a_server_failure_reports_error_with_a_message(self, h):
        h.romm_api.fail_on_next(OSError("boom"))

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "error"
        assert result["message"]
        assert result["differences"] == []

    async def test_a_directory_matches_when_every_listed_file_is_present(self, h):
        a, b = b"a" * 4, b"b" * 6
        h.stage_detail(
            _multi_file_detail(
                files=[
                    {"file_name": "a.bin", "file_size_bytes": 4, "md5_hash": _md5(a)},
                    {"file_name": "b.bin", "file_size_bytes": 6, "md5_hash": _md5(b)},
                ]
            )
        )
        h.store.files["/roms/psx/Game/a.bin"] = a
        h.store.files["/roms/psx/Game/b.bin"] = b

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_extra_files_in_a_directory_do_not_break_the_match(self, h):
        a = b"a" * 4
        h.stage_detail(_multi_file_detail(files=[{"file_name": "a.bin", "file_size_bytes": 4, "md5_hash": _md5(a)}]))
        h.store.files["/roms/psx/Game/a.bin"] = a
        h.store.files["/roms/psx/Game/Game.m3u"] = b"generated"

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_missing_listed_file_is_a_mismatch(self, h):
        a = b"a" * 4
        h.stage_detail(
            _multi_file_detail(
                files=[
                    {"file_name": "a.bin", "file_size_bytes": 4, "md5_hash": _md5(a)},
                    {"file_name": "b.bin", "file_size_bytes": 6, "md5_hash": "ff"},
                ]
            )
        )
        h.store.files["/roms/psx/Game/a.bin"] = a

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert [d["name"] for d in result["differences"]] == ["b.bin"]

    async def test_a_nested_listed_file_is_found_by_name_when_the_server_did_not_locate_it(self, h):
        # No `full_path` in the payload → no relative path can be derived, so the
        # entry falls back to a search by filename anywhere in the tree.
        nested = b"n" * 8
        h.stage_detail(
            _multi_file_detail(files=[{"file_name": "EBOOT.BIN", "file_size_bytes": 8, "md5_hash": _md5(nested)}])
        )
        h.store.files["/roms/psx/Game/PS3_GAME/USRDIR/EBOOT.BIN"] = nested

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"


class TestVerifyArchives:
    """A zipped ROM is held to what is inside it, from the file-level digest alone.

    That is the shape a real server sends: ``archive_members`` arrived in RomM
    4.9.0 and stays null on every library not rescanned since, while the digest
    beside it has always described the content — the current scanner accumulates
    over every member, the older one took the archive's largest member, and for a
    single member those are the same bytes. The container's own bytes answer to
    neither, which is why comparing them reported a mismatch on a byte-perfect
    copy of what the server sent.
    """

    async def test_a_zipped_rom_matches_a_byte_perfect_copy_of_what_the_server_sent(self, h):
        # The device case: same bytes RomM served, re-offered to the gate.
        members = {"Game.gba": b"rom bytes" * 16}
        archive = _zip_bytes(members)
        h.stage_detail(_archived_detail(archive=archive, members=members))
        h.store.files["/roms/snes/Game.zip"] = archive

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"
        assert result["differences"] == []

    async def test_a_repacked_archive_of_the_same_rom_still_matches(self, h):
        # Same ROM inside, packed differently: the container's bytes and size
        # both change and neither is what the server's digest describes.
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes(members, compression=zipfile.ZIP_STORED)

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_changed_byte_inside_the_archive_is_a_mismatch(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"Game.gba": b"rom bytez" * 16})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"] == [{"name": "Game.zip", "detail": "contents differ from the server's copy"}]

    async def test_the_crc_alone_disqualifies_without_decompressing_anything(self, h):
        # The central directory states the member's CRC32 for free and the server
        # publishes its own beside the md5; a disagreement is already proof.
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"Game.gba": b"rom bytez" * 16})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert h.store.member_checksum_calls == []

    async def test_a_crc_less_server_still_confirms_the_member_by_its_digest(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        archive = _zip_bytes(members)
        detail = _archived_detail(archive=archive, members=members)
        detail["files"][0]["crc_hash"] = ""

        h.stage_detail(detail)
        h.store.files["/roms/snes/Game.zip"] = archive

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"
        assert h.store.member_checksum_calls == [("/roms/snes/Game.zip", "Game.gba", "md5")]

    async def test_several_members_the_server_described_only_as_a_whole_cannot_be_confirmed(self, h):
        # An arcade set is many members under one number, and that number is
        # either a composite over all of them or the largest member alone —
        # nothing in the payload says which, so neither answer would be honest.
        members = {"aburner.bin": b"one" * 8, "aburner2.bin": b"two" * 8}
        archive = _zip_bytes(members)
        h.stage_detail(_archived_detail(archive=archive, members=members))
        h.store.files["/roms/snes/Game.zip"] = archive

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"
        assert result["differences"] == []
        assert "whole archive" in result["message"]

    async def test_a_bundled_extra_file_leaves_a_single_member_archive_unconfirmed(self, h):
        # The same rule seen from the other side: a readme packed beside the ROM
        # makes two members, and the plugin will not guess which one the
        # server's single number covers.
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({**members, "readme.txt": b"packed by someone"})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"

    async def test_a_container_this_plugin_cannot_open_is_unverifiable_never_a_mismatch(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members))
        h.store.files["/roms/snes/Game.zip"] = b"7z\xbc\xaf\x27\x1c" + b"compressed" * 4

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"
        assert result["differences"] == []
        # Not the no-checksums message: this server published them, and saying
        # otherwise would send the user looking for a problem on the server.
        assert "could not be read" in result["message"]

    async def test_an_archive_format_the_plugin_cannot_read_is_not_accused_of_differing(self, h):
        # RomM hashes a .7z by its contents too, so the container's own bytes
        # match nothing it published — and this plugin cannot look inside one.
        payload = b"7z\xbc\xaf\x27\x1c" + b"compressed" * 4
        h.stage_detail(
            _single_file_detail(
                name="Game.7z",
                size=len(payload),
                files=[{"file_name": "Game.7z", "file_size_bytes": len(payload), "md5_hash": "0" * 32}],
            )
        )
        h.store.files["/roms/snes/Game.7z"] = payload

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"
        assert result["differences"] == []

    async def test_a_member_that_cannot_be_read_leaves_the_check_unconfirmed(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        archive = _zip_bytes(members)
        h.stage_detail(_archived_detail(archive=archive, members=members))
        h.store.files["/roms/snes/Game.zip"] = archive

        def unreadable(*_args, **_kwargs):
            raise NotImplementedError("compression type 9 (deflate64)")

        h.store.checksum_archive_member = unreadable

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"
        assert result["differences"] == []
        assert "could not be read" in result["message"]

    async def test_progress_counts_the_member_bytes_that_are_read(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        archive = _zip_bytes(members)
        h.stage_detail(_archived_detail(archive=archive, members=members))
        h.store.files["/roms/snes/Game.zip"] = archive

        await h.service.verify_existing_content(_ROM_ID)
        await asyncio.sleep(0)  # let the loop drain the scheduled emit tasks

        frames = [payload for name, payload in h.events if name == "verify_progress"]
        assert frames[-1] == {"rom_id": _ROM_ID, "bytes_done": 144, "bytes_total": 144}


class TestVerifyArchivesWithStatedMembers:
    """A rescanned library names every member, which is strictly better evidence.

    ``archive_members`` carries a name, an uncompressed size and three digests
    per member, so each one is confirmed separately and a set of them can be
    reported on individually — none of which the file-level digest can do.
    """

    async def test_a_multi_member_archive_matches_when_every_member_agrees(self, h):
        members = {"disc1.bin": b"one" * 8, "disc1.cue": b"cue sheet", "disc2.bin": b"two" * 8}
        archive = _zip_bytes(members)
        h.stage_detail(_archived_detail(archive=archive, members=members, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = archive

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_the_containers_own_digest_is_never_what_is_compared(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        archive = _zip_bytes(members)
        detail = _archived_detail(archive=archive, members=members, state_members=True)
        detail["files"][0]["md5_hash"] = "0" * 32

        h.stage_detail(detail)
        h.store.files["/roms/snes/Game.zip"] = archive

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_the_containers_own_size_is_never_what_is_compared(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        archive = _zip_bytes(members)
        detail = _archived_detail(archive=archive, members=members, state_members=True)
        detail["files"][0]["file_size_bytes"] = len(archive) + 4096

        h.stage_detail(detail)
        h.store.files["/roms/snes/Game.zip"] = archive

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_changed_byte_inside_the_archive_names_the_member(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"Game.gba": b"rom bytez" * 16})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"] == [
            {"name": "Game.zip/Game.gba", "detail": "contents differ from the server's copy"}
        ]

    async def test_a_member_whose_crc_disagrees_is_reported_without_decompressing_it(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"Game.gba": b"rom bytez" * 16})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert h.store.member_checksum_calls == []

    async def test_a_member_the_server_published_no_crc_for_is_still_held_to_its_digest(self, h):
        members = {"Game.gba": b"rom bytes" * 16}
        detail = _archived_detail(archive=_zip_bytes(members), members=members, state_members=True)
        detail["files"][0]["archive_members"][0]["crc_hash"] = ""

        h.stage_detail(detail)
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"Game.gba": b"rom bytez" * 16})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"] == [
            {"name": "Game.zip/Game.gba", "detail": "contents differ from the server's copy"}
        ]
        assert h.store.member_checksum_calls == [("/roms/snes/Game.zip", "Game.gba", "md5")]

    async def test_a_member_the_archive_does_not_hold_is_a_mismatch_naming_it(self, h):
        members = {"disc1.bin": b"one" * 8, "disc2.bin": b"two" * 8}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"disc1.bin": b"one" * 8})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"] == [{"name": "Game.zip/disc2.bin", "detail": "missing from the archive"}]

    async def test_a_partial_archive_cannot_read_as_a_match(self, h):
        # Every member that IS there agrees; the verdict still has to be no.
        members = {"disc1.bin": b"one" * 8, "disc2.bin": b"two" * 8}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({"disc1.bin": b"one" * 8})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] != "match"

    async def test_members_the_server_did_not_list_are_not_a_difference(self, h):
        # RomM's scanner drops excluded names and extensions from
        # ``archive_members``, so its own archive holds more than it listed.
        listed = {"Game.gba": b"rom bytes" * 16}
        h.stage_detail(_archived_detail(archive=_zip_bytes(listed), members=listed, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = _zip_bytes({**listed, "readme.txt": b"scanned by nobody"})

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_file_unpacked_from_a_single_member_archive_matches_that_member(self, h):
        # The user unzipped what the server keeps packed: one loose file where
        # the archive would be, which is the member and nothing else.
        member = b"rom bytes" * 16
        h.stage_detail(
            _archived_detail(archive=_zip_bytes({"Game.gba": member}), members={"Game.gba": member}, state_members=True)
        )
        h.store.files["/roms/snes/Game.zip"] = member

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_an_unpacked_file_that_differs_is_a_mismatch(self, h):
        member = b"rom bytes" * 16
        h.stage_detail(
            _archived_detail(archive=_zip_bytes({"Game.gba": member}), members={"Game.gba": member}, state_members=True)
        )
        h.store.files["/roms/snes/Game.zip"] = b"rom bytez" * 16

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert [d["name"] for d in result["differences"]] == ["Game.zip/Game.gba"]

    async def test_a_composite_over_several_members_has_no_single_file_to_answer_it(self, h):
        # One file on disk cannot be what a multi-member archive hashes to, so
        # there is nothing to compare — not a mismatch, not a match.
        members = {"disc1.bin": b"one" * 8, "disc2.bin": b"two" * 8}
        h.stage_detail(_archived_detail(archive=_zip_bytes(members), members=members, state_members=True))
        h.store.files["/roms/snes/Game.zip"] = b"one" * 8

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "unverifiable"
        assert result["differences"] == []


class TestVerifyLocatesFilesExactly:
    """RomM states where each file belongs, so the check holds it to that place.

    ``RomFile.is_top_level`` compares ``rom.full_path`` against ``file_path``, so
    the two are one coordinate system and the ROM-relative path is a subtraction,
    not a guess. An adopted row carries deletion authority (ADR-0028), and "the
    file exists somewhere in the tree" is weaker evidence than that row deserves.
    """

    async def test_a_correctly_nested_file_verifies_as_present(self, h):
        nested = b"n" * 8
        h.stage_detail(
            _multi_file_detail(
                full_path=_ROM_FULL_PATH,
                files=[_located("EBOOT.BIN", size=8, digest=_md5(nested), in_dir="PS3_GAME/USRDIR")],
            )
        )
        h.store.files["/roms/psx/Game/PS3_GAME/USRDIR/EBOOT.BIN"] = nested

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_file_in_the_wrong_subdirectory_reads_as_missing(self, h):
        # The case that motivated the exact match: the bytes are there, but not
        # where the server says this game's file belongs.
        nested = b"n" * 8
        h.stage_detail(
            _multi_file_detail(
                full_path=_ROM_FULL_PATH,
                files=[_located("EBOOT.BIN", size=8, digest=_md5(nested), in_dir="PS3_GAME/USRDIR")],
            )
        )
        h.store.files["/roms/psx/Game/somewhere/else/EBOOT.BIN"] = nested

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"] == [{"name": "PS3_GAME/USRDIR/EBOOT.BIN", "detail": "missing"}]

    async def test_two_same_named_files_in_different_subdirectories_are_told_apart(self, h):
        good, wrong = b"g" * 4, b"w" * 4
        h.stage_detail(
            _multi_file_detail(
                full_path=_ROM_FULL_PATH,
                files=[
                    _located("data.bin", size=4, digest=_md5(good), in_dir="disc1"),
                    _located("data.bin", size=4, digest=_md5(good), in_dir="disc2"),
                ],
            )
        )
        h.store.files["/roms/psx/Game/disc1/data.bin"] = good
        h.store.files["/roms/psx/Game/disc2/data.bin"] = wrong

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        # disc1 matched; only disc2's copy is reported, under its own path.
        assert result["differences"] == [{"name": "disc2/data.bin", "detail": "contents differ from the server's copy"}]

    async def test_a_top_level_file_is_located_at_the_rom_root(self, h):
        top = b"t" * 5
        h.stage_detail(
            _multi_file_detail(full_path=_ROM_FULL_PATH, files=[_located("a.bin", size=5, digest=_md5(top))])
        )
        h.store.files["/roms/psx/Game/a.bin"] = top

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_untidy_server_paths_still_line_up(self, h):
        # RomM is not guaranteed to hand back a tidy string; both sides are
        # normalised before the prefix subtraction.
        top = b"t" * 5
        detail = _multi_file_detail(
            full_path="/roms/psx/Game/",
            files=[{"file_name": "a.bin", "file_path": "roms//psx/Game", "file_size_bytes": 5, "md5_hash": _md5(top)}],
        )
        h.stage_detail(detail)
        h.store.files["/roms/psx/Game/a.bin"] = top

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_a_file_path_that_does_not_nest_falls_back_to_the_name(self, h):
        # The server stated a path, but not one under this ROM — nothing can be
        # subtracted, so the entry keeps the weaker by-name match rather than
        # inventing a location.
        top = b"t" * 5
        detail = _multi_file_detail(
            full_path=_ROM_FULL_PATH,
            files=[
                {"file_name": "a.bin", "file_path": "roms/psx/Other Game", "file_size_bytes": 5, "md5_hash": _md5(top)}
            ],
        )
        h.stage_detail(detail)
        h.store.files["/roms/psx/Game/deep/a.bin"] = top

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "match"

    async def test_an_entry_escaping_the_rom_directory_is_refused_not_looked_up(self, h):
        outside = b"o" * 5
        detail = _multi_file_detail(
            full_path=_ROM_FULL_PATH,
            files=[
                {
                    "file_name": "passwd",
                    "file_path": f"{_ROM_FULL_PATH}/../../../etc",
                    "file_size_bytes": 5,
                    "md5_hash": _md5(outside),
                }
            ],
        )
        h.stage_detail(detail)
        h.store.files["/roms/psx/Game/passwd"] = outside

        result = await h.service.verify_existing_content(_ROM_ID)

        assert result["status"] == "mismatch"
        assert result["differences"][0]["detail"] == "sits outside this game's folder"

    async def test_progress_is_reported_as_bytes_over_the_hashed_total(self, h):
        a, b = b"a" * 4, b"b" * 6
        h.stage_detail(
            _multi_file_detail(
                files=[
                    {"file_name": "a.bin", "file_size_bytes": 4, "md5_hash": _md5(a)},
                    {"file_name": "b.bin", "file_size_bytes": 6, "md5_hash": _md5(b)},
                ]
            )
        )
        h.store.files["/roms/psx/Game/a.bin"] = a
        h.store.files["/roms/psx/Game/b.bin"] = b

        await h.service.verify_existing_content(_ROM_ID)
        await asyncio.sleep(0)  # let the loop drain the scheduled emit tasks

        frames = [payload for name, payload in h.events if name == "verify_progress"]
        assert frames, "no verify_progress frame was emitted"
        assert frames[-1] == {"rom_id": _ROM_ID, "bytes_done": 10, "bytes_total": 10}
