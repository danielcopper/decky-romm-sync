from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from adapters.recovery_bundle import RecoveryBundleAdapter


def _adapter(tmp_path) -> RecoveryBundleAdapter:
    return RecoveryBundleAdapter(user_home=str(tmp_path), package_name="decky romm/sync", plugin_version="1.2.3")


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
            "20260724T120000Z_7_abc-123",
            _snapshot(),
            [{"source_path": str(nested), "safe_root": str(source_root), "kind": "installed_rom", "rom_id": 7}],
            "manual recovery\n",
            "7 seconds\n",
        )
    )

    assert sealed.name == "20260724T120000Z_7_abc-123"
    assert (sealed / "SEAL.json").exists()
    manifest = json.loads((sealed / "manifest.json").read_text())
    assert manifest["plugin_version"] == "1.2.3"
    assert manifest["artifacts"][0]["destination"] == "files/000001"
    assert manifest["artifacts"][0]["source_path"].endswith("disc 1.bin")
    assert (sealed / "files" / "000001").read_bytes() == b"rom"
    assert (sealed / "roms" / "7" / "state.json").exists()
    assert "files/000001" in (sealed / "checksums.sha256").read_text()


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
    with pytest.raises(ValueError, match="unsafe recovery bundle id"):
        adapter.seal_bundle("../escape", _snapshot(), [], "readme", "playtime")

    bundle_id = "20260724T120000Z_7_same"
    adapter.seal_bundle(bundle_id, _snapshot(), [], "readme", "playtime")
    with pytest.raises(FileExistsError):
        adapter.seal_bundle(bundle_id, _snapshot(), [], "readme", "playtime")


def test_failed_copy_cleans_staging_without_touching_existing_bundle(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    adapter = _adapter(tmp_path)

    def fail_copy(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(adapter, "_copy_opened_source", fail_copy)
    with pytest.raises(OSError, match="disk full"):
        adapter.seal_bundle(
            "20260724T120000Z_7_failure",
            _snapshot(),
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            "readme",
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
        "20260724T120000Z_7_validate",
        _snapshot(),
        [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
        "readme",
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
        "20260724T120000Z_7_identity",
        _snapshot(),
        [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
        "readme",
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
        "20260724T120000Z_7_late",
        _snapshot(),
        [{"source_path": str(source), "safe_root": str(safe), "kind": "steam_grid"}],
        "readme",
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
    with pytest.raises((OSError, ValueError)):
        adapter.seal_bundle(
            "20260724T120000Z_7_replaced",
            _snapshot(),
            [{"source_path": str(source), "safe_root": str(safe), "kind": "current_save", "rom_id": 7}],
            "readme",
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
    with pytest.raises(OSError, match="directory fsync failed"):
        adapter.seal_bundle("20260724T120000Z_7_fsync", _snapshot(), [], "readme", "playtime")


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
    sealed = Path(adapter.seal_bundle("20260724T120000Z_7_seal-fields", _snapshot(), [], "readme", "playtime"))
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

    adapter.seal_bundle("20260724T120000Z_7_new-parents", _snapshot(), [], "readme", "playtime")

    root = Path(adapter.root())
    assert tmp_path.stat().st_ino in synced
    assert root.stat().st_ino in synced
    assert (root / "staging").stat().st_ino in synced
    assert (root / "bundles").stat().st_ino in synced


def test_post_rename_fsync_failure_marks_bundle_durability_uncertain(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.free_bytes()
    bundles = Path(adapter.root()) / "bundles"
    bundles_inode = bundles.stat().st_ino
    original = os.fsync

    def fail_final_sync(fd: int) -> None:
        if os.fstat(fd).st_ino == bundles_inode:
            raise OSError(errno.EIO, "directory fsync failed")
        original(fd)

    monkeypatch.setattr("adapters.recovery_bundle.os.fsync", fail_final_sync)
    bundle_id = "20260724T120000Z_7_uncertain"
    with pytest.raises(OSError, match="durability is uncertain"):
        adapter.seal_bundle(bundle_id, _snapshot(), [], "readme", "playtime")

    assert not (bundles / bundle_id).exists()
    assert (bundles / f"{bundle_id}.durability-uncertain").is_dir()


def test_recovery_bundle_parent_replacement_is_not_reauthorized(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.free_bytes()
    bundles = Path(adapter.root()) / "bundles"
    detached = Path(adapter.root()) / "detached-bundles"
    outside = tmp_path / "outside-bundles"
    outside.mkdir()
    original = adapter._copy_artifacts

    def swap_parent(staging, artifacts, free_bytes):
        result = original(staging, artifacts, free_bytes)
        bundles.rename(detached)
        bundles.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(adapter, "_copy_artifacts", swap_parent)

    with pytest.raises(OSError, match="durability is uncertain"):
        adapter.seal_bundle("20260724T120000Z_7_swapped", _snapshot(), [], "readme", "playtime")

    assert list(outside.iterdir()) == []
