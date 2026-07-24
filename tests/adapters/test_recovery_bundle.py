from __future__ import annotations

import json
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

    monkeypatch.setattr("adapters.recovery_bundle.shutil.copyfile", fail_copy)
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
