"""Steam-only recovery state and files for a shortcut about to be removed."""

from __future__ import annotations

import contextlib
import hashlib
import os
from typing import TYPE_CHECKING, Any

from _vendor import vdf

from adapters.descriptor_paths import (
    identity_for_stat,
    open_directory_fd,
    open_regular_fd,
    remove_claimed,
    require_directory,
)
from domain.artwork_paths import grid_image_filenames

if TYPE_CHECKING:
    import logging

    from models.prune import MutationOutcome, RecoveryArtifact, SourceClaim, SourceIdentity, SteamRecoverySnapshot


class SteamRecoveryAdapter:
    """Own bounded Steam Input/grid snapshots and post-removal file cleanup."""

    def __init__(self, *, user_home: str, logger: logging.Logger) -> None:
        self._user_home = user_home
        self._logger = logger

    def snapshot(self, app_id: int) -> SteamRecoverySnapshot:
        user_dir, steam_root, user_id = self._resolve_user()
        controller_setting = self._controller_setting(app_id, user_dir)

        artifacts: list[RecoveryArtifact] = []
        grid = os.path.join(user_dir, "config", "grid")
        if os.path.lexists(grid):
            require_directory(grid, user_dir)
        for filename in grid_image_filenames(app_id):
            path = os.path.join(grid, filename)
            artifacts.append({"source_path": path, "safe_root": user_dir, "kind": "steam_grid"})
        first_input, second_input = self._input_roots(user_dir, steam_root, user_id, app_id)
        for path, root in ((first_input, user_dir), (second_input, steam_root)):
            if os.path.lexists(path):
                require_directory(path, root)
        artifacts.append({"source_path": first_input, "safe_root": user_dir, "kind": "steam_input"})
        artifacts.append({"source_path": second_input, "safe_root": steam_root, "kind": "steam_input"})
        return {
            "user_id": user_id,
            "user_dir": user_dir,
            "steam_root": steam_root,
            "controller_setting": controller_setting,
            "artifacts": artifacts,
        }

    def validate_state(self, app_id: int, snapshot: SteamRecoverySnapshot) -> bool:
        """Require the exact captured user and controller value before mutation."""
        try:
            user_dir, _steam_root, _user_id = self._validate_identity(snapshot)
            return self._controller_setting(app_id, user_dir) == snapshot["controller_setting"]
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def remove_state(
        self,
        app_id: int,
        snapshot: SteamRecoverySnapshot,
        claims: dict[str, SourceClaim],
    ) -> MutationOutcome:
        """Remove only state belonging to the exact user captured in *snapshot*."""
        changed = False
        ambiguous = False
        try:
            user_dir, steam_root, user_id = self._validate_identity(snapshot)
            controller = self._clear_controller_setting(app_id, user_dir, snapshot["controller_setting"])
            changed |= controller["changed"]
            ambiguous |= controller["ambiguous"]
            if not controller["success"]:
                return {**controller, "changed": changed, "ambiguous": ambiguous}
            grid = os.path.join(user_dir, "config", "grid")
            paths = [(os.path.join(grid, filename), user_dir) for filename in grid_image_filenames(app_id)]
            first_input, second_input = self._input_roots(user_dir, steam_root, user_id, app_id)
            paths.extend(((first_input, user_dir), (second_input, steam_root)))
            for path, root in paths:
                claim = claims.get(path)
                if claim is None:
                    raise ValueError(f"Sealed Steam source claim is missing: {path}")
                outcome = remove_claimed(path, root, claim)
                changed |= outcome["changed"]
                ambiguous |= outcome["ambiguous"]
                if not outcome["success"]:
                    return {**outcome, "changed": changed, "ambiguous": ambiguous}
        except Exception as exc:
            return {"success": False, "changed": changed, "ambiguous": ambiguous, "message": str(exc)}
        return {"success": True, "changed": changed, "ambiguous": ambiguous, "message": "Steam state removed"}

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
            require_directory(userdata, steam_root)
            users = sorted(name for name in os.listdir(userdata) if name.isdigit())
            if not users:
                continue
            user_id = self._most_recent_login(steam_root)
            if user_id is None and len(users) == 1:
                user_id = users[0]
            if user_id is not None and user_id in users:
                user_dir = os.path.join(userdata, user_id)
                require_directory(user_dir, steam_root)
                return user_dir, steam_root, user_id
        raise RuntimeError("Cannot locate the active Steam user directory")

    @staticmethod
    def _most_recent_login(steam_root: str) -> str | None:
        path = os.path.join(steam_root, "config", "loginusers.vdf")
        try:
            fd = open_regular_fd(path, steam_root)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, encoding="utf-8") as source:
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
        require_directory(expected_user, real_root)
        return expected_user, real_root, user_id

    @classmethod
    def _controller_setting(cls, app_id: int, user_dir: str) -> str | None:
        path = os.path.join(user_dir, "config", "localconfig.vdf")
        if not os.path.exists(path):
            return None
        fd = open_regular_fd(path, user_dir)
        with os.fdopen(fd, encoding="utf-8") as source:
            payload: dict[str, Any] = vdf.load(source)
        apps = payload.get("UserLocalConfigStore", {}).get("Apps", {})
        app = apps.get(str(app_id)) if isinstance(apps, dict) else None
        value = app.get("UseSteamControllerConfig") if isinstance(app, dict) else None
        return str(value) if value is not None else None

    @staticmethod
    def _clear_controller_setting(app_id: int, user_dir: str, expected: str | None) -> MutationOutcome:
        if expected is None:
            return {"success": True, "changed": False, "ambiguous": False, "message": "No controller setting"}
        config_dir = os.path.join(user_dir, "config")
        config_fd = open_directory_fd(config_dir, user_dir)
        try:
            for attempt in range(3):
                source_fd = os.open(
                    "localconfig.vdf",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=config_fd,
                )
                source_identity = identity_for_stat(os.fstat(source_fd))
                source_hash = SteamRecoveryAdapter._sha256_fd(source_fd)
                os.lseek(source_fd, 0, os.SEEK_SET)
                try:
                    with os.fdopen(os.dup(source_fd), encoding="utf-8") as source:
                        payload: dict[str, Any] = vdf.load(source)
                except BaseException:
                    os.close(source_fd)
                    raise
                apps = payload.get("UserLocalConfigStore", {}).get("Apps", {})
                app = apps.get(str(app_id)) if isinstance(apps, dict) else None
                if not isinstance(app, dict) or str(app.get("UseSteamControllerConfig")) != expected:
                    raise RuntimeError("Steam controller setting changed after recovery capture")
                del app["UseSteamControllerConfig"]
                if not app:
                    del apps[str(app_id)]

                temporary = f".localconfig.vdf.prune-new-{source_identity['inode']}-{attempt}"
                claimed = f".localconfig.vdf.prune-old-{source_identity['inode']}-{attempt}"
                source_claimed = False
                replacement_installed = False
                try:
                    temporary_fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=config_fd,
                    )
                    with os.fdopen(temporary_fd, "w", encoding="utf-8") as output:
                        vdf.dump(payload, output, pretty=True)
                        output.flush()
                        os.fsync(output.fileno())
                    os.rename(
                        "localconfig.vdf",
                        claimed,
                        src_dir_fd=config_fd,
                        dst_dir_fd=config_fd,
                    )
                    source_claimed = True
                    claimed_stat = os.stat(claimed, dir_fd=config_fd, follow_symlinks=False)
                    stable = ("device", "inode", "mode", "size", "mtime_ns")
                    claimed_identity = identity_for_stat(claimed_stat)
                    if any(claimed_identity[field] != source_identity[field] for field in stable):
                        SteamRecoveryAdapter._restore_or_discard_claim(config_fd, claimed)
                        os.unlink(temporary, dir_fd=config_fd)
                        os.close(source_fd)
                        continue
                    if not SteamRecoveryAdapter._source_unchanged(source_fd, source_identity, source_hash):
                        SteamRecoveryAdapter._restore_or_discard_claim(config_fd, claimed)
                        os.unlink(temporary, dir_fd=config_fd)
                        os.close(source_fd)
                        continue
                    try:
                        os.link(
                            temporary,
                            "localconfig.vdf",
                            src_dir_fd=config_fd,
                            dst_dir_fd=config_fd,
                            follow_symlinks=False,
                        )
                        replacement_installed = True
                    except FileExistsError:
                        os.unlink(claimed, dir_fd=config_fd)
                        os.unlink(temporary, dir_fd=config_fd)
                        os.fsync(config_fd)
                        os.close(source_fd)
                        continue
                    os.unlink(temporary, dir_fd=config_fd)
                    if not SteamRecoveryAdapter._source_unchanged(source_fd, source_identity, source_hash):
                        os.unlink("localconfig.vdf", dir_fd=config_fd)
                        SteamRecoveryAdapter._restore_or_discard_claim(config_fd, claimed)
                        os.fsync(config_fd)
                        os.close(source_fd)
                        continue
                    try:
                        os.unlink(claimed, dir_fd=config_fd)
                        os.fsync(config_fd)
                    except OSError as exc:
                        os.close(source_fd)
                        return {
                            "success": False,
                            "changed": True,
                            "ambiguous": True,
                            "message": f"Controller setting changed but durability is uncertain: {exc}",
                        }
                    os.close(source_fd)
                    return {
                        "success": True,
                        "changed": True,
                        "ambiguous": False,
                        "message": "Controller setting removed",
                    }
                except BaseException as exc:
                    with contextlib.suppress(OSError):
                        os.close(source_fd)
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=config_fd)
                    try:
                        if source_claimed and SteamRecoveryAdapter._stat_at(config_fd, claimed) is not None:
                            if replacement_installed:
                                os.unlink(claimed, dir_fd=config_fd)
                                os.fsync(config_fd)
                            else:
                                SteamRecoveryAdapter._restore_or_discard_claim(config_fd, claimed)
                    except OSError as cleanup_exc:
                        return {
                            "success": False,
                            "changed": source_claimed,
                            "ambiguous": True,
                            "message": f"Controller rewrite rollback is uncertain: {cleanup_exc}",
                        }
                    if replacement_installed:
                        return {
                            "success": False,
                            "changed": True,
                            "ambiguous": True,
                            "message": f"Controller setting changed but cleanup was incomplete: {exc}",
                        }
                    raise
            raise RuntimeError("Steam controller config kept changing during cleanup")
        finally:
            os.close(config_fd)

    @staticmethod
    def _restore_or_discard_claim(config_fd: int, claimed: str) -> None:
        if SteamRecoveryAdapter._stat_at(config_fd, "localconfig.vdf") is None:
            os.rename(claimed, "localconfig.vdf", src_dir_fd=config_fd, dst_dir_fd=config_fd)
        else:
            os.unlink(claimed, dir_fd=config_fd)
        os.fsync(config_fd)

    @staticmethod
    def _stat_at(directory_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _sha256_fd(fd: int) -> str:
        digest = hashlib.sha256()
        os.lseek(fd, 0, os.SEEK_SET)
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _source_unchanged(fd: int, expected_identity: SourceIdentity, expected_hash: str) -> bool:
        current = identity_for_stat(os.fstat(fd))
        stable = ("device", "inode", "mode", "size", "mtime_ns")
        return all(current[field] == expected_identity[field] for field in stable) and (
            SteamRecoveryAdapter._sha256_fd(fd) == expected_hash
        )

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
