from __future__ import annotations

import os

import pytest

from adapters.descriptor_paths import (
    claim_source,
    identity_for_stat,
    remove_claimed,
    remove_exact,
    rename_claimed,
    stat_beneath,
)


def test_inside_root_symlink_replacement_is_not_removed(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "source.srm"
    source.write_bytes(b"sealed")
    captured = stat_beneath(str(source), str(safe))
    assert captured is not None
    identity = identity_for_stat(captured)
    replacement = safe / "replacement.srm"
    replacement.write_bytes(b"not sealed")
    source.unlink()
    source.symlink_to(replacement)

    with pytest.raises(RuntimeError, match="identity changed"):
        remove_exact(str(source), str(safe), identity)

    assert source.is_symlink()
    assert replacement.read_bytes() == b"not sealed"


def test_intermediate_symlink_never_authorizes_deletion_outside_root(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.srm"
    target.write_bytes(b"keep")
    (safe / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        stat_beneath(str(safe / "linked" / "target.srm"), str(safe))

    assert target.read_bytes() == b"keep"


def test_replacement_in_pre_rename_window_is_verified_and_rolled_back(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "source.srm"
    source.write_bytes(b"sealed")
    captured = stat_beneath(str(source), str(safe))
    assert captured is not None
    identity = identity_for_stat(captured)
    original = os.rename
    replaced = False

    def replace_then_rename(src, dst, *args, **kwargs):
        nonlocal replaced
        if src == source.name and not replaced:
            replaced = True
            source.unlink()
            source.write_bytes(b"replacement")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr("adapters.descriptor_paths.os.rename", replace_then_rename)
    with pytest.raises(RuntimeError, match="while it was claimed"):
        remove_exact(str(source), str(safe), identity)

    assert source.read_bytes() == b"replacement"


def test_claimed_directory_revalidates_descendants_and_restores_root_on_change(tmp_path):
    safe = tmp_path / "safe"
    source = safe / "game"
    source.mkdir(parents=True)
    child = source / "disc.bin"
    child.write_bytes(b"sealed")
    claim = claim_source(str(source), str(safe))
    child.write_bytes(b"replacement")

    with pytest.raises(RuntimeError, match="subtree changed"):
        remove_claimed(str(source), str(safe), claim)

    assert source.is_dir()
    assert child.read_bytes() == b"replacement"


def test_remove_reports_post_unlink_fsync_uncertainty(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    claim = claim_source(str(source), str(safe))
    monkeypatch.setattr("adapters.descriptor_paths.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError("EIO")))

    outcome = remove_claimed(str(source), str(safe), claim)

    assert outcome == {
        "success": False,
        "changed": True,
        "ambiguous": True,
        "message": "Source was removed but directory durability is uncertain: EIO",
    }
    assert not source.exists()


def test_rename_reports_post_move_fsync_uncertainty(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    destination = safe / "backup.srm"
    source.write_bytes(b"save")
    claim = claim_source(str(source), str(safe))
    monkeypatch.setattr("adapters.descriptor_paths.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError("EIO")))

    outcome = rename_claimed(str(source), str(destination), str(safe), claim)

    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert destination.read_bytes() == b"save"
