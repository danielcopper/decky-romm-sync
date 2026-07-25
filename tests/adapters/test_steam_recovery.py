from __future__ import annotations

import logging
from pathlib import Path

from _vendor import vdf

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


def test_snapshots_controller_setting_grid_and_both_input_roots(tmp_path):
    app_id = 0x80000007
    first, second, grid = _layout(tmp_path, app_id)
    adapter = SteamRecoveryAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
    snapshot = adapter.snapshot(app_id)
    assert snapshot["controller_setting"] == "2"
    sources = {Path(item["source_path"]) for item in snapshot["artifacts"]}
    assert {grid / f"{app_id}p.png", first, second} <= sources
    assert grid / f"{app_id}.png" in sources

    adapter.remove_state(app_id, snapshot)
    assert not (grid / f"{app_id}p.png").exists()
    assert not first.exists()
    assert not second.exists()
    with (grid.parent / "localconfig.vdf").open() as source:
        payload = vdf.load(source)
    assert str(app_id) not in payload["UserLocalConfigStore"]["Apps"]


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
    adapter.remove_state(app_id, snapshot)

    assert not (grid / f"{app_id}p.png").exists()
    assert not first.exists()
    assert (other_grid / f"{app_id}p.png").read_bytes() == b"other-cover"
