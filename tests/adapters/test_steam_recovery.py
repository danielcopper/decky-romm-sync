from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest
from _vendor import vdf

from adapters.descriptor_paths import claim_source
from adapters.steam_recovery import SteamRecoveryAdapter


def _layout(tmp_path, app_id: int):
    steam = tmp_path / ".local" / "share" / "Steam"
    user = steam / "userdata" / "123"
    config = user / "config"
    grid = config / "grid"
    grid.mkdir(parents=True)
    localconfig = config / "localconfig.vdf"
    with localconfig.open("w") as output:
        vdf.dump({"UserLocalConfigStore": {"Apps": {str(app_id): {"UseSteamControllerConfig": "2"}}}}, output)
    (grid / f"{app_id}p.png").write_bytes(b"cover")
    first = config / "controller_configs" / "apps" / str(app_id)
    first.mkdir(parents=True)
    (first / "layout.vdf").write_text("one")
    second = steam / "steamapps" / "common" / "Steam Controller Configs" / "123" / "config" / str(app_id)
    second.mkdir(parents=True)
    (second / "controller_neptune.vdf").write_text("two")
    return first, second, grid


def _source_claims(snapshot):
    claims = {}
    for artifact in snapshot["artifacts"]:
        source = artifact["source_path"]
        claims[source] = claim_source(source, artifact["safe_root"])
    return claims


def test_snapshots_controller_setting_grid_and_both_input_roots(tmp_path):
    app_id = 0x80000007
    first, second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    assert snapshot["controller_setting"] == "2"
    sources = {Path(item["source_path"]) for item in snapshot["artifacts"]}
    assert {grid / f"{app_id}p.png", first, second} <= sources
    assert grid / f"{app_id}.png" in sources

    assert adapter.remove_state(app_id, snapshot, _source_claims(snapshot))["success"] is True
    assert not (grid / f"{app_id}p.png").exists()
    assert not first.exists()
    assert not second.exists()
    with (grid.parent / "localconfig.vdf").open() as source:
        payload = vdf.load(source)
    assert str(app_id) not in payload["UserLocalConfigStore"]["Apps"]


def test_preopened_steam_input_writer_retains_the_claimed_tree(tmp_path):
    app_id = 0x80000007
    first, _second, _grid = _layout(tmp_path, app_id)
    layout = first / "layout.vdf"
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    claims = _source_claims(snapshot)
    writer = os.open(layout, os.O_WRONLY)
    try:
        outcome = adapter.remove_state(app_id, snapshot, claims)
    finally:
        os.close(writer)

    assert outcome["success"] is False
    assert "active writer" in outcome["message"]
    assert layout.read_text() == "one"


def test_missing_active_user_is_loud(tmp_path):
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    try:
        adapter.snapshot(7)
    except RuntimeError as exc:
        assert "active Steam user" in str(exc)
    else:
        raise AssertionError("missing Steam user should not produce an empty recovery snapshot")


def test_cleanup_uses_captured_login_identity_when_active_user_changes(tmp_path):
    app_id = 0x80000007
    first, _second, grid = _layout(tmp_path, app_id)
    steam = tmp_path / ".local" / "share" / "Steam"
    other_grid = steam / "userdata" / "456" / "config" / "grid"
    other_grid.mkdir(parents=True)
    (other_grid / f"{app_id}p.png").write_bytes(b"other-cover")
    loginusers = steam / "config" / "loginusers.vdf"
    loginusers.parent.mkdir()
    with loginusers.open("w") as output:
        vdf.dump(
            {
                "users": {
                    str(76561197960265728 + 123): {"MostRecent": "1"},
                    str(76561197960265728 + 456): {"MostRecent": "0"},
                }
            },
            output,
        )
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    assert snapshot["user_id"] == "123"

    with loginusers.open("w") as output:
        vdf.dump(
            {
                "users": {
                    str(76561197960265728 + 123): {"MostRecent": "0"},
                    str(76561197960265728 + 456): {"MostRecent": "1"},
                }
            },
            output,
        )
    assert adapter.remove_state(app_id, snapshot, _source_claims(snapshot))["success"] is True

    assert not (grid / f"{app_id}p.png").exists()
    assert not first.exists()
    assert (other_grid / f"{app_id}p.png").read_bytes() == b"other-cover"


@pytest.mark.parametrize("subtree", ["grid", "user", "input"])
def test_symlinked_steam_subtrees_are_rejected_without_touching_target(tmp_path, subtree):
    app_id = 0x80000007
    first, _second, grid = _layout(tmp_path, app_id)
    steam = tmp_path / ".local" / "share" / "Steam"
    user = steam / "userdata" / "123"
    outside = tmp_path / f"outside-{subtree}"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_text("keep")

    if subtree == "grid":
        for child in grid.iterdir():
            child.unlink()
        grid.rmdir()
        grid.symlink_to(outside, target_is_directory=True)
    elif subtree == "user":
        shutil.rmtree(user)
        user.symlink_to(outside, target_is_directory=True)
    else:
        shutil.rmtree(first)
        first.symlink_to(outside, target_is_directory=True)

    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    with pytest.raises((OSError, ValueError)):
        adapter.snapshot(app_id)

    assert marker.read_text() == "keep"


