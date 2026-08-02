"""Steam-only recovery state and files for a shortcut about to be removed."""

from __future__ import annotations

import contextlib
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from _vendor import vdf

from adapters.descriptor_paths import (
    hold_writer_exclusion,
    identity_for_stat,
    open_directory_fd,
    open_regular_fd,
    remove_claimed,
    rename_noreplace_at,
    require_directory,
)
from domain.artwork_paths import grid_image_filenames

if TYPE_CHECKING:
    import logging

    from models.prune import MutationOutcome, RecoveryArtifact, SourceClaim, SourceIdentity, SteamRecoverySnapshot

_LOCALCONFIG_NAME = "localconfig.vdf"


@dataclass
class _ControllerRewrite:
    """One in-flight rewrite of ``localconfig.vdf`` — its pinned source and how far the swap got."""

    config_dir: str
    config_fd: int
    source_fd: int
    identity: SourceIdentity
    source_hash: str
    temporary: str
    claimed: str
    source_claimed: bool = False
    replacement_installed: bool = False


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
        path = os.path.join(user_dir, "config", _LOCALCONFIG_NAME)
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
                outcome = SteamRecoveryAdapter._attempt_controller_rewrite(
                    app_id, config_dir, config_fd, expected, attempt
                )
                if outcome is not None:
                    return outcome
            raise RuntimeError("Steam controller config kept changing during cleanup")
        finally:
            os.close(config_fd)

    @staticmethod
    def _attempt_controller_rewrite(
        app_id: int, config_dir: str, config_fd: int, expected: str, attempt: int
    ) -> MutationOutcome | None:
        """Rewrite the controller config once. ``None`` means the source moved — try again."""
        source_fd, payload, identity, source_hash = SteamRecoveryAdapter._pin_source_and_drop_setting(
            config_fd, app_id, expected
        )
        rewrite = _ControllerRewrite(
            config_dir=config_dir,
            config_fd=config_fd,
            source_fd=source_fd,
            identity=identity,
            source_hash=source_hash,
            temporary=f".localconfig.vdf.prune-new-{identity['inode']}-{attempt}",
            claimed=f".localconfig.vdf.prune-old-{identity['inode']}-{attempt}",
        )
        try:
            return SteamRecoveryAdapter._install_controller_rewrite(rewrite, payload)
        except BaseException as exc:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(rewrite.temporary, dir_fd=config_fd)
            cleanup_outcome = SteamRecoveryAdapter._roll_back_claimed_source(rewrite, exc)
            with contextlib.suppress(OSError):
                os.close(source_fd)
            if cleanup_outcome is not None:
                return cleanup_outcome
            if rewrite.replacement_installed:
                return {
                    "success": False,
                    "changed": True,
                    "ambiguous": True,
                    "message": (
                        "Controller setting changed but cleanup was incomplete; "
                        f"the newer source was preserved as {os.path.join(config_dir, rewrite.claimed)}: {exc}"
                    ),
                }
            raise

    @staticmethod
    def _pin_source_and_drop_setting(
        config_fd: int, app_id: int, expected: str
    ) -> tuple[int, dict[str, Any], SourceIdentity, str]:
        """Pin the current config by descriptor and return its payload without the captured setting."""
        source_fd = os.open(
            _LOCALCONFIG_NAME,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=config_fd,
        )
        try:
            source_identity = identity_for_stat(os.fstat(source_fd))
            source_hash = SteamRecoveryAdapter._sha256_fd(source_fd)
            os.lseek(source_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(source_fd), encoding="utf-8") as source:
                payload: dict[str, Any] = vdf.load(source, mapper=vdf.VDFDict, merge_duplicate_keys=False)
            apps = payload.get("UserLocalConfigStore", {}).get("Apps", {})
            app = apps.get(str(app_id)) if isinstance(apps, dict) else None
            if not isinstance(app, dict) or str(app.get("UseSteamControllerConfig")) != expected:
                raise RuntimeError("Steam controller setting changed after recovery capture")
            del app["UseSteamControllerConfig"]
            if not app:
                del apps[str(app_id)]
        except BaseException:
            os.close(source_fd)
            raise
        return source_fd, payload, source_identity, source_hash

    @staticmethod
    def _install_controller_rewrite(rewrite: _ControllerRewrite, payload: dict[str, Any]) -> MutationOutcome | None:
        """Claim the pinned source and link the edited payload in its place."""
        config_fd = rewrite.config_fd
        source_fd = rewrite.source_fd
        temporary_fd = os.open(
            rewrite.temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=config_fd,
        )
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as output:
            vdf.dump(payload, output, pretty=True)
            output.flush()
            os.fsync(output.fileno())
        rename_noreplace_at(config_fd, _LOCALCONFIG_NAME, config_fd, rewrite.claimed)
        rewrite.source_claimed = True
        claimed_stat = os.stat(rewrite.claimed, dir_fd=config_fd, follow_symlinks=False)
        stable = ("device", "inode", "mode", "size", "mtime_ns")
        claimed_identity = identity_for_stat(claimed_stat)
        if any(claimed_identity[field] != rewrite.identity[field] for field in stable):
            return SteamRecoveryAdapter._abandon_before_install(rewrite)
        if not SteamRecoveryAdapter._source_unchanged(source_fd, rewrite.identity, rewrite.source_hash):
            return SteamRecoveryAdapter._abandon_before_install(rewrite)
        try:
            os.link(
                rewrite.temporary,
                _LOCALCONFIG_NAME,
                src_dir_fd=config_fd,
                dst_dir_fd=config_fd,
                follow_symlinks=False,
            )
            rewrite.replacement_installed = True
        except FileExistsError:
            return SteamRecoveryAdapter._abandon_before_install(rewrite)
        os.unlink(rewrite.temporary, dir_fd=config_fd)
        if not SteamRecoveryAdapter._source_unchanged(source_fd, rewrite.identity, rewrite.source_hash):
            return SteamRecoveryAdapter._abandon_installed_replacement(rewrite)
        return SteamRecoveryAdapter._discard_claim_under_exclusion(rewrite)

    @staticmethod
    def _discard_claim_under_exclusion(rewrite: _ControllerRewrite) -> MutationOutcome | None:
        """Drop the claimed source once writer exclusion proves it never changed."""
        config_fd = rewrite.config_fd
        source_fd = rewrite.source_fd
        try:
            source_changed = False
            with hold_writer_exclusion(source_fd, os.path.join(rewrite.config_dir, rewrite.claimed)):
                SteamRecoveryAdapter._require_claim_matches_source(config_fd, rewrite.claimed, source_fd)
                source_changed = not SteamRecoveryAdapter._source_unchanged(
                    source_fd, rewrite.identity, rewrite.source_hash
                )
                if not source_changed:
                    os.unlink(rewrite.claimed, dir_fd=config_fd)
            if source_changed:
                return SteamRecoveryAdapter._abandon_installed_replacement(rewrite)
            os.fsync(config_fd)
        except OSError as exc:
            retained_claim = SteamRecoveryAdapter._stat_at(config_fd, rewrite.claimed)
            os.close(source_fd)
            if retained_claim is not None:
                return {
                    "success": False,
                    "changed": True,
                    "ambiguous": True,
                    "message": (
                        "Controller setting changed but writer exclusion failed; the source was preserved "
                        f"as {os.path.join(rewrite.config_dir, rewrite.claimed)}: {exc}"
                    ),
                }
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

    @staticmethod
    def _abandon_before_install(rewrite: _ControllerRewrite) -> MutationOutcome | None:
        """Give the claimed source back and drop the unused replacement."""
        preserved = SteamRecoveryAdapter._restore_or_preserve_claim(
            rewrite.config_fd, rewrite.claimed, rewrite.source_fd, rewrite.identity, rewrite.source_hash
        )
        os.unlink(rewrite.temporary, dir_fd=rewrite.config_fd)
        os.close(rewrite.source_fd)
        if preserved is not None:
            return SteamRecoveryAdapter._preserved_controller_outcome(rewrite.config_dir, preserved)
        return None

    @staticmethod
    def _abandon_installed_replacement(rewrite: _ControllerRewrite) -> MutationOutcome | None:
        """Take the installed replacement back out and give the claimed source back."""
        os.unlink(_LOCALCONFIG_NAME, dir_fd=rewrite.config_fd)
        preserved = SteamRecoveryAdapter._restore_or_preserve_claim(
            rewrite.config_fd, rewrite.claimed, rewrite.source_fd, rewrite.identity, rewrite.source_hash
        )
        os.fsync(rewrite.config_fd)
        os.close(rewrite.source_fd)
        if preserved is not None:
            return SteamRecoveryAdapter._preserved_controller_outcome(rewrite.config_dir, preserved)
        return None

    @staticmethod
    def _roll_back_claimed_source(rewrite: _ControllerRewrite, exc: BaseException) -> MutationOutcome | None:
        """Undo a failed rewrite, reporting every outcome that leaves the source somewhere else."""
        config_fd = rewrite.config_fd
        try:
            claim_stat = SteamRecoveryAdapter._stat_at(config_fd, rewrite.claimed) if rewrite.source_claimed else None
            if rewrite.source_claimed and claim_stat is None:
                return {
                    "success": False,
                    "changed": True,
                    "ambiguous": True,
                    "message": f"Controller source claim removal is uncertain: {exc}",
                }
            if rewrite.source_claimed:
                if rewrite.replacement_installed:
                    # The claimed inode may contain a newer unrelated Steam edit.
                    # If rollback could not restore it, retain that only good copy.
                    SteamRecoveryAdapter._require_claim_matches_source(config_fd, rewrite.claimed, rewrite.source_fd)
                    os.fsync(config_fd)
                    return None
                preserved = SteamRecoveryAdapter._restore_or_preserve_claim(
                    config_fd, rewrite.claimed, rewrite.source_fd, rewrite.identity, rewrite.source_hash
                )
                if preserved is not None:
                    return SteamRecoveryAdapter._preserved_controller_outcome(rewrite.config_dir, preserved, cause=exc)
        except Exception as cleanup_exc:
            return {
                "success": False,
                "changed": rewrite.source_claimed,
                "ambiguous": True,
                "message": (
                    "Controller rewrite rollback is uncertain; the claimed source was preserved as "
                    f"{os.path.join(rewrite.config_dir, rewrite.claimed)}: {cleanup_exc}"
                ),
            }
        return None

    @staticmethod
    def _restore_or_preserve_claim(
        config_fd: int,
        claimed: str,
        source_fd: int,
        expected_identity: SourceIdentity,
        expected_hash: str,
    ) -> str | None:
        SteamRecoveryAdapter._require_claim_matches_source(config_fd, claimed, source_fd)
        if SteamRecoveryAdapter._stat_at(config_fd, _LOCALCONFIG_NAME) is None:
            try:
                rename_noreplace_at(config_fd, claimed, config_fd, _LOCALCONFIG_NAME)
            except FileExistsError:
                pass
            else:
                os.fsync(config_fd)
                return None
        if not SteamRecoveryAdapter._source_unchanged(source_fd, expected_identity, expected_hash):
            os.fsync(config_fd)
            return claimed
        with hold_writer_exclusion(source_fd, claimed):
            SteamRecoveryAdapter._require_claim_matches_source(config_fd, claimed, source_fd)
            if not SteamRecoveryAdapter._source_unchanged(source_fd, expected_identity, expected_hash):
                os.fsync(config_fd)
                return claimed
            os.unlink(claimed, dir_fd=config_fd)
        os.fsync(config_fd)
        return None

    @staticmethod
    def _require_claim_matches_source(config_fd: int, claimed: str, source_fd: int) -> None:
        claimed_stat = SteamRecoveryAdapter._stat_at(config_fd, claimed)
        if claimed_stat is None:
            raise RuntimeError(f"Claimed Steam controller source disappeared: {claimed}")
        held_stat = os.fstat(source_fd)
        fields = ("st_dev", "st_ino", "st_mode")
        if any(getattr(claimed_stat, field) != getattr(held_stat, field) for field in fields):
            raise RuntimeError(f"Claimed Steam controller source identity changed: {claimed}")

    @staticmethod
    def _preserved_controller_outcome(
        config_dir: str, claimed: str, *, cause: BaseException | None = None
    ) -> MutationOutcome:
        suffix = f": {cause}" if cause is not None else ""
        return {
            "success": False,
            "changed": True,
            "ambiguous": True,
            "message": (
                "Controller rewrite collided with concurrent Steam state; the newer source was preserved as "
                f"{os.path.join(config_dir, claimed)}{suffix}"
            ),
        }

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
