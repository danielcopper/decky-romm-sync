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
import re
import zipfile
import zlib
from types import SimpleNamespace
from typing import Any

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_adoption_move import FakeAdoptionMoveStore
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_download_file_store import FakeDownloadFileStore
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_romm_api import FakeRommApi
from fakes.fake_save_quarantine import FakeSaveQuarantine
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.rom import Rom
from domain.rom_candidates import CANDIDATE_LIMIT
from domain.rom_install import RomInstall
from domain.save_layout import ContentDir, InSaveDir, SaveLayout
from services.rom_adoption import RomAdoptionService, RomAdoptionServiceConfig
from services.rom_install_recorder import RomInstallRecorder, RomInstallRecorderConfig

_ROMS = "/roms"
_SAVES = "/saves"
_STATES = "/states"
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
        self.paths = FakeRetroDeckPaths(roms=_ROMS, saves=_SAVES, states=_STATES)
        self.move = FakeAdoptionMoveStore(self.store)
        self.quarantine = FakeSaveQuarantine(self.store)
        # The layouts a stock RetroDECK install reports: savefiles content-sorted,
        # savestates not sorted at all. Tests that care flip them individually.
        #
        # ``save_layout`` is the LIVE retroarch.cfg and answers only whether
        # savefiles are written next to the ROM; ``save_sorting`` is what the save
        # sync resolves its own paths with, which is the recorded observation and
        # differs from the live config while a save-sort migration is pending.
        self.save_layout: SaveLayout = InSaveDir(sort_by_content=True, sort_by_core=False)
        self.save_sorting = InSaveDir(sort_by_content=True, sort_by_core=False)
        self.savestate_layout: SaveLayout = InSaveDir(sort_by_content=False, sort_by_core=False)
        # Per-core save sorting is off by default, so neither of these is read
        # until a test turns ``sort_by_core`` on.
        self.active_core = FakeActiveCoreResolver(default=(None, None))
        self.core_name: str | None = None
        # Records every rom_id the supersede was asked about, and answers with
        # whatever a test has staged. Default: nothing to supersede.
        self.superseded: list[int] = []
        self.supersede_result: dict[str, Any] | None = None
        # Which directories ES-DE lists as systems. A system no test staged
        # answers ``None`` — the source could not answer, which is not a denial,
        # so the search behaves exactly as it did before the check existed.
        self.known_systems: dict[str, bool | None] = {}
        self.debug_log: list[str] = []
        self.service = RomAdoptionService(
            config=RomAdoptionServiceConfig(
                romm_api=self.romm_api,
                download_file_store=self.store,
                adoption_move=self.move,
                quarantine_save=self.quarantine,
                resolve_system=lambda platform_slug, platform_fs_slug=None: platform_fs_slug or platform_slug,
                retrodeck_paths=self.paths,
                install_recorder=self.recorder,
                m3u_support=lambda system_name: self.m3u_supported,
                system_extensions=lambda system_name: self.system_extensions.get(system_name, frozenset()),
                save_layout=lambda: self.save_layout,
                save_sorting=lambda: self.save_sorting,
                savestate_layout=lambda: self.savestate_layout,
                active_core=self.active_core,
                system_known=lambda system_name: self.known_systems.get(system_name),
                get_core_name=lambda core_so: self.core_name,
                sibling_supersede=lambda: self._supersede,
                uow_factory=FakeUnitOfWorkFactory(self.uow),
                loop=loop,
                logger=logging.getLogger("test_rom_adoption"),
                log_debug=lambda msg: self.debug_log.append(msg),
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
        assert result["existing"]["kind"] == "file"
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
        assert result["existing"]["kind"] == "dir"
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

    async def test_a_symlink_at_the_target_is_refused_by_the_acting_site_itself(self, h):
        # The offering sites already refuse one, and this is the reachable case
        # they cannot cover: the entry was a regular file when the dialog opened
        # and is a link by the time the user confirms. Only this check stands
        # between that and a row ``claim_source`` will never let go of.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.links["/roms/snes/Game.sfc"] = "/roms/snes/real.sfc"

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "unexpected_content_kind"
        assert result["message"] == "A shortcut is in the way — a shortcut cannot be used as this game"
        assert h.uow.rom_installs.get(_ROM_ID) is None
        assert set(h.store.links) == {"/roms/snes/Game.sfc"}

    async def test_a_named_pipe_at_the_target_is_refused_the_same_way(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.other_kinds.add("/roms/snes/Game.sfc")

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "unexpected_content_kind"
        assert result["message"] == "What is in the way is neither a file nor a folder"
        assert h.uow.rom_installs.get(_ROM_ID) is None

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

    def _change_target_during_supersede(self, h, change) -> None:
        """Run *change* against the store in the window ``_adopt_io`` guards.

        The validation has already accepted the content and the supersede has
        already run its real I/O, so this is the only place a test can put a
        change that the last re-stat is the sole check against. Hooking the
        supersede seam rather than counting ``describe_path`` calls keeps the
        witness tied to the ordering the service actually has.
        """

        async def _supersede(rom_id: int) -> None:
            h.superseded.append(rom_id)
            change()

        h.service._sibling_supersede = lambda: _supersede

    async def test_content_that_turns_into_a_link_across_the_supersede_is_refused(self, h):
        # The window the re-stat exists for: a regular file when the validation
        # looked, a link by the time the row would be written. Recording it would
        # be the install ``claim_source`` never releases.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        def _becomes_a_link() -> None:
            del h.store.files["/roms/snes/Game.sfc"]
            h.store.links["/roms/snes/Game.sfc"] = "/roms/snes/real.sfc"

        self._change_target_during_supersede(h, _becomes_a_link)

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "unexpected_content_kind"
        assert result["message"] == "A shortcut is in the way — a shortcut cannot be used as this game"
        assert h.superseded == [_ROM_ID]
        assert h.uow.rom_installs.get(_ROM_ID) is None
        assert set(h.store.links) == {"/roms/snes/Game.sfc"}

    async def test_content_that_loses_its_kind_across_the_supersede_is_refused_too(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        def _becomes_a_pipe() -> None:
            del h.store.files["/roms/snes/Game.sfc"]
            h.store.other_kinds.add("/roms/snes/Game.sfc")

        self._change_target_during_supersede(h, _becomes_a_pipe)

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["reason"] == "unexpected_content_kind"
        assert result["message"] == "What is in the way is neither a file nor a folder"
        assert h.uow.rom_installs.get(_ROM_ID) is None

    async def test_content_that_vanishes_across_the_supersede_says_it_is_gone(self, h):
        # The guard's other half, and a different situation: nothing is there, so
        # "the files are no longer there" is the true sentence here and the wrong
        # one above.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        self._change_target_during_supersede(h, lambda: h.store.files.pop("/roms/snes/Game.sfc"))

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is False
        assert result["reason"] == "nothing_to_adopt"
        assert result["message"] == "The files are no longer there — nothing was adopted"
        assert h.superseded == [_ROM_ID]
        assert h.uow.rom_installs.get(_ROM_ID) is None

    async def test_content_that_survives_the_supersede_unchanged_is_still_recorded(self, h):
        # The control: the same seam, the same ordering, no change — so the three
        # refusals above are about what changed and not about the hook itself.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        self._change_target_during_supersede(h, lambda: None)

        result = await h.service.adopt_existing_rom(_ROM_ID)

        assert result["success"] is True
        assert h.uow.rom_installs.get(_ROM_ID) is not None

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


# ── the candidate search ─────────────────────────────────────────────────


def _crc32(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


class TestCandidateSearch:
    async def test_an_empty_platform_directory_lets_the_download_proceed(self, h):
        result = await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False)
        assert result is None

    async def test_a_folder_of_other_games_lets_the_download_proceed(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Other Game (USA).sfc"] = b"z"
        h.store.files["/roms/snes/Third Game (USA).sfc"] = b"m"

        result = await h.service.check_download_target(_single_file_detail(), "/roms/snes/Game.sfc", replace=False)
        assert result is None

    async def test_the_same_game_under_another_name_refuses_the_download(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x" * 10
        h.store.mtimes["/roms/snes/Game (U).sfc"] = 1_700_000_000.0

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc", size=10), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["success"] is False
        assert result["reason"] == "adoption_candidates"
        assert result["incoming"] == {"name": "Game (USA).sfc", "size_bytes": 10}
        assert result["candidates"] == [
            {
                "name": "Game (U).sfc",
                "path": "/roms/snes/Game (U).sfc",
                "is_dir": False,
                "size_bytes": 10,
                "modified_at": 1_700_000_000.0,
                "evidence": "size",
                "detail": result["candidates"][0]["detail"],
            }
        ]
        assert result["truncated"] is False

    async def test_the_search_writes_nothing(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"mine"

        await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert h.store.files == {"/roms/snes/Game (U).sfc": b"mine"}

    async def test_several_candidates_are_ranked_by_what_each_rests_on(self, h):
        h.system_extensions = {"snes": frozenset({".sfc", ".zip"})}
        member = b"cartridge bytes"
        h.store.files["/roms/snes/Game (E).zip"] = _zip_bytes({"Game.sfc": member})
        h.store.files["/roms/snes/Game (J).sfc"] = b"x" * 10
        h.store.files["/roms/snes/Game (U).sfc"] = b"x" * 7

        result = await h.service.check_download_target(
            _single_file_detail(
                name="Game (USA).sfc",
                size=10,
                files=[{"file_name": "Game (USA).sfc", "file_size_bytes": 10, "crc_hash": _crc32(member)}],
            ),
            "/roms/snes/Game (USA).sfc",
            replace=False,
        )

        assert result is not None
        assert [(c["name"], c["evidence"]) for c in result["candidates"]] == [
            ("Game (E).zip", "crc32"),
            ("Game (J).sfc", "size"),
            ("Game (U).sfc", "name"),
        ]

    async def test_a_candidate_an_install_row_accounts_for_is_never_offered(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"
        h.seed_install(rom_id=99, file_path="/roms/snes/Game (U).sfc")

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )

    async def test_a_multi_file_rom_s_directory_install_is_never_offered(self, h):
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.bin"] = b"x"
        h.seed_install(rom_id=99, file_path="/roms/psx/Game (U)/disc.bin", rom_dir="/roms/psx/Game (U)")

        assert (
            await h.service.check_download_target(
                _multi_file_detail(dir_name="Game (USA)"), "/roms/psx/Game (USA)", replace=False
            )
            is None
        )

    async def test_an_extension_the_system_does_not_accept_is_not_offered(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).txt"] = b"notes"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )

    async def test_a_directory_candidate_is_offered_for_a_multi_file_rom(self, h):
        h.system_extensions = {"psx": frozenset({".cue"})}
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"x"

        result = await h.service.check_download_target(
            _multi_file_detail(dir_name="Game (USA)"), "/roms/psx/Game (USA)", replace=False
        )

        assert result is not None
        assert result["candidates"][0]["name"] == "Game (U)"
        assert result["candidates"][0]["is_dir"] is True
        assert result["candidates"][0]["size_bytes"] == 0

    async def test_a_same_named_folder_is_not_offered_as_a_single_file_rom_s_candidate(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Game (U)")
        h.store.files["/roms/snes/Game (U)/notes.txt"] = b"x"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "unusable_namesake"
        assert "candidates" not in result

    async def test_the_user_who_chose_download_is_not_asked_again(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=True
            )
            is None
        )

    async def test_a_resume_is_never_refused_by_the_candidate_the_user_declined(self, h):
        # A paused multi-file transfer has no extract directory yet, so the gate
        # sees a free path. Searching again would hand back the very candidate the
        # user declined when they started the download, and the frontend's only
        # exit is Cancel — which discards the transferred bytes.
        h.system_extensions = {"psx": frozenset({".cue"})}
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"declined"

        result = await h.service.check_download_target(
            _multi_file_detail(dir_name="Game"), "/roms/psx/Game", replace=False, resume=True
        )

        assert result is None

    async def test_the_same_candidate_still_refuses_a_fresh_download(self, h):
        h.system_extensions = {"psx": frozenset({".cue"})}
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"declined"

        result = await h.service.check_download_target(
            _multi_file_detail(dir_name="Game"), "/roms/psx/Game", replace=False, resume=False
        )

        assert result is not None
        assert result["reason"] == "adoption_candidates"

    async def test_an_occupied_target_is_still_the_other_dialog_s_subject(self, h):
        # The search only ever runs on a free target: an occupied one is already
        # a comparison the user is being shown.
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (USA).sfc"] = b"in the way"
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "target_occupied"


class TestTheAdmissionRule:
    """What an entry is decides what may be said about it — nothing follows a link."""

    async def test_a_symlink_is_never_offered_as_a_candidate(self, h):
        # Adopting one writes an install row the UI can never undo: every
        # uninstall goes through ``claim_source``, which refuses a symlink.
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.links["/roms/snes/Game (U).sfc"] = "/roms/snes/real.sfc"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc", size=10), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "unusable_namesake"
        assert result["existing"] == [{"name": "Game (U).sfc", "path": "/roms/snes/Game (U).sfc", "kind": "link"}]
        assert "candidates" not in result

    async def test_a_symlink_is_named_for_what_it_is(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.links["/roms/snes/Game (U).sfc"] = "/roms/snes/real.sfc"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["message"] == (
            "'Game (U).sfc' has this game's name but is a shortcut to somewhere else, "
            "which cannot be used as this game whatever it points at"
        )

    async def test_a_named_pipe_is_not_mentioned_at_all(self, h):
        # It reported as an ordinary zero-byte file and was offered as a game.
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.other_kinds.add("/roms/snes/Game (U).sfc")

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )
        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is False

    async def test_a_real_file_is_still_a_candidate_beside_a_link(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.links["/roms/snes/Game (J).sfc"] = "/roms/snes/real.sfc"
        h.store.files["/roms/snes/Game (U).sfc"] = b"mine"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "adoption_candidates"
        assert [candidate["name"] for candidate in result["candidates"]] == ["Game (U).sfc"]

    async def test_the_page_still_reports_a_link_so_the_button_is_honest(self, h):
        # It is content the user has: a download lands beside it and leaves two.
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.links["/roms/snes/Game (U).sfc"] = "/roms/snes/real.sfc"

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is True

    async def test_downloading_anyway_leaves_the_link_alone(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.links["/roms/snes/Game (U).sfc"] = "/roms/snes/real.sfc"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=True
            )
            is None
        )
        assert set(h.store.links) == {"/roms/snes/Game (U).sfc"}


class TestSymlinkAtTheTargetPath:
    """The same rule through the other door — content at the ROM's own location."""

    async def test_a_link_at_the_target_path_is_not_adoptable(self, h):
        # PR #1712's dialog offered to adopt it, because ``describe_path``
        # followed and reported ordinary content.
        h.store.links["/roms/snes/Game.sfc"] = "/roms/snes/real.sfc"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game.sfc"), "/roms/snes/Game.sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "target_occupied"
        assert result["adoptable"] is False

    async def test_ordinary_content_of_the_right_shape_is_still_adoptable(self, h):
        h.store.files["/roms/snes/Game.sfc"] = b"mine"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game.sfc"), "/roms/snes/Game.sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "target_occupied"
        assert result["adoptable"] is True

    async def test_a_link_at_the_target_path_is_not_read_as_nothing(self, h):
        # Reported as absent, the download proceeded and the finalize replace
        # destroyed the link in silence. It occupies the path; the user is asked.
        h.store.links["/roms/snes/Game.sfc"] = "/roms/snes/real.sfc"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game.sfc"), "/roms/snes/Game.sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "target_occupied"

    async def test_a_named_pipe_at_the_target_path_is_reported_rather_than_written_over(self, h):
        # The listings leave one out, so the search says "nothing here" and the
        # download would have run and replaced it without a word. This door
        # reports it — with no kind, because there is no honest word for it.
        h.store.other_kinds.add("/roms/snes/Game.sfc")

        result = await h.service.check_download_target(
            _single_file_detail(name="Game.sfc"), "/roms/snes/Game.sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "target_occupied"
        assert result["existing"]["kind"] is None
        assert result["adoptable"] is False
        assert result["sizes_match"] is None
        assert result["message"] == "Something named 'Game.sfc' is already in place"

    async def test_a_link_at_the_target_path_states_no_size_verdict(self, h):
        # Its byte count is the length of the path it stores, and the dialog was
        # showing that as the content's and comparing it with the server's.
        h.store.links["/roms/snes/Game.sfc"] = "/roms/snes/real.sfc"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game.sfc", size=len("/roms/snes/real.sfc")),
            "/roms/snes/Game.sfc",
            replace=False,
        )

        assert result is not None
        assert result["sizes_match"] is None


class TestSearchableDirectory:
    """A directory that is not an ES-DE system is not a place a game can live."""

    async def test_a_directory_that_is_not_a_system_yields_no_candidate(self, h):
        h.known_systems = {"snes": False}
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )
        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is False

    async def test_a_real_system_still_answers(self, h):
        h.known_systems = {"snes": True}
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is True

    async def test_a_source_that_could_not_answer_is_not_a_denial(self, h):
        # ``None`` is "es_systems.xml could not be read", which must not turn the
        # search off — the same default-safe reading the accept-list applies.
        h.known_systems = {}
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is True

    async def test_an_unknown_directory_with_no_accept_list_matches_nothing(self, h):
        # The second hole the same check closes: an empty accept-list means
        # "cannot tell" and skips the extension test, so without the system check
        # every file in a non-system directory would match on name alone.
        h.known_systems = {"not-a-system": False}
        h.system_extensions = {}
        h.store.files["/roms/not-a-system/Game (U).sfc"] = b"x"

        assert h.service.has_adoption_candidate("not-a-system", "Game (USA).sfc") is False


class TestVanishedBackstop:
    """The page found a copy and the search cannot name one."""

    async def test_it_refuses_rather_than_downloading_silently(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc", size=10),
            "/roms/snes/Game (USA).sfc",
            replace=False,
            page_saw_candidate=True,
        )

        assert result is not None
        assert result["success"] is False
        assert result["reason"] == "candidate_vanished"
        assert result["incoming"] == {"name": "Game (USA).sfc", "size_bytes": 10}

    async def test_a_page_that_found_nothing_downloads_as_before(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )

    async def test_every_specific_answer_wins_over_it(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"),
            "/roms/snes/Game (USA).sfc",
            replace=False,
            page_saw_candidate=True,
        )

        assert result is not None
        assert result["reason"] == "adoption_candidates"

    async def test_answering_it_downloads(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"),
                "/roms/snes/Game (USA).sfc",
                replace=True,
                page_saw_candidate=True,
            )
            is None
        )

    async def test_a_resume_is_never_refused_by_it(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"),
                "/roms/snes/Game (USA).sfc",
                replace=False,
                resume=True,
                page_saw_candidate=True,
            )
            is None
        )


class TestSearchUnderBothNames:
    """A ROM RomM serves as a folder around one differently-named file."""

    @staticmethod
    def _nested_single(h) -> dict[str, Any]:
        """The payload where the derived name and ``fs_name`` genuinely differ.

        ``has_nested_single_file`` is what makes ``resolve_local_file_name`` take
        the inner file's name; with exactly one file the download still takes the
        single-file path, so it writes ``Inner Disc.cue`` while ``fs_name`` — and
        the user's own copy — is named after the game. Without that flag all
        three wanted names collapse to one string and this class proves nothing.
        """
        h.system_extensions = {"psx": frozenset({".cue"})}
        detail = _single_file_detail(name="Game (USA)", size=10)
        detail["platform_slug"] = "psx"
        detail["has_nested_single_file"] = True
        detail["files"] = [{"file_name": "Inner Disc.cue"}]
        return detail

    async def test_the_user_s_copy_is_found_under_the_fs_name(self, h):
        # The search under the derived name alone finds nothing here: the copy is
        # named after the game, and no user names a file after the inner disc.
        detail = self._nested_single(h)
        h.store.files["/roms/psx/Game (U).cue"] = b"mine"

        result = await h.service.check_download_target(detail, "/roms/psx/Inner Disc.cue", replace=False)

        assert result is not None
        assert result["reason"] == "adoption_candidates"
        assert [candidate["name"] for candidate in result["candidates"]] == ["Game (U).cue"]

    async def test_the_derived_name_still_matches_where_it_is_the_one_on_disk(self, h):
        # The other half: a copy named after the inner file is found too, so
        # widening the search added a name rather than swapping one.
        detail = self._nested_single(h)
        h.store.files["/roms/psx/Inner Disc (U).cue"] = b"mine"

        result = await h.service.check_download_target(detail, "/roms/psx/Inner Disc.cue", replace=False)

        assert result is not None
        assert result["reason"] == "adoption_candidates"
        assert [candidate["name"] for candidate in result["candidates"]] == ["Inner Disc (U).cue"]

    async def test_an_unrelated_file_is_still_not_a_candidate(self, h):
        detail = self._nested_single(h)
        h.store.files["/roms/psx/Other Game (U).cue"] = b"not mine"

        assert await h.service.check_download_target(detail, "/roms/psx/Inner Disc.cue", replace=False) is None


class TestSearchLogging:
    """A divergence has to be reconstructible from the log afterwards."""

    async def test_the_click_search_records_what_it_looked_for_and_found(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        (line,) = [entry for entry in h.debug_log if entry.startswith("adopt search:")]
        assert "dir=/roms/snes" in line
        assert "'game'" in line
        assert "candidates=1" in line
        assert "unusable=0" in line
        assert "page_saw_candidate=False" in line

    async def test_the_page_probe_records_its_own_answer(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        h.service.has_adoption_candidate("snes", "Game (USA).sfc")

        (line,) = [entry for entry in h.debug_log if entry.startswith("adopt probe:")]
        assert "dir=/roms/snes" in line
        assert "name=game" in line
        assert "found=1" in line

    async def test_a_probe_with_no_name_to_match_still_says_what_it_answered(self, h):
        # Every exit from the probe leaves a line, including the ones that give
        # up before reading anything — a probe that logged nothing is a probe
        # that cannot be told apart from one that never ran.
        h.service.has_adoption_candidate("snes", "")

        (line,) = [entry for entry in h.debug_log if entry.startswith("adopt probe:")]
        assert "slug=snes" in line
        assert "name=<empty>" in line
        assert "found=0" in line

    async def test_a_name_that_is_only_tags_answers_the_same_way(self, h):
        # It has a name and still nothing to match on, which is the same answer
        # and now the same line.
        h.service.has_adoption_candidate("snes", "(USA).sfc")

        (line,) = [entry for entry in h.debug_log if entry.startswith("adopt probe:")]
        assert "name=<empty>" in line
        assert "found=0" in line

    async def test_every_exit_states_the_same_five_keys(self, h):
        # A log read across a divergence is unreadable if its shape changes per
        # exit — the reason this line exists is to be compared with the click
        # search's, and with the same call on another day. The game name carries
        # a space on purpose: a normalized name usually does, so a line that only
        # parses for single-word titles would be no shape at all.
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Example Quest (U).sfc"] = b"x"

        h.service.has_adoption_candidate("snes", "Example Quest (USA).sfc")
        h.service.has_adoption_candidate("snes", "")
        h.service.has_adoption_candidate("../escape", "Example Quest (USA).sfc")

        lines = [entry for entry in h.debug_log if entry.startswith("adopt probe:")]
        assert len(lines) == 3
        shape = re.compile(r"adopt probe: slug=(.*) dir=(.*) name=(.*) entries=(\d+) found=(\d+)")
        names: list[str] = []
        for line in lines:
            match = shape.fullmatch(line)
            assert match is not None, line
            names.append(match.group(3))
        assert names == ["example quest", "<empty>", "example quest"]


class TestWrongShapeNamesake:
    """A namesake of the wrong shape is asked about, never quietly downloaded past."""

    async def test_a_folder_where_the_server_sends_one_file_is_refused(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Game (U)")
        h.store.files["/roms/snes/Game (U)/rom.sfc"] = b"mine"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc", size=10), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["success"] is False
        assert result["reason"] == "unusable_namesake"
        assert result["message"] == (
            "'Game (U)' has this game's name but is a folder, and the server sends this game as a single file"
        )
        assert result["incoming"] == {"name": "Game (USA).sfc", "size_bytes": 10}
        assert result["existing"] == [{"name": "Game (U)", "path": "/roms/snes/Game (U)", "kind": "dir"}]
        assert result["served_is_dir"] is False
        assert result["truncated"] is False

    async def test_a_loose_file_where_the_server_sends_a_folder_is_refused(self, h):
        h.system_extensions = {"psx": frozenset({".cue"})}
        h.store.files["/roms/psx/Game (U).cue"] = b"mine"

        result = await h.service.check_download_target(
            _multi_file_detail(dir_name="Game (USA)"), "/roms/psx/Game (USA)", replace=False
        )

        assert result is not None
        assert result["reason"] == "unusable_namesake"
        assert result["message"] == (
            "'Game (U).cue' has this game's name but is a single file, and the server sends this game as a folder"
        )
        assert result["existing"] == [{"name": "Game (U).cue", "path": "/roms/psx/Game (U).cue", "kind": "file"}]
        assert result["served_is_dir"] is True

    async def test_the_refusal_touches_nothing(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Game (U)")
        h.store.files["/roms/snes/Game (U)/rom.sfc"] = b"mine"

        await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert h.store.files == {"/roms/snes/Game (U)/rom.sfc": b"mine"}
        assert h.store.dirs == {"/roms/snes/Game (U)"}

    async def test_the_user_who_chose_to_download_anyway_is_not_asked_again(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Game (U)")
        h.store.files["/roms/snes/Game (U)/rom.sfc"] = b"mine"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=True
            )
            is None
        )

    async def test_a_resume_is_never_refused_by_an_unusable_namesake(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Game (U)")
        h.store.files["/roms/snes/Game (U)/rom.sfc"] = b"mine"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False, resume=True
            )
            is None
        )

    async def test_a_folder_named_after_another_game_is_no_conflict(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Other Game (U)")
        h.store.files["/roms/snes/Other Game (U)/rom.sfc"] = b"z"

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )

    async def test_a_file_the_system_does_not_accept_is_no_conflict(self, h):
        # The extension filter answers before the shape question does: notes
        # beside a folder-served game are not something to ask about.
        h.system_extensions = {"psx": frozenset({".cue"})}
        h.store.files["/roms/psx/Game (U).txt"] = b"notes"

        assert (
            await h.service.check_download_target(
                _multi_file_detail(dir_name="Game (USA)"), "/roms/psx/Game (USA)", replace=False
            )
            is None
        )

    async def test_content_an_install_row_accounts_for_is_no_conflict(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.dirs.add("/roms/snes/Game (U)")
        h.store.files["/roms/snes/Game (U)/rom.sfc"] = b"other game"
        h.seed_install(rom_id=99, file_path="/roms/snes/Game (U)/rom.sfc", rom_dir="/roms/snes/Game (U)")

        assert (
            await h.service.check_download_target(
                _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
            )
            is None
        )

    async def test_a_candidate_of_the_right_shape_wins_over_one_of_the_wrong_shape(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"mine"
        h.store.dirs.add("/roms/snes/Game (E)")
        h.store.files["/roms/snes/Game (E)/rom.sfc"] = b"also mine"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "adoption_candidates"
        assert [candidate["name"] for candidate in result["candidates"]] == ["Game (U).sfc"]

    async def test_a_capped_list_says_so(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        for index in range(CANDIDATE_LIMIT + 1):
            h.store.dirs.add(f"/roms/snes/Game ({index})")
            h.store.files[f"/roms/snes/Game ({index})/rom.sfc"] = b"mine"

        result = await h.service.check_download_target(
            _single_file_detail(name="Game (USA).sfc"), "/roms/snes/Game (USA).sfc", replace=False
        )

        assert result is not None
        assert result["reason"] == "unusable_namesake"
        assert len(result["existing"]) == CANDIDATE_LIMIT
        assert result["truncated"] is True


class TestHasAdoptionCandidate:
    """The game-detail read's half of the search — a boolean, and never a raise."""

    async def test_a_matching_file_is_a_candidate(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is True

    async def test_a_folder_of_other_games_is_not(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Other Game (USA).sfc"] = b"z"

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is False

    async def test_a_matching_directory_counts_too(self, h):
        # The page cannot know whether RomM serves this ROM as one file or a
        # folder, so it does not filter on shape — the click-time search does.
        h.system_extensions = {"psx": frozenset()}
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"x"

        assert h.service.has_adoption_candidate("psx", "Game.zip") is True

    async def test_the_rom_s_own_target_is_not_its_own_candidate(self, h):
        # That is the occupied-target state, which the page answers separately.
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game.sfc"] = b"x"

        assert h.service.has_adoption_candidate("snes", "Game.sfc") is False

    async def test_an_install_row_s_content_is_another_game_s(self, h):
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files["/roms/snes/Game (U).sfc"] = b"x"
        h.seed_install(rom_id=99, file_path="/roms/snes/Game (U).sfc")

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is False

    async def test_it_never_reads_an_archive_index(self, h):
        # Ranking evidence is the dialog's cost, not the page's: the page only has
        # to say "something is here", never which of several is strongest.
        h.system_extensions = {"snes": frozenset({".zip"})}
        h.store.files["/roms/snes/Game (U).zip"] = _zip_bytes({"Game.sfc": b"cartridge"})
        opened: list[str] = []

        def record(path):
            opened.append(path)
            return

        h.store.list_archive_members = record

        assert h.service.has_adoption_candidate("snes", "Game (USA).zip") is True
        assert opened == []

    async def test_a_missing_roms_path_answers_quietly(self, h):
        h.paths.roms = ""

        assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is False

    async def test_a_read_that_raises_answers_quietly(self, h, caplog):
        # A search that could not run must never make a game look uninstallable.
        # The probe's own listing is the lean one — the fake no longer projects
        # it from the full one, so breaking the wrong method proves nothing.
        def boom(_directory):
            raise OSError("SD card ejected")

        h.store.list_top_level_names = boom

        with caplog.at_level(logging.WARNING):
            assert h.service.has_adoption_candidate("snes", "Game (USA).sfc") is False

        assert any("candidate probe failed" in record.message for record in caplog.records)

    async def test_an_unsafe_platform_slug_answers_quietly(self, h):
        assert h.service.has_adoption_candidate("../../etc", "passwd") is False

        # Refused where the directory is derived rather than thrown at the
        # blanket guard, so the log says a probe ran and found nowhere to look —
        # False is also its "nothing here" answer, and the line is what tells
        # the two apart afterwards.
        assert any(entry.startswith("adopt probe:") and "dir=unresolved" in entry for entry in h.debug_log)


# ── downloading over a candidate ─────────────────────────────────────────


_OLD = "/roms/snes/Game (U).sfc"
_NEW = "/roms/snes/Game.sfc"


class TestDiscardCandidate:
    """The dialog's second confirmation names a deletion, so the deletion happens."""

    @staticmethod
    def _stage(h) -> None:
        h.system_extensions = {"snes": frozenset({".sfc"})}
        h.store.files[_OLD] = b"user's own dump"

    async def test_downloading_over_a_candidate_removes_it(self, h):
        self._stage(h)

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is None
        assert _OLD not in h.store.files

    async def test_its_saves_and_savestates_arrive_under_the_canonical_name(self, h):
        self._stage(h)
        h.store.files["/saves/snes/Game (U).srm"] = b"battery"
        h.store.files["/states/Game (U).state"] = b"snapshot"

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is None
        assert h.store.files["/saves/snes/Game.srm"] == b"battery"
        assert h.store.files["/states/Game.state"] == b"snapshot"
        assert "/saves/snes/Game (U).srm" not in h.store.files

    async def test_none_of_these_removes_nothing(self, h):
        # The user declined every candidate rather than choosing one, so no
        # particular file was the subject and none may be deleted for them.
        self._stage(h)
        h.store.files["/saves/snes/Game (U).srm"] = b"battery"

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True)

        assert result is None
        assert h.store.files[_OLD] == b"user's own dump"
        assert h.store.files["/saves/snes/Game (U).srm"] == b"battery"

    async def test_a_failed_removal_aborts_with_the_candidate_intact(self, h):
        self._stage(h)
        h.store.remove_failures = {_OLD}

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is not None
        assert result["success"] is False
        assert result["reason"] == "replace_failed"
        assert h.store.files[_OLD] == b"user's own dump"

    async def test_a_failed_removal_says_the_saves_have_already_moved(self, h):
        # Carry-then-remove means the second step can fail over a first step that
        # succeeded. The file the user keeps can no longer find its saves, so a
        # bare "download aborted" would be the abort reporting itself as clean.
        self._stage(h)
        h.store.files["/saves/snes/Game (U).srm"] = b"battery"
        h.store.remove_failures = {_OLD}

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is not None
        assert result["reason"] == "replace_failed"
        assert "Game.srm" in result["message"]
        assert h.store.files[_OLD] == b"user's own dump"
        assert h.store.files["/saves/snes/Game.srm"] == b"battery"

    async def test_a_failed_removal_with_no_saves_says_nothing_about_them(self, h):
        self._stage(h)
        h.store.remove_failures = {_OLD}

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is not None
        assert result["reason"] == "replace_failed"
        assert "already renamed" not in result["message"]

    async def test_a_taken_save_name_raises_the_same_collision_question(self, h):
        self._stage(h)
        h.store.files["/saves/snes/Game (U).srm"] = b"mine"
        h.store.files["/saves/snes/Game.srm"] = b"the other version's"

        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is not None
        assert result["reason"] == "rename_collisions"
        assert [c["path"] for c in result["collisions"]] == ["/saves/snes/Game.srm"]
        # Nothing moved and nothing removed while the question is open.
        assert h.store.files[_OLD] == b"user's own dump"
        assert h.store.files["/saves/snes/Game.srm"] == b"the other version's"

    async def test_the_collision_answer_completes_the_discard(self, h):
        self._stage(h)
        h.store.files["/saves/snes/Game (U).srm"] = b"mine"
        h.store.files["/saves/snes/Game.srm"] = b"the other version's"

        result = await h.service.check_download_target(
            _single_file_detail(), _NEW, replace=True, candidate_path=_OLD, collision_choice="overwrite"
        )

        assert result is None
        assert h.store.files["/saves/snes/Game.srm"] == b"mine"
        assert _OLD not in h.store.files

    async def test_keep_leaves_the_old_saves_and_still_removes_the_candidate(self, h):
        self._stage(h)
        h.store.files["/saves/snes/Game (U).srm"] = b"mine"
        h.store.files["/saves/snes/Game.srm"] = b"the other version's"

        result = await h.service.check_download_target(
            _single_file_detail(), _NEW, replace=True, candidate_path=_OLD, collision_choice="keep"
        )

        assert result is None
        assert h.store.files["/saves/snes/Game.srm"] == b"the other version's"
        assert h.store.files["/saves/snes/Game (U).srm"] == b"mine"
        assert _OLD not in h.store.files

    async def test_a_candidate_outside_the_platform_folder_is_refused(self, h):
        h.store.files["/roms/gba/Game (U).sfc"] = b"different platform"

        result = await h.service.check_download_target(
            _single_file_detail(), _NEW, replace=True, candidate_path="/roms/gba/Game (U).sfc"
        )

        assert result is not None
        assert result["reason"] == "invalid_candidate"
        assert h.store.files["/roms/gba/Game (U).sfc"] == b"different platform"

    async def test_a_candidate_that_vanished_lets_the_download_proceed(self, h):
        result = await h.service.check_download_target(_single_file_detail(), _NEW, replace=True, candidate_path=_OLD)

        assert result is None

    async def test_a_multi_file_candidate_goes_but_its_saves_stay_put(self, h):
        # The downloaded directory's launch file is inside an archive that has not
        # been fetched, so the name those saves would need is unknown. Untouched
        # and findable beats moved to a name nothing reads.
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"cue"
        h.store.files["/saves/Game (U)/disc.srm"] = b"battery"

        result = await h.service.check_download_target(
            _multi_file_detail(dir_name="Game"), "/roms/psx/Game", replace=True, candidate_path="/roms/psx/Game (U)"
        )

        assert result is None
        assert "/roms/psx/Game (U)/disc.cue" not in h.store.files
        assert h.store.files["/saves/Game (U)/disc.srm"] == b"battery"


# ── adopting a candidate ─────────────────────────────────────────────────


class TestAdoptCandidate:
    async def test_the_candidate_is_renamed_and_recorded_at_the_canonical_name(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"user's own dump"

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is True
        assert result["file_path"] == _NEW
        assert h.move.moves == [(_OLD, _NEW)]

    async def test_a_save_and_a_savestate_travel_with_it(self, h):
        # The stock RetroDECK shape: savefiles content-sorted under
        # saves/<system>, savestates not sorted at all.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"srm"
        h.store.files["/states/Game (U).state"] = b"state"

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is True
        assert sorted(h.move.moves) == sorted(
            [
                (_OLD, _NEW),
                ("/saves/snes/Game (U).srm", "/saves/snes/Game.srm"),
                ("/states/Game (U).state", "/states/Game.state"),
            ]
        )

    async def test_the_savestate_layout_is_read_separately_from_the_savefile_one(self, h):
        # Assuming one from the other would look under /states/snes, where a
        # stock RetroDECK install has never written anything.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files["/states/snes/Game (U).state"] = b"wrong dir"
        h.store.files["/states/Game (U).state"] = b"state"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/states/Game (U).state", "/states/Game.state") in h.move.moves
        assert not any(source.startswith("/states/snes/") for source, _target in h.move.moves)

    async def test_another_game_s_save_is_never_carried_along(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"srm"
        h.store.files["/saves/snes/Other Game (U).srm"] = b"not mine"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert all("Other Game" not in source for source, _target in h.move.moves)

    async def test_a_rom_with_no_saves_at_all_still_moves(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is True
        assert h.move.moves == [(_OLD, _NEW)]

    async def test_saves_written_beside_the_rom_are_carried_without_moving_the_rom_twice(self, h):
        # ``savefiles_in_content_dir=true`` puts the saves in the platform folder
        # itself, which is also where the ROM is — so the ROM's own filename
        # matches the stem prefix and would be paired a second time.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.save_layout = ContentDir()
        h.store.files[_OLD] = b"rom"
        h.store.files["/roms/snes/Game (U).srm"] = b"srm"

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is True
        assert h.move.moves == [(_OLD, _NEW), ("/roms/snes/Game (U).srm", "/roms/snes/Game.srm")]

    async def test_a_directory_rom_s_own_saves_travel_inside_it_and_are_not_paired(self, h):
        # A multi-file ROM whose emulator writes beside the game: those files move
        # as part of the directory's own rename, and pairing them would move them
        # a second time — out of a directory that no longer exists.
        h.seed_rom()
        h.stage_detail(_multi_file_detail(dir_name="Game"))
        h.save_layout = ContentDir()
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"cue"
        h.store.files["/roms/psx/Game (U)/disc.srm"] = b"srm"

        result = await h.service.adopt_existing_rom(_ROM_ID, "/roms/psx/Game (U)", None)

        assert result["success"] is True
        assert h.move.moves == [("/roms/psx/Game (U)", "/roms/psx/Game")]

    async def test_a_content_sorted_directory_rom_moves_its_whole_save_folder(self, h):
        h.seed_rom()
        h.stage_detail(_multi_file_detail(dir_name="Game"))
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/roms/psx/Game (U)/disc.cue"] = b"cue"
        h.store.files["/saves/Game (U)/disc.srm"] = b"srm"

        result = await h.service.adopt_existing_rom(_ROM_ID, "/roms/psx/Game (U)", None)

        assert result["success"] is True
        assert ("/saves/Game (U)/disc.srm", "/saves/Game/disc.srm") in h.move.moves

    async def test_the_savefile_directory_is_the_one_the_sync_resolves(self, h):
        # No migration pending: the two sources agree, and adoption reaches the
        # same directory `find_save_files` would.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"srm"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/saves/snes/Game (U).srm", "/saves/snes/Game.srm") in h.move.moves

    async def test_a_pending_save_sort_migration_keeps_the_rename_in_the_old_layout(self, h):
        # The regression this pins. While a save-sort migration is pending the
        # files are still in the PREVIOUS layout and the sync deliberately keeps
        # looking there (#238). Reading the live config would move them to
        # /saves/Game (U)/ — out from under the sync, and out from under the
        # pending migration that is about to go looking for them.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.save_layout = InSaveDir(sort_by_content=False, sort_by_core=False)
        h.save_sorting = InSaveDir(sort_by_content=True, sort_by_core=False)
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"recorded layout"
        h.store.files["/saves/Game (U).srm"] = b"live-config layout"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/saves/snes/Game (U).srm", "/saves/snes/Game.srm") in h.move.moves
        assert all(not source.startswith("/saves/Game (U)") for source, _target in h.move.moves)
        assert h.store.files["/saves/Game (U).srm"] == b"live-config layout"

    async def test_a_savefile_migration_never_moves_the_savestates(self, h):
        # The markers track savefile sorting only, so a pending savefile
        # migration says nothing about savestates: they keep coming from the
        # live config, whatever the recorded savefile layout says.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.save_layout = InSaveDir(sort_by_content=False, sort_by_core=False)
        h.save_sorting = InSaveDir(sort_by_content=True, sort_by_core=False)
        h.store.files[_OLD] = b"rom"
        h.store.files["/states/Game (U).state"] = b"state"
        h.store.files["/states/snes/Game (U).state"] = b"not where states live"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/states/Game (U).state", "/states/Game.state") in h.move.moves
        assert h.store.files["/states/snes/Game (U).state"] == b"not where states live"

    async def test_the_content_dir_question_still_comes_from_the_live_config(self, h):
        # MigrationService writes no marker for a ContentDir machine, so there is
        # nothing recorded to prefer — and a recorded sorting must not override
        # the live "saves sit next to the ROM".
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.save_layout = ContentDir()
        h.save_sorting = InSaveDir(sort_by_content=True, sort_by_core=False)
        h.store.files[_OLD] = b"rom"
        h.store.files["/roms/snes/Game (U).srm"] = b"beside the rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"not read on this machine"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/roms/snes/Game (U).srm", "/roms/snes/Game.srm") in h.move.moves
        assert h.store.files["/saves/snes/Game (U).srm"] == b"not read on this machine"

    async def test_per_core_sorting_reaches_the_core_subdirectory(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.save_sorting = InSaveDir(sort_by_content=True, sort_by_core=True)
        h.active_core.default = ("snes9x_libretro", "Snes9x")
        h.core_name = "Snes9x"
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Snes9x/Game (U).srm"] = b"srm"

        await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/saves/snes/Snes9x/Game (U).srm", "/saves/snes/Snes9x/Game.srm") in h.move.moves

    async def test_an_unresolvable_corename_looks_where_save_sync_looks(self, h, caplog):
        # Warn-and-fall-back, exactly as RomInfoService does with the same
        # question, so the rename and the sync never disagree about the directory.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.save_sorting = InSaveDir(sort_by_content=True, sort_by_core=True)
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"srm"

        with caplog.at_level(logging.WARNING):
            await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert ("/saves/snes/Game (U).srm", "/saves/snes/Game.srm") in h.move.moves
        assert any("corename" in record.message for record in caplog.records)

    async def test_a_candidate_outside_the_platform_folder_is_refused(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files["/roms/gba/Game (U).sfc"] = b"rom"

        result = await h.service.adopt_existing_rom(_ROM_ID, "/roms/gba/Game (U).sfc", None)

        assert result["success"] is False
        assert result["reason"] == "invalid_candidate"
        assert h.move.moves == []

    async def test_a_traversing_candidate_path_is_refused(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())

        result = await h.service.adopt_existing_rom(_ROM_ID, "/roms/snes/../../etc/passwd", None)

        assert result["success"] is False
        assert result["reason"] == "invalid_candidate"
        assert h.move.moves == []

    async def test_a_candidate_that_vanished_is_refused_before_anything_moves(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is False
        assert result["reason"] == "nothing_to_adopt"
        assert h.move.moves == []
        assert h.superseded == []

    async def test_content_arriving_at_the_canonical_name_refuses_the_rename(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files[_NEW] = b"someone else's"

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is False
        assert result["reason"] == "target_taken"
        assert h.move.moves == []
        assert h.store.files[_NEW] == b"someone else's"

    async def test_content_arriving_between_the_check_and_the_move_refuses_too(self, h):
        # The window between validating and planning is short but real, and what
        # lands in it is the occupied-target case the other dialog owns — never
        # something to overwrite or skip as part of the collision question.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        seen: list[str] = []
        free_until_planned = h.move.exists

        def exists(path: str) -> bool:
            if path == _NEW and _NEW not in seen:
                seen.append(_NEW)
                return False
            return free_until_planned(path) or path == _NEW

        h.move.exists = exists

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is False
        assert result["reason"] == "target_taken"
        assert h.move.moves == []
        assert h.store.files[_OLD] == b"rom"

    async def test_a_directory_candidate_with_nothing_launchable_inside_still_moves(self, h):
        # No launch file means no stem, and an empty stem must claim no saves
        # rather than every file in the save directory.
        h.seed_rom()
        h.stage_detail(_multi_file_detail(dir_name="Game"))
        h.store.dirs.add("/roms/psx/Game (U)")
        h.store.files["/saves/psx/Game (U).srm"] = b"someone else's"

        result = await h.service.adopt_existing_rom(_ROM_ID, "/roms/psx/Game (U)", None)

        assert result["success"] is True
        assert h.move.moves == [("/roms/psx/Game (U)", "/roms/psx/Game")]

    async def test_the_supersede_runs_only_after_the_rename_succeeded(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.move.outcome = {"moved": [], "stranded": [], "unmoved": [_OLD], "error": "disk on fire"}

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is False
        assert result["reason"] == "rename_failed"
        assert h.superseded == []

    async def test_a_partial_move_names_what_arrived_and_what_did_not(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"srm"
        h.move.outcome = {
            "moved": [_NEW],
            "stranded": [],
            "unmoved": ["/saves/snes/Game (U).srm"],
            "error": "could not move Game (U).srm",
        }

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is False
        assert "Game (U).srm" in result["message"]
        assert "Game.sfc" in result["message"]

    async def test_a_stranded_old_copy_is_not_a_failure(self, h):
        # One inode under two names loses nothing and a re-run finishes it, so the
        # user's game is playable and the dialog says nothing alarming.
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.move.outcome = {"moved": [], "stranded": [_OLD], "unmoved": [], "error": "old copies remain"}

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is True
        assert result["file_path"] == _NEW
        assert h.store.files[_NEW] == b"rom"
        assert h.store.files[_OLD] == b"rom"


class TestAdoptCandidateCollisions:
    @staticmethod
    def _stage(h) -> None:
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.store.files["/saves/snes/Game (U).srm"] = b"mine"
        h.store.files["/saves/snes/Game.srm"] = b"the other version's"
        h.store.files["/states/Game (U).state"] = b"mine"
        h.store.files["/states/Game.state"] = b"the other version's"

    async def test_an_unanswered_collision_refuses_before_a_single_file_moves(self, h):
        self._stage(h)

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["success"] is False
        assert result["reason"] == "rename_collisions"
        assert h.move.moves == []
        assert h.quarantine.quarantined == []

    async def test_every_collision_is_listed_not_just_the_first(self, h):
        self._stage(h)

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert [collision["path"] for collision in result["collisions"]] == [
            "/saves/snes/Game.srm",
            "/states/Game.state",
        ]
        assert [collision["kind"] for collision in result["collisions"]] == ["save", "savestate"]

    async def test_overwrite_replaces_the_occupied_names_then_moves_everything(self, h):
        self._stage(h)

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "overwrite")

        assert result["success"] is True
        # Replaced, not destroyed: both go through the sanctioned .romm-backup
        # funnel, so a save the user chose to lose is still recoverable.
        assert h.quarantine.quarantined == ["/saves/snes/Game.srm", "/states/Game.state"]
        assert h.store.files["/saves/snes/.romm-backup/Game.srm"] == b"the other version's"
        assert h.store.files["/states/.romm-backup/Game.state"] == b"the other version's"
        assert sorted(h.move.moves) == sorted(
            [
                (_OLD, _NEW),
                ("/saves/snes/Game (U).srm", "/saves/snes/Game.srm"),
                ("/states/Game (U).state", "/states/Game.state"),
            ]
        )

    async def test_keep_leaves_the_occupied_names_and_their_old_named_files_alone(self, h):
        self._stage(h)

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "keep")

        assert result["success"] is True
        assert h.quarantine.quarantined == []
        assert h.move.moves == [(_OLD, _NEW)]
        assert h.store.files["/saves/snes/Game (U).srm"] == b"mine"
        assert h.store.files["/states/Game (U).state"] == b"mine"
        assert h.store.files["/saves/snes/Game.srm"] == b"the other version's"

    async def test_a_replace_that_fails_moves_nothing_and_names_what_went(self, h):
        self._stage(h)
        h.quarantine.failures = {"/states/Game.state"}

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "overwrite")

        assert result["success"] is False
        assert result["reason"] == "replace_failed"
        assert "Game.state" in result["message"]
        # The one that did go is named, so the user is not told nothing happened.
        assert "Game.srm" in result["message"]
        assert h.move.moves == []
        assert h.store.files[_OLD] == b"rom"
        assert h.store.files["/saves/snes/.romm-backup/Game.srm"] == b"the other version's"

    async def test_an_unrecognised_answer_is_refused_rather_than_guessed(self, h):
        self._stage(h)

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "delete everything")

        assert result["success"] is False
        assert result["reason"] == "rename_collisions"
        assert h.move.moves == []

    async def test_a_move_that_fails_after_the_clear_says_where_the_replaced_saves_went(self, h):
        # The clear succeeded, so the other version's saves are in .romm-backup
        # for a replacement that never arrived. Reporting only the rename failure
        # would leave them nowhere in the message and nowhere the user would look.
        self._stage(h)
        h.move.outcome = {
            "moved": [],
            "stranded": [],
            "unmoved": [_OLD, "/saves/snes/Game (U).srm", "/states/Game (U).state"],
            "error": "disk on fire",
        }

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "overwrite")

        assert result["success"] is False
        assert result["reason"] == "rename_failed"
        assert ".romm-backup" in result["message"]
        assert "Game.srm" in result["message"]
        assert "Game.state" in result["message"]
        assert h.quarantine.quarantined == ["/saves/snes/Game.srm", "/states/Game.state"]

    async def test_a_move_that_fails_with_nothing_replaced_says_nothing_about_backups(self, h):
        h.seed_rom()
        h.stage_detail(_single_file_detail())
        h.store.files[_OLD] = b"rom"
        h.move.outcome = {"moved": [], "stranded": [], "unmoved": [_OLD], "error": "disk on fire"}

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, None)

        assert result["reason"] == "rename_failed"
        assert ".romm-backup" not in result["message"]

    async def test_a_folder_at_a_collision_target_is_refused_before_anything_moves(self, h):
        # The up-front `_not_a_file` pass, which the funnel is never reached past:
        # a folder at a savestate's name would otherwise no-op the clear and fail
        # later at the link with nothing explaining why.
        self._stage(h)
        h.store.files.pop("/states/Game.state")
        h.store.dirs.add("/states/Game.state")
        h.store.files["/states/Game.state/stray"] = b"a folder at a savestate's name"

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "overwrite")

        assert result["success"] is False
        assert result["reason"] == "replace_failed"
        assert "Game.state" in result["message"]
        # Refused up front: the savefile beside it was never quarantined either.
        assert h.quarantine.quarantined == []
        assert h.move.moves == []
        assert h.store.files["/saves/snes/Game.srm"] == b"the other version's"

    async def test_a_collision_target_that_vanished_is_never_named_as_replaced(self, h):
        # Present at the plan's exists() probe, gone by the time the funnel looks:
        # it reports False and moves nothing, so nothing may claim it did.
        #
        # The production list is observable only inside a refusal, so this stages
        # one — asserting on the fake's own record instead would be a tautology
        # about the fake (it returns False *before* it appends), green whatever
        # the renamer does with the return value.
        self._stage(h)
        h.quarantine.missing = {"/states/Game.state"}
        h.move.outcome = {
            "moved": [],
            "stranded": [],
            "unmoved": [_OLD, "/saves/snes/Game (U).srm", "/states/Game (U).state"],
            "error": "disk on fire",
        }

        result = await h.service.adopt_existing_rom(_ROM_ID, _OLD, "overwrite")

        assert result["success"] is False
        assert result["reason"] == "rename_failed"
        assert "moved to .romm-backup" in result["message"]
        assert "Game.srm" in result["message"]
        # The declined one is absent from the whole message, backup clause
        # included. `Game (U).state` in the unmoved list does not contain it.
        assert "Game.state" not in result["message"]
