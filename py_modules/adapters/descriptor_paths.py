"""Descriptor-relative exact-identity mutation helpers for recovery-backed cleanup."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.prune import SourceIdentity


def identity_for_stat(value: os.stat_result) -> SourceIdentity:
    return {
        "exists": True,
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": value.st_mode,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def missing_identity() -> SourceIdentity:
    return {"exists": False, "device": 0, "inode": 0, "mode": 0, "size": 0, "mtime_ns": 0, "ctime_ns": 0}


def stat_beneath(path: str, safe_root: str) -> os.stat_result | None:
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        return None
    try:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
    finally:
        os.close(parent_fd)


def remove_exact(path: str, safe_root: str, expected: SourceIdentity) -> bool:
    """Atomically claim and remove only the exact entry represented by *expected*."""
    try:
        parent_fd, name = _open_parent(path, safe_root)
    except FileNotFoundError:
        if expected["exists"]:
            raise RuntimeError(f"Recovery source disappeared after sealing: {path}") from None
        return False
    try:
        current = _stat_name(parent_fd, name)
        _require_identity(path, current, expected)
        if current is None:
            return False
        temporary = f".{name}.romm-prune-{current.st_ino}"
        if _stat_name(parent_fd, temporary) is not None:
            raise FileExistsError(f"Prune staging entry already exists: {temporary}")
        os.rename(name, temporary, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        claimed = _stat_name(parent_fd, temporary)
        try:
            _require_claimed_identity(path, claimed, expected)
        except Exception:
            if _stat_name(parent_fd, name) is None:
                os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            raise
        if stat.S_ISDIR(current.st_mode):
            shutil.rmtree(temporary, dir_fd=parent_fd)
        else:
            os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
    finally:
        os.close(parent_fd)


def remove_current(path: str, safe_root: str) -> bool:
    """Remove the current entry through anchored parents without following symlinks."""
    current = stat_beneath(path, safe_root)
    return remove_exact(path, safe_root, identity_for_stat(current) if current is not None else missing_identity())


def rename_exact(src: str, dst: str, safe_root: str, expected: SourceIdentity) -> bool:
    """Rename only the exact sealed source entry, using anchored parent descriptors."""
    source_fd, source_name = _open_parent(src, safe_root)
    destination_fd, destination_name = _open_parent(dst, safe_root)
    try:
        current = _stat_name(source_fd, source_name)
        _require_identity(src, current, expected)
        if current is None:
            return False
        if _stat_name(destination_fd, destination_name) is not None:
            raise FileExistsError(f"Recovery destination already exists: {dst}")
        os.rename(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )
        claimed = _stat_name(destination_fd, destination_name)
        try:
            _require_claimed_identity(src, claimed, expected)
        except Exception:
            if _stat_name(source_fd, source_name) is None:
                os.rename(
                    destination_name,
                    source_name,
                    src_dir_fd=destination_fd,
                    dst_dir_fd=source_fd,
                )
                os.fsync(source_fd)
                if destination_fd != source_fd:
                    os.fsync(destination_fd)
            raise
        os.fsync(source_fd)
        if destination_fd != source_fd:
            os.fsync(destination_fd)
        return True
    finally:
        os.close(source_fd)
        os.close(destination_fd)


def require_directory(path: str, safe_root: str) -> None:
    """Require every component through *path* to be a real directory, never a symlink."""
    fd = _open_directory(path, safe_root)
    os.close(fd)


def open_directory_fd(path: str, safe_root: str) -> int:
    """Open an anchored directory path with no-follow traversal."""
    return _open_directory(path, safe_root)


def open_regular_fd(path: str, safe_root: str) -> int:
    """Open an anchored regular file with no-follow traversal."""
    parent_fd, name = _open_parent(path, safe_root)
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError(f"Expected a regular file: {path}")
        return fd
    finally:
        os.close(parent_fd)


def _open_parent(path: str, safe_root: str) -> tuple[int, str]:
    parts = _relative_parts(path, safe_root)
    if not parts:
        raise ValueError(f"Path must be below its safe root: {path}")
    parent = os.path.join(safe_root, *parts[:-1]) if len(parts) > 1 else safe_root
    return _open_directory(parent, safe_root), parts[-1]


def _open_directory(path: str, safe_root: str) -> int:
    root = os.path.realpath(os.path.abspath(safe_root))
    parts = _relative_parts(path, safe_root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags)
    try:
        for component in parts:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _relative_parts(path: str, safe_root: str) -> list[str]:
    absolute_root = os.path.abspath(safe_root)
    absolute_path = os.path.abspath(path)
    try:
        if os.path.commonpath((absolute_root, absolute_path)) != absolute_root:
            raise ValueError(f"Path is outside its safe root: {path}")
    except ValueError as exc:
        raise ValueError(f"Path is outside its safe root: {path}") from exc
    relative = os.path.relpath(absolute_path, absolute_root)
    if relative == ".":
        return []
    parts = relative.split(os.sep)
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe path component: {path}")
    return parts


def _stat_name(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    if not expected["exists"]:
        if current is not None:
            raise RuntimeError(f"Recovery source appeared after sealing: {path}")
        return
    if current is None:
        raise RuntimeError(f"Recovery source disappeared after sealing: {path}")
    actual = identity_for_stat(current)
    if actual != expected:
        raise RuntimeError(f"Recovery source identity changed after sealing: {path}")


def _require_claimed_identity(path: str, current: os.stat_result | None, expected: SourceIdentity) -> None:
    if current is None or not expected["exists"]:
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
    actual = identity_for_stat(current)
    stable_fields = ("device", "inode", "mode", "size", "mtime_ns")
    if any(actual[field] != expected[field] for field in stable_fields):
        raise RuntimeError(f"Recovery source identity changed while it was claimed: {path}")
