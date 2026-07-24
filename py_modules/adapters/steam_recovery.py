"""Steam-only recovery state and files for a shortcut about to be removed."""

from __future__ import annotations

import contextlib
import os
import shutil
from typing import TYPE_CHECKING, Any

from _vendor import vdf

from domain.artwork_paths import grid_image_filenames

if TYPE_CHECKING:
    import logging

    from models.prune import RecoveryArtifact, SteamRecoverySnapshot


class SteamRecoveryAdapter:
    """Own bounded Steam Input/grid snapshots and post-removal file cleanup."""

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger

    def snapshot(self, app_id: int) -> SteamRecoverySnapshot:
        user_dir, steam_root, user_id = self._resolve_user()
        localconfig = os.path.join(user_dir, "config", "localconfig.vdf")
        controller_setting: str | None = None
        if os.path.exists(localconfig):
            with open(localconfig, encoding="utf-8") as source:
                data: dict[str, Any] = vdf.load(source)
            apps = data.get("UserLocalConfigStore", {}).get("Apps", {})
            app = apps.get(str(app_id), {}) if isinstance(apps, dict) else {}
            value = app.get("UseSteamControllerConfig") if isinstance(app, dict) else None
            controller_setting = str(value) if value is not None else None

        artifacts: list[RecoveryArtifact] = []
        grid = os.path.join(user_dir, "config", "grid")
        for filename in grid_image_filenames(app_id):
            path = os.path.join(grid, filename)
            if os.path.isfile(path):
                artifacts.append({"source_path": path, "safe_root": grid, "kind": "steam_grid"})
        for path in self._input_roots(user_dir, steam_root, user_id, app_id):
            if os.path.exists(path):
                artifacts.extend([{"source_path": path, "safe_root": os.path.dirname(path), "kind": "steam_input"}])
        return {"controller_setting": controller_setting, "artifacts": artifacts}

    def remove_files(self, app_id: int) -> None:
        user_dir, steam_root, user_id = self._resolve_user()
        grid = os.path.join(user_dir, "config", "grid")
        for filename in grid_image_filenames(app_id):
            with contextlib.suppress(FileNotFoundError):
                os.remove(os.path.join(grid, filename))
        for path in self._input_roots(user_dir, steam_root, user_id, app_id):
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)

    def _resolve_user(self) -> tuple[str, str, str]:
        candidates = (
            os.path.join(self._user_home, ".local", "share", "Steam"),
            os.path.join(self._user_home, ".steam", "steam"),
        )
        for steam_root in candidates:
            userdata = os.path.join(steam_root, "userdata")
            if not os.path.isdir(userdata):
                continue
            users = [name for name in os.listdir(userdata) if name.isdigit()]
            if not users:
                continue
            users.sort(key=lambda name: os.path.getmtime(os.path.join(userdata, name)), reverse=True)
            user_id = users[0]
            return os.path.join(userdata, user_id), steam_root, user_id
        raise RuntimeError("Cannot locate the active Steam user directory")

    @staticmethod
    def _input_roots(user_dir: str, steam_root: str, user_id: str, app_id: int) -> tuple[str, str]:
        return (
            os.path.join(user_dir, "config", "controller_configs", "apps", str(app_id)),
            os.path.join(
                steam_root,
                "steamapps",
                "common",
                "Steam Controller Configs",
                user_id,
                "config",
                str(app_id),
            ),
        )