def test_controller_replacement_fsyncs_its_containing_directory(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    config_inode = grid.parent.stat().st_ino
    synced_directory_inodes: list[int] = []
    original = os.fsync

    def track(fd: int) -> None:
        current = os.fstat(fd)
        if current.st_ino == config_inode:
            synced_directory_inodes.append(current.st_ino)
        original(fd)

    monkeypatch.setattr("adapters.steam_recovery.os.fsync", track)
    assert adapter.remove_state(app_id, snapshot, _source_claims(snapshot))["success"] is True

    assert synced_directory_inodes == [config_inode]


def test_controller_writer_exclusion_release_failure_is_ambiguous(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    module = __import__("adapters.descriptor_paths", fromlist=["fcntl"])
    original = module.fcntl.fcntl

    def fail_unlock(fd, command, *args):
        if command == module.fcntl.F_SETLEASE and args == (module.fcntl.F_UNLCK,):
            raise OSError("injected controller lease release failure")
        return original(fd, command, *args)

    monkeypatch.setattr(module.fcntl, "fcntl", fail_unlock)

    outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))

    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert "injected controller lease release failure" in outcome["message"]
    assert not list(grid.parent.glob(".localconfig.vdf.prune-old-*"))


def test_controller_writer_exclusion_setup_failure_surfaces_retained_claim(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    module = __import__("adapters.descriptor_paths", fromlist=["fcntl"])
    original = module.fcntl.fcntl

    def fail_setup(fd, command, *args):
        if command == module._F_SETOWN_EX:
            raise OSError("injected controller lease setup failure")
        return original(fd, command, *args)

    monkeypatch.setattr(module.fcntl, "fcntl", fail_setup)

    outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))

    claimed = list(grid.parent.glob(".localconfig.vdf.prune-old-*"))
    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert len(claimed) == 1
    assert str(claimed[0]) in outcome["message"]
    assert "injected controller lease setup failure" in outcome["message"]


def test_controller_cleanup_retries_and_preserves_unrelated_concurrent_write(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    localconfig = grid.parent / "localconfig.vdf"
    module = __import__("adapters.steam_recovery", fromlist=["rename_noreplace_at"])
    original = module.rename_noreplace_at
    injected = False

    def write_then_rename(source_fd, src, destination_fd, dst):
        nonlocal injected
        if src == "localconfig.vdf" and not injected:
            injected = True
            with localconfig.open() as source:
                payload = vdf.load(source)
            payload["UserLocalConfigStore"]["Unrelated"] = {"Fresh": "value"}
            with localconfig.open("w") as output:
                vdf.dump(payload, output, pretty=True)
        return original(source_fd, src, destination_fd, dst)

    monkeypatch.setattr(module, "rename_noreplace_at", write_then_rename)

    outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))

    assert outcome["success"] is True
    with localconfig.open() as source:
        payload = vdf.load(source)
    assert payload["UserLocalConfigStore"]["Unrelated"] == {"Fresh": "value"}
    assert str(app_id) not in payload["UserLocalConfigStore"]["Apps"]


def test_controller_cleanup_retains_write_through_held_fd_after_rename(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    localconfig = grid.parent / "localconfig.vdf"
    with localconfig.open() as source:
        fresh_payload = vdf.load(source)
    fresh_payload["UserLocalConfigStore"]["Unrelated"] = {"Fresh": "value"}
    held_fd = os.open(localconfig, os.O_RDWR)
    original = os.link
    injected = False

    def write_then_link(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            with os.fdopen(os.dup(held_fd), "w") as output:
                output.seek(0)
                output.truncate()
                vdf.dump(fresh_payload, output, pretty=True)
                output.flush()
        return original(*args, **kwargs)

    monkeypatch.setattr("adapters.steam_recovery.os.link", write_then_link)
    try:
        outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))
    finally:
        os.close(held_fd)

    claimed = list(grid.parent.glob(".localconfig.vdf.prune-old-*"))
    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert len(claimed) == 1
    assert str(claimed[0]) in outcome["message"]
    with localconfig.open() as source:
        payload = vdf.load(source)
    assert str(app_id) not in payload["UserLocalConfigStore"]["Apps"]
    with claimed[0].open() as source:
        preserved = vdf.load(source)
    assert preserved["UserLocalConfigStore"]["Unrelated"] == {"Fresh": "value"}


def test_controller_cleanup_retains_claim_when_preopened_writer_blocks_final_exclusion(tmp_path):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    localconfig = grid.parent / "localconfig.vdf"
    writer = os.open(localconfig, os.O_WRONLY)
    try:
        outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))
    finally:
        os.close(writer)

    claimed = list(grid.parent.glob(".localconfig.vdf.prune-old-*"))
    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert len(claimed) == 1
    assert str(claimed[0]) in outcome["message"]


