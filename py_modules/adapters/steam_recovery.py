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
            artifacts.append({"source_path": path, "safe_root": grid, "kind": "steam_grid"})
        artifacts.extend(
            {"source_path": path, "safe_root": os.path.dirname(path), "kind": "steam_input"}
            for path in self._input_roots(user_dir, steam_root, user_id, app_id)
        )
        return {
            "user_id": user_id,
            "user_dir": user_dir,
            "steam_root": steam_root,
            "controller_setting": controller_setting,
            "artifacts": artifacts,
        }

    def remove_state(self, app_id: int, snapshot: SteamRecoverySnapshot) -> None:
        """Remove only state belonging to the exact user captured in *snapshot*."""
        user_dir, steam_root, user_id = self._validate_identity(snapshot)
        self._clear_controller_setting(app_id, user_dir, snapshot["controller_setting"])
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
            steam_root = os.path.realpath(steam_root)
            userdata = os.path.join(steam_root, "userdata")
            if not os.path.isdir(userdata):
                continue
            users = sorted(name for name in os.listdir(userdata) if name.isdigit())
            if not users:
                continue
            user_id = self._most_recent_login(steam_root)
            if user_id is None and len(users) == 1:
                user_id = users[0]
            if user_id is not None and user_id in users:
                return os.path.realpath(os.path.join(userdata, user_id)), steam_root, user_id
        raise RuntimeError("Cannot locate the active Steam user directory")

    @staticmethod
    def _most_recent_login(steam_root: str) -> str | None:
        path = os.path.join(steam_root, "config", "loginusers.vdf")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as source:
            payload: dict[str, Any] = vdf.load(source)
        users = payload.get("users")
        if not isinstance(users, dict):
            return None
        recent = [
            steam_id for steam_id, value in users.items() if isinstance(value, dict) and value.get("MostRecent") == "1"
        ]
        if len(recent) != 1 or not str(recent[0]).isdigit():
            return None
        return str(int(recent[0]) & 0xFFFFFFFF)

    def _validate_identity(self, snapshot: SteamRecoverySnapshot) -> tuple[str, str, str]:
        user_id = snapshot.get("user_id")
        user_dir = snapshot.get("user_dir")
        steam_root = snapshot.get("steam_root")
        if not user_id.isdigit():
            raise ValueError("Invalid Steam recovery identity")
        allowed_roots = {
            os.path.realpath(os.path.join(self._user_home, ".local", "share", "Steam")),
            os.path.realpath(os.path.join(self._user_home, ".steam", "steam")),
        }
        real_root = os.path.realpath(steam_root)
        expected_user = os.path.realpath(os.path.join(real_root, "userdata", user_id))
        if (
            real_root not in allowed_roots
            or os.path.realpath(user_dir) != expected_user
            or not os.path.isdir(expected_user)
        ):
            raise ValueError("Steam recovery identity no longer matches the captured user")
        return expected_user, real_root, user_id

    @staticmethod
    def _clear_controller_setting(app_id: int, user_dir: str, expected: str | None) -> None:
        if expected is None:
            return
        path = os.path.join(user_dir, "config", "localconfig.vdf")
        if not os.path.isfile(path):
            raise OSError(f"Captured Steam controller config is missing: {path}")
        with open(path, encoding="utf-8") as source:
            payload: dict[str, Any] = vdf.load(source)
        apps = payload.get("UserLocalConfigStore", {}).get("Apps", {})
        app = apps.get(str(app_id)) if isinstance(apps, dict) else None
        if not isinstance(app, dict) or str(app.get("UseSteamControllerConfig")) != expected:
            raise RuntimeError("Steam controller setting changed after recovery capture")
        del app["UseSteamControllerConfig"]
        if not app:
            del apps[str(app_id)]
        temporary = path + ".prune.tmp"
        try:
            with open(temporary, "x", encoding="utf-8") as output:
                vdf.dump(payload, output, pretty=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(temporary)
            raise

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
