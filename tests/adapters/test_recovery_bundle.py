from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from adapters import descriptor_paths, recovery_bundle
from adapters.recovery_bundle import RecoveryBundleAdapter
from lib.errors import OperationAbortedError

if TYPE_CHECKING:
    from models.prune import RecoveryArtifact

    from domain.prune import BundleReadmeContext


def _adapter(tmp_path) -> RecoveryBundleAdapter:
    return RecoveryBundleAdapter(user_home=str(tmp_path), package_name="decky romm/sync", plugin_version="1.2.3")


def _create_layout(adapter: RecoveryBundleAdapter) -> None:
    """Materialize ``staging`` / ``bundles``; ``free_bytes`` deliberately does not."""
    adapter._close_layout(adapter._open_layout(create=True))


def _reseal(sealed: Path) -> None:
    """Re-derive ``checksums.sha256`` and ``SEAL.json`` over the bundle's current bytes."""
    checksums = sealed / "checksums.sha256"
    lines = []
    for raw in checksums.read_text().splitlines():
        relative = raw.split("  ", 1)[1]
        digest = hashlib.sha256((sealed / relative).read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative}\n")
    checksums.write_text("".join(sorted(lines)))
    seal_path = sealed / "SEAL.json"
    seal = json.loads(seal_path.read_text())
    seal["checksums_sha256"] = hashlib.sha256(checksums.read_bytes()).hexdigest()
    seal_path.write_text(json.dumps(seal, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def _readme_context(bundle_id: str = "TestGame_2026-07-24_abc123") -> BundleReadmeContext:
    return {
        "bundle_id": bundle_id,
        "created_at": "2026-07-24T12:00:00+00:00",
        "games": [
            {
                "rom_id": 7,
                "name": "Test Game",
                "fs_name": "Test Game.chd",
                "platform_slug": "dc",
                "role": "removed by this cleanup",
            }
        ],
        "playtime_lines": ["Test Game (ROM 7): 894 seconds — 0h 14m 54s"],
    }


def _snapshot() -> dict[str, object]:
    return {
        "roms": [{"rom_id": 7, "name": "Game"}],
        "installs": [],
        "metadata": [],
        "save_sync": [],
        "playtime": [],
        "warnings": [],
    }


def test_seals_verified_bundle_with_generated_destinations(tmp_path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    nested = source_root / "server supplied name"
    nested.mkdir()
    (nested / "disc 1.bin").write_bytes(b"rom")
    adapter = _adapter(tmp_path)

    sealed = Path(
        adapter.seal_bundle(
            "TestGame_2026-07-24_abc123",
            _snapshot(),
            [{"source_path": str(nested), "safe_root": str(source_root), "kind": "installed_rom", "rom_id": 7}],
            _readme_context(),
            "7 seconds\n",
        )
    )

    assert sealed.name == "TestGame_2026-07-24_abc123"
    assert (sealed / "SEAL.json").exists()
    manifest = json.loads((sealed / "manifest.json").read_text())
    assert manifest["plugin_version"] == "1.2.3"
    assert manifest["artifacts"][0]["destination"] == "files/000001"
    assert manifest["artifacts"][0]["source_path"].endswith("disc 1.bin")
    assert (sealed / "files" / "000001").read_bytes() == b"rom"
    assert (sealed / "roms" / "7" / "state.json").exists()
    assert "files/000001" in (sealed / "checksums.sha256").read_text()

    # The README is the manual-restore surface, so it has to name the real game,
    # the real blob, and the real absolute path the blob came from.
    readme = (sealed / "README.txt").read_text()
    assert "Test Game" in readme
    assert "Test Game.chd" in readme
    assert "removed by this cleanup" in readme
    assert "files/000001" in readme
    assert str(nested / "disc 1.bin") in readme
    assert "downloaded ROM content" in readme
    assert "894 seconds" in readme
    assert "sha256sum -c checksums.sha256" in readme
    # The README is covered by the seal like every other file in the bundle.
    assert "README.txt" in (sealed / "checksums.sha256").read_text()


def test_recovery_root_explains_itself_once_it_exists(tmp_path):
    adapter = _adapter(tmp_path)

    adapter.seal_bundle("TestGame_2026-07-24_root01", _snapshot(), [], _readme_context(), "none\n")

    root_readme = Path(adapter.root()) / "README.txt"
    assert root_readme.is_file()
    assert "bundles/" in root_readme.read_text()
    assert "there is no restore button" in root_readme.read_text()


def test_reading_free_space_still_creates_no_recovery_root(tmp_path):
    adapter = _adapter(tmp_path)

    assert adapter.free_bytes() > 0

    # L17: a preview must never bring the recovery layout — or its README —
    # into existence just by showing how much space is free.
    assert not Path(adapter.root()).exists()


def test_rejects_path_escape_symlink_and_duplicate_identity(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    link = safe / "link.bin"
    link.symlink_to(outside)
    adapter = _adapter(tmp_path)
    with pytest.raises(ValueError, match=r"outside|symlink"):
        adapter.measure_path(str(link), str(safe))
    with pytest.raises(ValueError, match="outside"):
        adapter.measure_path(str(outside), str(safe))
    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(ValueError, match="unsafe recovery bundle id"):
        adapter.seal_bundle("../escape", snapshot, [], readme, "playtime")

    bundle_id = "TestGame_2026-07-24_same"
    adapter.seal_bundle(bundle_id, _snapshot(), [], _readme_context(), "playtime")
    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(FileExistsError):
        adapter.seal_bundle(bundle_id, snapshot, [], readme, "playtime")


def test_failed_copy_cleans_staging_without_touching_existing_bundle(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    adapter = _adapter(tmp_path)

    def fail_copy(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(adapter, "_copy_opened_source", fail_copy)
    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(OSError, match="disk full"):
        adapter.seal_bundle(
            "TestGame_2026-07-24_failure",
            snapshot,
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            readme,
            "playtime",
        )
    staging = Path(adapter.root()) / "staging"
    assert list(staging.iterdir()) == []


def test_sealed_bundle_revalidates_manifest_and_source_bytes(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"before")
    adapter = _adapter(tmp_path)
    sealed = adapter.seal_bundle(
        "TestGame_2026-07-24_validate",
        _snapshot(),
        [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
        _readme_context(),
        "playtime",
    )

    assert adapter.validate_sources(sealed) is True
    source.write_bytes(b"after")
    assert adapter.validate_sources(sealed) is False


def test_same_byte_source_replacement_fails_sealed_identity_validation(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"same bytes")
    adapter = _adapter(tmp_path)
    sealed = adapter.seal_bundle(
        "TestGame_2026-07-24_identity",
        _snapshot(),
        [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
        _readme_context(),
        "playtime",
    )
    source.unlink()
    source.write_bytes(b"same bytes")

    assert adapter.validate_sources(sealed) is False
    source.write_bytes(b"before")
    (Path(sealed) / "manifest.json").write_text("{}")
    assert adapter.validate_sources(sealed) is False


def test_sealed_bundle_rejects_file_that_appears_in_an_initially_empty_source_set(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "late-grid.png"
    adapter = _adapter(tmp_path)
    sealed = adapter.seal_bundle(
        "TestGame_2026-07-24_late",
        _snapshot(),
        [{"source_path": str(source), "safe_root": str(safe), "kind": "steam_grid"}],
        _readme_context(),
        "playtime",
    )

    assert adapter.validate_sources(sealed) is True
    source.write_bytes(b"new")
    assert adapter.validate_sources(sealed) is False


def test_source_replacement_with_symlink_fails_at_open_time(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"inside")
    outside = tmp_path / "outside.srm"
    outside.write_bytes(b"outside")
    adapter = _adapter(tmp_path)
    original = adapter._open_regular_beneath
    replaced = False

    def replace_before_open(path: str, safe_root: str) -> int:
        nonlocal replaced
        if path == str(source) and not replaced:
            replaced = True
            source.unlink()
            source.symlink_to(outside)
        return original(path, safe_root)

    monkeypatch.setattr(adapter, "_open_regular_beneath", replace_before_open)
    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises((OSError, ValueError)):
        adapter.seal_bundle(
            "TestGame_2026-07-24_replaced",
            snapshot,
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            readme,
            "playtime",
        )


def test_real_directory_fsync_failure_aborts_sealing(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    original = os.fsync

    def fail_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        original(fd)

    monkeypatch.setattr("adapters.recovery_bundle.os.fsync", fail_directory)
    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(OSError, match="directory fsync failed"):
        adapter.seal_bundle("TestGame_2026-07-24_fsync", snapshot, [], readme, "playtime")


def test_symlinked_recovery_root_is_rejected(tmp_path):
    adapter = _adapter(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    Path(adapter.root()).symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="trusted directory"):
        adapter.free_bytes()


@pytest.mark.parametrize(
    ("field", "value"),
    [("sealed", False), ("bundle_id", "other"), ("file_count", 99)],
)
def test_seal_contract_fields_are_revalidated(tmp_path, field, value):
    adapter = _adapter(tmp_path)
    sealed = Path(adapter.seal_bundle("TestGame_2026-07-24_sealfields", _snapshot(), [], _readme_context(), "playtime"))
    seal_path = sealed / "SEAL.json"
    seal = json.loads(seal_path.read_text())
    seal[field] = value
    seal_path.write_text(json.dumps(seal))

    assert adapter.validate_sources(str(sealed)) is False


def test_new_recovery_directories_fsync_each_parent(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    synced: list[int] = []
    original = os.fsync

    def track(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        original(fd)

    monkeypatch.setattr("adapters.recovery_bundle.os.fsync", track)

    adapter.seal_bundle("TestGame_2026-07-24_newparents", _snapshot(), [], _readme_context(), "playtime")

    root = Path(adapter.root())
    assert tmp_path.stat().st_ino in synced
    assert root.stat().st_ino in synced
    assert (root / "staging").stat().st_ino in synced
    assert (root / "bundles").stat().st_ino in synced


def test_post_rename_fsync_failure_marks_bundle_durability_uncertain(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _create_layout(adapter)
    bundles = Path(adapter.root()) / "bundles"
    bundles_inode = bundles.stat().st_ino
    original = os.fsync

    def fail_final_sync(fd: int) -> None:
        if os.fstat(fd).st_ino == bundles_inode:
            raise OSError(errno.EIO, "directory fsync failed")
        original(fd)

    monkeypatch.setattr("adapters.recovery_bundle.os.fsync", fail_final_sync)
    bundle_id = "TestGame_2026-07-24_uncertain"
    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(OSError, match="durability is uncertain"):
        adapter.seal_bundle(bundle_id, snapshot, [], readme, "playtime")

    assert not (bundles / bundle_id).exists()
    assert (bundles / f"{bundle_id}.durability-uncertain").is_dir()


def test_recovery_bundle_parent_replacement_is_not_reauthorized(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _create_layout(adapter)
    bundles = Path(adapter.root()) / "bundles"
    detached = Path(adapter.root()) / "detached-bundles"
    outside = tmp_path / "outside-bundles"
    outside.mkdir()
    original = adapter._copy_artifacts

    def swap_parent(staging, artifacts, free_bytes, should_abort=None):
        result = original(staging, artifacts, free_bytes)
        bundles.rename(detached)
        bundles.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(adapter, "_copy_artifacts", swap_parent)

    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(OSError, match="durability is uncertain") as caught:
        adapter.seal_bundle("TestGame_2026-07-24_swapped", snapshot, [], readme, "playtime")

    assert list(outside.iterdir()) == []
    preserved = Path(str(caught.value).split(": ", 1)[1])
    assert preserved.parent == detached
    assert (preserved / "SEAL.json").is_file()


def test_failed_seal_preserves_staging_instead_of_crossing_nested_mount(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _create_layout(adapter)
    module = __import__("adapters.descriptor_paths", fromlist=["_mount_id"])
    original_mount_id = module._mount_id
    staging_path: Path | None = None

    def fake_mount_id(fd: int) -> int:
        target = os.readlink(f"/proc/self/fd/{fd}")
        return original_mount_id(fd) + (1 if target.endswith("/mounted") else 0)

    def add_mount_transition_then_fail(staging_fd: int, checksums, should_abort=None) -> None:
        nonlocal staging_path
        del checksums
        staging_path = Path(os.readlink(f"/proc/self/fd/{staging_fd}"))
        mounted = staging_path / "mounted"
        mounted.mkdir()
        (mounted / "outside-marker").write_bytes(b"keep")
        raise OSError("injected seal failure")

    monkeypatch.setattr("adapters.descriptor_paths._mount_id", fake_mount_id)
    monkeypatch.setattr(adapter, "_verify_staging_checksums", add_mount_transition_then_fail)

    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(RuntimeError, match="unsafe staging was preserved") as caught:
        adapter.seal_bundle("TestGame_2026-07-24_mounted", snapshot, [], readme, "playtime")

    assert staging_path is not None
    assert str(staging_path) in str(caught.value)
    assert (staging_path / "mounted" / "outside-marker").read_bytes() == b"keep"


def test_claim_digest_rejects_same_name_bundle_replacement(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    adapter = _adapter(tmp_path)
    sealed = Path(
        adapter.seal_bundle(
            "TestGame_2026-07-24_bound",
            _snapshot(),
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            _readme_context(),
            "playtime",
        )
    )
    decoded = adapter.source_claims(str(sealed))
    original = sealed.with_name("original-bound")
    sealed.rename(original)
    shutil.copytree(original, sealed)

    assert adapter.validate_sources(str(sealed), decoded["bundle_digest"]) is False
    assert decoded["claims"][str(source)]["sha256"] is not None


def test_resealed_artifact_record_must_still_match_its_verified_source_claim(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    adapter = _adapter(tmp_path)
    sealed = Path(
        adapter.seal_bundle(
            "TestGame_2026-07-24_resealed",
            _snapshot(),
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            _readme_context(),
            "playtime",
        )
    )
    manifest_path = sealed / "manifest.json"

    _reseal(sealed)
    assert adapter.validate_sources(str(sealed)) is True

    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    _reseal(sealed)

    assert adapter.validate_sources(str(sealed)) is False


def test_measure_path_sums_the_whole_tree_without_hashing_any_byte(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    rom = safe / "big rom"
    (rom / "nested").mkdir(parents=True)
    (rom / "disc 1.bin").write_bytes(b"a" * 5000)
    (rom / "nested" / "disc 2.bin").write_bytes(b"b" * 320)
    adapter = _adapter(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("measure_path must not read source content")

    monkeypatch.setattr(descriptor_paths, "_sha256_fd", forbidden)
    monkeypatch.setattr(recovery_bundle, "claim_source", forbidden)
    monkeypatch.setattr(RecoveryBundleAdapter, "_sha256_fd", staticmethod(forbidden))
    monkeypatch.setattr(hashlib, "sha256", forbidden)
    monkeypatch.setattr(os, "read", forbidden)
    try:
        total = adapter.measure_path(str(rom), str(safe))
    finally:
        monkeypatch.undo()

    assert total == 5320
    assert adapter.measure_path(str(safe / "absent"), str(safe)) == 0
    assert adapter.measure_path(str(safe / "no" / "such" / "parent"), str(safe)) == 0


def test_measure_path_refuses_an_unsupported_entry_it_cannot_measure(tmp_path):
    safe = tmp_path / "safe"
    rom = safe / "rom"
    rom.mkdir(parents=True)
    (rom / "disc.bin").write_bytes(b"rom")
    os.mkfifo(rom / "pipe")
    os.mkfifo(safe / "lone-pipe")
    adapter = _adapter(tmp_path)

    with pytest.raises(ValueError, match="unsupported entry"):
        adapter.measure_path(str(rom), str(safe))
    with pytest.raises(ValueError, match="unsupported type"):
        adapter.measure_path(str(safe / "lone-pipe"), str(safe))


def test_directory_source_artifacts_are_verified_against_their_sealed_claim(tmp_path):
    safe = tmp_path / "safe"
    rom = safe / "multi disc game"
    (rom / "extra").mkdir(parents=True)
    (rom / "disc 1.bin").write_bytes(b"one")
    (rom / "extra" / "disc 2.bin").write_bytes(b"two")
    adapter = _adapter(tmp_path)
    sealed = adapter.seal_bundle(
        "TestGame_2026-07-24_multidisc",
        _snapshot(),
        [{"source_path": str(rom), "safe_root": str(safe), "kind": "installed_rom", "rom_id": 7}],
        _readme_context(),
        "playtime",
    )

    decoded = adapter.source_claims(sealed)
    assert sorted(decoded["claims"][str(rom)]["entries"]) == ["disc 1.bin", "extra", "extra/disc 2.bin"]
    assert adapter.validate_sources(sealed) is True

    (rom / "extra" / "disc 2.bin").write_bytes(b"TWO")
    assert adapter.validate_sources(sealed) is False


def test_free_bytes_never_creates_the_recovery_layout(tmp_path):
    adapter = _adapter(tmp_path)

    assert adapter.free_bytes() > 0
    assert not Path(adapter.root()).exists()


def test_unrenamable_uncertain_bundle_is_reported_and_kept_where_it_is(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _create_layout(adapter)
    bundles = Path(adapter.root()) / "bundles"
    bundle_id = "TestGame_2026-07-24_stuck"
    original_rename = os.rename

    def refuse_marker_rename(src, dst, **kwargs):
        if isinstance(dst, str) and dst.endswith(".durability-uncertain"):
            raise OSError(errno.EIO, "marker rename failed")
        return original_rename(src, dst, **kwargs)

    def fail_attachment(*_args, **_kwargs):
        raise OSError(errno.EIO, "layout attachment failed")

    monkeypatch.setattr("adapters.recovery_bundle.os.rename", refuse_marker_rename)
    monkeypatch.setattr(RecoveryBundleAdapter, "_require_layout_attached", fail_attachment)

    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(OSError, match="durability is uncertain") as caught:
        adapter.seal_bundle(bundle_id, snapshot, [], readme, "playtime")

    assert str(bundles / bundle_id) in str(caught.value)
    assert ".durability-uncertain" not in str(caught.value)
    assert (bundles / bundle_id / "SEAL.json").is_file()
    assert not (bundles / f"{bundle_id}.durability-uncertain").exists()


def test_destination_replacement_cannot_modify_outside_file(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    adapter = _adapter(tmp_path)
    original = adapter._copy_opened_source

    def replace_copied_destination(source_path, safe_root, parent_fd, name, should_abort=None):
        result = original(source_path, safe_root, parent_fd, name, should_abort)
        os.unlink(name, dir_fd=parent_fd)
        os.symlink(outside, name, dir_fd=parent_fd)
        return result

    monkeypatch.setattr(adapter, "_copy_opened_source", replace_copied_destination)

    snapshot = _snapshot()
    readme = _readme_context()
    with pytest.raises(RuntimeError, match="unsafe staging was preserved"):
        adapter.seal_bundle(
            "TestGame_2026-07-24_destination",
            snapshot,
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            readme,
            "playtime",
        )

    assert outside.read_bytes() == b"outside"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600


class TestCooperativeAbort:
    """A cancelled run must stop copying, not finish copying then be discarded."""

    def _sources(self, tmp_path) -> Path:
        source_root = tmp_path / "sources"
        source_root.mkdir()
        # Several chunks, so the copy has interior points to notice the abort at.
        (source_root / "big.bin").write_bytes(b"x" * (3 * 1024 * 1024))
        (source_root / "small.bin").write_bytes(b"y")
        return source_root

    def _artifacts(self, source_root: Path) -> list[RecoveryArtifact]:
        return [
            {
                "source_path": str(source_root / name),
                "safe_root": str(source_root),
                "kind": "installed_rom",
                "rom_id": 7,
            }
            for name in ("big.bin", "small.bin")
        ]

    def test_an_abort_stops_the_copy_and_leaves_no_bundle_or_staging(self, tmp_path):
        source_root = self._sources(tmp_path)
        adapter = _adapter(tmp_path)

        snapshot = _snapshot()
        readme = _readme_context()
        artifacts = self._artifacts(source_root)
        with pytest.raises(OperationAbortedError):
            adapter.seal_bundle(
                "TestGame_2026-07-24_abc123",
                snapshot,
                artifacts,
                readme,
                "playtime",
                lambda: True,
            )

        recovery_root = tmp_path / "decky-romm-sync-recovery"
        assert list((recovery_root / "bundles").iterdir()) == [], "no bundle is published"
        assert list((recovery_root / "staging").iterdir()) == [], "staging is cleaned up"

    def test_an_abort_partway_through_still_leaves_nothing_behind(self, tmp_path):
        """The abort lands mid-stream, not before the first byte."""
        source_root = self._sources(tmp_path)
        adapter = _adapter(tmp_path)
        checks = {"count": 0}

        def should_abort() -> bool:
            checks["count"] += 1
            return checks["count"] > 3

        snapshot = _snapshot()
        readme = _readme_context()
        artifacts = self._artifacts(source_root)
        with pytest.raises(OperationAbortedError):
            adapter.seal_bundle(
                "TestGame_2026-07-24_abc123",
                snapshot,
                artifacts,
                readme,
                "playtime",
                should_abort,
            )

        assert checks["count"] > 3, "the abort was polled repeatedly, not once up front"
        recovery_root = tmp_path / "decky-romm-sync-recovery"
        assert list((recovery_root / "bundles").iterdir()) == []
        assert list((recovery_root / "staging").iterdir()) == []

    def test_the_copy_loop_itself_polls_between_chunks(self, tmp_path):
        """The requirement is a stop mid-file, not merely between files.

        Driven straight at the copy helper so the poll count is unambiguous: a
        4 MB source is five 1 MB reads, and an abort armed after the first read
        must unwind from inside that loop rather than at the next artifact.
        """
        source_root = tmp_path / "sources"
        source_root.mkdir()
        source = source_root / "big.bin"
        source.write_bytes(b"x" * (4 * 1024 * 1024))
        destination_root = tmp_path / "staging"
        destination_root.mkdir()
        adapter = _adapter(tmp_path)
        destination_fd = os.open(str(destination_root), os.O_RDONLY | os.O_DIRECTORY)
        polls = {"count": 0}

        def should_abort() -> bool:
            polls["count"] += 1
            return polls["count"] > 1

        try:
            with pytest.raises(OperationAbortedError):
                adapter._copy_opened_source(str(source), str(source_root), destination_fd, "000001", should_abort)
        finally:
            os.close(destination_fd)

        assert polls["count"] == 2, "the abort was noticed on the second chunk, not after the whole file"

    def test_a_cleanup_that_fails_is_reported_rather_than_read_as_a_clean_stop(self, tmp_path, monkeypatch):
        """Never rewrite a cleanup failure into a tidy cancellation."""
        source_root = self._sources(tmp_path)
        adapter = _adapter(tmp_path)
        monkeypatch.setattr(
            recovery_bundle,
            "remove_current",
            lambda path, root: {"success": False, "changed": False, "ambiguous": True, "message": "staging is stuck"},
        )

        snapshot = _snapshot()
        readme = _readme_context()
        artifacts = self._artifacts(source_root)
        with pytest.raises(RuntimeError, match="unsafe staging was preserved"):
            adapter.seal_bundle(
                "TestGame_2026-07-24_abc123",
                snapshot,
                artifacts,
                readme,
                "playtime",
                lambda: True,
            )

    def test_the_authoritative_revalidation_takes_no_abort_poll(self, tmp_path):
        """What gates the destructive phase must not be interruptible.

        Sealing is pre-commit work and may be abandoned; ``validate_sources`` is
        the proof the cascade is authorized by, and a poll reaching into it would
        make that proof stoppable by the very cancellation it is meant to
        outrank. It is un-abortable structurally — there is no parameter to pass
        — and this pins that rather than trusting the next reader to notice.
        """
        for name in ("validate_sources", "source_claims"):
            parameters = inspect.signature(getattr(RecoveryBundleAdapter, name)).parameters
            assert "should_abort" not in parameters, f"{name} must not become interruptible"

    def test_validation_still_succeeds_while_a_backup_elsewhere_is_aborting(self, tmp_path):
        source_root = self._sources(tmp_path)
        adapter = _adapter(tmp_path)
        sealed = adapter.seal_bundle(
            "TestGame_2026-07-24_abc123",
            _snapshot(),
            self._artifacts(source_root),
            _readme_context(),
            "playtime",
        )

        assert adapter.validate_sources(sealed) is True

    def test_no_abort_callable_seals_exactly_as_before(self, tmp_path):
        source_root = self._sources(tmp_path)
        adapter = _adapter(tmp_path)

        sealed = Path(
            adapter.seal_bundle(
                "TestGame_2026-07-24_abc123",
                _snapshot(),
                self._artifacts(source_root),
                _readme_context(),
                "playtime",
            )
        )

        assert sealed.is_dir()
        assert adapter.validate_sources(str(sealed)) is True