def test_controller_collision_retains_claim_when_preopened_writer_blocks_discard(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    localconfig = grid.parent / "localconfig.vdf"
    replacement_payload = {"UserLocalConfigStore": {"Unrelated": {"NewPath": "value"}}}
    writer = os.open(localconfig, os.O_WRONLY)
    original_link = os.link
    injected = False

    def collide_then_link(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            with localconfig.open("w") as output:
                vdf.dump(replacement_payload, output, pretty=True)
        return original_link(*args, **kwargs)

    monkeypatch.setattr("adapters.steam_recovery.os.link", collide_then_link)
    try:
        outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))
    finally:
        os.close(writer)

    claimed = list(grid.parent.glob(".localconfig.vdf.prune-old-*"))
    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert len(claimed) == 1
    assert str(claimed[0]) in outcome["message"]
    with localconfig.open() as source:
        assert vdf.load(source) == replacement_payload


def test_controller_link_collision_preserves_newer_claimed_inode(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    localconfig = grid.parent / "localconfig.vdf"
    with localconfig.open() as source:
        fresh_payload = vdf.load(source)
    fresh_payload["UserLocalConfigStore"]["Unrelated"] = {"HeldFresh": "value"}
    replacement_payload = {"UserLocalConfigStore": {"Unrelated": {"NewPath": "value"}}}
    held_fd = os.open(localconfig, os.O_RDWR)
    original_link = os.link
    injected = False

    def write_recreate_then_link(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            with os.fdopen(os.dup(held_fd), "w") as output:
                output.seek(0)
                output.truncate()
                vdf.dump(fresh_payload, output, pretty=True)
                output.flush()
            with localconfig.open("w") as output:
                vdf.dump(replacement_payload, output, pretty=True)
        return original_link(*args, **kwargs)

    monkeypatch.setattr("adapters.steam_recovery.os.link", write_recreate_then_link)
    try:
        outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))
    finally:
        os.close(held_fd)

    claimed = list(grid.parent.glob(".localconfig.vdf.prune-old-*"))
    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert "newer source was preserved" in outcome["message"]
    assert len(claimed) == 1
    assert str(claimed[0]) in outcome["message"]
    with localconfig.open() as source:
        assert vdf.load(source) == replacement_payload
    with claimed[0].open() as source:
        preserved = vdf.load(source)
    assert preserved["UserLocalConfigStore"]["Unrelated"] == {"HeldFresh": "value"}


def test_controller_rollback_failure_preserves_claimed_newer_vdf(tmp_path, monkeypatch):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    localconfig = grid.parent / "localconfig.vdf"
    with localconfig.open() as source:
        fresh_payload = vdf.load(source)
    fresh_payload["UserLocalConfigStore"]["Unrelated"] = {"Fresh": "value"}
    held_fd = os.open(localconfig, os.O_RDWR)
    original_link = os.link
    original_unlink = os.unlink
    injected = False

    def write_then_link(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            with os.fdopen(os.dup(held_fd), "w") as output:
                output.seek(0)
                output.truncate()
                vdf.dump(fresh_payload, output, pretty=True)
                output.flush()
        return original_link(*args, **kwargs)

    def fail_replacement_unlink(path, *args, **kwargs):
        if path == "localconfig.vdf":
            raise OSError("injected replacement unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr("adapters.steam_recovery.os.link", write_then_link)
    monkeypatch.setattr("adapters.steam_recovery.os.unlink", fail_replacement_unlink)
    try:
        outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))
    finally:
        os.close(held_fd)

    claimed = list(grid.parent.glob(".localconfig.vdf.prune-old-*"))
    assert outcome["success"] is False
    assert outcome["ambiguous"] is True
    assert "newer source was preserved" in outcome["message"]
    assert len(claimed) == 1
    with claimed[0].open() as source:
        preserved = vdf.load(source)
    assert preserved["UserLocalConfigStore"]["Unrelated"] == {"Fresh": "value"}


def test_controller_cleanup_preserves_unrelated_duplicate_vdf_keys(tmp_path):
    app_id = 0x80000007
    _first, _second, grid = _layout(tmp_path, app_id)
    localconfig = grid.parent / "localconfig.vdf"
    localconfig.write_text(
        '"UserLocalConfigStore"\n'
        "{\n"
        '\t"Apps"\n'
        "\t{\n"
        f'\t\t"{app_id}"\n'
        "\t\t{\n"
        '\t\t\t"UseSteamControllerConfig"\t\t"2"\n'
        '\t\t\t"LastPlayed"\t\t"1700000000"\n'
        "\t\t}\n"
        "\t}\n"
        '\t"Software"\n'
        "\t{\n"
        '\t\t"Launcher"\t\t"first"\n'
        '\t\t"Launcher"\t\t"second"\n'
        "\t}\n"
        "}\n"
    )
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    assert snapshot["controller_setting"] == "2"

    outcome = adapter.remove_state(app_id, snapshot, _source_claims(snapshot))

    assert outcome["success"] is True
    with localconfig.open() as source:
        rewritten = vdf.load(source, mapper=vdf.VDFDict, merge_duplicate_keys=False)
    assert rewritten["UserLocalConfigStore"]["Software"].get_all_for("Launcher") == ["first", "second"]
    assert list(rewritten["UserLocalConfigStore"]["Apps"][str(app_id)].items()) == [("LastPlayed", "1700000000")]
